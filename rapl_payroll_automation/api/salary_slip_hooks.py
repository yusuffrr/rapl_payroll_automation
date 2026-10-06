# Copyright (c) 2026, RAPL and contributors
#
# Salary Slip hooks: two, registered as:
#   doc_events = {
#       "Salary Slip": {
#           "before_validate": "rapl_payroll_automation.api.salary_slip_hooks.set_precomputed_fields",
#           "validate": "rapl_payroll_automation.api.salary_slip_hooks.correct_statutory_deductions",
#       }
#   }
#
# ARCHITECTURE CHANGE (post a same-day HRMS update) -- see git history / commit
# messages for the full investigation. Confirmed via a real production
# traceback: HRMS now evaluates every Salary Structure formula EARLIER than
# before, inside Salary Structure Assignment's own
# _evaluate_component_table() (salary_structure_assignment.py), using a data
# context built ONLY from Employee + Salary Structure Assignment fields --
# it never sees the Salary Slip at all. The previous design (set a custom
# field on the Salary Slip via before_validate, reference it directly in the
# PF/PT/ESI formula text) is no longer viable: that formula-evaluation
# environment has no way to ever see anything set on the Salary Slip,
# regardless of hook timing.
#
# NEW APPROACH: let PF/PT/ESI's Salary Component formulas revert to
# referencing only B/HRA (safe everywhere, crashes nowhere, since it
# references nothing outside what's natively available in every evaluation
# context). Then, AFTER Salary Slip's own validate()/calculate_net_pay() has
# already run (a plain `validate` doc_event, confirmed via
# frappe/model/document.py's Document.hook()/compose(): the doctype's own
# method runs first, doc_events hooks run after), directly RECOMPUTE the
# correct PF/PT/ESI amounts in plain Python and overwrite those rows'
# `amount` field on the actual Salary Slip -- bypassing the formula sandbox
# entirely, so none of this depends on HRMS's internal evaluation-context
# implementation ever again.
#
# custom_overtime_for_pt / custom_conveyance_for_deductions (Currency fields
# on Salary Slip) are still populated by before_validate, purely for
# visibility/audit on the slip -- they are no longer referenced by any
# formula, only by this module's own Python logic below.
#
# Corrected values, replicated in Python (must match RAPL_Component_
# Reconciliation.md's locked formulas exactly):
#   PF:  1800 if (B+CON)>15000 else (B+CON)*0.12         -- condition: custom_pf==1
#   PT:  5-band gender/month logic on (B+HRA+CON+OT)      -- condition: custom_pt==1
#   ESI: (B+HRA+CON+OT)*0.0075                            -- condition: custom_esi==1
# where CON = custom_conveyance_for_deductions, OT = custom_overtime_for_pt,
# B/HRA = this slip's own Basic/HRA earning row amounts. Component NAMES
# ("PF"/"Professional Tax"/"ESI") are NOT hardcoded -- read from
# RAPL Payroll Automation Settings (pf_salary_component/pt_salary_component/
# esi_salary_component), same configurable pattern already used for Overtime/
# Late Mark, to avoid a repeat of the exact-name-mismatch bugs hit earlier in
# this build (e.g. the Attendance custom_late_deduction_fraction typo).
#
# set_net_pay() (called at the end here) safely resums gross_pay/
# total_deduction/net_pay/rounded_total from CURRENT row amounts via
# get_component_totals() -- verified this is a pure summation over existing
# `.amount` values, NOT a re-trigger of formula evaluation, so it cannot
# undo the correction just applied.

import frappe
from frappe.utils import flt, getdate

from rapl_payroll_automation.api.payroll_math import esi_employee_contribution, round_half_up

from rapl_payroll_automation.api.payroll_automation_utils import (
	get_additional_salary_total,
	get_automation_settings,
)


def set_precomputed_fields(doc, method):
	"""before_validate -- populates the two reference/audit fields on the slip.
	No longer read by any formula (see module docstring); kept for visibility."""
	if not doc.employee or not doc.start_date or not doc.end_date:
		return

	settings = get_automation_settings()

	doc.custom_overtime_for_pt = get_additional_salary_total(
		doc.employee, settings.overtime_salary_component, doc.start_date, doc.end_date
	)
	doc.custom_conveyance_for_deductions = get_additional_salary_total(
		doc.employee, "Conveyance", doc.start_date, doc.end_date
	)


