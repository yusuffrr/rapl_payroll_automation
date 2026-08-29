import frappe
from frappe.utils import flt


def update_outstanding_balance(doc, method):
	"""Called on Employee Advance validate and on_update_after_submit."""
	doc.custom_outstanding_balance = (
		flt(doc.paid_amount) - flt(doc.claimed_amount) - flt(doc.return_amount)
	)


def update_outstanding_balance_from_payment_entry(doc, method):
	"""Called on Payment Entry on_submit and on_cancel."""
	for ref in doc.references:
		if ref.reference_doctype == "Employee Advance":
			_recalculate_outstanding(ref.reference_name)


def update_outstanding_balance_from_expense_claim(doc, method):
	"""Called on Expense Claim on_submit and on_cancel."""
	for advance in doc.advances:
		if advance.employee_advance:
			_recalculate_outstanding(advance.employee_advance)


def update_outstanding_balance_from_journal_entry(doc, method):
	"""Called on Journal Entry on_submit and on_cancel."""
	for row in doc.accounts:
		if row.reference_type == "Employee Advance" and row.reference_name:
			_recalculate_outstanding(row.reference_name)


def update_outstanding_balance_from_additional_salary(doc, method):
	"""Called on Additional Salary on_submit and on_cancel."""
	if doc.ref_doctype == "Employee Advance" and doc.ref_docname:
		_recalculate_outstanding(doc.ref_docname)


@frappe.whitelist()
def get_employee_advance_summary(employee, current_advance=None):
	"""Returns submitted EAs for the employee with outstanding balance > 0
	that were posted on or before the current advance, excluding the current one."""
	filters = {
		"employee": employee,
		"docstatus": 1,
	}

	if current_advance:
		filters["name"] = ["!=", current_advance]
		posting_date = frappe.db.get_value("Employee Advance", current_advance, "posting_date")
		if posting_date:
			filters["posting_date"] = ["<=", posting_date]

	advances = frappe.get_all(
		"Employee Advance",
		filters=filters,
		fields=[
			"name", "purpose", "posting_date",
			"paid_amount", "claimed_amount", "return_amount",
			"status", "currency",
		],
		order_by="posting_date asc",
	)

	result = []
	for d in advances:
		outstanding = flt(d.paid_amount) - flt(d.claimed_amount) - flt(d.return_amount)
		if outstanding > 0:
			d["custom_outstanding_balance"] = outstanding
			result.append(d)

	return result


def _recalculate_outstanding(ea_name):
	values = frappe.db.get_value(
		"Employee Advance",
		ea_name,
		["paid_amount", "claimed_amount", "return_amount"],
		as_dict=True,
	)
	if not values:
		return
	outstanding = flt(values.paid_amount) - flt(values.claimed_amount) - flt(values.return_amount)
	frappe.db.set_value("Employee Advance", ea_name, "custom_outstanding_balance", outstanding)
