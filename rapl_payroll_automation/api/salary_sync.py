# Copyright (c) 2026, RAPL and contributors
"""Employee > Monthly Gross Salary / Basic / HRA / Salary Structure / CTC,
kept in step with the Salary Structure Assignment.

Since October 2026 pay is decided by Base on the Salary Structure Assignment.
These Employee fields are read-only copies for HR to see (and for the app's
pre-October fallback): never typed, always filled from here.

  custom_monthly_salary    Base of the assignment in force TODAY (gross,
                           before PF/PT/ESI; labelled "Monthly Gross Salary")
  custom_basic             that assignment's full-month Basic
  custom_hra               that assignment's full-month HRA
  custom_salary_structure  that assignment's Salary Structure (list filter)
  ctc                      that assignment's yearly CTC (standard Employee
                           field). Employee Promotion also writes it (Revised
                           CTC); the next sync puts the assignment's figure
                           back -- the assignment is what pays.

Basic and HRA come from HRMS's own evaluation of the assignment's salary
structure (SalaryStructureAssignment._evaluate_all_components, present in
HRMS v16.50 and later), so a new structure needs no code change here.

Refreshed when an assignment is submitted or cancelled, and daily, so a
future-dated raise reaches the Employee on its From Date. An employee whose
assignment in force has no Base (the pre-October assignments, Base 0) is left
untouched: for them the Employee field still is the salary.
"""

import frappe
from frappe.utils import flt, getdate, today

from rapl_payroll_automation.api.payroll_automation_utils import get_automation_settings

# The three site Custom Fields made read-only by patch v0_5.
COPY_FIELDS = ("custom_monthly_salary", "custom_basic", "custom_hra")
# Everything the sync writes (each only if Employee has the field).
FIELDS = COPY_FIELDS + ("custom_salary_structure", "ctc")


def _assignment_in_force(employee, on_date):
	fields = ["name", "base", "salary_structure"]
	# CTC on the assignment is newer HRMS; read it only where the column exists.
	if frappe.get_meta("Salary Structure Assignment").has_field("ctc"):
		fields.append("ctc")
	rows = frappe.get_all(
		"Salary Structure Assignment",
		filters={"employee": employee, "docstatus": 1, "from_date": ["<=", on_date]},
		fields=fields,
		order_by="from_date desc, creation desc",
		limit=1,
	)
	return rows[0] if rows else None


def _component_amount(rows, names):
	"""Full-month amount of the first earnings row matching a component name or
	its abbreviation (Settings may hold either)."""
	names = {n for n in names if n}
	for row in rows:
		if row.get("salary_component") in names or row.get("abbr") in names:
			return flt(row.get("default_amount"))
	return None


def salary_values(employee, on_date=None):
	"""{field: value} for the Employee copy fields, or None to leave them."""
	ssa = _assignment_in_force(employee, getdate(on_date or today()))
	if not ssa or flt(ssa.base) <= 0:
		return None
	values = {"custom_monthly_salary": flt(ssa.base)}
	if ssa.get("salary_structure"):
		values["custom_salary_structure"] = ssa.salary_structure
	if flt(ssa.get("ctc")) > 0:
		values["ctc"] = flt(ssa.ctc)
	try:
		doc = frappe.get_doc("Salary Structure Assignment", ssa.name)
		_data, rows_by_type = doc._evaluate_all_components()
		earnings = rows_by_type.get("earnings") or []
		settings = get_automation_settings()
		basic = _component_amount(earnings, (settings.get("basic_salary_component") or "Basic", "Basic", "B"))
		hra = _component_amount(
			earnings, (settings.get("hra_salary_component") or "HRA", "House Rent Allowance", "HRA")
		)
		if basic is not None:
			values["custom_basic"] = basic
		if hra is not None:
			values["custom_hra"] = hra
	except Exception:
		# Monthly salary is still right; Basic/HRA stay as they were.
		frappe.log_error(frappe.get_traceback(), f"RAPL: Basic/HRA not computed for {employee}")
		return values
	# An assignment saved before HRMS computed CTC has 0 stored: compute it in
	# memory (nothing is saved on the assignment). A failure here only skips CTC.
	if "ctc" not in values and hasattr(doc, "calculate_ctc_and_gross"):
		try:
			doc.calculate_ctc_and_gross()
			if flt(doc.get("ctc")) > 0:
				values["ctc"] = flt(doc.ctc)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"RAPL: CTC not computed for {employee}")
	return values


def sync_employee(employee, on_date=None):
	"""Write the values if they changed. Returns True when something changed."""
	values = salary_values(employee, on_date)
	if not values:
		return False
	meta = frappe.get_meta("Employee")
	values = {k: v for k, v in values.items() if meta.has_field(k)}
	current = frappe.db.get_value("Employee", employee, list(values), as_dict=True) or {}
	changed = {k: v for k, v in values.items() if _differs(current.get(k), v)}
	if changed:
		frappe.db.set_value("Employee", employee, changed, update_modified=False)
	return bool(changed)


def _differs(old, new):
	"""Numbers compare as numbers (17765 == 17765.0), text as text."""
	if isinstance(new, (int, float)):
		return flt(old) != flt(new)
	return (old or "") != (new or "")


def on_assignment_change(doc, method=None):
	"""Salary Structure Assignment on_submit / on_cancel."""
	sync_employee(doc.employee)


def sync_all():
	"""Daily: every employee who is not Inactive (a raise dated today lands)."""
	changed = 0
	for emp in frappe.get_all("Employee", filters={"status": ["!=", "Inactive"]}, pluck="name"):
		try:
			changed += 1 if sync_employee(emp) else 0
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"RAPL: salary sync failed for {emp}")
	return changed
