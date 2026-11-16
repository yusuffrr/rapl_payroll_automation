# Copyright (c) 2026, RAPL and contributors
#
# Attendance Console -- HR-only editing surface.
#
# WHY EDITS GO THROUGH SQL
# ------------------------
# Verified against hrms attendance.json: NOT ONE field on Attendance has
# allow_on_submit. And frappe customize_form.py explicitly refuses to enable it
# on a standard field ("Not allowed to enable Allow on Submit for standard
# fields"). So a submitted Attendance simply cannot be edited through the ORM,
# and validate() cannot be made to fire for an in-place correction.
#
# The realistic options were: cancel + amend (renames the document and fills
# the trail with cancelled records), or a direct write. HR already corrects
# submitted attendance by hand in the Database tool, so this is the same
# mechanism -- with the rules re-applied, an audit row written, and a
# concurrency check, none of which raw SQL gives you.
#
# What that costs, stated plainly: no version history, no docstatus
# enforcement, validate() never runs. Mitigated by writing ONLY in_time,
# out_time and status (never employee, attendance_date, docstatus or company),
# gating on HR roles, and re-deriving every dependent field immediately after.
#
# CREATION is different -- a missing record has no docstatus problem, so it
# goes through the ORM and gets the full validate() chain. Six standard
# validations can reject it (status, active employee, joining date, duplicate,
# overlapping shift, leave record), so creation reports per-row outcomes rather
# than assuming success.

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import add_days, cint, date_diff, flt, get_datetime, get_last_day, getdate, now

from rapl_payroll_automation.api.attendance_automation import derive_attendance_fields
from rapl_payroll_automation.api.attendance_data import (
	build_month_rows,
	get_band_definitions,
	summarise,
)
from rapl_payroll_automation.api.ot_engine import is_ot_eligible
from rapl_payroll_automation.api.payroll_math import (
	expected_payment_days,
	month_cutoff,
	ot_amount,
	pay_rates,
	round_half_up,
)
from rapl_payroll_automation.api.payroll_automation_utils import (
	additional_salary_already_exists,
	get_additional_salary_total,
	get_automation_settings,
	get_employee_holiday_dates,
	get_employee_weekly_off_dates,
	get_grade_ot_rule,
	get_salary_month,
	get_total_working_days,
	get_weekly_off_dates,
)
from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee

HR_ROLES = {"HR Manager", "HR User"}
EDITABLE_FIELDS = ("in_time", "out_time", "status")

#: Day-level manual overrides. Each pins one derived field so the rules stop
#: recalculating it. Sending None resets that field back to automatic.
OVERRIDE_FIELDS = {
	"custom_overtime_hours": "custom_overtime_manual",
	"custom_late_mark_band": "custom_late_mark_manual",
}

#: Visit types. NOT statuses -- attendance.py hardcodes the allowed status list
#: and throws on anything outside it, so "Site Visit" cannot be one.
VISIT_TYPES = ("Site Visit", "Client Visit", "Vendor Visit")

# What the Console may set by hand. "On Leave" is deliberately absent: leave is
# created through a Leave Application (create_leave_applications), never by
# rewriting the status, or the leave would not be booked against a balance.
CONSOLE_STATUSES = ("Present", "Absent", "Half Day", "Work From Home")


MAX_ROWS_PER_APPLY = 500
MAX_PERIOD_DAYS = 62
MAX_EMPLOYEES = 300


def _json(value, default=None):
	"""Parse a JSON argument that may arrive as an empty string.

	A JS `null` sent through frappe.call arrives server-side as "" -- not None.
	isinstance("", str) is True, so guarding on the type alone still reached
	frappe.parse_json(""), which raises JSONDecodeError on a zero-length
	document. Every optional JSON argument on this page hit that.
	"""
	if value is None or value == "":
		return default
	if isinstance(value, str):
		return frappe.parse_json(value)
	return value


def get_advance_cutoff(start_date, end_date, settings=None, override=None):
	"""The date up to which unrecovered advances are offered for recovery.

	Payroll does not run on a fixed day -- August 2026 ran on the 17th -- so the
	Settings value is only a default and the Console passes an override.
	"""
	if override:
		return getdate(override)
	settings = settings or get_automation_settings()
	day = cint(settings.get("advance_cutoff_day")) or 10
	from frappe.utils import add_months, get_first_day
	nxt = get_first_day(add_months(getdate(end_date), 1))
	try:
		return nxt.replace(day=day)
	except ValueError:          # e.g. day 31 in a 30-day month
		from frappe.utils import get_last_day
		return get_last_day(nxt)


def get_recoverable_advances(employee, cutoff):
	"""Unrecovered Employee Advances that can be taken out of salary.

	Filtered exactly as HRMS's own "Deduction from Salary" button is gated:
	repay_unclaimed_amount_from_salary must be ticked, otherwise the advance is
	settled by Journal Entry instead and must never appear here.

	Outstanding is paid - claimed - returned, NOT HRMS's paid - claimed.
	Recovery writes return_amount (confirmed against live data: every recovered
	advance shows status 'Returned' with return_amount set), so HRMS's figure
	would stay at the full amount after a partial return and offer money back
	that has already been recovered.

	A fully recovered advance drops out of this list on its own -- its
	outstanding becomes zero. Nothing needs to remember what was recovered.
	"""
	rows = frappe.get_all(
		"Employee Advance",
		filters={
			"docstatus": 1,
			"employee": employee,
			"repay_unclaimed_amount_from_salary": 1,
			"posting_date": ["<=", getdate(cutoff)],
		},
		fields=["name", "posting_date", "purpose", "advance_amount", "paid_amount",
				"claimed_amount", "return_amount", "status", "company", "currency",
				"custom_outstanding_balance"],
		order_by="posting_date, name",
	)
	out = []
	rec_state = _advance_recovery_state([r.name for r in rows])
	for r in rows:
		outstanding = _recoverable_amount(r, rec_state)
		if outstanding <= 0.005:
			continue
		out.append({
			"name": r.name,
			"posting_date": str(r.posting_date),
			"purpose": r.purpose,
			"outstanding": flt(outstanding, 2),
			"status": r.status,
			"company": r.company,
			"currency": r.currency,
		})
	return out


def get_leave_context(employee, start_date, end_date, settings=None):
	"""Paid-leave eligibility, balance and days already taken.

	Eligibility is decided by LEAVE ALLOCATION, not by the custom_paid_leave
	flag on Employee -- that flag is wired to nothing. validate_balance_leaves()
	on Leave Application checks the allocation, so allocation is the only thing
	that actually governs whether leave can be created.

	Balance uses get_leave_balance_on(..., for_consumption=True), which returns
	the CONSUMABLE figure: an employee may hold 10 days while an allocation
	expiring next week caps what they can actually take at 1.
	"""
	settings = settings or get_automation_settings()
	leave_type = settings.get("absent_leave_type")
	ctx = {"leave_type": leave_type, "eligible": False, "balance": 0.0, "taken": 0}

	if leave_type:
		ctx["taken"] = frappe.db.count("Attendance", {
			"employee": employee, "docstatus": 1, "status": "On Leave",
			"attendance_date": ["between", [start_date, end_date]],
		})
		try:
			from hrms.hr.doctype.leave_application.leave_application import get_leave_balance_on
			balance = get_leave_balance_on(
				employee, leave_type, getdate(end_date), for_consumption=True
			)
			if isinstance(balance, dict):
				ctx["balance"] = flt(balance.get("leave_balance_for_consumption"), 2)
			else:
				ctx["balance"] = flt(balance, 2)
			ctx["eligible"] = ctx["balance"] > 0
		except Exception:
			# No allocation, or the balance call refused -- not eligible, and
			# not an error worth failing the whole page load for.
			ctx["eligible"] = False
	return ctx


def _require_hr(ptype="read"):
	"""Role AND real DocType permission.

	A role check alone is not a permission check: an HR User whose write
	permission on Attendance was removed would still get through, because
	every write here goes via SQL and never touches the permission layer.
	frappe.has_permission is the actual authority.
	"""
	if not (HR_ROLES & set(frappe.get_roles())):
		frappe.throw("Attendance Console is restricted to HR.", frappe.PermissionError)
	if not frappe.has_permission("Attendance", ptype=ptype):
		frappe.throw(
			f"You do not have {ptype} permission on Attendance.", frappe.PermissionError
		)


def _permitted_employees(employees=None, include_inactive=False):
	"""Employee list filtered by the caller's User Permissions.

	SQL writes bypass User Permissions entirely, so an HR User restricted to
	one Company would otherwise see and edit every company's attendance.
	frappe.get_list applies the restriction (frappe.get_all does NOT -- it is
	get_list with ignore_permissions -- which is what this used before).

	include_inactive: relieved employees still need their final period
	corrected, so existing records stay reachable. Attendance.validate() calls
	validate_active_employee() and throws on INSERT, so creation for them is
	blocked by the framework and is not worked around here.
	"""
	filters = {} if include_inactive else {"status": "Active"}
	if employees:
		filters["name"] = ["in", list(employees)]
	return frappe.get_list(
		"Employee",
		filters=filters,
		fields=["name", "employee_name", "grade", "status",
				"date_of_joining", "relieving_date", "company"],
		order_by="name",
		limit_page_length=0,
	)


