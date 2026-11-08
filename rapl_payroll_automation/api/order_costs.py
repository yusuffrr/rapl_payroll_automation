"""Order costs on HR documents (order-cost rollout, step 6).

Travel Request and Expense Claim lines that name a Sales Order are linked to that
order's Project. The Project is found or created by frappe_attachment_plus
(api/order_project.get_or_create_project), called through frappe.get_attr so this
app doesn't import the other directly.

D  Advances stay with the engineer and roll over. Nothing here touches Employee
   Advance; the outstanding-balance logic in api/employee_advance.py is unchanged.
   Each claim LINE names its order.
2  "Paid By" on a claim line fills from the trip's Travel Type, else from the
   order's "Travel, Boarding & Lodging" answer. The engineer doesn't choose it.
3  No ₹0 lines for expenses the client paid directly: "Client paid directly" is no
   longer offered.
X  A trip or claim line on an order that doesn't have the two installation answers
   yet (older orders) asks for them first.
F  Tickets (middle path): "Paid via" on the Travel Request is required when it has a
   Ticket Price. Supplier and card tickets go on a Purchase Invoice made from the
   Travel Request (make_ticket_purchase_invoice). Wallet tickets count from Ticket
   Price. A Travel Request can be on only one live Purchase Invoice.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt

PAID_VIA = ("IRCTC wallet", "Supplier invoice", "Company card", "Personal claim")
PAID_VIA_INVOICED = ("Supplier invoice", "Company card")

NOT_BILLABLE = "Rinix - not billable"
BILL_TO_CLIENT = "Rinix - bill to client"

# Expense Claim Types on this site that are travel, boarding or lodging
# (Expense Claim Type names, matched by their first word).
TRAVEL_TYPE_PREFIXES = ("Travel", "Accommodation", "Food")

# Sales Order "Travel, Boarding & Lodging" → claim line "Paid By"
ORDER_ANSWER_TO_PAID_BY = {
	"Rinix bears": NOT_BILLABLE,
	"Client reimburses": BILL_TO_CLIENT,
	"Client arranges directly": NOT_BILLABLE,  # client books it; anything still claimed is Rinix's
}
# Travel Request "Travel Type" (this site's options) → claim line "Paid By"
TRIP_TYPE_TO_PAID_BY = {
	"Client will Pay": BILL_TO_CLIENT,
	"Free of Cost": NOT_BILLABLE,
}
# Order answer → the Travel Type a trip would normally carry (used only for a warning)
ORDER_ANSWER_TO_TRIP_TYPE = {
	"Rinix bears": "Free of Cost",
	"Client reimburses": "Client will Pay",
	"Client arranges directly": "Client will Pay",
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def is_travel_expense(expense_type):
	return bool(expense_type) and expense_type.split(" ")[0] in TRAVEL_TYPE_PREFIXES


def paid_by_for(expense_type, order_answer, trip_travel_type=None):
	"""Claim line "Paid By". Non-travel expenses are Rinix's. For travel, boarding and
	lodging: the trip's Travel Type wins when it says who pays (it's the per-trip
	answer your team already sets today); otherwise the order's answer."""
	if not is_travel_expense(expense_type):
		return NOT_BILLABLE
	if trip_travel_type in TRIP_TYPE_TO_PAID_BY:
		return TRIP_TYPE_TO_PAID_BY[trip_travel_type]
	return ORDER_ANSWER_TO_PAID_BY.get(order_answer, NOT_BILLABLE)


# ---------------------------------------------------------------------------
# Bridge to frappe_attachment_plus
# ---------------------------------------------------------------------------


def _fap(name):
	if "frappe_attachment_plus" not in frappe.get_installed_apps():
		return None
	return frappe.get_attr(f"frappe_attachment_plus.api.order_project.{name}")


def _project_for(sales_order):
	fn = _fap("get_or_create_project")
	return fn(sales_order) if fn else None


def _recompute(projects):
	fn = _fap("recompute_order_costs")
	if fn:
		for p in {p for p in projects if p}:
			fn(p)


def _order_terms(sales_order):
	so = frappe.db.get_value(
		"Sales Order",
		sales_order,
		["docstatus", "customer", "custom_installation_charge", "custom_travel_lodging"],
		as_dict=True,
	)
	if not so:
		frappe.throw(_("Sales Order {0} not found.").format(sales_order))
	if so.docstatus != 1:
		frappe.throw(_("Sales Order {0} isn't submitted.").format(sales_order))
	if not so.custom_installation_charge or not so.custom_travel_lodging:
		frappe.throw(
			_(
				"Sales Order {0} doesn't say who pays for installation and travel yet. "
				"Fill <b>Installation Charge</b> and <b>Travel, Boarding & Lodging</b> on the order first."
			).format(sales_order),
			title=_("Order terms missing"),
		)
	return so


# ---------------------------------------------------------------------------
# Travel Request
# ---------------------------------------------------------------------------


def travel_request_validate(doc, method=None):
	if flt(doc.get("custom_ticket_price")) > 0 and not doc.get("custom_paid_via"):
		frappe.throw(_("Set <b>Paid via</b>: how was this ticket paid?"), title=_("Ticket"))

	if not doc.get("custom_sales_order"):
		doc.custom_project = None
		return

	so = _order_terms(doc.custom_sales_order)
	if not doc.get("custom_client"):
		doc.custom_client = so.customer
	doc.custom_project = _project_for(doc.custom_sales_order)

	expected = ORDER_ANSWER_TO_TRIP_TYPE.get(so.custom_travel_lodging)
	if expected and doc.get("travel_type") and doc.travel_type != expected and doc.travel_type in TRIP_TYPE_TO_PAID_BY:
		# H (trip overriding the order) is still open, so this warns and doesn't block.
		frappe.msgprint(
			_("Order {0} says travel is \"{1}\", but this trip's Travel Type is \"{2}\".").format(
				doc.custom_sales_order, so.custom_travel_lodging, doc.travel_type
			),
			indicator="orange",
			alert=True,
		)


def travel_request_on_change(doc, method=None):
	_recompute([doc.get("custom_project")])


@frappe.whitelist()
def make_ticket_purchase_invoice(source_name, target_doc=None):
	"""Draft Purchase Invoice for supplier, airline or card tickets.

	`source_name` is one Travel Request, or a JSON list of them (list view action).
	A shared booking becomes one invoice with a line per Travel Request, each at that
	person's Ticket Price. Accounts pick the supplier and add the supplier's invoice
	number and GST before submitting."""
	names = json.loads(source_name) if str(source_name).strip().startswith("[") else [source_name]
	if not frappe.has_permission("Purchase Invoice", "create"):
		frappe.throw(_("You don't have permission to create Purchase Invoices."), frappe.PermissionError)

	fap_settings = frappe.get_attr(
		"frappe_attachment_plus.frappe_attachment_plus.doctype.order_cost_settings.order_cost_settings.get_settings"
	)()
	item = fap_settings.ticket_item
	if not item:
		frappe.throw(_("Set <b>Ticket Item</b> in Order Cost Settings first."))

	if isinstance(target_doc, str):
		target_doc = json.loads(target_doc)
	pi = frappe.get_doc(target_doc) if target_doc else frappe.new_doc("Purchase Invoice")

	item_name, stock_uom = frappe.db.get_value("Item", item, ["item_name", "stock_uom"])
	for name in names:
		tr = frappe.get_doc("Travel Request", name)
		tr.check_permission("read")
		if tr.docstatus != 1:
			frappe.throw(_("{0} isn't submitted yet.").format(name))
		if tr.get("custom_paid_via") not in PAID_VIA_INVOICED:
			frappe.throw(
				_("{0} is paid via \"{1}\". Only supplier or company-card tickets go on a Purchase Invoice.").format(
					name, tr.get("custom_paid_via") or _("not set")
				)
			)
		if flt(tr.get("custom_ticket_price")) <= 0:
			frappe.throw(_("{0} has no Ticket Price.").format(name))
		existing = _invoice_for_trip(name)
		if existing:
			frappe.throw(_("{0} is already on Purchase Invoice {1}.").format(name, existing))
		if any(r.get("custom_travel_request") == name for r in pi.get("items", [])):
			continue
		if tr.get("company") and not pi.get("company"):
			pi.company = tr.company
		pi.append(
			"items",
			{
				"item_code": item,
				"item_name": item_name,
				"uom": stock_uom,
				"stock_uom": stock_uom,
				"conversion_factor": 1,
				"qty": 1,
				"rate": flt(tr.custom_ticket_price),
				"description": _("Ticket: {0}, {1} → {2} on {3} ({4})").format(
					tr.employee_name or tr.employee,
					tr.get("custom_source") or "",
					tr.get("custom_to") or "",
					frappe.utils.formatdate(tr.get("custom_date_of_travel")) if tr.get("custom_date_of_travel") else "",
					tr.name,
				),
				"custom_travel_request": tr.name,
				"custom_sales_order": tr.get("custom_sales_order"),
				"project": tr.get("custom_project"),
			},
		)
	return pi


def _invoice_for_trip(travel_request, exclude=None):
	rows = frappe.db.sql(
		"""
		SELECT pi.name FROM `tabPurchase Invoice Item` pii
		JOIN `tabPurchase Invoice` pi ON pi.name = pii.parent
		WHERE pii.custom_travel_request = %s AND pi.docstatus < 2 AND pi.name != %s
		LIMIT 1
		""",
		(travel_request, exclude or ""),
	)
	return rows[0][0] if rows else None


def purchase_invoice_validate(doc, method=None):
	"""A Travel Request can be on only one live Purchase Invoice, and its line carries the
	trip's order and Project."""
	for row in doc.items:
		name = row.get("custom_travel_request")
		if not name:
			continue
		other = _invoice_for_trip(name, exclude=doc.name)
		if other:
			frappe.throw(_("Row #{0}: {1} is already on Purchase Invoice {2}.").format(row.idx, name, other))
		so, project = frappe.db.get_value("Travel Request", name, ["custom_sales_order", "custom_project"])
		if so and not row.get("custom_sales_order"):
			row.custom_sales_order = so
		if project and not row.get("project"):
			row.project = project


# ---------------------------------------------------------------------------
# Expense Claim
# ---------------------------------------------------------------------------


def expense_claim_validate(doc, method=None):
	for row in doc.expenses:
		trip_type = None
		if row.get("custom_travel_request"):
			trip_so, trip_type = frappe.db.get_value(
				"Travel Request", row.custom_travel_request, ["custom_sales_order", "travel_type"]
			)
			if trip_so and not row.get("custom_sales_order"):
				row.custom_sales_order = trip_so

		if not row.get("custom_sales_order"):
			if not row.get("custom_paid_by"):
				row.custom_paid_by = NOT_BILLABLE
			continue

		order_answer = None
		if is_travel_expense(row.expense_type):
			order_answer = _order_terms(row.custom_sales_order).custom_travel_lodging
		row.project = _project_for(row.custom_sales_order) or row.get("project")
		row.custom_paid_by = paid_by_for(row.expense_type, order_answer, trip_type)


def expense_claim_on_change(doc, method=None):
	_recompute([row.get("project") for row in doc.expenses])
