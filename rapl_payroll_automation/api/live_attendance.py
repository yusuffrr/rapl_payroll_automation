# Copyright (c) 2026, RAPL and contributors

"""Live attendance board built from biometric Employee Checkin.

Called from:  Page "today-attendance" (rapl_payroll_automation/rapl_payroll_automation/page/today_attendance)

The board follows the employee's Shift Type exactly like HRMS auto-attendance does,
so what HR sees in the morning matches the Attendance created the next day:
  * shift day window = start - "Begin check-in before" .. end + "Allow check-out after"
    (Regular 9:30-18:00, 90 / 839 min  ->  08:00 today .. 07:59 tomorrow)
  * the board rolls over when the window opens (08:00), not at midnight
  * "Alternating entries" -> first punch in the window is the check-in, log type ignored
    "Strictly based on Log Type" -> first IN punch is the check-in
"""

import datetime
from collections import Counter

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, now_datetime, strip_html, today

ALLOWED_ROLES = ("HR Manager", "HR User", "System Manager")

# Count only punches that came from a device (Employee Checkin.device_id filled).
# If no checkin has a device_id at all (sync doesn't fill it), every punch is counted
# instead, so the board never shows everyone as Absent.
BIOMETRIC_ONLY = True

# Overnight: the previous shift day ended on an unpaired punch made at/after this time,
# and the person has not punched yet today -> still inside, counted as Present.
# Earlier unpaired punches (e.g. a lone 18:04 when the IN was missed) are treated as a
# missed punch, not overnight work. 20:00 is after the evening leaving rush.
OVERNIGHT_FROM = datetime.timedelta(hours=20)

STRICT = "Strictly based on Log Type in Employee Checkin"
STATUS_ORDER = ["Absent", "On Leave", "On Duty", "Present", "Holiday"]
ONE_DAY = datetime.timedelta(days=1)


# ------------------------------------------------------------------ workspace widgets
# Number Cards (Type: Custom) and the "Today Attendance Status" chart source call these.
# They share one cached computation so a workspace with 4 cards + a chart runs it once a minute.

CACHE_SECONDS = 60


def _cached_status(grade=None):
	key = f"rapl_live_attendance::{grade or 'all'}"
	data = frappe.cache().get_value(key)
	if data is None:
		data = _compute_status(grade)
		frappe.cache().set_value(key, data, expires_in_sec=CACHE_SECONDS)
	return data


def _card(status):
	frappe.only_for(ALLOWED_ROLES)
	return {
		"value": (_cached_status().get("summary") or {}).get(status, 0),
		"fieldtype": "Int",
		"route": ["today-attendance"],
		"route_options": {"status": status},  # page opens with this card selected
	}


@frappe.whitelist()
def card_absent(filters=None):
	return _card("Absent")


@frappe.whitelist()
def card_present(filters=None):
	return _card("Present")


@frappe.whitelist()
def card_on_leave(filters=None):
	return _card("On Leave")


@frappe.whitelist()
def card_overnight(filters=None):
	return _card("Overnight")


# ------------------------------------------------------------------ missing punch

#: device_id stamped on punches HR adds from the board. Non-empty on purpose, so the
#: board's "device punches only" filter still counts them, and they stay easy to audit.
MANUAL_DEVICE_ID = "HR - added on board"


@frappe.whitelist(methods=["POST"])
def add_missing_punch(employee, time):
	"""HR adds the arrival punch someone forgot, for the shift day that is open now.

	Auto-attendance builds a day only after its window closes (next morning), so a
	punch added today is used like any device punch: the Attendance comes out right
	with no correction afterwards.
	"""
	frappe.only_for(ALLOWED_ROLES)
	now = now_datetime()
	shift_of = _shift_map([frappe._dict(name=employee,
		default_shift=frappe.db.get_value("Employee", employee, "default_shift"))], getdate(today()))
	st = _load_shift_types(shift_of.values()).get(shift_of.get(employee))

	cal_today = getdate(today())
	board_date = cal_today if now >= _window(st, cal_today)[0] else add_days(cal_today, -1)
	win_start, win_end = _window(st, board_date)

	t = _to_td(time)
	punch = datetime.datetime.combine(board_date, datetime.time.min) + t
	if punch < win_start:  # e.g. 01:00 belongs to the night after board_date
		punch += ONE_DAY
	if punch > now:
		frappe.throw(_("The punch time {0} is in the future.").format(_fmt(punch, now)))
	if not (win_start <= punch <= win_end):
		frappe.throw(_("The punch time must fall in today's shift day ({0} to {1}).").format(
			_fmt(win_start, now), _fmt(win_end, now)))

	doc = frappe.new_doc("Employee Checkin")
	doc.employee = employee
	doc.time = punch
	doc.log_type = "IN"
	doc.device_id = MANUAL_DEVICE_ID
	doc.insert()
	doc.add_comment("Comment", _("Missing arrival punch added by {0} from Today's Attendance.").format(
		frappe.session.user))
	return {"name": doc.name, "time": _fmt(punch, now)}


