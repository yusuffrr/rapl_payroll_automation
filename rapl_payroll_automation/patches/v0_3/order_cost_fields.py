"""Order-cost rollout, step 6: Travel Request and Expense Claim fields.

Follows this app's pattern (see v0_2/add_manual_override_fields): create_custom_fields,
idempotent, post_model_sync. No existing document is edited.

Trial fields (Sep–Oct): Travel Request "Project" (custom_project) is reused if it exists.
Employee Advance "Advance Type" and "Project", and Travel Request "Installation Note",
are removed if empty. The trial field Expense Claim Detail "Paid By" (custom_paid_by) is reused. It keeps its
two Rinix options, drops "Client paid directly" (decision 3: no ₹0 lines for what
the client paid) unless some line already uses it, and becomes read-only (decision 2:
it fills from the trip or the order).
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
	_update_paid_by()
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


def _update_paid_by():
	name = "Expense Claim Detail-custom_paid_by"
	if not frappe.db.exists("Custom Field", name):
		return
	options = ["", "Rinix - not billable", "Rinix - bill to client"]
	if frappe.db.sql(
		"SELECT 1 FROM `tabExpense Claim Detail` WHERE custom_paid_by = 'Client paid directly' LIMIT 1"
	):
		options.append("Client paid directly")  # keep it so old lines stay valid
	field = frappe.get_doc("Custom Field", name)
	field.options = "\n".join(options)
	field.read_only = 1
	field.description = "Fills from the trip's Travel Type, else from the order's Travel, Boarding & Lodging."
	field.flags.ignore_permissions = True
	field.save()
