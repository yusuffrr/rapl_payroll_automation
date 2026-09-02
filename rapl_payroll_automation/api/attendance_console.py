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
from frappe.utils import cint, flt, get_datetime, getdate, now

from rapl_payroll_automation.api.attendance_automation import derive_attendance_fields
from rapl_payroll_automation.api.attendance_data import (
	build_month_rows,
	get_band_definitions,
	summarise,
)
from rapl_payroll_automation.api.ot_engine import is_ot_eligible
from rapl_payroll_automation.api.payroll_automation_utils import (
	additional_salary_already_exists,
	get_all_holiday_dates,
	get_automation_settings,
	get_grade_ot_rule,
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


MAX_ROWS_PER_APPLY = 500


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
	frappe.get_all applies the restriction; we then work only within that set.

	include_inactive: relieved employees still need their final period
	corrected, so existing records stay reachable. Attendance.validate() calls
	validate_active_employee() and throws on INSERT, so creation for them is
	blocked by the framework and is not worked around here.
	"""
	filters = {} if include_inactive else {"status": "Active"}
	if employees:
		filters["name"] = ["in", employees]
	return frappe.get_all(
		"Employee",
		filters=filters,
		fields=["name", "employee_name", "grade", "status",
				"date_of_joining", "relieving_date", "company"],
		order_by="name",
	)


def _period_bounds(start_date, end_date):
	if not start_date or not end_date:
		frappe.throw("Set a period first")
	return getdate(start_date), getdate(end_date)


# ---------------------------------------------------------------- read


@frappe.whitelist()
def get_console_data(start_date=None, end_date=None, employees=None, only_flagged=0):
	"""Employee summary rows + their day rows, for the grouped grid."""
	_require_hr("read")
	start_date, end_date = _period_bounds(start_date, end_date)

	if isinstance(employees, str):
		employees = frappe.parse_json(employees)
	only_flagged = int(only_flagged or 0)

	settings = get_automation_settings()
	bands = get_band_definitions(settings)

	# Anyone with attendance in the period is included even if no longer
	# Active -- a leaver's final month still needs correcting.
	with_records = frappe.get_all(
		"Attendance",
		filters={"attendance_date": ["between", [start_date, end_date]], "docstatus": ["<", 3]},
		pluck="employee", distinct=True,
	)
	candidates = _permitted_employees(employees, include_inactive=True)
	allowed = {e.name for e in _permitted_employees(employees)} | set(with_records)
	candidates = [e for e in candidates if e.name in allowed]

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
			"ot_eligible": is_ot_eligible(emp.name),
			"summary": summary,
			"entry": _entry_preview(emp, start_date, end_date, summary, bands, settings),
			"rows": rows,
		})

	return {
		"start_date": str(start_date),
		"end_date": str(end_date),
		"bands": bands,
		"groups": groups,
		"loaded_at": now(),
	}


def _entry_preview(emp, start_date, end_date, summary, bands, settings):
	"""What the RAPL Overtime / Late Mark Processing Entry rows WOULD hold.

	Rate derivation is copied in shape from rapl_overtime_processing so the
	preview and the created draft cannot disagree: per-day amount rounded to
	whole rupees FIRST, then the hourly rate from that.
	"""
	monthly = flt(frappe.db.get_value("Employee", emp.name, settings.ot_rate_base_fieldname))
	rule = get_grade_ot_rule(settings, emp.grade)

	total_days = get_total_working_days(start_date, end_date)
	if rule:
		holiday_list = get_holiday_list_for_employee(emp.name)
		sundays = get_weekly_off_dates(holiday_list, start_date, end_date) or []
		ot_days = total_days - len(sundays)
	else:
		ot_days = total_days

	per_day = round(monthly / ot_days) if (monthly and ot_days > 0) else 0
	hourly = round(per_day / flt(settings.ot_hours_divisor), 2) if per_day else 0

	ot_hours = flt(summary["overtime_hours"], 2)
	band_counts = {b["label"]: summary["band_counts"].get(b["label"], 0) for b in bands}
	late_fraction = sum(flt(b["fraction"]) * band_counts[b["label"]] for b in bands)

	return {
		"ot_working_days": ot_days,
		"per_day_rate": per_day,
		"ot_rate": hourly,
		"ot_hours": ot_hours,
		# Duration fields store SECONDS. rapl_overtime_processing_entry's
		# ot_hours_hhmm is the editable one and ot_hours (Float) is derived
		# from it, so the console must offer the same pair.
		"ot_hours_hhmm": int(round(ot_hours * 3600)),
		"ot_amount": round(ot_hours * hourly),
		"band_counts": band_counts,
		"late_amount": round(late_fraction * per_day),
		"ot_already_processed": additional_salary_already_exists(
			emp.name, settings.overtime_salary_component, end_date
		),
		"late_already_processed": additional_salary_already_exists(
			emp.name, settings.late_mark_salary_component, end_date
		),
	}


# ---------------------------------------------------------------- edit


def _recompute(record, settings):
	holiday_list = get_holiday_list_for_employee(record["employee"])
	holiday_dates = set(
		get_all_holiday_dates(holiday_list, record["attendance_date"], record["attendance_date"]) or []
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
	if isinstance(changes, str):
		changes = frappe.parse_json(changes)
	if not changes:
		return {"applied": [], "failed": []}
	if len(changes) > MAX_ROWS_PER_APPLY:
		frappe.throw(
			f"Too many rows in one go ({len(changes)}). Apply at most "
			f"{MAX_ROWS_PER_APPLY} at a time."
		)

	settings = get_automation_settings()
	applied, failed = [], []
	confirmed = frappe.parse_json(confirm_processed) if isinstance(confirm_processed, str) else confirm_processed

	for change in changes:
		name = change.get("name")
		if not name:
			failed.append({"name": None, "error": "No attendance record on that row"})
			continue

		current = frappe.db.get_value(
			"Attendance", name,
			["name", "employee", "attendance_date", "docstatus", "status", "leave_type",
			 "leave_application", "half_day_status", "in_time", "out_time",
			 "working_hours", "shift", "modified", "custom_overtime_hours",
			 "custom_late_mark_band", "custom_overtime_manual", "custom_late_mark_manual"],
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
			try:
				doc = frappe.get_doc("Attendance", name)
				for field in EDITABLE_FIELDS:
					if field in change:
						value = change[field] or None
						if field in ("in_time", "out_time"):
							value = _combine(doc.attendance_date, value)
						setattr(doc, field, value)
				doc.working_hours = 0
				doc.save()
				applied.append({
					"name": name, "employee": doc.employee,
					"attendance_date": str(doc.attendance_date), "via": "orm",
					"values": {"status": doc.status,
							   "working_hours": flt(doc.working_hours, 2)},
				})
			except Exception as e:
				failed.append({"name": name,
							   "error": frappe.utils.strip_html(str(e))[:500]})
			continue

		record = dict(current)
		for field in EDITABLE_FIELDS:
			if field in change:
				value = change[field] or None
				if field in ("in_time", "out_time"):
					value = _combine(current.attendance_date, value)
				record[field] = value

		if record["in_time"] and record["out_time"]:
			if get_datetime(record["out_time"]) <= get_datetime(record["in_time"]):
				failed.append({"name": name, "error": "Check-out is not after check-in"})
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
		}
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


def _processed_components(employee, attendance_date, settings):
	"""Which components are already submitted for the month containing this date."""
	from frappe.utils import get_last_day

	period_end = get_last_day(attendance_date)
	found = []
	for label, component in (
		("Overtime", settings.overtime_salary_component),
		("Late Mark", settings.late_mark_salary_component),
	):
		if component and additional_salary_already_exists(employee, component, period_end):
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
				  "custom_overtime_manual", "custom_late_mark_manual"):
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
def recalculate(names):
	"""Re-apply the rules without changing punches -- clears drift on records
	corrected outside the app."""
	_require_hr("write")
	if isinstance(names, str):
		names = frappe.parse_json(names)
	return apply_edits([{"name": n} for n in (names or [])])


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
	if isinstance(rows, str):
		rows = frappe.parse_json(rows)

	created, failed = [], []
	for index, row in enumerate(rows or []):
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
			doc.attendance_date = getdate(row["attendance_date"])
			doc.status = row.get("status") or "Present"
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


def _apply_overrides(doc, kind, overrides, bands):
	"""Write the Console's edited summary values onto the draft's entry rows.

	Values the user typed win over what get_employees() computed. Rows the user
	did not touch are left exactly as fetched.
	"""
	if not overrides:
		return []

	touched = []
	by_employee = {row.employee: row for row in doc.entries}

	for employee, values in overrides.items():
		row = by_employee.get(employee)
		if not row:
			continue

		if kind == "overtime":
			if values.get("ot_hours_hhmm") is not None:
				row.ot_hours_hhmm = int(values["ot_hours_hhmm"])
				row.ot_hours = flt(row.ot_hours_hhmm / 3600.0, 2)
			if values.get("ot_rate") is not None:
				row.ot_rate = flt(values["ot_rate"])
			row.amount = (
				flt(values["amount"]) if values.get("amount") is not None
				else round(flt(row.ot_hours) * flt(row.ot_rate))
			)
		else:
			for index, band in enumerate(bands, start=1):
				key = f"band_{index}_count"
				if key in values:
					setattr(row, key, int(values[key] or 0))
			if values.get("per_day_rate") is not None:
				row.per_day_rate = flt(values["per_day_rate"])
			# The Console keys the late-mark total as "late_amount", not
			# "amount": one employee has BOTH an overtime amount and a late-mark
			# amount, and the override dict is keyed per employee, so a single
			# "amount" key would collide. "amount" is still accepted as a
			# fallback for any older payload.
			late_amount = values.get("late_amount")
			if late_amount is None:
				late_amount = values.get("amount")
			if late_amount is not None:
				row.amount = flt(late_amount)
			else:
				fraction = sum(
					flt(b["fraction"]) * int(getattr(row, f"band_{i}_count", 0) or 0)
					for i, b in enumerate(bands, start=1)
				)
				row.amount = round(fraction * flt(row.per_day_rate))
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

	if isinstance(employees, str):
		employees = frappe.parse_json(employees)
	if isinstance(overrides, str):
		overrides = frappe.parse_json(overrides)
	overrides = overrides or {}

	# An employee with Employee.custom_ot = 0 is skipped by get_employees()'s
	# default mode, so a manually entered OT figure for them would be dropped.
	# Naming them explicitly switches get_employees() to its first mode --
	# "exactly those employees", which bypasses the custom_ot filter by design
	# -- so a manual override always survives.
	if overrides:
		employees = sorted(set(employees or []) | set(overrides.keys()))

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
	result = get_employees(doc.name, all_employees=False, employees=employees or None)

	doc.reload()
	overridden = _apply_overrides(doc, kind, overrides, get_band_definitions(get_automation_settings()))
	doc.reload()

	return {
		"overridden": overridden,
		"doctype": doctype,
		"name": doc.name,
		"reused": reused,
		"rows_before": rows_before,
		"rows_after": len(doc.entries),
		"filtered": bool(employees),
		"result": result,
		"route": f"/app/{frappe.scrub(doctype).replace('_', '-')}/{doc.name}",
	}
