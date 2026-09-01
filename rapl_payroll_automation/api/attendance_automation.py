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
from frappe.utils import flt, get_datetime, getdate, time_diff_in_hours

from rapl_payroll_automation.api.payroll_automation_utils import (
	get_all_holiday_dates,
	get_automation_settings,
	get_datetime_combine,
	time_to_seconds,
)
from rapl_payroll_automation.api.ot_engine import (
	NightShiftNotSupported,
	compute_day_ot,
	is_ot_eligible,
	resolve_shift,
)
from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee


def derive_attendance_fields(
	employee, attendance_date, in_time, out_time, working_hours, status,
	leave_type, shift, settings, holiday_dates=None, ot_eligible=None,
	leave_application=None, half_day_status=None,
):
	"""Apply every attendance rule to a set of VALUES and return the derived
	fields. No document, no writes.

	Extracted so the Attendance Console can apply the identical rules to
	records it edits by SQL. Attendance has no allow_on_submit fields (verified
	against hrms attendance.json), so a submitted record cannot be edited
	through the ORM and validate() cannot be made to fire -- the Console writes
	directly and calls this to recompute. Without a shared function that path
	would need its own copy of the rules, which is exactly how the OT
	calculation ended up with two disagreeing implementations.

	Returns a dict of the fields to write. Callers assign them; this never does.
	Guard ORDER is significant and preserved exactly from the original hook.
	"""
	derived = {
		# "rules_applied" False means an early guard fired (leave day, no
		# check-in, or holiday). The original hook returned at those points
		# WITHOUT resetting custom_late_mark_band / early_exit / status, so
		# callers must assign only working_hours and the OT fields in that
		# case. Getting this wrong would silently clear late marks on leave
		# days.
		"rules_applied": False,
		"working_hours": working_hours,
		"custom_overtime_hours": 0,
		"custom_overtime": 0,
		"custom_late_mark_band": None,
		"early_exit": 0,
		"status": status,
		"half_day_status": half_day_status,
		"leave_type": leave_type,
		"cleared_half_day": False,
	}

	# --- working hours: only fill when empty (never clobber auto-attendance) ---
	if not working_hours and in_time and out_time:
		in_dt, out_dt = get_datetime(in_time), get_datetime(out_time)
		if out_dt > in_dt:
			derived["working_hours"] = time_diff_in_hours(out_dt, in_dt)

	if ot_eligible is None:
		ot_eligible = is_ot_eligible(employee)

	def _ot(current_status):
		if not in_time or not out_time:
			return 0.0
		try:
			return flt(
				compute_day_ot(
					in_time=in_time,
					out_time=out_time,
					working_hours=derived["working_hours"],
					attendance_date=attendance_date,
					status=current_status,
					shift=resolve_shift(shift, settings),
					settings=settings,
					holiday_dates=holiday_dates or set(),
					ot_eligible=ot_eligible,
				),
				2,
			)
		except NightShiftNotSupported as e:
			frappe.log_error(str(e), "RAPL OT: night shift not supported")
			return 0.0
		except Exception:
			frappe.log_error(frappe.get_traceback(), "RAPL OT: compute failed")
			return 0.0

	# --- Guard 1: GENUINE leave day ---
	# check_leave_record() (native, runs earlier in Attendance's own validate())
	# sets leave_type AND leave_application from a real approved Leave
	# Application. A genuine leave decision outranks our lateness rules.
	#
	# BUT this guard used to test `leave_type` alone, and THIS function sets
	# leave_type = settings.half_day_leave_type whenever it applies a Half Day.
	# So every auto-applied Half Day tripped its own guard on the next save and
	# froze: the band never recomputed, OT stayed 0, and correcting the punch
	# could never lift the Half Day. Records sat wrong permanently.
	#
	# The automation's own marker is now recognised as ours and recomputed.
	# Anything else -- a real Leave Application, or a manually chosen leave
	# type -- still short-circuits untouched.
	own_marker = settings.half_day_leave_type
	genuine_leave = bool(leave_type) and (
		bool(leave_application) or leave_type != own_marker
	)
	if genuine_leave:
		return derived

	# --- Guard 2: no check-in data at all ---
	if not in_time:
		return derived

	# --- Guard 3: Sunday or any holiday -- OT only, no lateness rules ---
	if holiday_dates and getdate(attendance_date) in holiday_dates:
		hours = _ot(status)
		derived["custom_overtime_hours"] = hours
		derived["custom_overtime"] = 1 if hours > 0 else 0
		return derived

	derived["rules_applied"] = True

	band_label, past_all_bands = match_late_band(in_time, settings)
	derived["custom_late_mark_band"] = band_label
	is_half_day = past_all_bands

	if out_time and is_early_exit(out_time, attendance_date, settings):
		is_half_day = True
		derived["early_exit"] = 1

	if is_half_day:
		derived["status"] = "Half Day"
		derived["half_day_status"] = "Absent"
		derived["leave_type"] = settings.half_day_leave_type
	elif status == "Half Day" and leave_type == own_marker and not leave_application:
		# The rules no longer call for a Half Day and the existing one is OUR
		# marker (not a real Leave Application, not a manual choice), so it was
		# applied by an earlier run against punches that have since been
		# corrected. Clear it. Without this the Half Day is one-way: the rules
		# could add it but never remove it, so fixing a late punch left the
		# employee on a half day's pay.
		derived["status"] = "Present"
		derived["half_day_status"] = None
		derived["leave_type"] = None
		derived["cleared_half_day"] = True

	# Overtime last: it reads the status the Half Day branch may have changed.
	hours = _ot(derived["status"])
	derived["custom_overtime_hours"] = hours
	derived["custom_overtime"] = 1 if hours > 0 else 0
	return derived


