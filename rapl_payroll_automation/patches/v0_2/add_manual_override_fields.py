# Copyright (c) 2026, RAPL and contributors
"""Add the two per-day manual-override flags to Attendance.

WHY THESE EXIST
---------------
attendance_automation.derive_attendance_fields() recomputes
custom_overtime_hours and custom_late_mark_band on EVERY save. Without a flag,
a figure typed by hand survives only until the next time that record is
touched, then is silently overwritten by the rules -- worse than not offering
the edit at all.

TWO flags, not one, because they are independent decisions. Clearing a late
mark says nothing about that day's overtime: if a shared flag froze both,
correcting the out-time afterwards would leave OT stuck at its old value.

    custom_overtime_manual   -> leave custom_overtime_hours alone
    custom_late_mark_manual  -> leave custom_late_mark_band alone

Both are cleared by the Console's "reset to computed" action, after which the
rules take over again.

Idempotent: create_custom_fields() skips a field that already exists.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Attendance": [
				{
					"fieldname": "custom_overtime_manual",
					"label": "Overtime Set Manually",
					"fieldtype": "Check",
					"default": "0",
					"read_only": 1,
					"insert_after": "custom_overtime_hours",
					"description": (
						"Set by the Attendance Console when an overtime figure is entered "
						"by hand. While this is ticked the automation will not recalculate "
						"Overtime Hours for this day."
					),
				},
				{
					"fieldname": "custom_late_mark_manual",
					"label": "Late Mark Set Manually",
					"fieldtype": "Check",
					"default": "0",
					"read_only": 1,
					"insert_after": "custom_late_mark_band",
					"description": (
						"Set by the Attendance Console when a late mark band is changed or "
						"cleared by hand. While this is ticked the automation will not "
						"recalculate the band for this day."
					),
				},
			]
		},
		ignore_validate=True,
	)
	frappe.db.commit()
