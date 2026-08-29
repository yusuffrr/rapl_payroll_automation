# Copyright (c) 2026, RAPL and contributors
#
# Attendance query helpers for Overtime.
#
# HISTORY: this module previously also contained a complete pilot-era Overtime
# calculation and two whitelisted endpoints (compute_overtime_preview and
# process_overtime) that created and submitted Additional Salary directly.
# Those were superseded by the RAPL Overtime Processing doctype, which carries
# the current, corrected math -- notably rounding the per-day amount to whole
# rupees BEFORE deriving the hourly rate, so that the displayed Rate/hr
# multiplied by the displayed Hours reconciles by hand against the Amount.
# The pilot version rounded only at the end and produced slightly different
# figures.
#
# Keeping two live implementations of the same calculation was a drift risk,
# and the pilot endpoints were still callable over REST. Verified before
# removal (2026-08): no Client Script, no Server Script, no Page and no
# workflow referenced them.
#
# What remains here are the two attendance query helpers.
# get_attendance_for_employee() is imported by
# rapl_payroll_automation/doctype/rapl_overtime_processing/rapl_overtime_processing.py.

import frappe


def get_employees_with_attendance_in_period(start_date, end_date, employees=None):
	filters = {
		"attendance_date": ["between", [start_date, end_date]],
		"docstatus": 1,
		"status": "Present",
	}
	rows = frappe.get_all("Attendance", filters=filters, pluck="employee", distinct=True)
	result = sorted(set(rows))
	if employees:
		result = [e for e in result if e in employees]
	return result


def get_attendance_for_employee(employee, start_date, end_date):
	"""Explicit filters: docstatus=1 (submitted only), status=Present only --
	Absent/On Leave/draft/cancelled records must never reach the OT calculation."""
	return frappe.get_all(
		"Attendance",
		filters={
			"employee": employee,
			"attendance_date": ["between", [start_date, end_date]],
			"docstatus": 1,
			"status": "Present",
		},
		fields=[
			"name",
			"attendance_date",
			"in_time",
			"out_time",
			"working_hours",
			# Written by attendance_automation.py via ot_engine; read by
			# rapl_overtime_processing instead of recomputing per day.
			"custom_overtime_hours",
		],
	)
