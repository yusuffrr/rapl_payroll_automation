# Copyright (c) 2026, RAPL and contributors
#
# Pre-flight data quality check for the Payroll Automation Dashboard.
# Runs automatically on the Dashboard's "Load" step, before any OT/Late Mark
# numbers are computed. Surfaces issues as a list; the Dashboard shows these
# as a popup. User can fix-and-retry, or proceed excluding flagged employees.

import frappe
from frappe.utils import getdate


@frappe.whitelist()
def run_preflight_check(start_date, end_date):
	# Lists names and data problems for the whole company -- HR only.
	from rapl_payroll_automation.api.employee_attendance import _is_hr

	if not _is_hr() or not frappe.has_permission("Attendance", "read"):
		frappe.throw("Payroll pre-flight check is restricted to HR.", frappe.PermissionError)
	if not start_date or not end_date or getdate(start_date) > getdate(end_date):
		frappe.throw("Set a valid period first")

	issues = []

	issues += _check_missing_checkout(start_date, end_date)
	issues += _check_missing_monthly_salary(start_date, end_date)
	issues += _check_missing_grade_rule(start_date, end_date)
	issues += _check_bad_timestamps(start_date, end_date)
	issues += _check_duplicate_attendance(start_date, end_date)
	issues += _check_missing_salary_structure_assignment(start_date, end_date)

	flagged_employees = sorted({i["employee"] for i in issues})
	return {"issues": issues, "flagged_employees": flagged_employees}


def _check_missing_checkout(start_date, end_date):
	rows = frappe.get_all(
		"Attendance",
		filters={
			"attendance_date": ["between", [start_date, end_date]],
			"docstatus": 1,
			"status": "Present",
			"in_time": ["is", "set"],
			"out_time": ["is", "not set"],
		},
		fields=["employee", "employee_name", "attendance_date", "name"],
	)
	return [
		{
			"employee": r.employee,
			"employee_name": r.employee_name,
			"reason": f"Missing checkout on {r.attendance_date} (Attendance: {r.name})",
		}
		for r in rows
	]


def _check_missing_monthly_salary(start_date, end_date):
	employees = frappe.get_all(
		"Attendance",
		filters={"attendance_date": ["between", [start_date, end_date]], "docstatus": 1},
		pluck="employee",
		distinct=True,
	)
	issues = []
	for e in _employee_rows(employees):
		if not e.custom_monthly_salary:
			issues.append({"employee": e.name, "employee_name": e.employee_name,
						   "reason": "Missing custom_monthly_salary"})
	return issues


def _employee_rows(employees):
	"""One query for the whole list instead of 1-2 get_value calls per employee."""
	if not employees:
		return []
	return frappe.get_all(
		"Employee",
		filters={"name": ["in", list(employees)]},
		fields=["name", "employee_name", "grade", "custom_monthly_salary"],
	)


def _check_missing_grade_rule(start_date, end_date):
	settings = frappe.get_single("RAPL Payroll Automation Settings")
	configured_grades = {row.grade for row in settings.grade_ot_rules}

	employees = frappe.get_all(
		"Attendance",
		filters={"attendance_date": ["between", [start_date, end_date]], "docstatus": 1, "status": "Present"},
		pluck="employee",
		distinct=True,
	)
	issues = []
	for e in _employee_rows(employees):
		emp, grade, name = e.name, e.grade, e.employee_name
		if not grade:
			issues.append({"employee": emp, "employee_name": name, "reason": "Missing Grade (needed for OT rate)"})
		elif grade not in configured_grades:
			issues.append(
				{
					"employee": emp,
					"employee_name": name,
					"reason": f"Grade '{grade}' has no matching rule in RAPL Payroll Automation Settings",
				}
			)
	return issues


def _check_bad_timestamps(start_date, end_date):
	rows = frappe.get_all(
		"Attendance",
		filters={
			"attendance_date": ["between", [start_date, end_date]],
			"docstatus": 1,
			"in_time": ["is", "set"],
			"out_time": ["is", "set"],
		},
		fields=["employee", "employee_name", "attendance_date", "in_time", "out_time", "name"],
	)
	issues = []
	for r in rows:
		if r.out_time < r.in_time:
			issues.append(
				{
					"employee": r.employee,
					"employee_name": r.employee_name,
					"reason": f"Checkout before checkin on {r.attendance_date} (Attendance: {r.name}) -- data entry error",
				}
			)
	return issues


def _check_duplicate_attendance(start_date, end_date):
	rows = frappe.db.sql(
		"""
		SELECT employee, employee_name, attendance_date, COUNT(*) as cnt
		FROM `tabAttendance`
		WHERE attendance_date BETWEEN %(start)s AND %(end)s
			AND docstatus = 1
		GROUP BY employee, attendance_date
		HAVING COUNT(*) > 1
		""",
		{"start": start_date, "end": end_date},
		as_dict=True,
	)
	return [
		{
			"employee": r.employee,
			"employee_name": r.employee_name,
			"reason": f"Duplicate Attendance records on {r.attendance_date} ({r.cnt} found)",
		}
		for r in rows
	]


def _check_missing_salary_structure_assignment(start_date, end_date):
	employees = frappe.get_all(
		"Attendance",
		filters={"attendance_date": ["between", [start_date, end_date]], "docstatus": 1},
		pluck="employee",
		distinct=True,
	)
	with_assignment = set(frappe.get_all(
		"Salary Structure Assignment",
		filters={"employee": ["in", employees or [""]], "docstatus": 1, "from_date": ["<=", end_date]},
		pluck="employee",
	))
	issues = []
	for e in _employee_rows(employees):
		if e.name not in with_assignment:
			issues.append({"employee": e.name, "employee_name": e.employee_name,
						   "reason": "No active Salary Structure Assignment found"})
	return issues