def _permitted_names(employees):
	"""Subset of `employees` the caller may act on (User Permissions applied)."""
	employees = {e for e in employees if e}
	if not employees:
		return set()
	return {e.name for e in _permitted_employees(sorted(employees), include_inactive=True)}


def _period_bounds(start_date, end_date):
	if not start_date or not end_date:
		frappe.throw("Set a period first")
	start, end = getdate(start_date), getdate(end_date)
	if start > end:
		frappe.throw("Start date is after end date.")
	if date_diff(end, start) + 1 > MAX_PERIOD_DAYS:
		frappe.throw(f"Choose a period of at most {MAX_PERIOD_DAYS} days.")
	return start, end


def _employee_list(employees):
	employees = _json(employees)
	if employees is None:
		return None
	if not isinstance(employees, list):
		frappe.throw("employees must be a list")
	employees = list(dict.fromkeys(str(e) for e in employees if e))
	if len(employees) > MAX_EMPLOYEES:
		frappe.throw(f"At most {MAX_EMPLOYEES} employees at a time.")
	return employees


# ---------------------------------------------------------------- read


@frappe.whitelist()
def get_console_data(start_date=None, end_date=None, employees=None, only_flagged=0,
					 advance_cutoff=None):
	"""Employee summary rows + their day rows, for the grouped grid."""
	_require_hr("read")
	start_date, end_date = _period_bounds(start_date, end_date)

	employees = _employee_list(employees)
	only_flagged = cint(only_flagged)

	settings = get_automation_settings()
	bands = get_band_definitions(settings)
	cutoff = get_advance_cutoff(start_date, end_date, settings, advance_cutoff)

	# Anyone with attendance in the period is included even if no longer
	# Active -- a leaver's final month still needs correcting.
	with_records = frappe.get_all(
		"Attendance",
		filters={"attendance_date": ["between", [start_date, end_date]], "docstatus": ["<", 3]},
		pluck="employee", distinct=True,
	)
	candidates = _permitted_employees(employees, include_inactive=True)
	allowed = {e.name for e in candidates if e.status == "Active"} | set(with_records)
	candidates = [e for e in candidates if e.name in allowed]

	# Bulk prefetch: these used to be 4-6 queries PER employee inside the loop.
	pre = _prefetch([e.name for e in candidates], start_date, end_date, cutoff, settings)

	groups = []
	for emp in candidates:
		data = build_month_rows(emp.name, start_date, end_date, settings)
		rows = data["rows"]
		summary = summarise(rows, settings)

		if only_flagged:
			rows = [r for r in rows if r["flags"]]
			if not rows:
				continue

		groups.append({
			"employee": emp.name,
			"employee_name": emp.employee_name,
			"grade": emp.grade,
			"employee_status": emp.status,
			"relieving_date": str(emp.relieving_date) if emp.relieving_date else None,
			"can_create": emp.status == "Active",
			"ot_eligible": pre["ot_eligible"].get(emp.name, False),
			"summary": summary,
			"entry": _entry_preview(emp, start_date, end_date, summary, bands, settings, pre),
			"advances": pre["advances"].get(emp.name, []),
			"leave": get_leave_context(emp.name, start_date, end_date, settings),
			"rows": rows,
		})

	today = getdate()
	return {
		"start_date": str(start_date),
		"end_date": str(end_date),
		# A month that has not finished: the page says so, counts stop at today,
		# and Compute Net Pay prices month-to-date instead of the whole month.
		"today": str(today),
		"month_in_progress": today <= end_date,
		"bands": bands,
		"advance_cutoff": str(cutoff),
		"groups": groups,
		"loaded_at": now(),
	}


def _prefetch(names, start_date, end_date, cutoff, settings):
	"""Everything get_console_data needs per employee, in a handful of queries."""
	out = {"monthly": {}, "ot_eligible": {}, "processed": set(), "advances": {}}
	if not names:
		return out
	base = settings.ot_rate_base_fieldname
	for e in frappe.get_all(
		"Employee", filters={"name": ["in", names]}, fields=["name", "custom_ot", base]
	):
		out["monthly"][e.name] = flt(e.get(base))
		out["ot_eligible"][e.name] = bool(e.custom_ot)

	components = [c for c in (settings.overtime_salary_component,
							  settings.late_mark_salary_component) if c]
	if components:
		for a in frappe.get_all(
			"Additional Salary",
			filters={"employee": ["in", names], "salary_component": ["in", components],
					 "docstatus": 1, "payroll_date": ["between", [start_date, end_date]]},
			fields=["employee", "salary_component"],
		):
			out["processed"].add((a.employee, a.salary_component))

	advances = frappe.get_all(
		"Employee Advance",
		filters={"docstatus": 1, "employee": ["in", names],
				 "repay_unclaimed_amount_from_salary": 1,
				 "posting_date": ["<=", getdate(cutoff)]},
		fields=["name", "employee", "posting_date", "purpose", "paid_amount",
				"claimed_amount", "return_amount", "status", "company", "currency"],
		order_by="posting_date, name",
	)
	rec_state = _advance_recovery_state([a.name for a in advances])
	for r in advances:
		# Not yet covered by a recovery record: a partial recovery leaves the
		# rest available (it used to block the advance for good), and an
		# amount already scheduled is not offered twice.
		outstanding = _recoverable_amount(r, rec_state)
		if outstanding <= 0.005:
			continue
		out["advances"].setdefault(r.employee, []).append({
			"name": r.name, "posting_date": str(r.posting_date), "purpose": r.purpose,
			"outstanding": flt(outstanding, 2), "status": r.status,
			"company": r.company, "currency": r.currency,
		})
	return out


def _advance_recovery_state(advance_names):
	"""{advance: {"drafts": n, "scheduled": amount}} from its recovery records.

	scheduled = submitted Additional Salary recoveries -- the figure HRMS's own
	validate_employee_advance_return() subtracts (it allows partial
	recoveries up to paid - claimed - scheduled). A draft recovery means one
	is already being prepared, so another must not be offered.
	"""
	state = {}
	if not advance_names:
		return state
	for r in frappe.get_all(
		"Additional Salary",
		filters={"ref_doctype": "Employee Advance", "ref_docname": ["in", list(advance_names)],
				 "docstatus": ["<", 2]},
		fields=["ref_docname", "docstatus", "amount"],
	):
		st = state.setdefault(r.ref_docname, {"drafts": 0, "scheduled": 0.0})
		if r.docstatus == 0:
			st["drafts"] += 1
		else:
			st["scheduled"] += flt(r.amount)
	return state


def _recoverable_amount(adv, rec_state):
	"""What can still be recovered: what is outstanding (paid - claimed -
	returned) but never more than HRMS will accept (paid - claimed - already
	scheduled). Zero while a draft recovery exists."""
	st = rec_state.get(adv.name) or {"drafts": 0, "scheduled": 0.0}
	if st["drafts"]:
		return 0.0
	outstanding = flt(adv.paid_amount) - flt(adv.claimed_amount) - flt(adv.return_amount)
	allowed = flt(adv.paid_amount) - flt(adv.claimed_amount) - st["scheduled"]
	return flt(max(min(outstanding, allowed), 0), 2)


def _entry_preview(emp, start_date, end_date, summary, bands, settings, pre=None):
	"""What the RAPL Overtime / Late Mark Processing Entry rows WOULD hold.

	Rates come from payroll_math.pay_rates(), the single derivation shared with
	the processing documents and the statement. Two rates, not one:

	  per_day_rate   LATE MARK per-day = monthly / ALL calendar days. This is
	                 what RAPL Late Mark Processing divides by for every grade.
	  ot_rate        OVERTIME per-hour, from the grade's own denominator.

	This function used to feed the OT denominator (monthly / (days - Sundays)
	for Floor grades) into the late-mark figures too, so the Console showed -- and
	the net-pay preview deducted -- more late mark than the draft would pay.
	"""
	if pre is not None:
		monthly = pre["monthly"].get(emp.name, 0)
	else:
		monthly = flt(frappe.db.get_value("Employee", emp.name, settings.ot_rate_base_fieldname))
	rule = get_grade_ot_rule(settings, emp.grade)

	month_first, month_last, total_days = get_salary_month(start_date)
	sundays = 0
	if rule:
		sundays = len(get_employee_weekly_off_dates(emp.name, month_first, month_last))

	rates = pay_rates(monthly, total_days, sundays, bool(rule), settings.ot_hours_divisor)
	# Processing adds an employee whose grade has no OT rule with 0 hours/rate
	# and asks for a manual edit. Mirror that, or the Console promises an OT
	# figure the draft will not contain.
	ot_rule_missing = rule is None
	hourly = 0 if ot_rule_missing else rates["hourly"]

	exact_hours = flt(summary.get("overtime_hours_exact", summary["overtime_hours"]))
	seconds = round_half_up(exact_hours * 3600)
	ot_hours = round_half_up(seconds / 3600, 2)
	band_counts = {b["label"]: summary["band_counts"].get(b["label"], 0) for b in bands}
	late_fraction = sum(flt(b["fraction"]) * band_counts[b["label"]] for b in bands)

	return {
		"ot_working_days": rates["ot_days"],
		"per_day_rate": rates["late_per_day"],
		"ot_per_day_rate": rates["ot_per_day"],
		"ot_rate": hourly,
		"ot_rule_missing": ot_rule_missing,
		"ot_hours": ot_hours,
		# Duration fields store SECONDS. rapl_overtime_processing_entry's
		# ot_hours_hhmm is the editable one and ot_hours (Float) is derived
		# from it, so the console must offer the same pair.
		"ot_hours_hhmm": int(seconds),
		"ot_amount": ot_amount(seconds, hourly),
		"band_counts": band_counts,
		"late_amount": round_half_up(late_fraction * rates["late_per_day"]),
		# Any submitted record dated inside the period, not only one dated
		# exactly end_date -- a processing run for a shorter period dates its
		# Additional Salary on that period's own end.
		"ot_already_processed": _is_processed(
			pre, emp.name, settings.overtime_salary_component, start_date, end_date),
		"late_already_processed": _is_processed(
			pre, emp.name, settings.late_mark_salary_component, start_date, end_date),
	}


