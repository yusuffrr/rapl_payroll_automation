# Copyright (c) 2026, RAPL and contributors
#
# Employee Attendance -- READ ONLY monthly statement.
# Replaces the old hrms_custom "Attendance Export" page.
#
# ACCESS CONTROL -- read this before changing anything here
# --------------------------------------------------------
# ERPNext restricts an employee to their own Employee record via a User
# Permission, auto-created when Employee.user_id is set AND
# Employee.create_user_permission is ticked (verified in erpnext
# employee.py::update_user_permissions). HRMS ships NO
# permission_query_conditions for Attendance -- its hooks.py has them
# commented out -- so that User Permission is the only thing restricting
# Attendance in standard list views.
#
# User Permissions are applied by the ORM. This module resolves the target
# employee itself and reads through frappe.get_all, so the permission is NOT
# automatically applied to the employee argument. Access is therefore enforced
# explicitly in _resolve_access():
#
#   HR Manager / HR User -> any employee, money columns included
#   anyone else          -> forced to their OWN linked Employee, money stripped
#
# The employee argument from a non-HR caller is IGNORED rather than validated,
# so there is no way to probe for a colleague's data by passing an id.
#
# Money stripping happens SERVER SIDE. The rate block and the two currency
# columns never reach a non-HR browser at all -- hiding them in JavaScript
# would leave them readable in the JSON response.

import frappe
from frappe.utils import date_diff, flt, getdate

from rapl_payroll_automation.api.attendance_data import (
	build_month_rows,
	get_band_definitions,
	summarise,
)
from rapl_payroll_automation.api.ot_engine import is_ot_eligible
from rapl_payroll_automation.api.payroll_math import pay_rates, round_half_up
from rapl_payroll_automation.api.payroll_automation_utils import (
	get_automation_settings,
	get_grade_ot_rule,
	get_salary_month,
	get_total_working_days,
	get_employee_weekly_off_dates,
)
from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee

HR_ROLES = {"HR Manager", "HR User"}


def _is_hr():
	"""HR role AND Attendance read -- the one test every entry point uses, so
	the picker never offers what the statement call would then refuse."""
	return bool(HR_ROLES & set(frappe.get_roles())) and bool(frappe.has_permission("Attendance", "read"))


def _own_employee():
	employee = frappe.db.get_value(
		"Employee", {"user_id": frappe.session.user, "status": "Active"}, "name"
	)
	if not employee:
		frappe.throw(
			"Your user account is not linked to an Employee record. "
			"Ask HR to set the User ID on your Employee master.",
			frappe.PermissionError,
		)
	return employee


def _resolve_access(employee):
	"""Return (employee, show_money). Never trust the caller's employee arg."""
	if _is_hr():
		employee = employee or _own_employee()
		# HR is still subject to its own User Permissions (e.g. one Company).
		if not frappe.has_permission("Employee", "read", doc=employee):
			frappe.throw(f"Not permitted to view {employee}", frappe.PermissionError)
		return employee, True
	return _own_employee(), False


MAX_PERIOD_DAYS = 62
MAX_BULK = 300


def _check_period(start_date, end_date):
	if not start_date or not end_date:
		frappe.throw("Set a period first")
	start, end = getdate(start_date), getdate(end_date)
	if start > end:
		frappe.throw("Start date is after end date.")
	if date_diff(end, start) + 1 > MAX_PERIOD_DAYS:
		frappe.throw(f"Choose a period of at most {MAX_PERIOD_DAYS} days.")
	return start, end


def _pay_context(employee, start_date, end_date, settings):
	"""Monthly salary -> per-day -> per-hour, mirroring the rate derivation in
	rapl_overtime_processing so the statement cannot disagree with what is paid."""
	monthly = flt(frappe.db.get_value("Employee", employee, settings.ot_rate_base_fieldname))
	grade = frappe.db.get_value("Employee", employee, "grade")
	rule = get_grade_ot_rule(settings, grade)

	# Rates use the salary MONTH (see payroll_automation_utils.get_salary_month).
	month_first, month_last, total_days = get_salary_month(start_date)
	sundays = []
	exclude = bool(rule)
	if exclude:
		sundays = get_employee_weekly_off_dates(employee, month_first, month_last)
	r = pay_rates(monthly, total_days, len(sundays), exclude, settings.ot_hours_divisor)

	# per_day_rate = calendar-day rate (late mark / half day, as Late Mark
	# Processing pays); ot_per_day_rate = the overtime denominator.
	return {
		"monthly_salary": monthly,
		"ot_working_days": r["ot_days"],
		"per_day_rate": r["late_per_day"],
		"ot_per_day_rate": r["ot_per_day"],
		"hourly_rate": r["hourly"],
		"ot_hours_divisor": flt(settings.ot_hours_divisor),
		"grade": grade,
	}


