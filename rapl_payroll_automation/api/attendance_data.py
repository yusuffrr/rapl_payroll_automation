# Copyright (c) 2026, RAPL and contributors
#
# Shared data layer for BOTH the Employee Attendance page (read-only) and the
# Attendance Console (HR editing). One query, one set of rules, two surfaces.
#
# WHY CALENDAR-DRIVEN
# -------------------
# Rows are generated from the calendar and Attendance is LEFT JOINed on, so a
# working day with NO Attendance document still produces a row. That is the
# only way to surface "nobody marked attendance for Ramesh on the 17th", which
# a record-driven query cannot see. The previous hrms_custom export already
# used a recursive CTE for this; the logic is inherited, not invented.
#
# WHY DRIFT DETECTION
# -------------------
# For every day this recomputes what the rules SAY the band and OT should be,
# and compares against what is STORED on the record. They diverge whenever a
# record was changed without passing through Attendance.validate() -- most
# commonly a direct SQL correction. Before this, that drift was invisible until
# payroll. Now it is an amber flag.
#
# The recomputation calls match_late_band(), is_early_exit() and
# compute_day_ot() -- the SAME functions the live validate hook uses. It must
# never grow its own copy of a rule; two implementations of the OT calculation
# is precisely the bug this app spent a month unwinding.

import frappe
from frappe.utils import flt, get_datetime, getdate, time_diff_in_hours

from rapl_payroll_automation.api.attendance_automation import (
	is_early_exit,
	match_late_band,
)
from rapl_payroll_automation.api.ot_engine import (
	NightShiftNotSupported,
	compute_day_ot,
	resolve_shift,
)
from rapl_payroll_automation.api.payroll_automation_utils import (
	get_all_holiday_dates,
	get_automation_settings,
)
from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee

# Flag codes. "red" = something is wrong and payroll will be affected.
# "amber" = valid but worth a look. "info" = context, not a problem.
FLAG_MISSING_PUNCH = "missing_punch"
FLAG_NO_RECORD = "no_record"
FLAG_CANCELLED_ONLY = "cancelled_only"
FLAG_OT_DRIFT = "ot_drift"
FLAG_LATE_DRIFT = "late_drift"
FLAG_OFF_DAY_WORKED = "off_day_worked"
FLAG_ON_LEAVE = "on_leave"

FLAG_LEVELS = {
	FLAG_MISSING_PUNCH: "red",
	FLAG_NO_RECORD: "red",
	FLAG_CANCELLED_ONLY: "red",
	FLAG_OT_DRIFT: "amber",
	FLAG_LATE_DRIFT: "amber",
	FLAG_OFF_DAY_WORKED: "amber",
	FLAG_ON_LEAVE: "info",
}


def _daterange(start_date, end_date):
	from datetime import timedelta

	current = getdate(start_date)
	last = getdate(end_date)
	while current <= last:
		yield current
		current += timedelta(days=1)


def _fetch_attendance(employee, start_date, end_date):
	"""All Attendance for the period INCLUDING cancelled (docstatus 2).

	Cancelled records matter: validate_duplicate_record() in attendance.py
	filters on docstatus < 2, so a cancelled record does NOT block creating a
	new one for that date. A day whose only record is cancelled is therefore
	genuinely missing as far as payroll is concerned, and must be flagged as
	such rather than looking occupied.
	"""
	rows = frappe.get_all(
		"Attendance",
		filters={
			"employee": employee,
			"attendance_date": ["between", [start_date, end_date]],
			"docstatus": ["<", 3],
		},
		fields=[
			"name", "docstatus", "attendance_date", "status", "half_day_status",
			"in_time", "out_time", "working_hours", "shift", "late_entry",
			"early_exit", "leave_type", "leave_application",
			"custom_late_mark_band", "custom_overtime_hours", "custom_overtime",
			"modified",
		],
		order_by="attendance_date, docstatus",
	)

	by_date = {}
	for row in rows:
		by_date.setdefault(getdate(row.attendance_date), []).append(row)
	return by_date


