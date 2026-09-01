# Copyright (c) 2026, RAPL and contributors

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import append_number_if_name_exists
from frappe.utils import cint, flt, getdate

from rapl_payroll_automation.api.payroll_automation_utils import (
	additional_salary_already_exists,
	create_and_submit_additional_salary,
	get_automation_settings,
	get_total_working_days,
	time_to_seconds,
)

# Fixed number of band-count columns on RAPL Late Mark Processing Entry
# (band_1_count .. band_5_count). Matches the cap enforced in
# RAPLPayrollAutomationSettings.validate_band_ordering() -- see that
# doctype's controller for the reasoning (Frappe doctypes have a fixed
# schema; this many columns are always present, unused ones hidden by
# rapl_late_mark_processing.js at runtime based on how many bands actually
# exist in Settings).
MAX_BANDS = 5


class RAPLLateMarkProcessing(Document):
	def autoname(self):
		"""Same pattern as RAPL Overtime Processing -- e.g. "May 2026 - Late Mark",
		with automatic "-1"/"-2" suffixing for a second document in the same month."""
		if not self.start_date:
			frappe.throw(_("Set Start Date before saving (required to generate the name)."))
		base_name = getdate(self.start_date).strftime("%B %Y") + " - Late Mark"
		self.name = append_number_if_name_exists("RAPL Late Mark Processing", base_name, separator="-")

	def on_submit(self):
		settings = get_automation_settings()
		errors = []
		for row in self.entries:
			if not row.amount or row.amount <= 0:
				continue
			if additional_salary_already_exists(row.employee, settings.late_mark_salary_component, self.end_date):
				errors.append(f"{row.employee}: already processed for this period, skipped")
				continue
			doc = create_and_submit_additional_salary(
				row.employee, settings.late_mark_salary_component, row.amount, self.start_date, self.end_date
			)
			row.db_set("additional_salary", doc.name, update_modified=False)

		if errors:
			frappe.msgprint(
				_("Some rows were skipped:") + "<br>" + "<br>".join(errors),
				indicator="orange",
				title=_("Late Mark Processing -- Notes"),
			)


@frappe.whitelist()
def get_band_labels():
	"""
	Returns the configured band labels, in order, for the client script to
	rename column 1..5's headers and hide any beyond the actual band count.
	"""
	settings = get_automation_settings()
	bands = sorted(settings.late_mark_bands, key=lambda r: time_to_seconds(r.from_time))
	return [b.label for b in bands]


@frappe.whitelist()
def get_employees(docname, all_employees=False, employees=None):
	"""
	Three mutually exclusive modes (checked in this priority order):
	  1. `employees` given (list of Employee IDs, from the manual multi-select
	     picker) -- use exactly this list, no other filter applied.
	  2. all_employees=True -- every active Employee, regardless of attendance.
	  3. all_employees=False (default), employees=None -- employees with any
	     submitted Attendance in the period (no other gate -- unlike Overtime,
	     Late Mark has no equivalent of custom_ot; presence of Attendance is
	     the only automatic criterion).
	"""
	doc = frappe.get_doc("RAPL Late Mark Processing", docname)
	settings = get_automation_settings()
	start_date, end_date = doc.start_date, doc.end_date

	if isinstance(employees, str):
		employees = frappe.parse_json(employees)
	if isinstance(all_employees, str):
		all_employees = all_employees.lower() in ("1", "true", "yes")

	if employees:
		employees = list(employees)
	elif all_employees:
		employees = frappe.get_all("Employee", filters={"status": "Active"}, pluck="name")
	else:
		employees = sorted(
			set(
				frappe.get_all(
					"Attendance",
					filters={"attendance_date": ["between", [start_date, end_date]], "docstatus": 1},
					pluck="employee",
				)
			)
		)

	# Preserve any existing rows (manual additions/edits, or a previous
	# "Get Employees" run) -- only append rows for employees NOT already
	# present.
	existing_employees = {row.employee for row in doc.entries}
	errors = []
	working_days = get_total_working_days(start_date, end_date)
	bands = sorted(settings.late_mark_bands, key=lambda r: time_to_seconds(r.from_time))

	for emp in employees:
		if emp in existing_employees:
			continue  # already in the table (manual or previous fetch) -- don't touch it
		if additional_salary_already_exists(emp, settings.late_mark_salary_component, end_date):
			errors.append(f"{emp}: already processed for this period, excluded")
			continue

		result, err = _compute_employee_late_mark_details(emp, start_date, end_date, working_days, bands)
		row = doc.append("entries", {})
		row.employee = emp
		if err:
			errors.append(err)
		for i, count in enumerate(result["band_counts"]):
			setattr(row, f"band_{i + 1}_count", count)
		row.per_day_rate = result["per_day_rate"]
		row.amount = result["amount"]

	doc.save()

	if errors:
		frappe.msgprint("<br>".join(errors), indicator="orange", title=_("Get Employees -- Notes"))

	return doc.name


