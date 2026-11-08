// Employee Advance form (order-cost rollout, step 6).
// Moved unchanged from Client Script "Employee Advance, Partial Payment Entry enabler",
// with one fix: Purpose and Status are HTML-escaped before going into the banner, since
// Purpose is free text.

frappe.ui.form.on("Employee Advance", {
	refresh: function(frm) {
		// --- existing: restore Payment Entry button when repay_unclaimed blocks it ---
		if (
			frm.doc.docstatus === 1 &&
			flt(frm.doc.paid_amount) < flt(frm.doc.advance_amount) &&
			frappe.model.can_create("Payment Entry") &&
			frm.doc.repay_unclaimed_amount_from_salary == 1 &&
			frm.doc.paid_amount
		) {
			frm.add_custom_button(
				__("Payment"),
				function () {
					frm.events.make_payment_entry(frm);
				},
				__("Create")
			);
		}

		// --- new: outstanding advances summary for this employee ---
		$(frm.wrapper).find(".outstanding-advances-summary").remove();

		if (!frm.doc.employee) return;

		frappe.call({
			method: "rapl_payroll_automation.api.employee_advance.get_employee_advance_summary",
			args: {
				employee: frm.doc.employee,
				current_advance: frm.is_new() ? null : frm.doc.name,
			},
			callback: function (r) {
				if (!r.message || !r.message.length) return;

				const currency = frm.doc.currency || frappe.defaults.get_default("currency");
				const total = r.message.reduce(
					(sum, d) => sum + flt(d.custom_outstanding_balance), 0
				);

				const rows = r.message
					.map(
						(d) => `
					<tr>
						<td style="padding:5px 8px;">
							<a href="/app/employee-advance/${d.name}" target="_blank">${d.name}</a>
						</td>
						<td style="padding:5px 8px;">${frappe.utils.escape_html(d.purpose || "")}</td>
						<td style="padding:5px 8px;">${frappe.datetime.str_to_user(d.posting_date)}</td>
						<td style="padding:5px 8px; text-align:right;">${format_currency(d.paid_amount, currency)}</td>
						<td style="padding:5px 8px; text-align:right; color:var(--red-500); font-weight:600;">
							${format_currency(d.custom_outstanding_balance, currency)}
						</td>
						<td style="padding:5px 8px;">${frappe.utils.escape_html(d.status || "")}</td>
					</tr>`
					)
					.join("");

				const html = `
					<div class="outstanding-advances-summary"
						style="margin:10px 15px 0; padding:12px 15px;
							   background:var(--alert-bg-warning);
							   border:1px solid var(--alert-border-warning);
							   border-radius:6px;">
						<div style="font-weight:600; margin-bottom:10px; color:var(--alert-text-warning); font-size:13px;">
							⚠&nbsp; Previous Outstanding Advances for this Employee
						</div>
						<table style="width:100%; border-collapse:collapse; font-size:12px;">
							<thead>
								<tr style="border-bottom:1px solid var(--border-color); color:var(--text-muted);">
									<th style="padding:4px 8px; text-align:left;">Advance</th>
									<th style="padding:4px 8px; text-align:left;">Purpose</th>
									<th style="padding:4px 8px; text-align:left;">Date</th>
									<th style="padding:4px 8px; text-align:right;">Paid</th>
									<th style="padding:4px 8px; text-align:right;">Outstanding</th>
									<th style="padding:4px 8px; text-align:left;">Status</th>
								</tr>
							</thead>
							<tbody>${rows}</tbody>
							<tfoot>
								<tr style="border-top:1px solid var(--border-color); font-weight:600;">
									<td colspan="4" style="padding:6px 8px;">Total Outstanding</td>
									<td style="padding:6px 8px; text-align:right; color:var(--red-500);">
										${format_currency(total, currency)}
									</td>
									<td></td>
								</tr>
							</tfoot>
						</table>
					</div>`;

				$(frm.wrapper).find(".form-layout").prepend(html);
			},
		});
	},
});
