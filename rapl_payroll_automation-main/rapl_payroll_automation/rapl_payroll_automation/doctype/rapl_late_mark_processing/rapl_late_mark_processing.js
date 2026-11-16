// Copyright (c) 2026, RAPL and contributors

const MAX_BANDS = 5;

frappe.ui.form.on("RAPL Late Mark Processing", {
	refresh(frm) {
		setup_band_columns(frm);

		if (frm.doc.docstatus !== 0) return;

		frm.add_custom_button(__("Get Employees (With Attendance)"), () => {
			if (!frm.doc.start_date || !frm.doc.end_date) {
				frappe.msgprint(__("Set From Date and To Date first."));
				return;
			}
			get_employees(frm, false);
		});

		frm.add_custom_button(__("Get All Employees"), () => {
			if (!frm.doc.start_date || !frm.doc.end_date) {
				frappe.msgprint(__("Set From Date and To Date first."));
				return;
			}
			frappe.confirm(
				__("This adds EVERY active employee, regardless of attendance. Continue?"),
				() => get_employees(frm, true)
			);
		});

		// Ticked rows are read BEFORE any save: saving re-renders the grid and
		// drops the ticks, which would turn "these rows" into "every row".
		frm.add_custom_button(__("Waive Late Marks"), () => {
			const targets = waiver_targets(frm);
			with_saved(frm, () => open_waiver_dialog(frm, targets));
		}, __("Waiver"));
		frm.add_custom_button(__("Clear Waiver"), () => {
			const targets = waiver_targets(frm);
			with_saved(frm, () => clear_waiver(frm, targets));
		}, __("Waiver"));

		frm.add_custom_button(__("Select Employees Manually"), () => {
			new frappe.ui.form.MultiSelectDialog({
				doctype: "Employee",
				target: frm,
				setters: {
					employee_name: undefined,
					department: undefined,
					grade: undefined,
				},
				get_query() {
					return { filters: { status: "Active" } };
				},
				action(selections) {
					if (!selections || !selections.length) return;
					this.dialog.hide();
					if (!frm.is_new() && frm.is_dirty()) {
						// Save first: the server reads the saved document.
						return frm.save().then(() => { if (!frm.is_dirty()) this.action(selections); });
					}
					if (!frm.doc.start_date || !frm.doc.end_date) {
						frappe.msgprint(__("Set From Date and To Date first, then save, before selecting employees."));
						return;
					}
					if (frm.is_new()) {
						frappe.msgprint(__("Save the document once (with dates set) before selecting employees."));
						return;
					}
					frappe.call({
						method: "rapl_payroll_automation.rapl_payroll_automation.doctype.rapl_late_mark_processing.rapl_late_mark_processing.get_employees",
						args: { docname: frm.doc.name, employees: selections },
						freeze: true,
						freeze_message: __("Adding selected employees..."),
						callback: () => frm.reload_doc(),
					});
				},
			});
		});
	},
});

function get_employees(frm, all_employees) {
	// The server works on the SAVED document and the form is reloaded after,
	// so unsaved grid edits (and unsaved dates) would be silently lost.
	if (!frm.is_new() && frm.is_dirty()) {
		// Re-enter only if the save worked: frm.save() resolves even when the
		// server refuses it, and the form would still be dirty -> endless loop.
		return frm.save().then(() => { if (!frm.is_dirty()) get_employees(frm, all_employees); });
	}
	if (frm.is_new()) {
		frappe.msgprint(__("Save the document once (with dates set) before fetching employees."));
		return;
	}
	frappe.call({
		method: "rapl_payroll_automation.rapl_payroll_automation.doctype.rapl_late_mark_processing.rapl_late_mark_processing.get_employees",
		args: { docname: frm.doc.name, all_employees },
		freeze: true,
		freeze_message: __("Fetching employees..."),
		callback: () => frm.reload_doc(),
	});
}