def _is_processed(pre, employee, component, start_date, end_date):
	if not component:
		return False
	if pre is not None:
		return (employee, component) in pre["processed"]
	return bool(frappe.db.exists("Additional Salary", {
		"employee": employee, "salary_component": component, "docstatus": 1,
		"payroll_date": ["between", [start_date, end_date]],
	}))


# ---------------------------------------------------------------- edit


def _recompute(record, settings):
	holiday_dates = get_employee_holiday_dates(
		record["employee"], record["attendance_date"], record["attendance_date"]
	)
	return derive_attendance_fields(
		employee=record["employee"],
		attendance_date=record["attendance_date"],
		in_time=record["in_time"],
		out_time=record["out_time"],
		working_hours=record["working_hours"],
		status=record["status"],
		leave_type=record["leave_type"],
		shift=record["shift"],
		settings=settings,
		holiday_dates=holiday_dates,
		leave_application=record.get("leave_application"),
		half_day_status=record.get("half_day_status"),
		overtime_manual=record.get("custom_overtime_manual"),
		status_manual=record.get("custom_status_manual"),
		late_mark_manual=record.get("custom_late_mark_manual"),
		current_overtime_hours=record.get("custom_overtime_hours"),
		current_late_mark_band=record.get("custom_late_mark_band"),
	)