def _pick_active(records):
	"""Submitted beats draft beats cancelled. Returns (record, cancelled_only)."""
	if not records:
		return None, False
	submitted = [r for r in records if r.docstatus == 1]
	if submitted:
		return submitted[0], False
	draft = [r for r in records if r.docstatus == 0]
	if draft:
		return draft[0], False
	return records[0], True


def _employee_context(employee):
	emp = frappe.db.get_value(
		"Employee",
		employee,
		["name", "employee_name", "grade", "status", "date_of_joining",
		 "relieving_date", "company", "holiday_list"],
		as_dict=True,
	)
	if not emp:
		frappe.throw(f"Employee {employee} not found")
	return emp


def build_month_rows(employee, start_date, end_date, settings=None):
	"""One row per calendar day. The single source both pages read.

	Each row carries what is STORED and what the rules COMPUTE, so the caller
	can show either and the difference between them.
	"""
	settings = settings or get_automation_settings()
	emp = _employee_context(employee)

	holiday_list = get_holiday_list_for_employee(employee)
	holiday_dates = set(get_all_holiday_dates(holiday_list, start_date, end_date) or [])

	holiday_names = {}
	if holiday_list:
		for h in frappe.get_all(
			"Holiday",
			filters={"parent": holiday_list, "holiday_date": ["between", [start_date, end_date]]},
			fields=["holiday_date", "description", "weekly_off"],
		):
			holiday_names[getdate(h.holiday_date)] = {
				"description": h.description,
				"weekly_off": bool(h.weekly_off),
			}

	by_date = _fetch_attendance(employee, start_date, end_date)

	joining = getdate(emp.date_of_joining) if emp.date_of_joining else None
	relieving = getdate(emp.relieving_date) if emp.relieving_date else None

	rows = []
	for day in _daterange(start_date, end_date):
		# Outside employment: no row is expected, so never flag it as missing.
		in_service = (not joining or day >= joining) and (not relieving or day <= relieving)

		holiday = holiday_names.get(day)
		is_holiday = day in holiday_dates

		records = by_date.get(day, [])
		record, cancelled_only = _pick_active(records)

		row = {
			"date": str(day),
			"day_label": day.strftime("%a"),
			"in_service": in_service,
			"is_holiday": is_holiday,
			"is_weekly_off": bool(holiday and holiday["weekly_off"]),
			"holiday_description": (holiday or {}).get("description"),
			"attendance": None,
			"flags": [],
		}

		if record and not cancelled_only:
			row["attendance"] = {
				"name": record.name,
				"docstatus": record.docstatus,
				"status": record.status,
				"half_day_status": record.half_day_status,
				"in_time": str(record.in_time) if record.in_time else None,
				"out_time": str(record.out_time) if record.out_time else None,
				"working_hours": flt(record.working_hours, 2),
				"shift": record.shift,
				"late_entry": record.late_entry,
				"early_exit": record.early_exit,
				"leave_type": record.leave_type,
				"leave_application": record.leave_application,
				"late_mark_band": record.custom_late_mark_band,
				"overtime_hours": flt(record.custom_overtime_hours, 2),
				"modified": str(record.modified),
			}
			_add_record_flags(row, record, day, is_holiday, holiday_dates, settings, emp)
		elif in_service:
			if cancelled_only:
				_flag(row, FLAG_CANCELLED_ONLY, "Only a cancelled record exists for this day")
			elif not is_holiday:
				_flag(row, FLAG_NO_RECORD, "No attendance marked on a working day")

		rows.append(row)

	return {"employee": emp, "rows": rows}


def _flag(row, code, message, extra=None):
	entry = {"code": code, "level": FLAG_LEVELS.get(code, "info"), "message": message}
	if extra:
		entry.update(extra)
	row["flags"].append(entry)


def _add_record_flags(row, record, day, is_holiday, holiday_dates, settings, emp):
	att = row["attendance"]

	if record.leave_type:
		_flag(row, FLAG_ON_LEAVE, f"On leave: {record.leave_type}",
			  {"leave_application": record.leave_application})

	if record.status == "Present" and (not record.in_time or not record.out_time):
		missing = "check-out" if record.in_time else "check-in"
		_flag(row, FLAG_MISSING_PUNCH, f"Missing {missing}")

	if is_holiday:
		_flag(row, FLAG_OFF_DAY_WORKED, "Attendance marked on a holiday or weekly off")

	# --- drift: what the rules say now vs what is stored ---
	expected_working_hours = att["working_hours"]
	if not expected_working_hours and record.in_time and record.out_time:
		in_dt, out_dt = get_datetime(record.in_time), get_datetime(record.out_time)
		if out_dt > in_dt:
			expected_working_hours = flt(time_diff_in_hours(out_dt, in_dt), 2)

	expected_band = None
	if not record.leave_type and not is_holiday and record.in_time:
		expected_band, _past_all = match_late_band(record.in_time, settings)

	expected_ot = None
	if not record.leave_type:
		try:
			expected_ot = flt(
				compute_day_ot(
					in_time=record.in_time,
					out_time=record.out_time,
					working_hours=expected_working_hours,
					attendance_date=day,
					status=record.status,
					shift=resolve_shift(record.shift, settings),
					settings=settings,
					holiday_dates=holiday_dates,
				),
				2,
			)
		except NightShiftNotSupported:
			expected_ot = None

	att["expected_working_hours"] = expected_working_hours
	att["expected_late_mark_band"] = expected_band
	att["expected_overtime_hours"] = expected_ot
	att["expected_early_exit"] = (
		1 if (not is_holiday and is_early_exit(record.out_time, day, settings)) else 0
	)

	if expected_ot is not None and abs(flt(expected_ot) - att["overtime_hours"]) > 0.01:
		_flag(row, FLAG_OT_DRIFT,
			  f"Overtime shows {att['overtime_hours']}h, rules give {expected_ot}h",
			  {"stored": att["overtime_hours"], "expected": expected_ot})

	if (expected_band or None) != (att["late_mark_band"] or None):
		_flag(row, FLAG_LATE_DRIFT,
			  f"Late mark shows {att['late_mark_band'] or 'none'}, "
			  f"rules give {expected_band or 'none'}",
			  {"stored": att["late_mark_band"], "expected": expected_band})


def summarise(rows, settings=None):
	"""Period totals for one employee, from the day rows."""
	settings = settings or get_automation_settings()

	summary = {
		"present": 0, "half_day": 0, "absent": 0, "on_leave": 0,
		"holiday": 0, "no_record": 0,
		"overtime_hours": 0.0, "band_counts": {},
		"red_flags": 0, "amber_flags": 0,
	}

	for row in rows:
		for f in row["flags"]:
			if f["level"] == "red":
				summary["red_flags"] += 1
			elif f["level"] == "amber":
				summary["amber_flags"] += 1

		att = row["attendance"]
		if not att:
			if row["is_holiday"]:
				summary["holiday"] += 1
			elif row["in_service"]:
				summary["no_record"] += 1
			continue

		status_key = {
			"Present": "present", "Half Day": "half_day",
			"Absent": "absent", "On Leave": "on_leave",
		}.get(att["status"])
		if status_key:
			summary[status_key] += 1

		summary["overtime_hours"] += flt(att["overtime_hours"])
		band = att["late_mark_band"]
		if band:
			summary["band_counts"][band] = summary["band_counts"].get(band, 0) + 1

	summary["overtime_hours"] = flt(summary["overtime_hours"], 2)
	return summary


def get_band_definitions(settings=None):
	"""Band labels and fractions, in order. Drives the console's band columns
	and the statement footer -- add a band in Settings and a column appears."""
	settings = settings or get_automation_settings()
	return [
		{"label": b.label, "fraction": flt(b.fraction),
		 "from_time": str(b.from_time), "to_time": str(b.to_time)}
		for b in sorted(settings.late_mark_bands, key=lambda r: str(r.from_time))
	]


def get_active_employees(employees=None):
	filters = {"status": "Active"}
	if employees:
		filters["name"] = ["in", employees]
	return frappe.get_all(
		"Employee",
		filters=filters,
		fields=["name", "employee_name", "grade", "date_of_joining", "relieving_date"],
		order_by="name",
	)
