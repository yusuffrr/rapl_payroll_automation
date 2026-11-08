"""Order-cost rollout, step 6: Travel Request and Expense Claim fields.

Follows this app's pattern (see v0_2/add_manual_override_fields): create_custom_fields,
idempotent, post_model_sync. No existing document is edited.

Trial fields (Sep–Oct): Travel Request "Project" (custom_project) is reused if it exists.
Employee Advance "Advance Type" and "Project", and Travel Request "Installation Note",
are removed if empty. The trial field Expense Claim Detail "Paid By" (custom_paid_by) is hidden (decision 6,
9 Nov): who pays is decided on the order and applied at billing.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Travel Request": [
				{
					"fieldname": "custom_sales_order",
					"label": "Sales Order",
					"fieldtype": "Link",
					"options": "Sales Order",
					"insert_after": "custom_client",
					"description": "The order this trip is for. Its Project is linked automatically.",
				},
				{
					"fieldname": "custom_project",
					"label": "Project",
					"fieldtype": "Link",
					"options": "Project",
					"insert_after": "custom_sales_order",
					"read_only": 1,
					"no_copy": 1,
				},
				{
					"fieldname": "custom_paid_via",
					"label": "Paid via",
					"fieldtype": "Select",
					"options": "\nIRCTC wallet\nSupplier invoice\nCompany card\nPersonal claim",
					"insert_after": "custom_ticket_price",
					"mandatory_depends_on": "eval:flt(doc.custom_ticket_price) > 0",
					"description": "How the ticket was paid. Supplier invoice and Company card tickets go on a Purchase Invoice (Create → Ticket Purchase Invoice).",
				},
			],
			"Expense Claim Detail": [
				{
					"fieldname": "custom_sales_order",
					"label": "Sales Order",
					"fieldtype": "Link",
					"options": "Sales Order",
					"insert_after": "cost_center",
					"in_list_view": 1,
					"columns": 2,
					"description": "The order this expense was for. Its Project is linked automatically.",
				},
				{
					"fieldname": "custom_travel_request",
					"label": "Travel Request",
					"fieldtype": "Link",
					"options": "Travel Request",
					"insert_after": "custom_paid_by",
					"description": "For a ticket paid personally: the trip it was for.",
				},
			],
		},
		update=True,
	)
	_hide_paid_by()
	# Fields from the Sep–Oct trial that the final design doesn't use. Decision D:
	# advances aren't tagged to a job. The trip's link is the Sales Order, not the note.
	for doctype, fieldname in (
		("Employee Advance", "custom_advance_type"),
		("Employee Advance", "custom_project"),
		("Travel Request", "custom_installation_note"),
	):
		_drop_unused_trial_field(doctype, fieldname)


def _drop_unused_trial_field(doctype, fieldname):
	"""Remove only if no document holds a value. Nothing reads these fields (checked 8 Nov)."""
	name = f"{doctype}-{fieldname}"
	if not frappe.db.exists("Custom Field", name):
		return
	if frappe.db.has_column(doctype, fieldname) and frappe.db.sql(
		f"SELECT 1 FROM `tab{doctype}` WHERE IFNULL(`{fieldname}`, '') NOT IN ('', '0') LIMIT 1"
	):
		return
	frappe.delete_doc("Custom Field", name, ignore_permissions=True)


def _hide_paid_by():
	"""Decision 6 (9 Nov): no per-line Paid By. Who pays is decided on the order (Travel /
	Food / Accommodation) and applied at billing. The trial field is hidden, and any old
	values stay where they are."""
	if not frappe.db.exists("Custom Field", "Expense Claim Detail-custom_paid_by"):
		return
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	make_property_setter("Expense Claim Detail", "custom_paid_by", "hidden", "1", "Check")
	make_property_setter("Expense Claim Detail", "custom_paid_by", "in_list_view", "0", "Check")