def _build_statement(employee, start_date, end_date, show_money, settings):
	data = build_month_rows(employee, start_date, end_date, settings)
	rows = data["rows"]
	summary = summarise(rows, settings)
	bands = {b["label"]: b["fraction"] for b in get_band_definitions(settings)}

	statement = {
		"employee": data["employee"].name,
		"employee_name": data["employee"].employee_name,
		"grade": data["employee"].grade,
		# Employee.custom_ot -- when 0 the OT columns are dropped from the
		# statement entirely rather than shown as a column of zeros.
		"ot_eligible": is_ot_eligible(data["employee"].name),
		"start_date": str(start_date),
		"end_date": str(end_date),
		"rows": rows,
		"summary": summary,
		"bands": get_band_definitions(settings),
		"show_money": show_money,
	}

	if not show_money:
		# Strip every per-day money field before the payload leaves the server.
		for row in rows:
			att = row.get("attendance")
			if att:
				att.pop("overtime_amount", None)
				att.pop("late_deduction_amount", None)
		return statement

	pay = _pay_context(employee, start_date, end_date, settings)
	statement["pay"] = pay

	ot_total = 0.0
	for row in rows:
		att = row.get("attendance")
		if not att:
			continue
		amount = round_half_up(flt(att["overtime_hours"]) * flt(pay["hourly_rate"]))
		att["overtime_amount"] = amount
		ot_total += amount

		band = att.get("late_mark_band")
		att["late_deduction_amount"] = (
			round_half_up(flt(bands.get(band, 0)) * flt(pay["per_day_rate"])) if band else 0
		)

	# Period totals are recomputed from the period hours, NOT summed from the
	# per-day roundings -- that is how rapl_overtime_processing prices it, and
	# the two must agree.
	statement["totals"] = {
		"overtime_hours": summary["overtime_hours"],
		"overtime_amount": round_half_up(
			flt(summary.get("overtime_hours_exact", summary["overtime_hours"])) * flt(pay["hourly_rate"])
		),
		"overtime_amount_daily_sum": round_half_up(ot_total),
		"late_deduction_amount": round_half_up(
			sum(flt(bands.get(label, 0)) * count
				for label, count in summary["band_counts"].items())
			* flt(pay["per_day_rate"])
		),
		"half_day_amount": round_half_up(summary["half_day"] * 0.5 * flt(pay["per_day_rate"])),
	}
	return statement


@frappe.whitelist()
def get_statement(employee=None, start_date=None, end_date=None):
	start_date, end_date = _check_period(start_date, end_date)

	employee, show_money = _resolve_access(employee)
	settings = get_automation_settings()
	return _build_statement(employee, start_date, end_date, show_money, settings)


@frappe.whitelist()
def get_bulk_statements(employees, start_date=None, end_date=None):
	"""HR only. One statement per employee, each rendered on its own page."""
	if not _is_hr():
		frappe.throw("Only HR can view multiple employees", frappe.PermissionError)
	start_date, end_date = _check_period(start_date, end_date)

	employees = frappe.parse_json(employees) if employees not in (None, "") else []
	if not isinstance(employees, list):
		frappe.throw("employees must be a list")
	employees = list(dict.fromkeys(employees))
	if len(employees) > MAX_BULK:
		frappe.throw(f"At most {MAX_BULK} employees at a time.")
	allowed = set(frappe.get_list("Employee", filters={"name": ["in", employees or [""]]},
								  pluck="name", limit_page_length=0))
	denied = [e for e in employees if e not in allowed]
	if denied:
		frappe.throw(f"Not permitted: {', '.join(denied[:10])}", frappe.PermissionError)
	settings = get_automation_settings()
	return [
		_build_statement(emp, start_date, end_date, True, settings)
		for emp in employees
	]


@frappe.whitelist()
def get_selectable_employees():
	"""HR sees everyone active; an employee sees only themselves."""
	if _is_hr():
		return frappe.get_list(
			"Employee",
			filters={"status": "Active"},
			fields=["name", "employee_name", "grade"],
			order_by="name",
			limit_page_length=0,
		)
	own = _own_employee()
	return frappe.get_all(
		"Employee", filters={"name": own}, fields=["name", "employee_name", "grade"]
	)