@frappe.whitelist()
def apply_edits(changes, confirm_processed=0):
	"""Write edited punches, re-derive everything, report per row.

	changes: [{name, modified, in_time, out_time, status}, ...]

	`modified` is an optimistic lock. Attendance edited elsewhere (the doctype
	form, another Console session, a SQL correction) since this grid loaded is
	REJECTED rather than overwritten -- SQL writes bypass Frappe's own conflict
	detection, so without this a stale grid would silently undo someone's work.

	Rows are independent, so a bad row does not block the good ones: valid rows
	are written and invalid ones are returned with a reason.
	"""
	_require_hr("write")
	changes = _json(changes, [])
	if not changes:
		return {"applied": [], "failed": []}
	if len(changes) > MAX_ROWS_PER_APPLY:
		frappe.throw(
			f"Too many rows in one go ({len(changes)}). Apply at most "
			f"{MAX_ROWS_PER_APPLY} at a time."
		)

	if not isinstance(changes, list):
		frappe.throw("changes must be a list")
	settings = get_automation_settings()
	bands = get_band_definitions(settings)
	applied, failed = [], []
	confirmed = cint(_json(confirm_processed, 0))

	# Writes below go through SQL, which never checks User Permissions, so the
	# caller's employee scope is enforced here, once for the whole batch.
	owners = {
		r.name: r.employee
		for r in frappe.get_all(
			"Attendance",
			filters={"name": ["in", [c.get("name") for c in changes
									 if isinstance(c, dict) and c.get("name")] or [""]]},
			fields=["name", "employee"],
		)
	}
	in_scope = _permitted_names(owners.values())

	for change in changes:
		if not isinstance(change, dict):
			failed.append({"name": None, "error": "Malformed row"})
			continue
		name = change.get("name")
		if not name:
			failed.append({"name": None, "error": "No attendance record on that row"})
			continue
		if name in owners and owners[name] not in in_scope:
			failed.append({"name": name, "error": "Not permitted for this employee"})
			continue

		current = frappe.db.get_value(
			"Attendance", name,
			["name", "employee", "attendance_date", "docstatus", "status", "leave_type",
			 "leave_application", "half_day_status", "in_time", "out_time",
			 "working_hours", "shift", "modified", "custom_overtime_hours",
			 "custom_late_mark_band", "custom_overtime_manual",
			 "custom_late_mark_manual", "custom_status_manual",
			 "custom_attendance_type"],
			as_dict=True,
		)
		if not current:
			failed.append({"name": name, "error": "Record no longer exists"})
			continue

		if change.get("modified") and str(current.modified) != str(change["modified"]):
			failed.append({
				"name": name,
				"error": "Changed by someone else since this grid loaded. Reload and redo this row.",
			})
			continue

		if current.docstatus == 2:
			failed.append({"name": name, "error": "Record is cancelled"})
			continue

		# The JS dropdown limits the choice; the server must too -- this
		# writes by SQL, so nothing else would stop "Foo" reaching the table.
		new_status = change.get("status")
		if new_status and new_status not in CONSOLE_STATUSES:
			failed.append({"name": name, "error": f"Status '{new_status}' cannot be set here"})
			continue
		# A day backed by a real Leave Application is decided by that
		# application. Overwriting it to Absent would deduct the day AND keep
		# the leave consumed.
		# Same lock as the screen: a day decided by leave -- a Leave
		# Application, or a leave type that is not the automation's own
		# Half Day marker -- keeps its status. Bulk Apply used to get past it.
		genuine_leave = bool(current.leave_type) and (
			bool(current.leave_application) or current.leave_type != settings.half_day_leave_type
		)
		if new_status and new_status != current.status and genuine_leave:
			failed.append({
				"name": name,
				"error": (f"Backed by Leave Application {current.leave_application}. "
						  "Cancel that application instead of changing the status.")
						 if current.leave_application else
						 f"Marked as {current.leave_type}. Change it through a Leave Application.",
			})
			continue
		if "custom_attendance_type" in change:
			visit = change["custom_attendance_type"] or None
			if visit and visit not in VISIT_TYPES:
				failed.append({"name": name, "error": f"Unknown visit type '{visit}'"})
				continue
		pin_error = next(
			(err for f in OVERRIDE_FIELDS if f in change
			 for err in [_check_pin(f, change[f], bands)] if err),
			None,
		)
		if pin_error:
			failed.append({"name": name, "error": pin_error})
			continue

		# Already paid? additional_salary_already_exists() makes
		# get_employees() SKIP an employee whose OT or Late Mark is already
		# submitted for the period. Correcting their attendance afterwards is
		# therefore invisible to payroll: the draft will not include them and
		# nothing errors. Say so rather than let it pass silently.
		if not confirmed:
			blocked = _processed_components(current.employee, current.attendance_date, settings)
			if blocked:
				failed.append({
					"name": name,
					"needs_confirmation": True,
					"error": (
						f"{current.employee} already has submitted "
						f"{' and '.join(blocked)} for this period. Correcting attendance "
						f"now will NOT reach payroll -- the processing document skips "
						f"employees already paid. Cancel and redo that Additional Salary, "
						f"or confirm to edit anyway."
					),
				})
				continue

		if current.docstatus == 0:
			# A draft can be saved properly, so it IS: doc.save() runs the full
			# validate() chain including check_leave_record(), which sets
			# leave_type / leave_application / half_day_status correctly. SQL is
			# only used where the ORM refuses -- submitted records, which have
			# no allow_on_submit fields on Attendance.
			savepoint = f"rapl_edit_{len(applied) + len(failed)}"
			frappe.db.savepoint(savepoint)
			try:
				doc = frappe.get_doc("Attendance", name)
				for field in EDITABLE_FIELDS:
					if field in change:
						value = change[field] or None
						if field in ("in_time", "out_time"):
							value = _combine(doc.attendance_date, value)
						setattr(doc, field, value)
				# Same pins and overrides as the submitted path below --
				# previously only punches and status were copied here, so a
				# visit type / OT / band typed on a draft row was reported as
				# applied and silently dropped, and a hand-chosen status was
				# overwritten by the rules on this very save.
				if new_status and new_status != current.status:
					doc.custom_status_manual = 1
				elif change.get("reset_status"):
					doc.custom_status_manual = 0
				if "custom_attendance_type" in change:
					doc.custom_attendance_type = change["custom_attendance_type"] or None
				for field, flag in OVERRIDE_FIELDS.items():
					if field not in change:
						continue
					value = change[field]
					if value in (None, ""):
						doc.set(flag, 0)
						doc.set(field, None)
					elif value == "__none__":
						doc.set(flag, 1)
						doc.set(field, None)
					else:
						doc.set(flag, 1)
						doc.set(field, flt(value) if field.endswith("_hours") else value)
				if doc.in_time and doc.out_time and get_datetime(doc.out_time) <= get_datetime(doc.in_time):
					raise frappe.ValidationError("Check-out is not after check-in")
				if (doc.status == "Present" and not doc.in_time and not doc.out_time
						and not doc.get("custom_attendance_type")):
					raise frappe.ValidationError(
						"Present with no check-in or check-out needs a visit type "
						"(Site / Client / Vendor Visit)."
					)
				# Recomputed only when a punch changed: HRMS may have computed it
				# from every check-in (breaks excluded), which the raw in-to-out
				# span would overwrite on a plain Recalculate.
				if "in_time" in change or "out_time" in change:
					doc.working_hours = 0
				doc.save()
				applied.append({
					"name": name, "employee": doc.employee,
					"attendance_date": str(doc.attendance_date), "via": "orm",
					"values": {"status": doc.status,
							   "working_hours": flt(doc.working_hours, 2)},
				})
			except Exception as e:
				frappe.db.rollback(save_point=savepoint)
				# Reported in `failed`; don't also pop the raw error dialog.
				frappe.clear_messages()
				failed.append({"name": name,
							   "error": frappe.utils.strip_html(str(e))[:500]})
			continue

		record = dict(current)

		# Choosing a status by hand pins it: without this the rules re-apply
		# Half Day for a late arrival or early exit on the very next save, and
		# a deliberate override could never stick.
		if "status" in change and change["status"] and change["status"] != current.status:
			record["custom_status_manual"] = 1
		elif change.get("reset_status") :
			record["custom_status_manual"] = 0

		if "custom_attendance_type" in change:
			record["custom_attendance_type"] = change["custom_attendance_type"] or None

		# A bad time on one row must fail THAT row, not the whole batch:
		# _combine() throws, and this used to sit outside any try.
		try:
			for field in EDITABLE_FIELDS:
				if field in change:
					value = change[field] or None
					if field in ("in_time", "out_time"):
						value = _combine(current.attendance_date, value)
					record[field] = value
		except Exception as e:
			frappe.clear_messages()
			failed.append({"name": name, "error": frappe.utils.strip_html(str(e))[:300]})
			continue

		if record["in_time"] and record["out_time"]:
			if get_datetime(record["out_time"]) <= get_datetime(record["in_time"]):
				failed.append({"name": name, "error": "Check-out is not after check-in"})
				continue

		# A Present day with no punches at all needs a reason, or every site
		# visit reads as a forgotten punch and the missing-punch flag stops
		# meaning anything. Enforced HERE rather than in Attendance.validate()
		# so the doctype stays usable for imports and manual entry.
		if (record["status"] == "Present" and not record["in_time"]
				and not record["out_time"] and not record.get("custom_attendance_type")):
			failed.append({
				"name": name,
				"error": "Present with no check-in or check-out needs a visit type "
						 "(Site / Client / Vendor Visit).",
			})
			continue

		# working_hours is recomputed from scratch, so clear it first -- the
		# rules only FILL it when empty and would otherwise keep a stale value
		# from the punches this edit just replaced.
		# Day-level manual overrides. "" resets a field to automatic; any other
		# value pins it so derive_attendance_fields() stops recalculating it.
		for field, flag in OVERRIDE_FIELDS.items():
			if field not in change:
				continue
			value = change[field]
			if value in (None, ""):
				# Reset this day to automatic -- the rules take it back over.
				record[flag] = 0
				record[field] = None
			elif value == "__none__":
				# Pinned, but cleared: a deliberately waived late mark. Distinct
				# from "" so the waiver survives future saves instead of being
				# recalculated straight back.
				record[flag] = 1
				record[field] = None
			else:
				record[flag] = 1
				record[field] = flt(value) if field.endswith("_hours") else value

		if "in_time" in change or "out_time" in change:
			record["working_hours"] = 0
		derived = _recompute(record, settings)

		update = {
			"in_time": record["in_time"],
			"out_time": record["out_time"],
			"working_hours": derived["working_hours"],
			"custom_overtime_hours": derived["custom_overtime_hours"],
			"custom_overtime": derived["custom_overtime"],
			"modified": now(),
			"modified_by": frappe.session.user,
			"custom_overtime_manual": cint(record.get("custom_overtime_manual")),
			"custom_late_mark_manual": cint(record.get("custom_late_mark_manual")),
			"custom_status_manual": cint(record.get("custom_status_manual")),
			"custom_attendance_type": record.get("custom_attendance_type"),
		}
		if derived.get("reset_band"):
			update["custom_late_mark_band"] = derived["custom_late_mark_band"]
		if derived["rules_applied"]:
			update["custom_late_mark_band"] = derived["custom_late_mark_band"]
			update["early_exit"] = derived["early_exit"]
			update["status"] = derived["status"]
			if derived["status"] == "Half Day":
				update["half_day_status"] = derived["half_day_status"]
				update["leave_type"] = derived["leave_type"]
			elif derived["cleared_half_day"]:
				# An earlier run applied a Half Day against punches that have
				# since been corrected. Clear the marker along with the status,
				# or Guard 1 would freeze the record again on the next pass.
				update["half_day_status"] = None
				update["leave_type"] = None
		else:
			update["status"] = record["status"]
			# A Half Day that reached an early guard (no punches, holiday)
			# still needs half_day_status, or it costs nothing on the slip.
			if record["status"] == "Half Day" and derived.get("half_day_status") != current.half_day_status:
				update["half_day_status"] = derived.get("half_day_status")

		try:
			frappe.db.set_value("Attendance", name, update, update_modified=False)
		except Exception as e:
			failed.append({"name": name, "error": frappe.utils.strip_html(str(e))[:500]})
			continue

		_audit(name, current, update)
		applied.append({
			"name": name,
			"employee": current.employee,
			"attendance_date": str(current.attendance_date),
			"values": {k: str(v) if v is not None else None for k, v in update.items()},
		})

	# No explicit commit. Frappe commits a clean response for us; committing
	# here would also flush any unrelated pending work in this request and
	# could not be rolled back if something later failed.
	return {"applied": applied, "failed": failed}



def _check_pin(field, value, bands=None):
	"""Validate a day-level pin. Returns an error message or None.

	Overtime: 0-24 hours (a negative pin made the Console show less OT than
	Processing pays -- it clamps at 0 -- and an absurd one was paid as typed).
	Band: an existing band label, or "__none__" for a waived late mark.
	"""
	if value in (None, "", "__none__"):
		return None
	if field.endswith("_hours"):
		try:
			hours = float(value)
		except (TypeError, ValueError):
			return f"Overtime '{value}' is not a number"
		if not 0 <= hours <= 24:
			return "Overtime must be between 0:00 and 24:00"
		return None
	labels = {b["label"] for b in (bands or [])}
	if labels and value not in labels:
		return f"Unknown late mark band '{value}'"
	return None

def _processed_components(employee, attendance_date, settings):
	"""Which components are already submitted for the month containing this date."""
	from frappe.utils import get_first_day

	# Whole month, not just its last day: Processing dates Additional Salary on
	# its own end_date, which is not always the month end.
	month_start, month_end = get_first_day(attendance_date), get_last_day(attendance_date)
	found = []
	for label, component in (
		("Overtime", settings.overtime_salary_component),
		("Late Mark", settings.late_mark_salary_component),
	):
		if _is_processed(None, employee, component, month_start, month_end):
			found.append(label)
	return found


def _audit(name, before, after):
	"""SQL writes leave no version history, so record the change explicitly.

	Uses Comment rather than a custom DocType so the trail is visible on the
	Attendance record itself with no schema to install.
	"""
	changed = []
	for field in ("in_time", "out_time", "status", "working_hours", "leave_type",
				  "custom_late_mark_band", "custom_overtime_hours", "early_exit",
				  "custom_overtime_manual", "custom_late_mark_manual",
				  "custom_status_manual", "custom_attendance_type"):
		if field not in after:
			continue
		old, new = before.get(field), after.get(field)
		if str(old or "") != str(new or ""):
			changed.append(f"{field}: {old or '-'} &rarr; {new or '-'}")
	if not changed:
		return
	try:
		frappe.get_doc({
			"doctype": "Comment",
			"comment_type": "Edit",
			"reference_doctype": "Attendance",
			"reference_name": name,
			"content": "Attendance Console: " + "; ".join(changed),
		}).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "RAPL Console: audit comment failed")


