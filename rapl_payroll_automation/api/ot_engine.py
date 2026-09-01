# Copyright (c) 2026, RAPL and contributors
#
# Single source of truth for per-day Overtime hours.
#
# WHY THIS EXISTS
# ---------------
# Before this module, OT was computed in two independent places:
#
#   1. Server Script "OT Calculation - Attendance Records" (script_type =
#      Scheduler Event, ran nightly over YESTERDAY's Attendance only). It wrote
#      Attendance.custom_overtime_hours and custom_overtime via
#      frappe.db.set_value. The monthly attendance export reads that field, so
#      this is what staff SAW.
#
#   2. rapl_overtime_processing._compute_employee_overtime(), which recomputed
#      OT from scratch when you clicked Get Employees, ignoring the stored
#      field entirely. This is what staff were PAID.
#
# The two disagreed in ways that mattered:
#
#   - The Server Script had NO docstatus filter and NO status filter, so it
#     wrote OT onto draft and cancelled Attendance, and credited OT on Half Day
#     and Absent records. Processing filters docstatus=1 AND status='Present'.
#   - The Server Script only ever looked at yesterday, so any record corrected
#     later (e.g. by direct SQL) kept OT computed from the ORIGINAL punches and
#     was never recalculated.
#
# Both now call compute_day_ot(). The field is written on Attendance.validate
# and read back by Processing, so the report and the payment come from one
# calculation.
#
# BEHAVIOUR PRESERVED EXACTLY from _compute_employee_overtime():
#   - Holiday: full working_hours, falling back to raw in->out span when
#     working_hours is zero. (Note: the old comment in rapl_overtime_processing
#     claimed "raw span, no break deduction". That comment was wrong -- the code
#     preferred working_hours, and that behaviour is carried over unchanged.)
#   - Regular day: minutes past shift end; zero unless strictly greater than
#     settings.ot_minimum_minutes.
#   - Never negative.
#
# DELIBERATE RULES (see merge discussion):
#   - Half Day earns NO overtime. Processing filtered status='Present', so a
#     Half Day never contributed OT. Writing 0 here preserves that exactly.
#     Changing it would be a pay increase and must be a separate decision.
#   - Per-record doc.shift is used when set, falling back to
#     settings.reference_shift_type. All 4,775 submitted Attendance records
#     currently carry shift='Regular' and it is the only Shift Type on the
#     site, so this is defensive only and changes nothing today.
#   - Shifts crossing midnight are REFUSED, not guessed. The old Server Script
#     rolled the end forward a day; the app had no guard at all and would have
#     produced ~24h of phantom OT. RAPL has no night shift, so rather than ship
#     untested rollover semantics this raises so the caller can report it.

import frappe
from frappe.utils import flt, get_datetime

from rapl_payroll_automation.api.payroll_automation_utils import (
	get_all_holiday_dates,
	get_datetime_combine,
)
from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee


class NightShiftNotSupported(Exception):
	"""Raised when a Shift Type's end_time is at or before its start_time."""


def resolve_shift(shift_name, settings):
	"""Per-record shift when set, else the configured reference shift."""
	name = shift_name or settings.reference_shift_type
	if not name:
		return None
	return frappe.get_cached_doc("Shift Type", name)


def is_ot_eligible(employee):
	"""Employee.custom_ot -- the OT-eligibility flag on the Employee master.

	rapl_overtime_processing.get_employees() already filters its DEFAULT mode
	on custom_ot = 1, so payroll has always been correct. But nothing gated the
	Attendance field or the statement, so an ineligible employee accumulated
	custom_overtime_hours they would never be paid for, and saw those hours on
	their monthly statement. Gating here fixes all three surfaces at once.
	"""
	return bool(frappe.db.get_value("Employee", employee, "custom_ot"))


def compute_day_ot(
	in_time, out_time, working_hours, attendance_date, status, shift, settings,
	holiday_dates, ot_eligible=True,
):
	"""Overtime hours for ONE day. Returns a float, never negative.

	holiday_dates: a set/list of dates already resolved by the caller, so a
	bulk loop resolves the Holiday List once rather than once per day.

	ot_eligible: pass Employee.custom_ot. Defaults True so that
	rapl_overtime_processing's two DELIBERATE override modes -- an explicit
	employee list, and all_employees=True -- keep working exactly as documented
	("every active Employee, regardless of attendance or custom_ot"). Its
	default mode filters on custom_ot before it ever gets here, so passing True
	from there is correct in all three modes.
	"""
	if not ot_eligible:
		return 0.0

	if not in_time or not out_time:
		return 0.0

	# Half Day / Absent / On Leave earn no OT -- mirrors the status='Present'
	# filter that get_attendance_for_employee() applies on the Processing side.
	if status and status != "Present":
		return 0.0

	if not shift:
		return 0.0

	in_time = get_datetime(in_time)
	out_time = get_datetime(out_time)

	if attendance_date in holiday_dates:
		# Voluntary attendance on a holiday/weekly off -- the whole day counts.
		ot_hours = (
			flt(working_hours)
			if working_hours
			else (out_time - in_time).total_seconds() / 3600
		)
		return max(ot_hours, 0.0)

	if shift.end_time is not None and shift.start_time is not None:
		if shift.end_time <= shift.start_time:
			raise NightShiftNotSupported(
				f"Shift '{shift.name}' ends at or before it starts (crosses midnight); OT not supported"
			)

	shift_end_dt = get_datetime_combine(attendance_date, shift.end_time)
	minutes_over = (out_time - shift_end_dt).total_seconds() / 60

	if minutes_over <= flt(settings.ot_minimum_minutes):
		return 0.0

	return max(minutes_over / 60, 0.0)


def compute_ot_for_attendance_doc(doc, settings):
	"""Convenience wrapper for the Attendance validate hook (single document).

	Honours Employee.custom_ot: an employee not flagged for overtime never
	accumulates custom_overtime_hours, so the field, the monthly statement and
	what payroll actually pays cannot disagree.
	"""
	holiday_list = get_holiday_list_for_employee(doc.employee)
	holiday_dates = get_all_holiday_dates(holiday_list, doc.attendance_date, doc.attendance_date)

	shift = resolve_shift(doc.get("shift"), settings)

	return compute_day_ot(
		in_time=doc.in_time,
		out_time=doc.out_time,
		working_hours=doc.working_hours,
		attendance_date=doc.attendance_date,
		status=doc.status,
		shift=shift,
		settings=settings,
		holiday_dates=holiday_dates,
		ot_eligible=is_ot_eligible(doc.employee),
	)
