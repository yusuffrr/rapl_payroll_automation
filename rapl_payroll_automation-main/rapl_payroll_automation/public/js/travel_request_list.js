// Travel Request list (order-cost rollout, step 6):
// a shared booking (several people, one supplier invoice) becomes one Purchase Invoice.

frappe.listview_settings["Travel Request"] = frappe.listview_settings["Travel Request"] || {};

(function () {
	const settings = frappe.listview_settings["Travel Request"];
	const previous_onload = settings.onload;

	settings.onload = function (listview) {
		if (previous_onload) previous_onload(listview);

		listview.page.add_actions_menu_item(__("Ticket Purchase Invoice"), () => {
			const names = listview.get_checked_items(true);
			if (!names.length) {
				frappe.msgprint(__("Select the Travel Requests on the same booking first."));
				return;
			}
			frappe.model.open_mapped_doc({
				method: "rapl_payroll_automation.api.order_costs.make_ticket_purchase_invoice",
				source_name: JSON.stringify(names),
			});
		});
	};
})();