@frappe.whitelist()
def recalculate(names, confirm_processed=0):
	"""Re-apply the rules without changing punches -- clears drift on records
	corrected outside the app."""
	_require_hr("write")
	names = _json(names, [])
	if not isinstance(names, list):
		frappe.throw("names must be a list")
	return apply_edits([{"name": n} for n in names if n], confirm_processed=confirm_processed)


# ---------------------------------------------------------------- create


@frappe.whitelist()
def create_attendance(rows):
	"""Create missing Attendance through the ORM so validate() runs in full.

	rows: [{employee, attendance_date, in_time, out_time, status}, ...]

	Deliberately does NOT set leave_type. Attendance's own check_leave_record()
	resolves it from an approved Leave Application and will OVERRIDE the status
	we ask for (verified in hrms attendance.py). The result is reported back so
	a status that changed underneath is visible rather than silent.
	"""
	_require_hr("create")
	rows = _json(rows, [])
	if not isinstance(rows, list):
		frappe.throw("rows must be a list")
	if len(rows) > MAX_ROWS_PER_APPLY:
		frappe.throw(f"Create at most {MAX_ROWS_PER_APPLY} records at a time.")
	in_scope = _permitted_names(r.get("employee") for r in rows if isinstance(r, dict))

	created, failed = [], []
	for index, row in enumerate(rows):
		if not isinstance(row, dict) or row.get("employee") not in in_scope:
			failed.append({
				"employee": row.get("employee") if isinstance(row, dict) else None,
				"attendance_date": str(row.get("attendance_date")) if isinstance(row, dict) else None,
				"error": "Not permitted for this employee" if isinstance(row, dict) else "Malformed row",
			})
			continue
		# A SAVEPOINT per row. Attendance.validate() can reject a row for six
		# separate reasons (status, inactive employee, joining date, duplicate,
		# overlapping shift, leave record), and rows are independent -- one bad
		# row must not undo the good ones.
		#
		# The previous version called frappe.db.rollback() in the except block,
		# which rolls back the WHOLE transaction: rows created earlier in the
		# loop were silently destroyed while still being reported as created.
		savepoint = f"rapl_create_{index}"
		frappe.db.savepoint(savepoint)
		try:
			doc = frappe.new_doc("Attendance")
			doc.employee = row["employee"]
			if not row.get("attendance_date"):
				# getdate(None) is TODAY -- a row without a date silently
				# created attendance for today.
				raise frappe.ValidationError("Attendance date is required")
			doc.attendance_date = getdate(row["attendance_date"])
			doc.status = row.get("status") or "Present"
			# Same list as apply_edits. "On Leave" with no Leave Application
			# passes Attendance's own validation and becomes a paid day that
			# uses no leave balance -- leave is created via Create leave.
			if doc.status not in CONSOLE_STATUSES:
				raise frappe.ValidationError(f"Status '{doc.status}' cannot be set here")
			# Times are combined with the date HERE, not in the browser. The
			# dialog sends a bare "HH:MM"; concatenating it client-side was
			# fragile and silently produced nulls, creating records with no
			# punches at all.
			doc.in_time = _combine(doc.attendance_date, row.get("in_time"))
			doc.out_time = _combine(doc.attendance_date, row.get("out_time"))
			if row.get("shift"):
				doc.shift = row["shift"]
			doc.insert()
			doc.submit()
			created.append({
				"name": doc.name,
				"employee": doc.employee,
				"attendance_date": str(doc.attendance_date),
				"status": doc.status,
				"status_changed": doc.status != (row.get("status") or "Present"),
				"leave_type": doc.leave_type,
				"in_time": str(doc.in_time) if doc.in_time else None,
				"out_time": str(doc.out_time) if doc.out_time else None,
				"working_hours": flt(doc.working_hours, 2),
			})
		except Exception as e:
			frappe.db.rollback(save_point=savepoint)
			failed.append({
				"employee": row.get("employee"),
				"attendance_date": str(row.get("attendance_date")),
				"error": frappe.utils.strip_html(str(e))[:500],
			})
	return {"created": created, "failed": failed}


# ---------------------------------------------------------------- drafts


def _combine(attendance_date, value):
	"""Accept "HH:MM", "HH:MM:SS" or a full datetime and return a datetime on
	attendance_date. Returns None for anything empty."""
	if not value:
		return None
	value = str(value).strip()
	if not value:
		return None
	if len(value) > 10 and (" " in value or "T" in value):
		return get_datetime(value)          # already a full datetime
	parts = value.split(":")
	if len(parts) == 2:
		value = f"{value}:00"
	elif len(parts) != 3:
		frappe.throw(f"Could not read the time '{value}'. Use HH:MM.")
	return get_datetime(f"{getdate(attendance_date)} {value}")


def _existing_draft(doctype, start_date, end_date):
	return frappe.db.get_value(
		doctype,
		{"start_date": start_date, "end_date": end_date, "docstatus": 0},
		"name",
	)


# Which Console override keys belong to which draft. The overtime amount is
# keyed "amount" and the late-mark amount "late_amount" -- one employee can
# carry both, so they must never be read across.
OT_OVERRIDE_KEYS = {"ot_hours_hhmm", "ot_rate", "amount"}
LATE_OVERRIDE_KEYS = {"late_amount", "per_day_rate"} | {f"band_{i}_count" for i in range(1, 6)}
_OVERRIDE_KEYS = OT_OVERRIDE_KEYS | LATE_OVERRIDE_KEYS


def _clean_overrides(overrides):
	"""Reject a malformed override payload up front.

	It used to be used as-is: int("abc") raised an unhandled ValueError (a 500
	with a traceback) AFTER the draft had already been inserted, and negative
	or non-numeric amounts were accepted.
	"""
	if not overrides:
		return {}
	if not isinstance(overrides, dict):
		frappe.throw("overrides must be an object keyed by employee")
	clean = {}
	for employee, values in overrides.items():
		if not isinstance(values, dict):
			frappe.throw(f"Overrides for {employee} are malformed")
		row = {}
		for key, value in values.items():
			if key not in _OVERRIDE_KEYS or value in (None, ""):
				continue
			try:
				number = float(value)
			except (TypeError, ValueError):
				frappe.throw(f"{employee}: '{key}' must be a number, got '{value}'")
			if number < 0:
				frappe.throw(f"{employee}: '{key}' cannot be negative")
			row[key] = number
		clean[str(employee)] = row
	return clean


def _apply_overrides(doc, kind, overrides, bands):
	"""Write the Console's edited summary values onto the draft's entry rows.

	Values the user typed win over what get_employees() computed. Rows the user
	did not touch are left exactly as fetched.
	"""
	if not overrides:
		return []

	touched = []
	by_employee = {row.employee: row for row in doc.entries}

	kind_keys = OT_OVERRIDE_KEYS if kind == "overtime" else LATE_OVERRIDE_KEYS
	for employee, values in overrides.items():
		row = by_employee.get(employee)
		if not row or not (kind_keys & set(values)):
			continue

		if kind == "overtime":
			if values.get("ot_hours_hhmm") is not None:
				row.ot_hours_hhmm = int(round_half_up(values["ot_hours_hhmm"]))
				row.ot_hours = round_half_up(row.ot_hours_hhmm / 3600.0, 2)  # display
			if values.get("ot_rate") is not None:
				row.ot_rate = flt(values["ot_rate"])
			row.amount = (
				round_half_up(flt(values["amount"])) if values.get("amount") is not None
				# Priced from the exact seconds, not the 2-dp display hours.
				else ot_amount(row.ot_hours_hhmm, row.ot_rate)
			)
		else:
			for index, band in enumerate(bands, start=1):
				key = f"band_{index}_count"
				if key in values:
					setattr(row, key, int(round_half_up(values[key] or 0)))
			if values.get("per_day_rate") is not None:
				row.per_day_rate = flt(values["per_day_rate"])
			# "late_amount" ONLY. "amount" is the OVERTIME amount; the old
			# fallback to it wrote an employee's overtime pay into their
			# late-mark deduction when only their OT had been edited.
			late_amount = values.get("late_amount")
			if late_amount is not None:
				row.amount = round_half_up(flt(late_amount))   # whole rupees, like computed amounts
			else:
				fraction = sum(
					flt(b["fraction"]) * int(getattr(row, f"band_{i}_count", 0) or 0)
					for i, b in enumerate(bands, start=1)
				)
				row.amount = round_half_up(fraction * flt(row.per_day_rate))
			# On a row with a late-mark waiver, the Console's figure is the
			# deduction BEFORE the waiver; validate() then takes the waived
			# marks off it. Without this the save re-derived Amount from the
			# old pre-waiver figure and the Console's value was lost.
			if int(getattr(row, "waived_marks", 0) or 0):
				row.amount_before_waiver = row.amount
		touched.append(employee)

	if touched:
		doc.save()
	return touched


