# Copyright (c) 2026, RAPL and contributors
#
# Attendance `validate` hook: Late Mark bands + Half Day + Early Exit.
#
# Registered in hooks.py as:
#   doc_events = {
#       "Attendance": {
#           "validate": "rapl_payroll_automation.api.attendance_automation.apply_attendance_deduction_logic"
#       }
#   }
#
# Fires on every Attendance save, including the auto-attendance flow
# (attendance.save() then attendance.submit() back-to-back -- verified in
# employee_checkin.py's create_or_update_attendance/mark_attendance_and_link_log).
#
# Rules implemented (all confirmed with the user across extensive design discussion):
#   Bands are now fully configurable via RAPL Payroll Automation Settings'
#   "Late Mark Bands" table (label, from_time, to_time, fraction) -- no
#   longer hardcoded. A check-in before the earliest band's From Time is
#   grace (no deduction). A check-in after every band's latest To Time
#   triggers native Half Day. A check-in within a band's [from_time, to_time]
#   sets custom_late_mark_band to that band's Label (replacing the old
#   custom_late_deduction_fraction float -- see rewrite note below).
#   checkout before 17:00  ALSO Half Day (stacks additively with any late-arrival band)
#
# 2026-08 ADDITIONS (see merge discussion):
#   - early_exit is now set by THIS hook whenever out_time is before
#     settings.early_exit_cutoff, i.e. exactly when the Half Day penalty
#     applies. Shift Type 'Regular' has enable_early_exit_marking = 0, so
#     native auto-attendance never set this flag; the only records carrying it
#     were ones edited by hand or by direct SQL. The monthly attendance export
#     shows it as the "EO" marker, so it now means "this day cost half a day's
#     pay" rather than being scattered and rule-less. Deliberately NOT aligned
#     to shift end (18:00) -- that would flag an hour of departures that carry
#     no consequence.
#   - custom_overtime_hours / custom_overtime are now written here, via
#     ot_engine.compute_day_ot(). Previously the Server Script
#     "OT Calculation - Attendance Records" owned that field on a nightly
#     schedule with different rules; it is retired as part of this change.
#     See ot_engine.py for the full rationale.
#
# Native Half Day mechanism verified: get_half_absent_days() + payment_days
# reduction via Fraction of Daily Salary for Half Day (0.500) already delivers
# exactly a half-day pay cut through existing `Depends on Payment Days`
# proration -- no custom deduction component needed for the Half Day
# triggers, only for the fractional bands.
#
# REWRITE NOTE: custom_late_deduction_fraction (a bare float) has been
# replaced by custom_late_mark_band (Data -- stores the matched band's
# Label). This is necessary to support per-band COUNTING in RAPL Late Mark
# Processing (e.g. "2 occurrences in the 9:46-10:00 band") rather than just
# summing a single fraction value -- a bare number can't identify WHICH band
# a day belonged to. The old field is left in place, untouched, on any
# historical records; it is simply no longer written to or read from.
#
# leave_type is MANDATORY whenever status is Half Day/On Leave (verified:
# mandatory_depends_on on the Attendance doctype). We use a dedicated Leave
# Type (settings.half_day_leave_type) that is explicitly validated (at the
# Settings doctype level) to have BOTH is_lwp and is_ppl unticked, to avoid
# a double-deduction against payment_days (see payroll_automation_utils.py
# docstring and the Settings doctype's validate_half_day_leave_type()).

import frappe
from frappe.utils import flt, get_datetime

from rapl_payroll_automation.api.payroll_automation_utils import (
	get_all_holiday_dates,
	get_automation_settings,
	get_datetime_combine,
	time_to_seconds,
)
from rapl_payroll_automation.api.ot_engine import (
	NightShiftNotSupported,
	compute_ot_for_attendance_doc,
)
from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee


def apply_attendance_deduction_logic(doc, method):
	# Reset OT every run. Guards 1 and 2 below return early, and without this a
	# record that previously earned OT would keep a stale value if it later
	# became a leave day or had its in_time cleared (e.g. on amend).
	# _set_overtime_fields() overwrites these whenever a real value is computed.
	doc.custom_overtime_hours = 0
	doc.custom_overtime = 0

	# --- Guard 1: leave-application-driven day (incl. half-day paid leave) ---
	# check_leave_record() (native, runs earlier in Attendance's own validate())
	# will have already set doc.leave_type from a real, approved Leave
	# Application if one exists for this date. If it did, don't let our
	# attendance-time logic override that -- a genuine leave decision takes
	# priority over our automated lateness/early-exit rules.
	if doc.leave_type:
		return

	# --- Guard 2: no check-in data at all (Absent, or genuinely not yet arrived) ---
	if not doc.in_time:
		return

	# doc.in_time/out_time can arrive as plain strings (not yet cast to
	# datetime) on a fresh, client-submitted document at this point in the
	# save lifecycle -- confirmed via a real TypeError in production
	# ('str' - 'datetime.datetime'). get_datetime() is idempotent: safe to
	# call whether the value is already a datetime or still a string.
	doc.in_time = get_datetime(doc.in_time)
	if doc.out_time:
		doc.out_time = get_datetime(doc.out_time)

	settings = get_automation_settings()

	# --- Guard 3: Sunday or any holiday, per the configured Holiday List ---
	# Voluntary-attendance days (overtime only) -- late-mark/half-day/early-exit
	# rules don't apply here at all. Driven entirely by the Holiday List, not a
	# hardcoded weekday check, so this respects whatever "Weekly Off" is
	# actually configured (confirmed = Sunday for RAPL's "Public Holidays 2026").
	holiday_list = get_holiday_list_for_employee(doc.employee)
	if get_all_holiday_dates(holiday_list, doc.attendance_date, doc.attendance_date):
		# Late/half-day/early-exit rules don't apply, but voluntary attendance on
		# a holiday IS the main source of overtime -- so OT is still computed.
		_set_overtime_fields(doc, settings)
		return

	# Reset our own field every run so re-validation (e.g. amend) recomputes cleanly
	doc.custom_late_mark_band = None
	is_half_day = False

	check_in_seconds = time_to_seconds(doc.in_time.time())
	bands = sorted(settings.late_mark_bands, key=lambda r: time_to_seconds(r.from_time))

	matched_band = None
	for band in bands:
		from_s = time_to_seconds(band.from_time)
		to_s = time_to_seconds(band.to_time)
		if from_s <= check_in_seconds <= to_s:
			matched_band = band
			break

	if matched_band:
		doc.custom_late_mark_band = matched_band.label
	elif bands and check_in_seconds > time_to_seconds(bands[-1].to_time):
		is_half_day = True  # later than every defined band -- native Half Day
	# else: earlier than the first band's From Time -- grace, no deduction

	# --- Early exit check -- stacks additively with any late-arrival band above ---
	if doc.out_time:
		early_exit_cutoff = get_datetime_combine(doc.attendance_date, settings.early_exit_cutoff)
		doc.early_exit = 0
		if doc.out_time < early_exit_cutoff:
			is_half_day = True
			doc.early_exit = 1

	if is_half_day:
		doc.status = "Half Day"
		doc.half_day_status = "Absent"
		doc.leave_type = settings.half_day_leave_type

	# Overtime last: compute_day_ot() reads doc.status, which the Half Day
	# assignment above may have just changed. A Half Day earns no OT.
	_set_overtime_fields(doc, settings)


def _set_overtime_fields(doc, settings):
	"""Write custom_overtime_hours / custom_overtime. Never raises -- a shift
	misconfiguration must not block an Attendance save."""
	try:
		ot_hours = compute_ot_for_attendance_doc(doc, settings)
	except NightShiftNotSupported as e:
		frappe.log_error(str(e), "RAPL OT: night shift not supported")
		return
	except Exception:
		frappe.log_error(frappe.get_traceback(), "RAPL OT: compute failed")
		return

	doc.custom_overtime_hours = round(flt(ot_hours), 2)
	doc.custom_overtime = 1 if doc.custom_overtime_hours > 0 else 0