# ------------------------------------------------------------------ page

@frappe.whitelist()
def get_status(grade=None):
	frappe.only_for(ALLOWED_ROLES)
	return _compute_status(grade)


def _compute_status(grade=None):
	now = now_datetime()
	cal_today = getdate(today())

	# ---------------------------------------------------------------- employees
	emp_filters = {"status": "Active", "date_of_joining": ["<=", cal_today]}
	if grade:
		emp_filters["grade"] = grade
	employees = frappe.get_all(
		"Employee",
		filters=emp_filters,
		fields=[
			"name", "employee_name", "department", "designation", "grade",
			"company", "holiday_list", "default_shift", "cell_number",
		],
		order_by="department asc, employee_name asc",
	)
	if not employees:
		return {"date": str(cal_today), "rows": [], "summary": {}, "as_of": _fmt(now, now)}
	emp_ids = [e.name for e in employees]

	# ---------------------------------------------------------------- shifts & board date
	shift_of = _shift_map(employees, cal_today)
	shift_types = _load_shift_types(shift_of.values())

	# Board date = the shift day that is currently open. Before 08:00 that is still yesterday.
	opens = [_window(st, cal_today)[0] for st in shift_types.values()]
	board_date = cal_today if (not opens or now >= min(opens)) else add_days(cal_today, -1)
	if board_date != cal_today:
		shift_of = _shift_map(employees, board_date)
		shift_types = _load_shift_types(shift_of.values())
	prev_date = add_days(board_date, -1)

	# ---------------------------------------------------------------- punches (previous + current shift day)
	bio = "and ifnull(device_id, '') != ''" if _biometric_filter_usable() else ""
	fetch_from = datetime.datetime.combine(prev_date, datetime.time.min) - ONE_DAY
	punches = {}
	for p in frappe.db.sql(
		f"""select employee, time, log_type from `tabEmployee Checkin`
		where time >= %(f)s and time <= %(now)s and employee in %(emps)s {bio}
		  and ifnull(skip_auto_attendance, 0) = 0
		order by time asc""",
		{"f": fetch_from, "now": now, "emps": emp_ids},
		as_dict=True,
	):
		punches.setdefault(p.employee, []).append(p)
	punches = {emp: _dedupe(ps) for emp, ps in punches.items()}

	# ---------------------------------------------------------------- leaves / on duty / holidays
	leaves = _leaves(emp_ids, board_date)
	on_duty = {
		ar.employee: ar
		for ar in frappe.get_all(
			"Attendance Request",
			filters={"employee": ["in", emp_ids], "from_date": ["<=", board_date], "to_date": [">=", board_date], "docstatus": 1},
			fields=["name", "employee", "reason"],
		)
	}
	company_hl = {c.name: c.default_holiday_list for c in frappe.get_all("Company", fields=["name", "default_holiday_list"])}
	get_hl = _holiday_list_resolver()
	emp_hl = {}
	for e in employees:
		st = shift_types.get(shift_of.get(e.name))
		emp_hl[e.name] = (st and st.holiday_list) or _holiday_list_of(e, board_date, company_hl, get_hl)
	holidays = {}
	hl_names = list({h for h in emp_hl.values() if h})
	if hl_names:
		for h in frappe.get_all(
			"Holiday",
			filters={"parent": ["in", hl_names], "holiday_date": board_date},
			fields=["parent", "description", "weekly_off"],
		):
			holidays[h.parent] = h

	# ---------------------------------------------------------------- classify
	rows = []
	for e in employees:
		st = shift_types.get(shift_of.get(e.name))
		strict = bool(st and st.determine_check_in_and_check_out == STRICT)
		cur_s, cur_e = _window(st, board_date)
		prv_s, prv_e = _window(st, prev_date)
		plist = punches.get(e.name, [])
		cur = [p for p in plist if cur_s <= p.time <= cur_e]
		prv = [p for p in plist if prv_s <= p.time <= prv_e]

		check_in = _check_in(cur, strict)
		open_in = _open_in(prv, strict)  # unpaired punch from the previous shift day
		carried = bool(
			open_in
			and not cur  # once they punch today, today's punch decides
			and open_in.time >= datetime.datetime.combine(prev_date, datetime.time.min) + OVERNIGHT_FROM
		)

		shift_start = datetime.datetime.combine(board_date, datetime.time.min) + st.start if st else None
		grace = datetime.timedelta(minutes=cint(st.late_entry_grace_period)) if st else datetime.timedelta()

		row = frappe._dict(
			employee=e.name, employee_name=e.employee_name, department=e.department,
			designation=e.designation, grade=e.grade, phone=e.cell_number,
			shift=st.name if st else None, first_in=None, late_by=0, overnight=0, note="",
		)
		leave = leaves.get(e.name)
		holiday = holidays.get(emp_hl.get(e.name))

		if carried:
			row.status = "Present"
			row.overnight = 1
			row.first_in = _fmt(open_in.time, now)
			row.note = "Working overnight, no punch yet today"
		elif check_in:
			row.status = "Present"
			row.first_in = _fmt(check_in.time, now)
			if st and cint(st.enable_late_entry_marking) and check_in.time > shift_start + grace:
				row.late_by = int((check_in.time - shift_start).total_seconds() // 60)
			if leave and leave.status == "Approved" and leave.is_half:
				row.note = f"Half day leave ({leave.leave_type})"
		elif holiday:
			row.status = "Holiday"
			row.note = "Weekly Off" if cint(holiday.weekly_off) else strip_html(holiday.description or "")
		elif leave and leave.status == "Approved":
			row.status = "On Leave"
			row.note = leave.leave_type + (" (half day)" if leave.is_half else "")
		elif leave:
			row.status = "Absent"
			row.note = f"{leave.leave_type} applied, not approved yet"
		elif on_duty.get(e.name):
			row.status = "On Duty"
			row.note = on_duty[e.name].reason or ""
		else:
			row.status = "Absent"
			if open_in and not carried:
				row.note = f"Last shift day had no OUT after {_fmt(open_in.time, now)}"

		rows.append(row)

	summary = {s: 0 for s in STATUS_ORDER}
	for r in rows:
		summary[r.status] += 1
	summary["Late"] = sum(1 for r in rows if r.late_by)
	summary["Overnight"] = sum(1 for r in rows if r.overnight)
	summary["Total"] = len(rows)

	# label for the most common shift's window, e.g. "8:00 AM Mon → 7:59 AM Tue"
	window_label = None
	common = Counter(s for s in shift_of.values() if s in shift_types).most_common(1)
	if common:
		ws, we = _window(shift_types[common[0][0]], board_date)
		window_label = f"{ws.strftime('%I:%M %p %a').lstrip('0')} → {we.strftime('%I:%M %p %a').lstrip('0')}"

	last_sync = frappe.db.sql(f"select max(time) from `tabEmployee Checkin` where 1=1 {bio}")[0][0]

	return {
		"date": str(board_date),
		"window": window_label,
		"as_of": _fmt(now, now),
		"last_checkin": str(last_sync) if last_sync else None,
		"summary": summary,
		"rows": rows,
	}


# ------------------------------------------------------------------ helpers

def _biometric_filter_usable():
	if not BIOMETRIC_ONLY:
		return False
	return bool(frappe.db.sql("select 1 from `tabEmployee Checkin` where ifnull(device_id, '') != '' limit 1"))


def _shift_map(employees, date):
	shift_of = {e.name: e.default_shift for e in employees}
	for sa in frappe.db.sql(
		"""select employee, shift_type from `tabShift Assignment`
		where docstatus = 1 and status = 'Active' and employee in %(emps)s
		  and start_date <= %(d)s and (end_date is null or end_date >= %(d)s)
		order by start_date asc""",
		{"emps": [e.name for e in employees], "d": date},
		as_dict=True,
	):
		shift_of[sa.employee] = sa.shift_type
	return shift_of


def _load_shift_types(names):
	names = list({n for n in names if n})
	if not names:
		return {}
	out = {}
	for st in frappe.get_all(
		"Shift Type",
		filters={"name": ["in", names]},
		fields=[
			"name", "start_time", "end_time", "holiday_list",
			"begin_check_in_before_shift_start_time", "allow_check_out_after_shift_end_time",
			"determine_check_in_and_check_out", "enable_late_entry_marking", "late_entry_grace_period",
		],
	):
		st.start = _to_td(st.start_time)
		st.end = _to_td(st.end_time)
		st.before = datetime.timedelta(minutes=cint(st.begin_check_in_before_shift_start_time))
		st.after = datetime.timedelta(minutes=cint(st.allow_check_out_after_shift_end_time))
		out[st.name] = st
	return out


def _window(st, date):
	"""Same shift-day window HRMS auto-attendance uses."""
	day = datetime.datetime.combine(date, datetime.time.min)
	if not st:
		return day, day + ONE_DAY - datetime.timedelta(seconds=1)
	start = day + st.start - st.before
	end = day + st.end + st.after
	if st.end <= st.start:  # overnight shift
		end += ONE_DAY
	return start, end


def _check_in(ps, strict):
	if not ps:
		return None
	if strict:
		return next((p for p in ps if p.log_type == "IN"), None)
	return ps[0]


def _open_in(ps, strict):
	"""Unpaired punch at the end of a shift day = person never punched out.

	Alternating mode ignores IN/OUT labels (staff don't switch the key on the device),
	so pairing is by count after duplicates are removed.
	"""
	if not ps:
		return None
	if strict:
		return ps[-1] if ps[-1].log_type == "IN" else None
	return ps[-1] if len(ps) % 2 == 1 else None


DUPLICATE_WINDOW = datetime.timedelta(minutes=3)


def _dedupe(ps):
	"""Two punches within 3 minutes are one punch (double press or re-synced record),
	whatever their IN/OUT label."""
	out = []
	for p in ps:
		if out and p.time - out[-1].time <= DUPLICATE_WINDOW:
			continue
		out.append(p)
	return out


def _leaves(emp_ids, date):
	leaves = {}
	for la in frappe.get_all(
		"Leave Application",
		filters={
			"employee": ["in", emp_ids], "from_date": ["<=", date], "to_date": [">=", date],
			"docstatus": ["<", 2], "status": ["in", ["Open", "Approved"]],
		},
		fields=["name", "employee", "leave_type", "status", "half_day", "half_day_date"],
	):
		if leaves.get(la.employee) and leaves[la.employee].status == "Approved":
			continue
		la.is_half = bool(cint(la.half_day) and (not la.half_day_date or getdate(la.half_day_date) == date))
		leaves[la.employee] = la
	return leaves


def _to_td(value):
	if value is None or isinstance(value, datetime.timedelta):
		return value
	if isinstance(value, datetime.time):
		return datetime.timedelta(hours=value.hour, minutes=value.minute, seconds=value.second)
	h, m, *s = str(value).split(":")
	return datetime.timedelta(hours=int(h), minutes=int(m), seconds=float(s[0]) if s else 0)


def _fmt(dt, now):
	if not dt:
		return None
	t = dt.strftime("%I:%M %p").lstrip("0")
	if dt.date() == now.date():
		return t
	if dt.date() == (now - ONE_DAY).date():
		return f"{t} (yesterday)"
	return f"{t} ({dt.strftime('%d-%m')})"


def _holiday_list_resolver():
	for path in (
		"hrms.hr.utils.get_holiday_list_for_employee",
		"erpnext.setup.doctype.employee.employee.get_holiday_list_for_employee",
	):
		try:
			return frappe.get_attr(path)
		except Exception:
			continue
	return None


def _holiday_list_of(emp, date, company_hl, get_hl):
	if get_hl:
		for kwargs in ({"raise_exception": False, "as_on": date}, {"raise_exception": False}):
			try:
				result = get_hl(emp.name, **kwargs)
				if result:
					return result
				break
			except TypeError:
				continue
			except Exception:
				break
	return emp.holiday_list or company_hl.get(emp.company)