def correct_statutory_deductions(doc, method):
	"""validate -- runs AFTER Salary Slip's own calculate_net_pay() has already
	completed (confirmed hook-ordering, see module docstring). Overwrites
	PF/PT/ESI row amounts directly with the correct Conveyance+Overtime-
	inclusive figures, computed here in plain Python, then safely resums
	totals via set_net_pay()."""
	if not doc.employee or not doc.start_date:
		return

	settings = get_automation_settings()
	# Component names from Settings (defaults "Basic" / "HRA"). These were
	# literal strings: a component named anything else read as 0, so PT / ESI
	# were computed on a lower income without any warning.
	basic = _get_earning_amount(doc, settings.get("basic_salary_component") or "Basic")
	hra = _get_earning_amount(doc, settings.get("hra_salary_component") or "HRA")
	# Overtime and Conveyance as PAID on this slip -- their earnings rows,
	# after any proration by payment days -- like Basic and HRA above.
	# PF/PT/ESI are due on wages actually paid; the raw Additional Salary
	# totals overstated them whenever those components are prorated. The
	# totals are only the fallback for a slip without the row.
	overtime = _row_or(doc, settings.get("overtime_salary_component"), doc.custom_overtime_for_pt)
	conveyance = _row_or(doc, "Conveyance", doc.custom_conveyance_for_deductions)

	emp = frappe.db.get_value(
		"Employee", doc.employee, ["gender", "custom_pf", "custom_pt", "custom_esi"], as_dict=True
	)
	if not emp:
		return

	changed = False

	if emp.custom_pf:
		pf_base = basic + conveyance
		pf_amount = 1800 if pf_base > 15000 else pf_base * 0.12
		changed = _set_deduction_amount(doc, settings.pf_salary_component, pf_amount) or changed

	if emp.custom_pt:
		pt_amount = _compute_pt(emp.gender, basic, hra, conveyance, overtime, doc.start_date)
		changed = _set_deduction_amount(doc, settings.pt_salary_component, pt_amount) or changed

	if emp.custom_esi:
		# ESIC rule: 0.75% of wages, rounded UP to the next whole rupee
		# (113.06 -> 114), not to the nearest rupee.
		esi_amount = esi_employee_contribution(basic + hra + conveyance + overtime)
		changed = _set_deduction_amount(
			doc, settings.esi_salary_component, esi_amount, rounding=int
		) or changed

	if changed:
		doc.set_net_pay()
		# HRMS computes the year/month-to-date figures inside its own
		# validate(), BEFORE this hook runs, so they still hold the
		# pre-correction amounts. Recompute them from the corrected rows.
		for method in ("compute_year_to_date", "compute_month_to_date",
					   "compute_component_wise_year_to_date"):
			if hasattr(doc, method):
				getattr(doc, method)()


def _row_or(doc, component_name, fallback):
	"""Sum of EVERY earnings row for the component (HRMS gives each
	non-overwrite Additional Salary its own row); the fallback only when the
	slip has none."""
	rows = [flt(r.amount) for r in doc.earnings if component_name and r.salary_component == component_name]
	return sum(rows) if rows else flt(fallback)


def _get_earning_amount(doc, component_name):
	for row in doc.earnings:
		if row.salary_component == component_name:
			return flt(row.amount)
	return 0.0


def _set_deduction_amount(doc, component_name, amount, rounding=None):
	"""Overwrites the matching deduction row's amount, rounded to whole rupees
	(matches RAPL's confirmed 'Round to Nearest Integer' convention on these
	three components). Returns True if a matching row was found and updated."""
	# Half-up, not Python's round(): round() is banker's rounding
	# (round(1234.5) == 1234, round(1235.5) == 1236).
	amount = rounding(amount) if rounding else round_half_up(amount)
	for row in doc.deductions:
		if row.salary_component == component_name:
			row.amount = amount
			return True
	return False


def _compute_pt(gender, basic, hra, conveyance, overtime, start_date):
	income = basic + hra + conveyance + overtime
	month = getdate(start_date).month
	if gender == "Male":
		if income > 10000:
			return 300 if month == 2 else 200
		elif income > 7500:
			return 175
		return 0
	elif gender == "Female":
		if income > 25000:
			return 300 if month == 2 else 200
		return 0
	# Any other / blank gender: PT slab is undefined -- record it rather than
	# silently deducting nothing.
	frappe.msgprint(
		f"Professional Tax not computed: gender '{gender or '(blank)'}' has no PT slab.",
		indicator="orange", alert=True,
	)
	return 0
