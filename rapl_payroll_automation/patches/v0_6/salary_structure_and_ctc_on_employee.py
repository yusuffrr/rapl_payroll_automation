# Copyright (c) 2026, RAPL and contributors
"""Employee:
  * "Monthly salary" (custom_monthly_salary) relabelled "Monthly Gross Salary":
    it holds the assignment's Base, which is gross pay before PF/PT/ESI.
  * New read-only "Salary Structure" (custom_salary_structure), a standard list
    filter, filled from the assignment in force.
  * Standard "Cost to Company (CTC)" (ctc) made read-only on the form and filled
    from the assignment's CTC. Read-only is a form setting only: Employee
    Promotion still writes Revised CTC from the server, and the next sync puts
    the assignment's figure back.
Then everything is filled once by api/salary_sync.sync_all.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

from rapl_payroll_automation.api.salary_sync import sync_all

NOTE = "Read-only: comes from the Salary Structure Assignment in force today. Change pay with a new assignment."
CTC_NOTE = (
	"Read-only: the yearly CTC of the Salary Structure Assignment in force today "
	"(gross + employer contributions). An Employee Promotion's Revised CTC is replaced "
	"by the assignment's figure on the next sync."
)


def execute():
	meta = frappe.get_meta("Employee")

	name = frappe.db.get_value("Custom Field", {"dt": "Employee", "fieldname": "custom_monthly_salary"}, "name")
	if name:
		cf = frappe.get_doc("Custom Field", name)
		if cf.label != "Monthly Gross Salary":
			cf.label = "Monthly Gross Salary"
			cf.save(ignore_permissions=True)

	anchor = "custom_monthly_salary" if meta.has_field("custom_monthly_salary") else None
	field = {
		"fieldname": "custom_salary_structure",
		"label": "Salary Structure",
		"fieldtype": "Link",
		"options": "Salary Structure",
		"read_only": 1,
		"no_copy": 1,
		"in_standard_filter": 1,
		"description": NOTE,
	}
	if anchor:
		field["insert_after"] = anchor
	create_custom_fields({"Employee": [field]}, update=True)

	if meta.has_field("ctc"):
		make_property_setter("Employee", "ctc", "read_only", 1, "Check", validate_fields_for_doctype=False)
		make_property_setter("Employee", "ctc", "description", CTC_NOTE, "Text", validate_fields_for_doctype=False)

	frappe.clear_cache(doctype="Employee")
	sync_all()