@frappe.whitelist()
def create_processing_draft(
	kind, start_date=None, end_date=None, employees=None, overrides=None
):
	"""Create (or refresh) a DRAFT RAPL Overtime / Late Mark Processing.

	Never submits. Submission stays in the document's own form, where the
	existing additional_salary_already_exists() guards, permissions and
	workflow all still apply. A console button that created submitted payroll
	documents would be a different risk class entirely.

	Re-running against an existing draft calls get_employees again. That is
	safe: get_employees skips employees already in the table ("already in the
	table (manual or previous fetch) -- don't touch it"), so manual overrides
	survive a refresh.
	"""
	_require_hr("read")
	start_date, end_date = _period_bounds(start_date, end_date)

	doctype = {
		"overtime": "RAPL Overtime Processing",
		"late_mark": "RAPL Late Mark Processing",
	}.get(kind)
	if not doctype:
		frappe.throw("Unknown processing type")
	# This endpoint inserts and saves documents; say so up front rather than
	# failing half-way through at the ORM's own check.
	if not frappe.has_permission(doctype, "create") or not frappe.has_permission(doctype, "write"):
		frappe.throw(f"You need create and write permission on {doctype}.", frappe.PermissionError)

	employees = _employee_list(employees)
	overrides = _clean_overrides(_json(overrides, {}))
	# Decided from what the caller ASKED for, before any filtering: a list
	# that filters down to nothing must never become "the whole workforce".
	whole_workforce = not employees
	if employees:
		scope = _permitted_names(employees)
		employees = [e for e in employees if e in scope]
		if not employees:
			frappe.throw("None of the selected employees are available to you.", frappe.PermissionError)
	if overrides:
		scope = _permitted_names(overrides.keys())
		overrides = {k: v for k, v in overrides.items() if k in scope}

	# An employee with Employee.custom_ot = 0 is skipped by get_employees()'s
	# default mode, so a manually entered OT figure for them would be dropped.
	# Naming them explicitly switches get_employees() to its first mode --
	# "exactly those employees", which bypasses the custom_ot filter by design
	# -- so a manual override always survives.
	#
	# With NO employee list the draft covers the normal eligible workforce
	# (get_employees' default mode) AND the overridden employees on top --
	# merging them into one explicit list used to shrink a whole-month draft
	# down to just the employees someone had typed an override for.
	#
	# Only overrides that belong to THIS draft count: an OT edit must not pull
	# an employee into the late-mark draft, or the other way round.
	kind_keys = OT_OVERRIDE_KEYS if kind == "overtime" else LATE_OVERRIDE_KEYS
	overrides = {k: v for k, v in overrides.items() if kind_keys & set(v)}
	extra = sorted(overrides)
	if not whole_workforce:
		if kind == "overtime":
			# Naming an employee switches get_employees() to "exactly these",
			# which skips the custom_ot check. Keep that bypass for someone
			# whose OT was typed by hand; anyone else must be OT-eligible, or
			# an employee the Console showed with 0 OT got full punch OT.
			eligible = set(frappe.get_all(
				"Employee", filters={"name": ["in", employees], "custom_ot": 1}, pluck="name"
			))
			employees = [e for e in employees if e in eligible or e in overrides]
			if not employees and not extra:
				frappe.throw("None of the selected employees is eligible for overtime (Employee: OT unticked).")
		employees = sorted(set(employees) | set(extra))

	name = _existing_draft(doctype, start_date, end_date)
	if name:
		doc = frappe.get_doc(doctype, name)
		reused = True
		# A draft for this period already exists and may have been built for a
		# DIFFERENT employee set. get_employees() appends rather than replaces
		# ("already in the table -- don't touch it"), so reusing is safe, but
		# the caller is told the row count moved so a filtered run cannot
		# silently attach to a whole-month draft without anyone noticing.
		rows_before = len(doc.entries)
	else:
		rows_before = 0
		doc = frappe.new_doc(doctype)
		doc.start_date = start_date
		doc.end_date = end_date
		doc.insert()
		reused = False

	module = (
		"rapl_payroll_automation.rapl_payroll_automation.doctype."
		f"{frappe.scrub(doctype)}.{frappe.scrub(doctype)}"
	)
	get_employees = frappe.get_attr(f"{module}.get_employees")
	already_there = {row.employee for row in doc.entries}
	# get_employees() never recalculates a row already in the draft (it may
	# hold hand edits). Say so, so "refreshed" is not read as "recalculated".
	kept_rows = len(already_there)
	if whole_workforce:
		result = get_employees(doc.name, all_employees=False, employees=None)
		if extra:
			# Appends only employees not already in the table.
			get_employees(doc.name, all_employees=False, employees=extra)
	else:
		result = get_employees(doc.name, all_employees=False, employees=employees)

	doc.reload()
	if whole_workforce:
		# get_employees' default mode lists the workforce with frappe.get_all,
		# which ignores User Permissions. Drop the rows THIS call added for
		# employees outside the caller's scope. Rows that were already in a
		# shared draft are left alone -- they are someone else's work.
		added = {row.employee for row in doc.entries} - already_there
		out_of_scope = added - _permitted_names(added)
		if out_of_scope:
			doc.entries = [r for r in doc.entries if r.employee not in out_of_scope]
			doc.save()
			doc.reload()
	overridden = _apply_overrides(doc, kind, overrides, get_band_definitions(get_automation_settings()))
	doc.reload()

	return {
		"month_in_progress": getdate() <= end_date,
		"overridden": overridden,
		"doctype": doctype,
		"name": doc.name,
		"reused": reused,
		"rows_before": rows_before,
		"rows_after": len(doc.entries),
		"kept_rows": kept_rows,
		"filtered": not whole_workforce,
		"result": result,
		"route": f"/app/{frappe.scrub(doctype).replace('_', '-')}/{doc.name}",
	}


@frappe.whitelist()
def create_advance_drafts(advances, payroll_date=None):
	"""One DRAFT Additional Salary per selected Employee Advance.

	advances: ["EA103", "EA117", ...] -- the advances HR ticked.

	One record PER ADVANCE, not one lump sum, because each carries
	ref_doctype/ref_docname back to its Employee Advance. That is how HRMS's own
	"Deduction from Salary" button links them, and it is what lets the advance
	show as recovered afterwards. A single combined record would recover money
	against nothing.

	Amount is the full outstanding: paid - claimed - returned. Partial recovery
	is deliberately not offered yet.

	Never submitted. Review and submit happen on the Additional Salary itself.
	"""
	_require_hr("create")
	advances = _json(advances, [])
	if not isinstance(advances, list):
		frappe.throw("advances must be a list")
	advances = list(dict.fromkeys(str(a) for a in advances if a))
	if not advances:
		return {"created": [], "failed": []}
	if len(advances) > MAX_ROWS_PER_APPLY:
		frappe.throw(f"At most {MAX_ROWS_PER_APPLY} advances at a time.")
	# REQUIRED. It used to default to the advance's own posting date -- often
	# months earlier -- and HRMS only picks up Additional Salary dated inside
	# the slip's period, so the recovery was never deducted.
	if not payroll_date:
		frappe.throw("Payroll date is required: the recovery is deducted in the month it is dated.")
	payroll_date = getdate(payroll_date)
	if not frappe.has_permission("Additional Salary", "create"):
		frappe.throw("You need create permission on Additional Salary.", frappe.PermissionError)

	settings = get_automation_settings()
	component = settings.get("advance_salary_component")
	if not component:
		frappe.throw(
			"Set 'Advance Recovery Salary Component' in RAPL Payroll Automation Settings first."
		)

	created, failed = [], []
	for index, advance_name in enumerate(advances):
		savepoint = f"rapl_adv_{index}"
		frappe.db.savepoint(savepoint)
		try:
			adv = frappe.get_doc("Employee Advance", advance_name)
			if adv.employee not in _permitted_names([adv.employee]):
				raise frappe.PermissionError(f"{advance_name}: not permitted for {adv.employee}")
			if adv.docstatus != 1:
				raise frappe.ValidationError(f"{advance_name} is not submitted")
			if not cint(adv.repay_unclaimed_amount_from_salary):
				raise frappe.ValidationError(
					f"{advance_name} is not marked 'Repay Unclaimed Amount from Salary'"
				)
			rec_state = _advance_recovery_state([advance_name])
			if (rec_state.get(advance_name) or {}).get("drafts"):
				raise frappe.ValidationError(
					f"{advance_name} already has a draft recovery record -- submit or delete it first"
				)
			# Partial recoveries are allowed (HRMS caps a new one at paid -
			# claimed - already scheduled); only what is left is recovered.
			outstanding = _recoverable_amount(adv, rec_state)
			if outstanding <= 0.005:
				raise frappe.ValidationError(f"{advance_name} has nothing left to recover")

			ads = frappe.new_doc("Additional Salary")
			ads.employee = adv.employee
			ads.company = adv.company
			ads.currency = adv.currency
			ads.salary_component = component
			ads.amount = flt(outstanding, 2)
			# MUST be 0. The field defaults to 1, which would REPLACE the
			# structure amount for this component instead of adding to it.
			ads.overwrite_salary_structure_amount = 0
			ads.payroll_date = payroll_date
			ads.ref_doctype = "Employee Advance"
			ads.ref_docname = advance_name
			ads.insert()

			created.append({
				"name": ads.name, "advance": advance_name, "employee": adv.employee,
				"amount": flt(outstanding, 2),
				"route": f"/app/additional-salary/{ads.name}",
			})
		except Exception as e:
			frappe.db.rollback(save_point=savepoint)
			failed.append({
				"advance": advance_name,
				"error": frappe.utils.strip_html(str(e))[:400],
			})
	return {"created": created, "failed": failed}


