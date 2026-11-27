# Copyright (c) 2026, RAPL and contributors
"""Employee > Monthly salary, Basic and HRA become read-only, filled from the
Salary Structure Assignment by api/salary_sync.py, then filled once now.

The three are site Custom Fields (not created by this app); each is changed
only if it exists.
"""

import frappe

from rapl_payroll_automation.api.salary_sync import COPY_FIELDS, sync_all

NOTE = "Read-only: comes from the Salary Structure Assignment in force today. Change pay with a new assignment."


def execute():
	for fieldname in COPY_FIELDS:
		name = frappe.db.get_value("Custom Field", {"dt": "Employee", "fieldname": fieldname}, "name")
		if not name:
			continue
		cf = frappe.get_doc("Custom Field", name)
		if not cf.read_only or cf.description != NOTE:
			cf.read_only = 1
			cf.description = NOTE
			cf.save(ignore_permissions=True)
	frappe.clear_cache(doctype="Employee")
	sync_all()
