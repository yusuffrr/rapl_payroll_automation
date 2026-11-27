# Copyright (c) 2026, RAPL and contributors
"""Payroll Entry named after the month it pays, not the day it was created.

    PE-<MM>/<fiscal year>-<n>     e.g. October 2026 -> PE-10/2026-2027-1

MM is the month of the Payroll Entry's Start Date (Posting Date if no Start
Date). The site's own naming expression put the CREATION month there, so a
late October payroll made in November came out as PE-11/... .

Wired as the Payroll Entry "autoname" doc event. Frappe's set_new_name()
(frappe/model/naming.py, v16) runs, in order: amended name (kept: "-1"
suffix), Document Naming Rule, the autoname doc event (this), then the
DocType's autoname expression. A Document Naming Rule for Payroll Entry would
therefore still win -- there should be none.
"""

import frappe
from frappe.utils import getdate, today


def fiscal_year_name(date, company=None):
	"""ERPNext Fiscal Year name for the date (e.g. "2026-2027"); April-March
	calendar fallback when ERPNext has none."""
	try:
		from erpnext.accounts.utils import get_fiscal_year

		return get_fiscal_year(date, company=company)[0]
	except Exception:
		d = getdate(date)
		start = d.year if d.month >= 4 else d.year - 1
		return f"{start}-{start + 1}"


def name_prefix(doc):
	d = getdate(doc.get("start_date") or doc.get("posting_date") or today())
	return f"PE-{d.month:02d}/{fiscal_year_name(d, doc.get('company'))}-"


def _next_name(prefix):
	from frappe.model.naming import make_autoname

	return make_autoname(prefix + ".#", "Payroll Entry")


def autoname(doc, method=None):
	if doc.get("amended_from"):
		return  # Frappe already named it <original>-1
	prefix = name_prefix(doc)
	name = _next_name(prefix)
	# Series counters can lag behind names made by renaming: skip taken ones.
	for _ in range(100):
		if not frappe.db.exists("Payroll Entry", name):
			break
		name = _next_name(prefix)
	doc.name = name