@frappe.whitelist()
@rate_limit(limit=60, seconds=60)
def compute_net_pay(employee, start_date=None, end_date=None, overtime=0,
					late_mark=0, advance=0):
	"""Net pay from the LIVE Salary Structure, with the Console's pending
	amounts injected. Computes in memory and saves nothing.

	WHY INJECT RATHER THAN REIMPLEMENT
	----------------------------------
	salary_slip.get_additional_salaries() filters docstatus == 1, so a preview
	slip cannot see the Overtime / Late Mark / Advance the Console is about to
	create -- they are still drafts. Writing our own pay engine to work around
	that would mean a second implementation of every structure formula and tax
	slab, drifting from HRMS on every upgrade. Instead the rows are appended to
	earnings/deductions BEFORE calculate_net_pay() runs (verified in
	salary_slip.py: it does not clear those tables and update_component_row()
	updates a matching row rather than duplicating it).

	A component that is ALREADY submitted for the month is NOT injected again:
	the slip picks submitted Additional Salary up on its own, so injecting would
	count it twice.

	WHICH DAYS ARE PAID
	-------------------
	Payment days are rebuilt here from attendance (payroll_math.
	expected_payment_days) instead of trusting HRMS's figure, because HRMS's
	treatment of a day nobody marked depends on Payroll Settings ('Consider
	Unmarked Attendance As') and its treatment of Absent depends on
	'Payroll Based On'. HR wants absent days, unpaid half days AND days with no
	attendance record all to cost money. The HRMS figure is returned alongside
	(native_payment_days) so a difference is visible, not silent.

	A month that has not finished is priced MONTH-TO-DATE: only days up to
	today (or yesterday, if today has no attendance yet) are earned. Salary
	still divides by the full month's days, so five elapsed days is five
	thirty-firsts of pay, not a full month.

	The whole call runs inside a savepoint that is ALWAYS rolled back: this is
	a preview, and it must be provably incapable of writing even if some path
	inside HRMS does.
	"""
	_require_hr("read")
	if not _permitted_employees([employee], include_inactive=True):
		frappe.throw(f"Employee {employee} is not available to you.", frappe.PermissionError)

	start_date, end_date = _period_bounds(start_date, end_date)

	# A Monthly slip always snaps to one calendar month, so a period that is
	# not exactly one month would be priced for a different range than the
	# overtime and late-mark figures being injected.
	if start_date.day != 1 or end_date != get_last_day(start_date):
		return {"error": "Net pay is previewed one whole calendar month at a time."}

	today = getdate()
	if start_date > today:
		return {"error": "That month has not started yet, so there is nothing to compute."}

	settings = get_automation_settings()

	savepoint = "rapl_net_pay_preview"
	frappe.db.savepoint(savepoint)
	try:
		result = _build_net_pay_preview(
			employee, start_date, end_date, today, settings,
			flt(overtime), flt(late_mark), flt(advance),
		)
	except Exception as e:
		# Do NOT log_error here: it inserts an Error Log row INSIDE the
		# savepoint, and the rollback below would erase it -- the user is told
		# to "see Error Log" and finds nothing. Keep the traceback, log after.
		failure = (frappe.get_traceback(), str(e))
		result = {"error": frappe.utils.strip_html(failure[1])[:500]}
	else:
		failure = None
	finally:
		# ALWAYS roll back -- nothing here is ever meant to persist.
		try:
			frappe.db.rollback(save_point=savepoint)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "RAPL: net pay preview rollback failed")
	if failure:
		frappe.log_error(failure[0], f"RAPL: net pay preview failed for {employee}")
	return result


def _build_net_pay_preview(employee, start_date, end_date, today, settings,
						   overtime, late_mark, advance):
	slip = frappe.new_doc("Salary Slip")
	slip.employee = employee
	slip.start_date = start_date
	slip.end_date = end_date
	slip.payroll_frequency = "Monthly"
	slip.get_emp_and_working_day_details()

	if not slip.get("salary_structure"):
		# HRMS only msgprints here and carries on with an empty slip, which
		# would come back as a calm-looking zero.
		return {"error": f"{employee} has no active Salary Structure Assignment for this month."}

	ps = frappe.get_cached_value(
		"Payroll Settings", None,
		["include_holidays_in_total_working_days", "daily_wages_fraction_for_half_day",
		 "consider_marked_attendance_on_holidays", "disable_rounded_total"],
		as_dict=1,
	) or {}
	include_holidays = cint(ps.get("include_holidays_in_total_working_days"))
	half_fraction = flt(ps.get("daily_wages_fraction_for_half_day")) or 0.5
	count_on_holidays = include_holidays and cint(ps.get("consider_marked_attendance_on_holidays"))

	first, last = getdate(slip.actual_start_date), getdate(slip.actual_end_date)
	holidays = {getdate(d) for d in (slip.get_holidays_for_employee(first, last) or [])}

	marked = frappe.get_all(
		"Attendance",
		filters={"employee": employee, "docstatus": 1,
				 "attendance_date": ["between", [first, last]]},
		fields=["attendance_date", "status", "half_day_status"],
	)
	marked_dates = {getdate(a.attendance_date) for a in marked}

	cutoff, in_progress = month_cutoff(
		start_date, end_date, today,
		today_has_attendance=(today in marked_dates) or (today in holidays),
	)
	if cutoff is None or cutoff < first:
		return {"error": "No completed working day in this month yet, so nothing has been earned."}

	def counts_as_payable(day):
		return bool(include_holidays) or day not in holidays

	base_days = sum(
		1 for i in range(date_diff(last, first) + 1)
		if counts_as_payable(getdate(add_days(first, i)))
	)
	future_days = sum(
		1 for i in range(date_diff(last, cutoff))
		if counts_as_payable(getdate(add_days(cutoff, i + 1)))
	)

	# Never count past the employee's last day of service (relieving date).
	upto = min(cutoff, last)

	def deductible(day):
		return day <= upto and (day not in holidays or count_on_holidays)

	absent = sum(1 for a in marked
				 if a.status == "Absent" and deductible(getdate(a.attendance_date)))
	half_absent = sum(1 for a in marked
					  if a.status == "Half Day" and (a.half_day_status or "Absent") == "Absent"
					  and deductible(getdate(a.attendance_date)))
	missing = sum(
		1 for i in range(date_diff(upto, first) + 1)
		if getdate(add_days(first, i)) not in holidays
		and getdate(add_days(first, i)) not in marked_dates
	)

	# HRMS's leave_without_pay covers the WHOLE month, including approved LWP
	# dated after the cut-off -- but every day after the cut-off is already
	# removed as future_days. Take the future part out so it is not subtracted
	# twice in a month that is still running.
	lwp = max(
		flt(slip.leave_without_pay)
		- _lwp_days_after(employee, upto, last, holidays, include_holidays=include_holidays,
						  half_fraction=half_fraction),
		0,
	)

	native_payment_days = flt(slip.payment_days)
	slip.payment_days = expected_payment_days(
		base_days, lwp, absent, half_absent, half_fraction,
		missing, future_days,
	)
	slip.absent_days = absent + half_absent * half_fraction + missing

	# Inject what the Console is about to create -- unless it is already there.
	pending = [
		("earnings", "overtime", settings.get("overtime_salary_component"), overtime),
		("deductions", "late_mark", settings.get("late_mark_salary_component"), late_mark),
		("deductions", "advance", settings.get("advance_salary_component"), advance),
	]
	injected, already_submitted = [], []
	for table, key, component, amount in pending:
		if not component or not amount:
			continue
		# Overtime / Late Mark: one record per month, so a submitted one means
		# this is already on the slip. Advances are different -- the Console
		# only offers what no recovery record covers yet, so a pending advance
		# is always added on top of any recovery already submitted.
		if key != "advance" and get_additional_salary_total(employee, component, start_date, end_date) > 0:
			already_submitted.append({"component": component, "kind": key})
			continue
		comp = frappe.db.get_value(
			"Salary Component", component,
			["salary_component_abbr", "depends_on_payment_days", "variable_based_on_taxable_salary"],
			as_dict=True,
		) or {}
		if cint(comp.get("variable_based_on_taxable_salary")):
			continue   # a tax component is never injected (HRMS looks it up by name)
		# Shaped EXACTLY like the row HRMS update_component_row() builds for a
		# submitted, non-overwrite Additional Salary: default_amount 0, the
		# money in additional_amount, the component's own "Depends on Payment
		# Days", then HRMS's own proration applied to row.amount. The preview
		# used to hard-code "not prorated", so with that box ticked it promised
		# more overtime -- and a bigger deduction -- than the slip pays.
		# additional_salary must be non-empty for HRMS to treat the row as one;
		# the placeholder is never saved (the whole preview is rolled back).
		row = slip.append(table, {
			"salary_component": component,
			"abbr": comp.get("salary_component_abbr") or component[:3],
			"amount": amount,
			"default_amount": 0,
			"additional_amount": amount,
			"additional_salary": "Console preview (not saved)",
			"is_additional_component": 1,
			"depends_on_payment_days": cint(comp.get("depends_on_payment_days")),
		})
		slip.update_component_amount_based_on_payment_days(row)
		injected.append({"table": table, "component": component, "amount": amount, "kind": key})

	# skip_tax_breakup_computation: the year-to-date / tax-breakup figures are
	# display-only on the real slip and are the slowest part of the calculation.
	slip.calculate_net_pay(skip_tax_breakup_computation=True)

	# The real Salary Slip re-derives PF / PT / ESI in a validate hook after
	# calculate_net_pay(). Without running the same hook here the preview and
	# the slip HR actually saves disagree on those three lines.
	statutory_applied = False
	try:
		from rapl_payroll_automation.api.salary_slip_hooks import (
			correct_statutory_deductions,
			set_precomputed_fields,
		)
		set_precomputed_fields(slip, "before_validate")
		injected_ot = sum(i["amount"] for i in injected if i["kind"] == "overtime")
		if injected_ot:
			slip.custom_overtime_for_pt = flt(slip.get("custom_overtime_for_pt")) + injected_ot
		correct_statutory_deductions(slip, "validate")
		statutory_applied = True
	except Exception:
		# Not logged inside the savepoint (it would be rolled back); the
		# preview simply reports statutory_applied = False.
		pass

	def listing(rows, table):
		return [
			{"component": r.salary_component, "amount": flt(r.amount, 2),
			 "injected": any(i["component"] == r.salary_component and i["table"] == table
							 for i in injected)}
			for r in rows if flt(r.amount)
		]

	return {
		"employee": employee,
		"month_in_progress": in_progress,
		"through_date": str(cutoff),
		"payment_days": flt(slip.payment_days, 2),
		"native_payment_days": flt(native_payment_days, 2),
		"total_working_days": flt(slip.total_working_days, 2),
		"days": {
			"period": base_days, "absent": absent, "half_absent": half_absent,
			"half_fraction": half_fraction, "missing": missing,
			"lwp": flt(lwp, 2), "not_yet_earned": future_days,
		},
		"gross_pay": flt(slip.gross_pay, 2),
		"total_deduction": flt(slip.total_deduction, 2),
		"net_pay": flt(slip.net_pay, 2),
		"rounded_total": flt(slip.rounded_total, 2),
		"rounding_disabled": bool(cint(ps.get("disable_rounded_total"))),
		"earnings": listing(slip.earnings, "earnings"),
		"deductions": listing(slip.deductions, "deductions"),
		"injected": injected,
		"already_submitted": already_submitted,
		"statutory_applied": statutory_applied,
	}