def _compute_employee_late_mark_details(employee, start_date, end_date, working_days, bands, settings=None):
	"""
	Shared calculation, used both by bulk get_employees() and the
	single-employee get_employee_late_mark_details() (for rows added via the
	native grid's own Add Row).

	Counts occurrences PER BAND (matching custom_late_mark_band -- the
	Label stored on Attendance by attendance_automation.py -- against each
	band's own Label), rather than summing a single fraction value. This is
	what makes the per-band count columns possible; the old
	custom_late_deduction_fraction field is no longer read here at all.
	"""
	settings = settings or get_automation_settings()
	monthly_salary = frappe.db.get_value("Employee", employee, "custom_monthly_salary")
	if not monthly_salary:
		return (
			{"band_counts": [0] * MAX_BANDS, "per_day_rate": 0, "amount": 0},
			f"{employee}: missing custom_monthly_salary, added with 0 (edit manually)",
		)

	# Round FIRST, then use the rounded rate consistently in the amount
	# calculation too. Fixes a real, confirmed discrepancy: computing amount
	# from the full-precision rate but only rounding for display meant the
	# displayed per_day_rate and the actual math didn't agree -- anyone
	# manually verifying (multiplying displayed rate x displayed counts)
	# would get a different, "wrong-looking" number.
	# Rounded to WHOLE RUPEES (0 decimals), not 2 -- per explicit design
	# decision: per-day rate is a round-number reference point; only the
	# final Amount carries paisa-level currency precision.
	per_day_rate = round(flt(monthly_salary) / working_days)

	band_counts = []
	amount = 0.0
	for band in bands[:MAX_BANDS]:
		filters = {
			"employee": employee,
			"attendance_date": ["between", [start_date, end_date]],
			"docstatus": 1,
			"custom_late_mark_band": band.label,
		}
		# A band sitting on an Absent / On Leave / Work From Home record would
		# otherwise be counted, deducting a late mark for a day the employee
		# was not at work -- stacked on top of the absence itself. This filter
		# used to be missing entirely (only docstatus and the band label were
		# checked). Half Day is deliberately still counted: arriving at 10:15
		# earns a band AND leaving before 17:00 makes it a Half Day, and both
		# penalties legitimately apply to the same record.
		if cint(settings.get("count_late_marks_only_when_present", 1)):
			filters["status"] = ["in", ["Present", "Half Day"]]

		count = frappe.db.count("Attendance", filters=filters)
		band_counts.append(count)
		amount += count * flt(band.fraction) * per_day_rate

	# Pad to MAX_BANDS with 0 if fewer bands are configured than the column cap
	band_counts += [0] * (MAX_BANDS - len(band_counts))

	return (
		{
			"band_counts": band_counts,
			"per_day_rate": per_day_rate,
			"amount": round(amount),
		},
		None,
	)


@frappe.whitelist()
def get_employee_late_mark_details(docname, employee):
	"""
	Computes band counts/Per-Day Rate/Amount for ONE employee, used by the
	parent's own client script when a row is added via the native grid's
	own "Add Row" -- without this, such a row had Employee set but nothing
	else auto-populated.
	"""
	doc = frappe.get_doc("RAPL Late Mark Processing", docname)
	settings = get_automation_settings()
	working_days = get_total_working_days(doc.start_date, doc.end_date)
	bands = sorted(settings.late_mark_bands, key=lambda r: time_to_seconds(r.from_time))
	result, err = _compute_employee_late_mark_details(employee, doc.start_date, doc.end_date, working_days, bands)
	return {
		"band_counts": result["band_counts"],
		"per_day_rate": result["per_day_rate"],
		"amount": result["amount"],
		"errors": [err] if err else [],
	}
