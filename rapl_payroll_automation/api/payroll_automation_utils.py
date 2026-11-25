# Copyright (c) 2026, RAPL and contributors
# Shared helpers for the RAPL Payroll Automation system (Overtime + Late Mark).
#
# Design notes (all verified against hrms/erpnext `version-16` source during design):
# - Sunday/holiday detection is driven ENTIRELY by the configured Holiday List
#   (via get_holiday_dates_between), never by a hardcoded weekday check.
#   Requires the employee's Holiday List to have `Weekly Off` set to Sunday
#   (confirmed correct for RAPL's "Public Holidays 2026" list).
# - `total working days` for both Late Mark's per-day rate and Overtime's
#   "Office" grade denominator is simply date_diff(end,start)+1 -- confirmed
#   against get_working_days_details(): since RAPL has
#   "Include holidays in Total no. of Working Days" checked, the native
#   holiday-subtraction branch never runs, so total_working_days is just the
#   calendar day count of the period, identical for every employee.

import frappe
from frappe.utils import add_days, date_diff, flt, get_datetime, getdate
from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee
from hrms.utils.holiday_list import get_holiday_dates_between


def get_datetime_combine(date, time_value):
	"""Combine an attendance_date with a Time-field value into a full datetime."""
	return get_datetime(f"{getdate(date)} {time_value}")


def time_to_seconds(time_value):
	"""
	Converts a Time-field value (datetime.time, datetime.timedelta, or
	'HH:MM:SS' string -- Frappe Time fields can arrive as any of these
	depending on context) into seconds-since-midnight, for direct numeric
	comparison. Used by the Late Mark band-matching logic to compare a
	check-in's time-of-day against each configured band's From/To Time.
	"""
	import datetime

	if isinstance(time_value, datetime.timedelta):
		return time_value.total_seconds()
	if isinstance(time_value, datetime.time):
		return time_value.hour * 3600 + time_value.minute * 60 + time_value.second
	h, m, s = [int(x) for x in str(time_value).split(":")]
	return h * 3600 + m * 60 + s


def get_all_holiday_dates(holiday_list, start_date, end_date):
	"""Sunday + named holidays combined -- everything in the Holiday List for this range."""
	if not holiday_list:
		return set()
	return set(get_holiday_dates_between(holiday_list, start_date, end_date))


def get_weekly_off_dates(holiday_list, start_date, end_date):
	"""
	Sunday ONLY -- filtered from the Holiday List via the weekly_off flag.
	Used for Overtime's Floor/Office denominator split, which is Sunday-specific
	(unlike the late-mark/threshold-exception checks, which use ALL holidays).
	"""
	if not holiday_list:
		return set()
	records = get_holiday_dates_between(
		holiday_list, start_date, end_date, as_dict=True, select_weekly_off=True
	)
	return {r.holiday_date for r in records if r.weekly_off}


# ---------------------------------------------------------------------------
# Holidays for an employee OVER A PERIOD
#
# Every caller used to do get_holiday_list_for_employee(employee) with no date,
# which returns the Holiday List assigned TODAY. Processing a past month after
# the list changed -- e.g. December 2026 in January 2027, once the 2027 list is
# assigned -- then used the wrong year's list: December's Sundays were not
# holidays, Sunday work was priced as ordinary-day overtime, and the Floor-grade
# OT denominator lost its weekly offs. The Salary Slip itself already resolves
# the list for the period (HRMS 16.20+: per date via Holiday List Assignment).
# ---------------------------------------------------------------------------