def _lwp_days_after(employee, after, last, holidays=None, include_holidays=True, half_fraction=0.5):
	"""Approved leave-without-pay days strictly after `after`, up to `last`.

	Counted the way HRMS counts leave_without_pay: a holiday inside the leave
	is NOT a leave day unless the Leave Type has "Include holidays" ticked.
	"""
	if after >= last:
		return 0.0
	# Leave without pay AND partially paid leave, as HRMS's get_leave_type_map:
	# a PPL day counts as (1 - fraction paid) of an LWP day.
	lwp_types = {}
	for lt in frappe.get_all(
		"Leave Type", or_filters={"is_lwp": 1, "is_ppl": 1},
		fields=["name", "include_holiday", "is_ppl", "fraction_of_daily_salary_per_leave"],
	):
		weight = 1.0
		if cint(lt.is_ppl) and flt(lt.fraction_of_daily_salary_per_leave):
			weight = 1 - flt(lt.fraction_of_daily_salary_per_leave)
		lwp_types[lt.name] = {"include_holiday": cint(lt.include_holiday), "weight": weight}
	if not lwp_types:
		return 0.0
	holidays = holidays or set()
	total = 0.0
	for la in frappe.get_all(
		"Leave Application",
		filters={"employee": employee, "leave_type": ["in", list(lwp_types)], "docstatus": 1,
				 "status": "Approved", "to_date": [">", after], "from_date": ["<=", last]},
		fields=["leave_type", "from_date", "to_date", "half_day", "half_day_date"],
	):
		start = max(getdate(la.from_date), getdate(add_days(after, 1)))
		end = min(getdate(la.to_date), getdate(last))
		for i in range(date_diff(end, start) + 1):
			day = getdate(add_days(start, i))
			# HRMS: with "Include holidays in total working days" OFF, holidays
			# are never working days, so never LWP -- whatever the Leave Type.
			lt = lwp_types.get(la.leave_type) or {"include_holiday": 0, "weight": 1.0}
			if day in holidays and not (include_holidays and lt["include_holiday"]):
				continue
			if cint(la.half_day) and la.half_day_date and getdate(la.half_day_date) == day:
				total += (1 - flt(half_fraction)) * lt["weight"]   # as HRMS: (1 - half-day fraction)
			else:
				total += lt["weight"]
	return total


def _contiguous_blocks(dates):
	"""Group sorted dates into runs of consecutive days.

	Three scattered absences become three Leave Applications, not one range --
	a single from/to spanning them would swallow the working days in between
	and mark those as leave too.
	"""
	blocks, run = [], []
	for d in sorted(getdate(x) for x in dates):
		if run and (d - run[-1]).days == 1:
			run.append(d)
		else:
			if run:
				blocks.append(run)
			run = [d]
	if run:
		blocks.append(run)
	return blocks


@frappe.whitelist()
def create_leave_applications(employee, dates):
	"""Turn ticked Absent days into APPROVED Leave Applications.

	The Console never rewrites Attendance for leave. Leave Application's own
	update_attendance() does it on approve: it sets status to On Leave, fills
	leave_type and leave_application, and on a holiday it CANCELS AND DELETES
	the Attendance outright. Editing Attendance directly would be fighting that.

	Created as Approved, so update_attendance() runs immediately.

	Contiguous dates are grouped into one application per run.
	"""
	_require_hr("read")
	# Approved leave turns an unpaid Absent into a paid day, so this needs the
	# caller's own Leave Application rights -- not just Attendance create --
	# and goes through the normal permission layer (no ignore_permissions).
	for ptype in ("create", "submit"):
		if not frappe.has_permission("Leave Application", ptype):
			frappe.throw(f"You need {ptype} permission on Leave Application.", frappe.PermissionError)
	if employee not in _permitted_names([employee]):
		frappe.throw(f"Employee {employee} is not available to you.", frappe.PermissionError)

	dates = _json(dates, [])
	if not isinstance(dates, list):
		frappe.throw("dates must be a list")
	dates = sorted({getdate(d) for d in dates if d})
	if not dates:
		return {"created": [], "failed": []}
	if len(dates) > 31:
		frappe.throw("At most 31 days at a time.")

	# Only days that really are Absent can become leave -- the Console only
	# offers those, and the server now insists on it too.
	absent = {
		getdate(d) for d in frappe.get_all(
			"Attendance",
			filters={"employee": employee, "docstatus": 1, "status": "Absent",
					 "attendance_date": ["in", dates]},
			pluck="attendance_date",
		)
	}
	not_absent = [d for d in dates if d not in absent]
	dates = [d for d in dates if d in absent]

	settings = get_automation_settings()
	leave_type = settings.get("absent_leave_type")
	if not leave_type:
		frappe.throw("Set 'Leave Type for Absent Days' in RAPL Payroll Automation Settings first.")

	company = frappe.db.get_value("Employee", employee, "company")
	created = []
	failed = [{"from_date": str(d), "to_date": str(d),
			   "error": "Not an Absent day -- only Absent days can become leave"}
			  for d in not_absent]

	for index, block in enumerate(_contiguous_blocks(dates)):
		savepoint = f"rapl_leave_{index}"
		frappe.db.savepoint(savepoint)
		try:
			la = frappe.new_doc("Leave Application")
			la.employee = employee
			la.leave_type = leave_type
			la.from_date = block[0]
			la.to_date = block[-1]
			la.company = company
			la.posting_date = getdate()
			la.status = "Approved"
			la.follow_via_email = 0
			la.description = "Created from the Attendance Console"
			la.insert()
			la.submit()
			created.append({
				"name": la.name, "from_date": str(block[0]), "to_date": str(block[-1]),
				"days": flt(la.total_leave_days, 2),
				"route": f"/app/leave-application/{la.name}",
			})
		except Exception as e:
			frappe.db.rollback(save_point=savepoint)
			failed.append({
				"from_date": str(block[0]), "to_date": str(block[-1]),
				"error": frappe.utils.strip_html(str(e))[:400],
			})
	return {"created": created, "failed": failed}
