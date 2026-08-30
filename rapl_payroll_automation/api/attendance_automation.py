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
from frappe.utils import flt, get_datetime, time_diff_in_hours

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
	# Working hours first: OT's holiday branch reads it, and guard 3 (holiday)
	# returns early, so this must happen before any of the guards below.
	_fill_working_hours(doc)

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

	band_label, past_all_bands = match_late_band(doc.in_time, settings)
	doc.custom_late_mark_band = band_label
	is_half_day = past_all_bands

	# --- Early exit check -- stacks additively with any late-arrival band above ---
	if doc.out_time:
		doc.early_exit = 0
		if is_early_exit(doc.out_time, doc.attendance_date, settings):
			is_half_day = True
			doc.early_exit = 1

	if is_half_day:
		doc.status = "Half Day"
		doc.half_day_status = "Absent"
		doc.leave_type = settings.half_day_leave_type

	# Overtime last: compute_day_ot() reads doc.status, which the Half Day
	# assignment above may have just changed. A Half Day earns no OT.
	_set_overtime_fields(doc, settings)


def match_late_band(in_time, settings):
	"""Return (band_label_or_None, arrived_later_than_every_band).

	Extracted so the Attendance Console can compute the SAME expected band for
	drift detection without duplicating the rule. A second implementation is
	exactly how the OT calculation ended up disagreeing with itself, so this
	must stay the only copy.

	Semantics preserved exactly from the original inline block:
	  - bands sorted by from_time; first band whose [from_time, to_time] window
	    contains the check-in (INCLUSIVE both ends) wins
	  - later than the last band's to_time -> no band, Half Day instead
	  - earlier than the first band's from_time -> grace, no band, no Half Day
	"""
	if not in_time:
		return None, False

	check_in_seconds = time_to_seconds(get_datetime(in_time).time())
	bands = sorted(settings.late_mark_bands, key=lambda r: time_to_seconds(r.from_time))

	for band in bands:
		if time_to_seconds(band.from_time) <= check_in_seconds <= time_to_seconds(band.to_time):
			return band.label, False

	if bands and check_in_seconds > time_to_seconds(bands[-1].to_time):
		return None, True

	return None, False


def is_early_exit(out_time, attendance_date, settings):
	"""Return True when out_time is before settings.early_exit_cutoff.

	Shared with the Console for the same reason as match_late_band(). Note this
	is the app's own 17:00 penalty cutoff, NOT Shift Type's native early_exit
	marking (which is disabled -- enable_early_exit_marking = 0).
	"""
	if not out_time:
		return False
	cutoff = get_datetime_combine(attendance_date, settings.early_exit_cutoff)
	return get_datetime(out_time) < cutoff


def _fill_working_hours(doc):
	"""Populate working_hours when it is empty.

	ERPNext only ever calculates working_hours inside auto-attendance:
	shift_type.process_auto_attendance() -> get_attendance() ->
	employee_checkin.calculate_working_hours(), which derives it from Employee
	Checkin logs. attendance.py itself contains no working_hours logic at all,
	so an Attendance created by hand, by import, or without checkin logs keeps
	working_hours = 0 no matter how many times it is saved.

	Only fills when the field is empty. Never overwrites a value auto-attendance
	produced -- under "Every Valid Check-in and Check-out" that value legitimately
	excludes mid-day gaps, and clobbering it with a raw span would be wrong.

	Matches ERPNext's own formula for the shift's current mode, "First Check-in
	and Last Check-out": frappe.utils.time_diff_in_hours(out, in), i.e. the raw
	span with no break deduction, rounded to 6 places.
	"""
	if doc.working_hours:
		return
	if not doc.in_time or not doc.out_time:
		return

	in_time = get_datetime(doc.in_time)
	out_time = get_datetime(doc.out_time)
	if out_time <= in_time:
		# Bad punch (e.g. an in_time typed as 02:00). Leave it at 0 rather than
		# invent a number -- a wrong working_hours would feed holiday OT.
		return

	doc.working_hours = time_diff_in_hours(out_time, in_time)


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
