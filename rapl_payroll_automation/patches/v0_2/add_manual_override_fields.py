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
    custom_status_manual     -> leave status / half_day_status / leave_type alone

custom_attendance_type is not a pin -- it records WHERE someone was on a day
with no punches, so a legitimate site visit stops reading as a forgotten punch.
"Site / Vendor Visit" could not be a real Attendance status: attendance.py
hardcodes the allowed list in Python
(validate_status(self.status, ["Present", "Absent", "On Leave", "Half Day",
"Work From Home"])) and throws on anything else, so a Customize Form option
would be selectable and then fail on save.

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
					"fieldname": "custom_status_manual",
					"label": "Status Set Manually",
					"fieldtype": "Check",
					"default": "0",
					"read_only": 1,
					"insert_after": "half_day_status",
					"description": (
						"Set by the Attendance Console when a status is chosen by hand. While "
						"this is ticked the automation will not re-apply Half Day for a late "
						"arrival or an early exit on this day. Status drives PAYMENT DAYS, so "
						"this pin is more consequential than the overtime or late mark pins -- "
						"a pinned Present on a day someone left early is a full day's pay."
					),
				},
				{
					"fieldname": "custom_attendance_type",
					"label": "Visit Type",
					"fieldtype": "Select",
					"options": "\nSite Visit\nClient Visit\nVendor Visit",
					"insert_after": "status",
					"depends_on": "eval:doc.status=='Present'",
					"description": (
						"Where the employee was when there are no punch times. Required by the "
						"Attendance Console when status is Present and BOTH in and out are "
						"empty -- without it every site visit reads as a forgotten punch and "
						"the missing-punch flag becomes useless. Not required when a punch "
						"exists, and not required for Work From Home."
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
