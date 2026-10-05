# Copyright (c) 2026, RAPL and contributors

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import append_number_if_name_exists
from frappe.utils import cint, flt, getdate

from rapl_payroll_automation.api.processing_common import (
	before_submit_processing_doc,
	cancel_additional_salaries,
	validate_processing_doc,
)
from rapl_payroll_automation.api.payroll_math import RATE_DP, round_half_up
from rapl_payroll_automation.api.payroll_automation_utils import (
	additional_salary_already_exists,
	create_and_submit_additional_salary,
	get_automation_settings,
	get_salary_month,
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
		# " #" not "-": see RAPL Overtime Processing.autoname.
		self.name = append_number_if_name_exists("RAPL Late Mark Processing", base_name, separator=" #")

	def validate(self):
		validate_processing_doc(self)

	def before_submit(self):
		before_submit_processing_doc(self)

	def on_cancel(self):
		cancel_additional_salaries(self)

	def on_submit(self):
		settings = get_automation_settings()
		errors = []
		for row in self.entries:
			if not row.amount or row.amount <= 0:
				continue
			if additional_salary_already_exists(row.employee, settings.late_mark_salary_component, self.end_date, self.start_date):
				errors.append(f"{row.employee}: already processed for this period, skipped")
				# Never keep a link to a record this document did not create
				# (e.g. copied from the document that did).
				if row.additional_salary:
					row.db_set("additional_salary", None, update_modified=False)
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
def get_band_labels(with_fractions=0):
	"""
	Returns the configured band labels, in order, for the client script to
	rename column 1..5's headers and hide any beyond the actual band count.

	with_fractions=1 returns [{label, fraction}] instead, so the form gets the
	fractions in the SAME call. It used to fetch them with a second, async
	frappe.db.get_doc on Settings -- a count edited before that returned, or a
	user who cannot read Settings, priced the row at 0.
	"""
	frappe.has_permission("RAPL Late Mark Processing", "read", throw=True)
	settings = get_automation_settings()
	bands = sorted(settings.late_mark_bands, key=lambda r: time_to_seconds(r.from_time))
	if cint(with_fractions):
		return [{"label": b.label, "fraction": flt(b.fraction)} for b in bands]
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
	# get_doc does not check permission; rates expose salary.
	doc.check_permission("write")
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
	# Only employees the caller may see (User Permissions). get_all above and a
	# client-sent list both ignore them, and each row exposes pay rates.
	if employees:
		allowed = set(frappe.get_list(
			"Employee", filters={"name": ["in", list(employees)]}, pluck="name", limit_page_length=0
		))
		employees = [e for e in employees if e in allowed]

	existing_employees = {row.employee for row in doc.entries}
	errors = []
	working_days = get_salary_month(start_date)[2]   # the MONTH's days, not the period's
	bands = sorted(settings.late_mark_bands, key=lambda r: time_to_seconds(r.from_time))

	for emp in employees:
		if emp in existing_employees:
			continue  # already in the table (manual or previous fetch) -- don't touch it
		if additional_salary_already_exists(emp, settings.late_mark_salary_component, end_date, start_date):
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
	# Same base field the Console and Overtime use (Settings), not a literal.
	monthly_salary = frappe.db.get_value("Employee", employee, settings.ot_rate_base_fieldname or "custom_monthly_salary")
	if not monthly_salary:
		return (
			{"band_counts": [0] * MAX_BANDS, "per_day_rate": 0, "amount": 0},
			f"{employee}: missing monthly salary, added with 0 (edit manually)",
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
	if not working_days or working_days <= 0:
		return (
			{"band_counts": [0] * MAX_BANDS, "per_day_rate": 0, "amount": 0},
			f"{employee}: period has no days, added with 0",
		)
	# Exact (RATE_DP decimals). Only the final amount is rounded, once.
	per_day_rate = round_half_up(flt(monthly_salary) / working_days, RATE_DP)

	# ONE grouped query for every band, instead of one COUNT per band.
	#
	# Status filter: a band sitting on an Absent / On Leave / Work From Home
	# record would otherwise be counted, deducting a late mark for a day the
	# employee was not at work. Half Day is deliberately still counted:
	# arriving at 10:15 earns a band AND leaving before 17:00 makes it a Half
	# Day, and both penalties legitimately apply to the same record.
	labels = [b.label for b in bands[:MAX_BANDS]]
	counts_by_label = {}
	if labels:
		status_clause = ""
		if cint(settings.get("count_late_marks_only_when_present", 1)):
			status_clause = "AND status IN ('Present', 'Half Day')"
		rows = frappe.db.sql(
			f"""
			SELECT custom_late_mark_band, COUNT(*)
			FROM `tabAttendance`
			WHERE employee = %(employee)s
				AND attendance_date BETWEEN %(start)s AND %(end)s
				AND docstatus = 1
				AND custom_late_mark_band IN %(labels)s
				{status_clause}
			GROUP BY custom_late_mark_band
			""",
			{"employee": employee, "start": start_date, "end": end_date, "labels": tuple(labels)},
		)
		counts_by_label = {label: cint(cnt) for label, cnt in rows}

	band_counts = []
	amount = 0.0
	for band in bands[:MAX_BANDS]:
		count = counts_by_label.get(band.label, 0)
		band_counts.append(count)
		amount += count * flt(band.fraction)   # days; priced once, below

	# Pad to MAX_BANDS with 0 if fewer bands are configured than the column cap
	band_counts += [0] * (MAX_BANDS - len(band_counts))

	return (
		{
			"band_counts": band_counts,
			"per_day_rate": per_day_rate,
			# sum(count x fraction) x per-day, rounded once -- the same order as
			# the Console, so float error cannot tip the rupee differently.
			"amount": round_half_up(amount * per_day_rate),
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
	# get_doc does not check permission; rates expose salary.
	doc.check_permission("write")
	if not frappe.has_permission("Employee", "read", doc=employee):
		frappe.throw(_("Not permitted for employee {0}").format(employee), frappe.PermissionError)
	settings = get_automation_settings()
	working_days = get_salary_month(doc.start_date)[2]
	bands = sorted(settings.late_mark_bands, key=lambda r: time_to_seconds(r.from_time))
	result, err = _compute_employee_late_mark_details(employee, doc.start_date, doc.end_date, working_days, bands)
	return {
		"band_counts": result["band_counts"],
		"per_day_rate": result["per_day_rate"],
		"amount": result["amount"],
		"errors": [err] if err else [],
	}
