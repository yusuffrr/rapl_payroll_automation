# Copyright (c) 2026, RAPL and contributors
#
# Lifecycle rules shared by RAPL Overtime Processing and RAPL Late Mark
# Processing. Both documents create one submitted Additional Salary per row on
# submit, so both need the same guards on validate and the same undo on cancel.

import frappe
from frappe import _
from frappe.utils import flt, getdate


def validate_processing_doc(doc):
	"""Server-side checks the form's JavaScript used to be the only guard for."""
	if doc.start_date and doc.end_date:
		start, end = getdate(doc.start_date), getdate(doc.end_date)
		if start > end:
			frappe.throw(_("Start Date cannot be after End Date."))
		# Rates are a month's salary over that month's days, and the
		# Additional Salary lands in one payroll month: a period must not
		# straddle two months.
		if (start.year, start.month) != (end.year, end.month):
			frappe.throw(_("Start and End Date must be in the same month."))

	seen = set()
	for row in doc.entries:
		if not row.employee:
			frappe.throw(_("Row {0}: Employee is required.").format(row.idx))
		if row.employee in seen:
			frappe.throw(
				_("Row {0}: {1} appears more than once. Keep one row per employee.").format(
					row.idx, row.employee
				)
			)
		seen.add(row.employee)
		for field in ("amount", "ot_hours", "ot_rate", "per_day_rate"):
			if row.meta.has_field(field) and flt(row.get(field)) < 0:
				frappe.throw(
					_("Row {0}: {1} cannot be negative.").format(row.idx, row.meta.get_label(field))
				)


def before_submit_processing_doc(doc):
	if not doc.entries:
		frappe.throw(_("Add at least one employee before submitting."))
	if not any(flt(r.amount) > 0 for r in doc.entries):
		frappe.throw(_("Every row has a zero amount -- nothing would be paid or deducted."))


def cancel_additional_salaries(doc):
	"""Undo on_submit: cancel the Additional Salary each row created.

	Without this, cancelling the processing document left every Additional
	Salary submitted, so the money still reached the payslip -- and
	additional_salary_already_exists() then blocked a corrected re-run.

	Refuses (with the slip name) when a submitted Salary Slip already used the
	amount: the slip has to be cancelled first, in that order.
	"""
	for row in doc.entries:
		if not row.additional_salary:
			continue
		if frappe.db.get_value("Additional Salary", row.additional_salary, "docstatus") != 1:
			row.db_set("additional_salary", None, update_modified=False)
			continue

		slip = frappe.db.get_value(
			"Salary Detail",
			{"additional_salary": row.additional_salary, "parenttype": "Salary Slip", "docstatus": 1},
			"parent",
		)
		if slip:
			frappe.throw(
				_(
					"Row {0} ({1}): Additional Salary {2} is already on submitted Salary Slip {3}. "
					"Cancel that Salary Slip first."
				).format(row.idx, row.employee, row.additional_salary, slip)
			)

		ads = frappe.get_doc("Additional Salary", row.additional_salary)
		# Created with ignore_permissions on submit, so cancel the same way:
		# the gate is the processing document's own Cancel permission.
		ads.flags.ignore_permissions = True
		ads.cancel()
		row.db_set("additional_salary", None, update_modified=False)
