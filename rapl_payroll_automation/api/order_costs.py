"""Order costs on HR documents (order-cost rollout, step 6; revised 9 Nov).

Travel Request and Expense Claim lines that name a Sales Order are linked to that
order's Project. The Project is found or created by frappe_attachment_plus
(api/order_project.get_or_create_project), called through frappe.get_attr so this
app doesn't import the other directly.

D  Advances stay with the engineer and roll over. Nothing here touches Employee
   Advance; the outstanding-balance logic in api/employee_advance.py is unchanged.
   Each claim LINE names its order.
5  Trips and claims are NEVER blocked by the order's installation or site-cost answers.
   Those can be decided later; frappe_attachment_plus works out the client's share
   at billing.
6  No automatic "Paid By" on claim lines. Who pays is decided on the order (Travel /
   Food / Accommodation), so late decisions and splits work. The trial field is hidden.
7  Open: how to assign a ticket when one engineer goes straight from one client to
   another, or several engineers and clients overlap. Until decided, a trip belongs to
   whichever order is picked on it.
F  Tickets (middle path): "Paid via" is required when a Travel Request has a Ticket Price.
   Supplier and card tickets go on a Purchase Invoice made from the Travel Request.
   Wallet tickets count from Ticket Price. A Travel Request can be on only one live
   Purchase Invoice.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt

PAID_VIA = ("IRCTC wallet", "Supplier invoice", "Company card", "Personal claim")
PAID_VIA_INVOICED = ("Supplier invoice", "Company card")


# ---------------------------------------------------------------------------
# Bridge to frappe_attachment_plus
# ---------------------------------------------------------------------------


def _fap(path):
	if "frappe_attachment_plus" not in frappe.get_installed_apps():
		return None
	return frappe.get_attr(f"frappe_attachment_plus.{path}")


def _project_for(sales_order):
	fn = _fap("api.order_project.get_or_create_project")
	return fn(sales_order) if fn else None


def _refresh(projects):
	fn = _fap("api.order_project.recompute_order_costs")
	if fn:
		for p in {p for p in projects if p}:
			fn(p)


# ---------------------------------------------------------------------------
# Travel Request
# ---------------------------------------------------------------------------


def travel_request_validate(doc, method=None):
	if flt(doc.get("custom_ticket_price")) > 0 and not doc.get("custom_paid_via"):
		frappe.throw(_("Set <b>Paid via</b>: how was this ticket paid?"), title=_("Ticket"))

	if not doc.get("custom_sales_order"):
		doc.custom_project = None
		return

	if not doc.get("custom_client"):
		doc.custom_client = frappe.db.get_value("Sales Order", doc.custom_sales_order, "customer")
	doc.custom_project = _project_for(doc.custom_sales_order)


def travel_request_on_change(doc, method=None):
	_refresh([doc.get("custom_project")])


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

	settings = _fap(
		"frappe_attachment_plus.doctype.order_cost_settings.order_cost_settings.get_settings"
	)
	item = settings().ticket_item if settings else None
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
		if row.get("custom_travel_request") and not row.get("custom_sales_order"):
			row.custom_sales_order = frappe.db.get_value(
				"Travel Request", row.custom_travel_request, "custom_sales_order"
			)
		if row.get("custom_sales_order"):
			row.project = _project_for(row.custom_sales_order) or row.get("project")


def expense_claim_on_change(doc, method=None):
	_refresh([row.get("project") for row in doc.expenses])