// --- Dynamic column labeling/hiding for band_1_count .. band_5_count ---
// The doctype always has exactly 5 fixed columns (Frappe doctypes have a
// fixed schema -- can't literally grow columns at runtime). This function
// renames each column's header to match the actual band Label configured
// in RAPL Payroll Automation Settings, and hides any column beyond however
// many bands are actually defined -- so it LOOKS fully dynamic even though
// the underlying fields are fixed. Verified against real Frappe Grid
// source (grid.js): update_docfield_property(fieldname, property, value)
// is a generic public method (docfield[property] = value under the hood),
// used here for both "label" and "hidden".
function setup_band_columns(frm) {
	frappe.call({
		method: "rapl_payroll_automation.rapl_payroll_automation.doctype.rapl_late_mark_processing.rapl_late_mark_processing.get_band_labels",
		args: { with_fractions: 1 },
		callback(r) {
			const bands = r.message || [];
			const labels = bands.map((b) => b.label);
			frm.__late_mark_band_labels = labels;
			// Same call, same order as the server's band columns.
			frm.__late_mark_band_fractions = bands.map((b) => flt(b.fraction));

			const grid = frm.fields_dict["entries"].grid;
			for (let i = 1; i <= MAX_BANDS; i++) {
				const fieldname = `band_${i}_count`;
				const waived = `band_${i}_waived`;
				if (i <= labels.length) {
					grid.update_docfield_property(fieldname, "label", labels[i - 1]);
					grid.toggle_display(fieldname, true);
					grid.update_docfield_property(waived, "label", __("Waived: {0}", [labels[i - 1]]));
					grid.toggle_display(waived, true);
				} else {
					grid.toggle_display(fieldname, false);
					grid.toggle_display(waived, false);
				}
			}
			frm.refresh_field("entries");
		},
	});
}

// Numeric seconds-since-midnight, not raw string comparison -- a plain
// string sort would incorrectly order "9:46:00" AFTER "10:00:00" if
// Frappe ever returns an unpadded hour (comparing '9' > '1' as characters).
function time_str_to_seconds(t) {
	if (!t) return 0;
	const parts = String(t).split(":").map(Number);
	return (parts[0] || 0) * 3600 + (parts[1] || 0) * 60 + (parts[2] || 0);
}

// --- Child table (RAPL Late Mark Processing Entry) row-level triggers ---
// Must live here, in the PARENT's own client script -- see the equivalent
// comment in rapl_overtime_processing.js for the full explanation.
frappe.ui.form.on("RAPL Late Mark Processing Entry", {
	employee(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.employee) return;
		if (!frm.doc.start_date || !frm.doc.end_date) {
			frappe.msgprint(__("Set From Date and To Date on the parent document first, then save, before adding rows."));
			return;
		}
		if (frm.is_new()) {
			frappe.msgprint(__("Save the document once (with dates set) before adding rows -- a new row needs a saved parent to fetch Late Mark details against."));
			return;
		}
		frappe.call({
			method: "rapl_payroll_automation.rapl_payroll_automation.doctype.rapl_late_mark_processing.rapl_late_mark_processing.get_employee_late_mark_details",
			args: { docname: frm.doc.name, employee: row.employee },
			freeze: true,
			freeze_message: __("Fetching Late Mark details..."),
			callback: (r) => {
				if (!r.message) return;
				const counts = r.message.band_counts || [];
				for (let i = 0; i < MAX_BANDS; i++) {
					frappe.model.set_value(cdt, cdn, `band_${i + 1}_count`, counts[i] || 0);
				}
				frappe.model.set_value(cdt, cdn, "per_day_rate", r.message.per_day_rate);
				frappe.model.set_value(cdt, cdn, "amount", r.message.amount);
				if (r.message.errors && r.message.errors.length) {
					frappe.msgprint(r.message.errors.map((e) => frappe.utils.escape_html(String(e))).join("<br>"));
				}
			},
		});
	},
	band_1_count(frm, cdt, cdn) { recalculate_late_mark_amount(frm, cdt, cdn); },
	band_2_count(frm, cdt, cdn) { recalculate_late_mark_amount(frm, cdt, cdn); },
	band_3_count(frm, cdt, cdn) { recalculate_late_mark_amount(frm, cdt, cdn); },
	band_4_count(frm, cdt, cdn) { recalculate_late_mark_amount(frm, cdt, cdn); },
	band_5_count(frm, cdt, cdn) { recalculate_late_mark_amount(frm, cdt, cdn); },
	per_day_rate(frm, cdt, cdn) { recalculate_late_mark_amount(frm, cdt, cdn); },
	band_1_waived(frm, cdt, cdn) { recalculate_late_mark_amount(frm, cdt, cdn, true); },
	band_2_waived(frm, cdt, cdn) { recalculate_late_mark_amount(frm, cdt, cdn, true); },
	band_3_waived(frm, cdt, cdn) { recalculate_late_mark_amount(frm, cdt, cdn, true); },
	band_4_waived(frm, cdt, cdn) { recalculate_late_mark_amount(frm, cdt, cdn, true); },
	band_5_waived(frm, cdt, cdn) { recalculate_late_mark_amount(frm, cdt, cdn, true); },
});