def apply_attendance_deduction_logic(doc, method):
	"""Thin wrapper: gather values, apply the rules, assign the results."""
	if doc.in_time:
		# in_time/out_time can arrive as plain strings on a fresh document at
		# this point in the save lifecycle -- confirmed via a real TypeError in
		# production ('str' - 'datetime.datetime'). get_datetime() is
		# idempotent, so this is safe either way.
		doc.in_time = get_datetime(doc.in_time)
	if doc.out_time:
		doc.out_time = get_datetime(doc.out_time)

	settings = get_automation_settings()
	holiday_list = get_holiday_list_for_employee(doc.employee)
	holiday_dates = set(
		get_all_holiday_dates(holiday_list, doc.attendance_date, doc.attendance_date) or []
	)

	derived = derive_attendance_fields(
		employee=doc.employee,
		attendance_date=doc.attendance_date,
		in_time=doc.in_time,
		out_time=doc.out_time,
		working_hours=doc.working_hours,
		status=doc.status,
		leave_type=doc.leave_type,
		shift=doc.get("shift"),
		settings=settings,
		holiday_dates=holiday_dates,
		leave_application=doc.get("leave_application"),
		half_day_status=doc.get("half_day_status"),
	)

	# Always assigned: the original filled working_hours and reset the OT
	# fields before any guard could return.
	doc.working_hours = derived["working_hours"]
	doc.custom_overtime_hours = derived["custom_overtime_hours"]
	doc.custom_overtime = derived["custom_overtime"]

	if not derived["rules_applied"]:
		return

	doc.custom_late_mark_band = derived["custom_late_mark_band"]
	if doc.out_time:
		doc.early_exit = derived["early_exit"]
	if derived["status"] == "Half Day":
		doc.status = "Half Day"
		doc.half_day_status = derived["half_day_status"]
		doc.leave_type = derived["leave_type"]
	elif derived["cleared_half_day"]:
		doc.status = derived["status"]
		doc.half_day_status = None
		doc.leave_type = None


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


