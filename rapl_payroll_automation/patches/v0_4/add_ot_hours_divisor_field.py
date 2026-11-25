# Copyright (c) 2026, RAPL and contributors
"""Employee > OT Hours Divisor: normal working hours per day for one employee's
overtime rate (hourly = monthly / OT days / divisor).

Blank or 0 means the RAPL Payroll Automation Settings value (8). Set it only
for people on a longer duty, e.g. 10 for a driver on a 10-hour duty.

Idempotent: create_custom_fields() skips a field that already exists.
"""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Employee": [
				{
					"fieldname": "custom_ot_hours_divisor",
					"label": "OT Hours Divisor",
					"fieldtype": "Float",
					"insert_after": "custom_ot",
					"non_negative": 1,
					"depends_on": "eval:doc.custom_ot",
					"description": (
						"Normal duty hours per day used for this employee's overtime rate. "
						"Leave blank to use RAPL Payroll Automation Settings (8). "
						"Example: 10 for a driver on a 10-hour duty."
					),
				},
			]
		},
		update=True,
	)