function recalculate_late_mark_amount(frm, cdt, cdn, waiver_edit) {
	const row = locals[cdt][cdn];
	const fractions = frm.__late_mark_band_fractions;
	// Fractions not loaded yet: leave the amount alone rather than price it
	// at zero. The server value stands until they arrive.
	if (!fractions) return;
	// Same rules as the server (apply_waiver_math / waived_row_amount): a
	// waiver is kept between 0 and the band's own count, and it can only
	// LOWER the deduction. The server recomputes on save; this is only so the
	// row reads right now.
	let gross = 0, net = 0, waived_units = 0, waived_marks = 0;
	for (let i = 0; i < MAX_BANDS; i++) {
		const count = Math.max(cint(row[`band_${i + 1}_count`]), 0);
		const fraction = fractions[i] || 0;
		const waived = i < fractions.length
			? Math.min(Math.max(cint(row[`band_${i + 1}_waived`]), 0), count) : 0;
		row[`band_${i + 1}_waived`] = waived;
		waived_marks += waived;
		gross += count * fraction;
		net += (count - waived) * fraction;
		waived_units += waived * fraction;
	}
	const rate = flt(row.per_day_rate);
	const priced = round_half_up(gross * rate);
	// Pre-waiver amount. A count/rate edit re-prices it (as before waivers
	// existed); a waiver edit keeps it -- including a hand-typed amount.
	let base;
	if (!waiver_edit) base = priced;
	else if (flt(row.amount_before_waiver) > 0) base = flt(row.amount_before_waiver);
	else base = flt(row.amount);

	row.waived_marks = waived_marks;
	if (waived_marks) {
		row.amount_before_waiver = base;
		row.amount = base === priced
			? round_half_up(net * rate)
			: Math.max(base - round_half_up(waived_units * rate), 0);
		row.waived_amount = base - row.amount;
	} else {
		row.amount = base;
		row.amount_before_waiver = 0;
		row.waived_amount = 0;
	}
	frm.refresh_field("entries");
}

// Positive amounts only (a deduction is never negative), so half-up is
// Math.round after nudging away float noise like 340.49999999.
function round_half_up(x) {
	return Math.round(flt(x, 6));
}

const WAIVER_METHOD = "rapl_payroll_automation.rapl_payroll_automation.doctype.rapl_late_mark_processing.rapl_late_mark_processing";

// The server reads the SAVED document. Save first; continue only if the save
// actually worked (frm.save() resolves even when the server refuses it).
function with_saved(frm, fn) {
	if (frm.is_new()) {
		frappe.msgprint(__("Save the document and add employees first."));
		return;
	}
	if (frm.is_dirty()) {
		return frm.save().then(() => { if (!frm.is_dirty()) fn(); });
	}
	fn();
}

