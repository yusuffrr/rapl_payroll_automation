# Copyright (c) 2026, RAPL and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from rapl_payroll_automation.api.payroll_automation_utils import time_to_seconds


class RAPLPayrollAutomationSettings(Document):
	def validate(self):
		self.validate_half_day_leave_type()
		self.validate_grade_rules_not_duplicated()
		self.validate_band_ordering()
		self.validate_band_labels_and_fractions()
		self.validate_numbers()
		self.validate_components()

	def validate_half_day_leave_type(self):
		"""
		CRITICAL check: half_day_leave_type must NOT be an LWP or PPL leave type.

		Verified against hrms/payroll/doctype/salary_slip/salary_slip.py:
		- calculate_lwp_ppl_and_absent_days_based_on_attendance() adds to `lwp`
		  whenever status="Half Day" AND leave_type is set AND that leave_type
		  is in leave_type_map (i.e. is_lwp=1 or is_ppl=1).
		- get_half_absent_days() independently counts status="Half Day" AND
		  half_day_status="Absent" with NO leave_type awareness at all.
		Both subtract from payment_days. If half_day_leave_type is LWP/PPL,
		BOTH mechanisms fire on our automated half-day records, causing a
		double deduction (a full day's pay lost instead of half a day's).
		"""
		if not self.half_day_leave_type:
			return

		lt = frappe.db.get_value(
			"Leave Type", self.half_day_leave_type, ["is_lwp", "is_ppl"], as_dict=True
		)
		if not lt:
			frappe.throw(_("Half Day Leave Type '{0}' does not exist.").format(self.half_day_leave_type))
		if lt.is_lwp or lt.is_ppl:
			frappe.throw(
				_(
					"Half Day Leave Type '{0}' has 'Is LWP' or 'Is Partially Paid Leave' "
					"ticked. This WILL cause a double-deduction on every automated Half Day "
					"(verified: both calculate_lwp_ppl_and_absent_days_based_on_attendance() "
					"and get_half_absent_days() would fire on the same Attendance record). "
					"Create a dedicated Leave Type with BOTH flags unticked instead."
				).format(self.half_day_leave_type)
			)

	def validate_grade_rules_not_duplicated(self):
		seen = set()
		for row in self.grade_ot_rules:
			if row.grade in seen:
				frappe.throw(_("Grade '{0}' appears more than once in Grade OT Rules (row {1})").format(row.grade, row.idx))
			seen.add(row.grade)

	def validate_band_ordering(self):
		"""
		Replaces the old hardcoded grace/band1/band2 check now that bands are
		a flexible table. Enforces:
		- max 5 bands (matches the fixed count columns on RAPL Late Mark
		  Processing -- see rapl_late_mark_processing_entry.json)
		- each band's From Time < To Time
		- bands don't overlap each other (a given check-in time must match
		  at most one band, or the attendance script's classification would
		  be ambiguous/order-dependent)

		Uses time_to_seconds() (the same helper already proven correct in
		attendance_automation.py) rather than raw Python >=/<= on the raw
		Time field values directly. Confirmed necessary: on a freshly-added,
		not-yet-saved child row, Frappe Time field values can arrive in a
		type/format that doesn't compare correctly with raw operators (the
		same underlying class of issue as the confirmed 'str' vs
		'datetime.datetime' TypeError on Attendance.in_time) -- converting
		both sides to seconds-since-midnight sidesteps this entirely,
		regardless of what raw type/format the value happens to be in.
		"""
		if len(self.late_mark_bands) > 5:
			frappe.throw(
				_(
					"Maximum 5 Late Mark Bands supported (RAPL Late Mark Processing has 5 "
					"fixed count columns). You have {0}."
				).format(len(self.late_mark_bands))
			)

		sorted_bands = sorted(self.late_mark_bands, key=lambda r: time_to_seconds(r.from_time))
		for i, row in enumerate(sorted_bands):
			from_s = time_to_seconds(row.from_time)
			to_s = time_to_seconds(row.to_time)
			if from_s >= to_s:
				frappe.throw(
					_("Band '{0}': From Time must be before To Time.").format(row.label)
				)
			if i > 0:
				prev = sorted_bands[i - 1]
				if from_s <= time_to_seconds(prev.to_time):
					frappe.throw(
						_(
							"Bands '{0}' and '{1}' overlap. A check-in time must fall into "
							"at most one band -- adjust the From/To times so they don't overlap."
						).format(prev.label, row.label)
					)

	def validate_band_labels_and_fractions(self):
		"""Late Mark counts attendance PER LABEL, so two bands sharing a label
		would both count the same records -- a double deduction."""
		seen = set()
		for row in self.late_mark_bands:
			label = (row.label or "").strip()
			if not label:
				frappe.throw(_("Late Mark Band row {0}: Label is required.").format(row.idx))
			if label in seen:
				frappe.throw(_("Late Mark Band label '{0}' is used twice. Labels must be unique.").format(label))
			seen.add(label)
			if not 0 <= flt(row.fraction) <= 1:
				frappe.throw(
					_("Late Mark Band '{0}': fraction must be between 0 and 1 (of a day).").format(label)
				)

	def validate_numbers(self):
		if self.meta.has_field("ot_hours_divisor") and flt(self.ot_hours_divisor) <= 0:
			frappe.throw(_("OT Hours Divisor must be greater than 0."))
		if cint(self.get("ot_minimum_minutes")) < 0:
			frappe.throw(_("Minimum Overrun Minutes cannot be negative."))
		if cint(self.get("unpaid_break_minutes")) < 0:
			frappe.throw(_("Unpaid Break minutes cannot be negative."))
		day = cint(self.get("advance_cutoff_day"))
		if day and not 1 <= day <= 31:
			frappe.throw(_("Advance cut-off day must be between 1 and 31."))

	def validate_components(self):
		"""Catch a wrong component type now, not on payroll day."""
		expected = {
			"overtime_salary_component": "Earning",
			"basic_salary_component": "Earning",
			"hra_salary_component": "Earning",
			"late_mark_salary_component": "Deduction",
			"advance_salary_component": "Deduction",
			"pf_salary_component": "Deduction",
			"pt_salary_component": "Deduction",
			"esi_salary_component": "Deduction",
		}
		for field, kind in expected.items():
			name = self.get(field)
			if not name or not self.meta.has_field(field):
				continue
			actual = frappe.db.get_value("Salary Component", name, "type")
			if actual and actual != kind:
				frappe.throw(
					_("{0}: '{1}' is a {2} component, expected {3}.").format(
						self.meta.get_label(field), name, actual, kind
					)
				)
