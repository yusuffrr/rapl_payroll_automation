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
from rapl_payroll_automation.api.payroll_math import (
	RATE_DP,
	allocate_waiver,
	clamp_waived,
	round_half_up,
	waived_row_amount,
)
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
		apply_waiver_math(self)

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


# --- Late-mark waivers -------------------------------------------------------
#
# A waiver forgives a number of an employee's late marks for this document's
# month, band by band. The band counts themselves are never changed (they
# are the attendance record); the waived marks sit beside them, and Amount is
# priced from (count - waived). Waived counts are clamped to the band's count,
# so a waiver can bring a deduction to zero but never below it.

WAIVER_MODE_LABELS = {
	"costliest": "Any band -- highest deduction first",
	"cheapest": "Any band -- lowest deduction first",
}


def _bands():
	settings = get_automation_settings()
	bands = sorted(settings.late_mark_bands, key=lambda r: time_to_seconds(r.from_time))[:MAX_BANDS]
	return [b.label for b in bands], [flt(b.fraction) for b in bands]


def _counts(row, n):
	return [cint(row.get(f"band_{i}_count")) for i in range(1, n + 1)]


def _waived(row, n):
	return [cint(row.get(f"band_{i}_waived")) for i in range(1, n + 1)]


def _set_waived(row, waived):
	for i in range(1, MAX_BANDS + 1):
		row.set(f"band_{i}_waived", waived[i - 1] if i <= len(waived) else 0)


def _base_amount(row):
	"""What the row deducts with no waiver: the stored pre-waiver amount while
	a waiver is on, otherwise the row's own Amount (computed or hand-typed)."""
	if flt(row.get("amount_before_waiver")) > 0:
		return flt(row.amount_before_waiver)
	return flt(row.amount)


def apply_waiver_math(doc, fractions=None):
	"""validate(): keep every row's waiver consistent with its counts.

	Rows with a waiver get Amount derived from the pre-waiver amount minus the
	waived marks (priced once from count - waived in the normal case). Rows
	without one keep their Amount; one whose waiver was just removed gets its
	pre-waiver amount back.
	"""
	if fractions is None:
		fractions = _bands()[1]
	n = len(fractions)
	for row in doc.entries:
		counts = _counts(row, n)
		# Waivers on band columns that are no longer configured are dropped.
		waived = clamp_waived(counts, _waived(row, n))
		_set_waived(row, waived)
		row.waived_marks = sum(waived)
		if not row.waived_marks:
			if flt(row.get("amount_before_waiver")) > 0:
				row.amount = flt(row.amount_before_waiver)
			row.waived_amount = 0
			row.amount_before_waiver = 0
			row.waiver_reason = None
			continue
		if not (row.waiver_reason or "").strip():
			frappe.throw(
				_("Row {0} ({1}): enter a Waiver Reason for the waived late marks.").format(
					row.idx, row.employee
				)
			)
		before, after = waived_row_amount(counts, waived, fractions, row.per_day_rate, _base_amount(row))
		row.amount_before_waiver = before
		row.amount = after
		row.waived_amount = before - after


def _waiver_doc(docname):
	doc = frappe.get_doc("RAPL Late Mark Processing", docname)
	doc.check_permission("write")
	if doc.docstatus != 0:
		frappe.throw(_("Waivers can only be changed while the document is a draft."))
	return doc


def _target_rows(doc, employees):
	"""The rows a bulk action applies to: the named employees, or every row."""
	if isinstance(employees, str):
		employees = frappe.parse_json(employees) if employees.strip() else None
	if not employees:
		return list(doc.entries)
	wanted = set(employees)
	return [row for row in doc.entries if row.employee in wanted]


def _plan_waiver(doc, employees, mode, band, marks, combine):
	labels, fractions = _bands()
	n = len(fractions)
	if not n:
		frappe.throw(_("No late mark bands are configured in RAPL Payroll Automation Settings."))
	if mode not in ("band", "costliest", "cheapest"):
		frappe.throw(_("Choose where to waive the marks from."))
	band_index = None
	if mode == "band":
		band_index = cint(band) - 1
		if not 0 <= band_index < n:
			frappe.throw(_("Choose a valid band."))
	marks = cint(marks)
	if marks < 1:
		frappe.throw(_("Enter how many late marks to waive (1 or more)."))
	add = combine == "add"

	plan = []
	for row in _target_rows(doc, employees):
		counts = _counts(row, n)
		current = clamp_waived(counts, _waived(row, n))
		new = allocate_waiver(
			counts, fractions, marks, mode, band_index, existing=current if add else None
		)
		amount_now = round_half_up(flt(row.amount))
		applies = bool(sum(new))
		if not applies:
			# Nothing of this kind to waive for this employee (e.g. no marks in
			# the chosen band). Leave the row -- including any earlier waiver --
			# exactly as it is; removing waivers is "Clear Waiver"'s job.
			new, amount_after = current, amount_now
		else:
			base = _base_amount(row)
			amount_after = waived_row_amount(counts, new, fractions, row.per_day_rate, base)[1]
		plan.append({
			"row": row,
			"employee": row.employee,
			"employee_name": row.employee_name,
			"counts": counts,
			"waived_before": current,
			"waived_after": new,
			"amount_now": amount_now,
			"amount_after": amount_after,
			"saving": amount_now - amount_after,
			"changed": new != current,
			"waives": applies and new != current,
			"applies": applies,
		})
	return labels, plan


def _public(labels, plan):
	rows = [{k: v for k, v in p.items() if k not in ("row", "applies")} for p in plan]
	for r in rows:
		r["changed"] = r.pop("waives")
	return {
		"labels": labels,
		"rows": rows,
		"total_now": sum(p["amount_now"] for p in plan),
		"total_after": sum(p["amount_after"] for p in plan),
		"changed": sum(1 for p in plan if p["waives"]),
	}


@frappe.whitelist()
def preview_waiver(docname, mode, marks, band=None, combine="replace", employees=None):
	"""What a waiver WOULD do to each row, without saving anything."""
	doc = _waiver_doc(docname)
	labels, plan = _plan_waiver(doc, employees, mode, band, marks, combine)
	return _public(labels, plan)


@frappe.whitelist(methods=["POST"])
def apply_waiver(docname, mode, marks, reason, band=None, combine="replace", employees=None):
	"""Apply a waiver to the chosen rows (or every row) and save the draft."""
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Enter a reason for the waiver."))
	doc = _waiver_doc(docname)
	labels, plan = _plan_waiver(doc, employees, mode, band, marks, combine)
	for p in plan:
		if not p["applies"]:
			continue
		row = p["row"]
		# Remember the pre-waiver amount before the first waiver lowers it.
		row.amount_before_waiver = _base_amount(row)
		_set_waived(row, p["waived_after"])
		row.waiver_reason = reason   # also refreshed when the counts were already this
	doc.save()
	return _public(labels, plan)


@frappe.whitelist(methods=["POST"])
def clear_waiver(docname, employees=None):
	"""Remove the waiver from the chosen rows (or every row); amounts go back
	to the normal sum(count x fraction) x Per-Day Rate."""
	doc = _waiver_doc(docname)
	labels, fractions = _bands()
	n = len(fractions)
	cleared = 0
	for row in _target_rows(doc, employees):
		if not cint(row.waived_marks) and not any(_waived(row, MAX_BANDS)):
			continue
		row.amount = _base_amount(row)   # back to the pre-waiver amount
		_set_waived(row, [])
		row.amount_before_waiver = 0
		cleared += 1
	if cleared:
		doc.save()
	return {"cleared": cleared}