// Selected rows, or null for "every row".
function waiver_targets(frm) {
	const selected = frm.fields_dict.entries.grid.get_selected_children()
		.map((r) => r.employee).filter(Boolean);
	return selected.length ? selected : null;
}

function open_waiver_dialog(frm, targets) {
	if (!(frm.doc.entries || []).length) {
		frappe.msgprint(__("Add employees first (Get Employees)."));
		return;
	}
	const labels = frm.__late_mark_band_labels || [];
	const fractions = frm.__late_mark_band_fractions || [];
	if (!labels.length) {
		frappe.msgprint(__("Late mark bands are still loading, or none are configured in Settings. Try again in a moment."));
		return;
	}
	const scope = targets
		? __("Applies to the {0} selected row(s).", [targets.length])
		: __("No rows are selected, so this applies to ALL {0} rows. Tick rows in the table first to waive for some employees only.", [frm.doc.entries.length]);

	const from_options = labels.map((label, i) => ({
		label: __("{0} only (fraction {1})", [frappe.utils.escape_html(String(label)), fractions[i]]),
		value: `band:${i + 1}`,
	}));
	from_options.push({ label: __("Any band -- highest deduction first"), value: "costliest" });
	from_options.push({ label: __("Any band -- lowest deduction first"), value: "cheapest" });

	const d = new frappe.ui.Dialog({
		title: __("Waive Late Marks"),
		size: "extra-large",
		fields: [
			{ fieldtype: "HTML", fieldname: "scope_html" },
			{ fieldtype: "Int", fieldname: "marks", label: __("Late marks to waive (per employee)"), default: 1, reqd: 1 },
			{ fieldtype: "Select", fieldname: "waive_from", label: __("Waive from"), options: from_options, default: "costliest", reqd: 1,
			  description: __("A single band never spills into another: waiving 5 from a band with 4 marks waives 4. \"Any band\" moves on to the next band.") },
			{ fieldtype: "Column Break" },
			{ fieldtype: "Select", fieldname: "combine", label: __("Existing waivers on these rows"), default: "replace", reqd: 1,
			  options: [
				{ label: __("Replace them"), value: "replace" },
				{ label: __("Add to them"), value: "add" },
			  ] },
			{ fieldtype: "Small Text", fieldname: "reason", label: __("Reason"), reqd: 1 },
			{ fieldtype: "Section Break", label: __("Effect") },
			{ fieldtype: "HTML", fieldname: "preview_html" },
		],
		primary_action_label: __("Apply Waiver"),
		primary_action(values) {
			const args = waiver_args(frm, values, targets);
			if (!args) return;
			args.reason = values.reason;
			frappe.call({
				method: `${WAIVER_METHOD}.apply_waiver`,
				args,
				freeze: true,
				freeze_message: __("Applying waiver..."),
				callback(r) {
					d.hide();
					frm.reload_doc();
					const m = r.message || {};
					frappe.show_alert({
						message: __("Waiver applied to {0} row(s). Total deduction {1} -> {2}.",
							[m.changed || 0, format_currency(m.total_now || 0), format_currency(m.total_after || 0)]),
						indicator: "green",
					}, 7);
				},
			});
		},
	});
	d.fields_dict.scope_html.$wrapper.html(`<p class="text-muted">${frappe.utils.escape_html(scope)}</p>`);

	let timer = null, seq = 0;
	const refresh_preview = () => {
		clearTimeout(timer);
		timer = setTimeout(() => {
			const args = waiver_args(frm, d.get_values(true), targets);
			if (!args) {
				d.fields_dict.preview_html.$wrapper.html("");
				return;
			}
			const mine = ++seq;   // ignore answers to older requests
			frappe.call({
				method: `${WAIVER_METHOD}.preview_waiver`,
				args,
				callback(r) {
					if (mine !== seq) return;
					d.fields_dict.preview_html.$wrapper.html(render_waiver_preview(r.message || {}));
				},
				error() {
					if (mine === seq) d.fields_dict.preview_html.$wrapper.html("");
				},
			});
		}, 250);
	};
	["marks", "waive_from", "combine"].forEach((f) => {
		d.fields_dict[f].df.onchange = refresh_preview;
	});
	d.show();
	refresh_preview();
}