def get_holiday_list_ranges(employee, start_date, end_date):
	"""[(holiday_list, from_date, to_date), ...] covering [start_date, end_date].

	HRMS 16.20+ provides get_holiday_list_ranges_for_employee (the same function
	the Salary Slip uses), which handles any number of list changes. On older
	HRMS the list is resolved as on the first and last day, and when they differ
	the change-over day is found by bisection (one change per period assumed --
	assignments are normally yearly).
	"""
	start, end = getdate(start_date), getdate(end_date)
	if start > end:
		return []

	try:
		from hrms.utils.holiday_list import get_holiday_list_ranges_for_employee
	except ImportError:
		get_holiday_list_ranges_for_employee = None

	if get_holiday_list_ranges_for_employee:
		return [
			(r["holiday_list"], max(getdate(r["from_date"]), start), min(getdate(r["to_date"]), end))
			for r in get_holiday_list_ranges_for_employee(employee, start, end)
			if r.get("holiday_list")
		]

	def list_on(day):
		return get_holiday_list_for_employee(employee, as_on=day)

	first = list_on(start)
	last = first if start == end else list_on(end)
	if first == last:
		return [(first, start, end)] if first else []

	# Bisect for the first day that uses `last`.
	lo, hi = start, end  # list_on(lo) == first, list_on(hi) == last
	while date_diff(hi, lo) > 1:
		mid = add_days(lo, date_diff(hi, lo) // 2)
		if list_on(mid) == first:
			lo = mid
		else:
			hi = mid
	return [r for r in ((first, start, lo), (last, hi, end)) if r[0]]


def get_employee_holiday_rows(employee, start_date, end_date):
	"""Holiday rows (holiday_date, description, weekly_off) for the employee's period."""
	rows = []
	for holiday_list, frm, to in get_holiday_list_ranges(employee, start_date, end_date):
		rows += frappe.get_all(
			"Holiday",
			filters={"parent": holiday_list, "holiday_date": ["between", [frm, to]]},
			fields=["holiday_date", "description", "weekly_off"],
		)
	return rows


def get_employee_holiday_dates(employee, start_date, end_date):
	"""Weekly offs AND named holidays in force for the employee over the period."""
	return {getdate(r.holiday_date) for r in get_employee_holiday_rows(employee, start_date, end_date)}


def get_employee_weekly_off_dates(employee, start_date, end_date):
	"""Weekly offs ONLY (Sundays) -- the Floor/Office OT denominator split."""
	return {
		getdate(r.holiday_date)
		for r in get_employee_holiday_rows(employee, start_date, end_date)
		if r.weekly_off
	}


def get_salary_month(any_date):
	"""(first_day, last_day, days) of the calendar month containing any_date.

	Per-day and per-hour RATES are always a month's salary over that MONTH's
	days -- never over the length of whatever period is being processed. A
	1-15 run divided by 15 and paid every late mark and overtime hour at twice
	the rate.
	"""
	from frappe.utils import get_first_day, get_last_day

	first, last = get_first_day(any_date), get_last_day(any_date)
	return first, last, date_diff(last, first) + 1


def get_total_working_days(start_date, end_date):
	"""
	Matches native get_working_days_details() exactly for RAPL's settings
	(Include holidays in Total no. of Working Days = checked): total calendar
	days in the period, inclusive. Used by Late Mark's per-day rate AND by
	Overtime's "Office" grade denominator (same number, deliberately reused,
	not two separate calculations).
	"""
	return date_diff(end_date, start_date) + 1


def get_automation_settings():
	# Cached: this runs on every Attendance validate and every Salary Slip
	# validate. A Single's cache is cleared when the Settings are saved.
	return frappe.get_cached_doc("RAPL Payroll Automation Settings")


def pick_assignment_base(assignments, actual_start):
	"""Base of the assignment HRMS would use for a slip starting `actual_start`.

	Mirrors salary_slip.set_salary_structure_assignment(): the latest
	submitted assignment with from_date <= the slip's actual start (period
	start, or the joining date when that is later). `assignments` are dicts
	with from_date / base (docstatus 1 only). Returns 0 when none applies.
	"""
	start = getdate(actual_start)
	in_force = [a for a in assignments if getdate(a["from_date"]) <= start]
	if not in_force:
		return 0.0
	latest = max(in_force, key=lambda a: getdate(a["from_date"]))
	return flt(latest["base"])


def get_monthly_salaries(employees, start_date, settings=None):
	"""{employee: monthly salary} for overtime / late-mark rates in the month
	that starts `start_date`.

	The salary is Base on the Salary Structure Assignment in force for that
	month -- the same figure the Salary Slip pays Basic and HRA from -- so an
	increment from a given date changes the rates from that month on, and a
	past month is still priced with its own salary.

	Falls back to the Employee field named in Settings (custom_monthly_salary)
	only where no assignment with a Base applies: employees not yet moved to
	Base-driven structures.
	"""
	employees = [e for e in dict.fromkeys(employees or []) if e]
	if not employees:
		return {}
	settings = settings or get_automation_settings()
	field = settings.get("ot_rate_base_fieldname") or "custom_monthly_salary"
	emp_rows = {
		e.name: e for e in frappe.get_all(
			"Employee", filters={"name": ["in", employees]},
			fields=["name", "date_of_joining", field], limit_page_length=0,
		)
	}
	by_emp = {}
	for a in frappe.get_all(
		"Salary Structure Assignment",
		filters={"employee": ["in", employees], "docstatus": 1},
		fields=["employee", "from_date", "base"], limit_page_length=0,
	):
		by_emp.setdefault(a.employee, []).append(a)

	# The SLIP's start, not the processing period's: a run for 16-30 Nov must
	# still price with the assignment the November slip uses (in force on
	# 1 Nov, or on the joining date when the employee joined that month).
	month_first, month_last, _days = get_salary_month(start_date)
	out = {}
	for emp in employees:
		row = emp_rows.get(emp) or {}
		actual_start = month_first
		doj = row.get("date_of_joining")
		if doj and month_first < getdate(doj) <= month_last:
			actual_start = getdate(doj)
		base = pick_assignment_base(by_emp.get(emp, []), actual_start)
		out[emp] = base if base > 0 else flt(row.get(field))
	return out


def get_monthly_salary(employee, start_date, settings=None):
	"""Single-employee form of get_monthly_salaries()."""
	return get_monthly_salaries([employee], start_date, settings).get(employee, 0.0)


OT_DIVISOR_FIELD = "custom_ot_hours_divisor"


def has_ot_divisor_field():
	"""The Employee field arrives with patch v0_4; never fail before it runs."""
	try:
		return bool(frappe.get_meta("Employee").has_field(OT_DIVISOR_FIELD))
	except Exception:
		return False


def employee_ot_divisor(employee=None, settings=None, value=None):
	"""Normal working hours per day for this employee's overtime rate.

	Employee > OT Hours Divisor when it is set (e.g. 10 for a driver on a
	10-hour duty), otherwise RAPL Payroll Automation Settings > OT Hours
	Divisor (8). Pass `value` when the field was already fetched.
	"""
	settings = settings or get_automation_settings()
	if value is None and employee and has_ot_divisor_field():
		value = frappe.db.get_value("Employee", employee, OT_DIVISOR_FIELD)
	return flt(value) if flt(value) > 0 else flt(settings.ot_hours_divisor)


def get_grade_ot_rule(settings, grade):
	"""
	Returns True/False (exclude_weekly_off_from_denominator) for a grade,
	or None if no rule is configured for that grade at all.
	"""
	for row in settings.grade_ot_rules:
		if row.grade == grade:
			return bool(row.exclude_weekly_off_from_denominator)
	return None


def get_additional_salary_total(employee, salary_component, start_date, end_date):
	"""
	Sums submitted Additional Salary amounts for a component, for an employee,
	within a period. Used by the Salary Slip before_validate hook to pre-fetch
	Overtime/Conveyance totals for PF/PT/ESI formulas (which otherwise can't see
	Additional Salary, since add_structure_components runs before
	add_additional_salary_components -- verified in salary_slip.py).
	"""
	total = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(amount), 0)
		FROM `tabAdditional Salary`
		WHERE employee = %(employee)s
			AND salary_component = %(component)s
			AND docstatus = 1
			AND payroll_date BETWEEN %(start)s AND %(end)s
		""",
		{
			"employee": employee,
			"component": salary_component,
			"start": start_date,
			"end": end_date,
		},
	)
	return float(total[0][0]) if total else 0.0


def additional_salary_already_exists(employee, salary_component, payroll_date, start_date=None):
	"""
	Duplicate-run guard: prevents the bulk Overtime/Late Mark functions from
	double-processing an employee if accidentally run twice for the same period.
	Mirrors how Overtime Slip tags its own Additional Salary via ref_doctype/
	ref_docname (verified in overtime_slip.py) -- we use a simpler existence
	check against employee+component+payroll_date+docstatus=1 instead.
	"""
	# With start_date: ANY submitted record dated inside the period. Matching
	# only the exact end date let a 1-15 run and a 1-30 run both pay the
	# first fortnight.
	return frappe.db.exists(
		"Additional Salary",
		{
			"employee": employee,
			"salary_component": salary_component,
			"payroll_date": ["between", [start_date, payroll_date]] if start_date else payroll_date,
			"docstatus": 1,
		},
	)


def create_and_submit_additional_salary(employee, salary_component, amount, start_date, end_date, company=None):
	"""
	Creates and submits an Additional Salary record. Used by both bulk functions.
	overwrite_salary_structure_amount is left False (0) deliberately -- Overtime
	and Late Mark are NOT in the Salary Structure, so there is nothing to
	overwrite; the record simply appends as a new row when the Salary Slip is
	later created (verified in update_component_row()).
	"""
	emp = frappe.db.get_value("Employee", employee, ["company", "relieving_date"], as_dict=True) or {}
	company = company or emp.get("company")

	# HRMS refuses an Additional Salary dated after the relieving date, which
	# rolled back the WHOLE month's submit for one leaver. Date it on their
	# last day instead -- still inside their final payroll period.
	payroll_date = getdate(end_date)
	if emp.get("relieving_date") and getdate(emp["relieving_date"]) < payroll_date:
		payroll_date = getdate(emp["relieving_date"])

	doc = frappe.get_doc(
		{
			"doctype": "Additional Salary",
			"employee": employee,
			"company": company,
			"salary_component": salary_component,
			"amount": amount,
			"payroll_date": payroll_date,
			"overwrite_salary_structure_amount": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc
