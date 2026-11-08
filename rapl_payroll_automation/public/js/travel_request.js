// Travel Request form (order-cost rollout, step 6).

frappe.ui.form.on("Travel Request", {
	custom_sales_order(frm) {
		if (!frm.doc.custom_sales_order || frm.doc.custom_client) return;
		frappe.db.get_value("Sales Order", frm.doc.custom_sales_order, "customer").then((r) => {
			if (r.message && r.message.customer) frm.set_value("custom_client", r.message.customer);
		});
	},

	refresh(frm) {
		if (
			frm.doc.docstatus === 1 &&
			["Supplier invoice", "Company card"].includes(frm.doc.custom_paid_via) &&
			flt(frm.doc.custom_ticket_price) > 0
		) {
			frm.add_custom_button(
				__("Ticket Purchase Invoice"),
				() =>
					frappe.model.open_mapped_doc({
						method: "rapl_payroll_automation.api.order_costs.make_ticket_purchase_invoice",
						frm,
					}),
				__("Create")
			);
		}
	},
});