function waiver_args(frm, values, targets) {
	if (!values || cint(values.marks) < 1 || !values.waive_from) return null;
	const from = String(values.waive_from);
	const args = {
		docname: frm.doc.name,
		marks: cint(values.marks),
		combine: values.combine || "replace",
		employees: targets || [],
	};
	if (from.startsWith("band:")) {
		args.mode = "band";
		args.band = cint(from.split(":")[1]);
	} else {
		args.mode = from;
	}
	return args;
}

function render_waiver_preview(m) {
	const esc = (v) => frappe.utils.escape_html(String(v == null ? "" : v));
	const labels = m.labels || [];
	const rows = m.rows || [];
	if (!rows.length) return `<p class="text-muted">${esc(__("No rows to change."))}</p>`;
	const marks = (counts, waived) => labels.map((_, i) => {
		const c = counts[i] || 0, w = waived[i] || 0;
		return `<td class="text-right">${w ? `${esc(c)} <span class="text-success">(-${esc(w)})</span>` : esc(c)}</td>`;
	}).join("");
	const head = labels.map((l) => `<th class="text-right">${esc(l)}</th>`).join("");
	const body = rows.map((r) => `
		<tr${r.changed ? "" : ' class="text-muted"'}>
			<td>${esc(r.employee)}<br><small>${esc(r.employee_name || "")}</small></td>
			${marks(r.counts || [], r.waived_after || [])}
			<td class="text-right">${esc(format_currency(r.amount_now))}</td>
			<td class="text-right"><b>${esc(format_currency(r.amount_after))}</b></td>
			<td class="text-right">${esc(format_currency(r.saving))}</td>
		</tr>`).join("");
	const saving = (m.total_now || 0) - (m.total_after || 0);
	return `
		<p>${esc(__("{0} of {1} row(s) change. Marks shown as count (-waived). Rows with nothing to waive this way are left as they are.", [m.changed || 0, rows.length]))}</p>
		<div style="max-height: 50vh; overflow: auto;">
		<table class="table table-bordered table-condensed" style="font-size: 12px;">
			<thead><tr><th>${esc(__("Employee"))}</th>${head}
				<th class="text-right">${esc(__("Deduction now"))}</th>
				<th class="text-right">${esc(__("After waiver"))}</th>
				<th class="text-right">${esc(__("Waived"))}</th></tr></thead>
			<tbody>${body}</tbody>
			<tfoot><tr><th colspan="${labels.length + 1}">${esc(__("Total"))}</th>
				<th class="text-right">${esc(format_currency(m.total_now || 0))}</th>
				<th class="text-right">${esc(format_currency(m.total_after || 0))}</th>
				<th class="text-right">${esc(format_currency(saving))}</th></tr></tfoot>
		</table></div>`;
}

function clear_waiver(frm, targets) {
	const waived_rows = (frm.doc.entries || []).filter((r) =>
		cint(r.waived_marks) && (!targets || targets.includes(r.employee)));
	if (!waived_rows.length) {
		frappe.msgprint(targets ? __("None of the selected rows has a waiver.") : __("No row has a waiver."));
		return;
	}
	frappe.confirm(
		__("Remove the waiver from {0} row(s)? Their deduction goes back to the full amount.", [waived_rows.length]),
		() => frappe.call({
			method: `${WAIVER_METHOD}.clear_waiver`,
			args: { docname: frm.doc.name, employees: targets || [] },
			freeze: true,
			callback: () => frm.reload_doc(),
		})
	);
}
