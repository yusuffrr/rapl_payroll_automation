// Copyright (c) 2026, RAPL and contributors
//
// Attendance Console -- HR editing surface.
//
// Pending edits live in a Map keyed by Attendance name, NOT in the DOM.
// Collapsing a group, switching filters or reloading a section must never
// discard them, and the unsaved count stays accurate regardless of what is
// currently visible.
//
// Nothing here computes a rule. Derived values always come back from the
// server after Apply, because the browser must never be the thing that decides
// what a band or an overtime figure is.

frappe.pages["attendance-console"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Attendance Console"),
		single_column: true,
	});

	const MONTHS = [
		"January", "February", "March", "April", "May", "June",
		"July", "August", "September", "October", "November", "December",
	];

	const state = {
		data: null,
		pending: new Map(),   // attendance name -> {name, modified, in_time, out_time, status}
		overrides: new Map(), // employee -> edited summary values (ot hours/rate/amount, band counts, per-day rate)
		selected: new Set(),  // attendance names
		expanded: new Set(),  // employee ids
		adv: new Map(),       // employee -> Set of ticked Employee Advance names
		netpay: new Map(),    // employee -> computed pay result
		netopen: new Set(),   // employees whose breakdown is expanded
		leave: new Map(),     // employee -> Set of ticked absent dates
	};

	const month_field = page.add_field({
		fieldname: "month", label: __("Month"), fieldtype: "Select",
		options: MONTHS, default: MONTHS[new Date().getMonth()],
	});
	const year_field = page.add_field({
		fieldname: "year", label: __("Year"), fieldtype: "Select",
		options: year_options(), default: String(new Date().getFullYear()),
	});
	const employee_field = page.add_field({
		fieldname: "employee", label: __("Employee"), fieldtype: "Link",
		options: "Employee", get_query: () => ({ filters: { status: "Active" } }),
	});
	const cutoff_field = page.add_field({
		fieldname: "advance_cutoff", label: __("Advances to"), fieldtype: "Date",
	});

	const filter_field = page.add_field({
		fieldname: "only_flagged", label: __("Show"), fieldtype: "Select",
		options: [__("Needs attention"), __("All records")],
		default: __("Needs attention"),
	});

	const $body = $(`
		<div class="ac-wrap" style="padding:10px 0;">
			<div class="ac-bulk" style="display:none;"></div>
			<div class="ac-status text-muted" style="padding:24px 0; text-align:center;">
				${__("Choose a period, then Load.")}
			</div>
			<div class="ac-out"></div>
			<div class="ac-foot" style="display:none;"></div>
		</div>
	`).appendTo(page.main);

	page.set_primary_action(__("Load"), load);

	function year_options() {
		const now = new Date().getFullYear();
		const out = [];
		for (let y = now + 1; y >= now - 4; y--) out.push(String(y));
		return out;
	}

	function period() {
		const mi = MONTHS.indexOf(month_field.get_value());
		const year = parseInt(year_field.get_value(), 10);
		if (mi < 0 || !year) return null;
		const iso = (d) =>
			`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
		return { start: iso(new Date(year, mi, 1)), end: iso(new Date(year, mi + 1, 0)) };
	}

	function status(msg) {
		$body.find(".ac-status").text(msg || "").toggle(Boolean(msg));
	}

	// ---------------------------------------------------------------- load

	function load() {
		const range = period();
		if (!range) return frappe.msgprint(__("Choose a month and year."));

		if (state.pending.size) {
			frappe.confirm(
				__("Discard {0} unsaved change(s)?", [state.pending.size]),
				() => { state.pending.clear(); state.selected.clear(); do_load(range); }
			);
			return;
		}
		do_load(range);
	}

	function do_load(range) {
		const employee = employee_field.get_value();
		status(__("Loading..."));
		$body.find(".ac-out").empty();

		frappe.call({
			method: "rapl_payroll_automation.api.attendance_console.get_console_data",
			args: {
				start_date: range.start,
				end_date: range.end,
				employees: employee ? JSON.stringify([employee]) : null,
				only_flagged: filter_field.get_value() === __("Needs attention") ? 1 : 0,
				advance_cutoff: cutoff_field.get_value() || null,
			},
			freeze: true,
			freeze_message: __("Reading attendance..."),
			callback(r) {
				state.data = r.message;
				state.selected.clear();
				state.netpay.clear();
				// Every recoverable advance starts ticked -- HR usually recovers
				// everything and unticks the exceptions.
				state.adv.clear();
				state.leave.clear();
				(state.data.groups || []).forEach((g) => {
					state.adv.set(g.employee, new Set((g.advances || []).map((a) => a.name)));
				});
				if (state.data.advance_cutoff && !cutoff_field.get_value()) {
					cutoff_field.set_value(state.data.advance_cutoff);
				}
				if (state.data && state.data.groups.length === 1) {
					state.expanded.add(state.data.groups[0].employee);
				}
				render();
			},
		});
	}

	// ---------------------------------------------------------------- render

	// ONE table for the whole grid. The employee summary row and its day rows
	// share a single column set, so every summary value sits directly above the
	// day-level column it is the total of: Cut over Cut, OT h over OT h,
	// OT money over OT money. Two separate tables could never line up, and the
	// mismatched widths were the reason the page looked unbalanced.
	const VISIT_TYPES = ["Site Visit", "Client Visit", "Vendor Visit"];

	const COLS = [
		{ w: "5%" }, { w: "4%" },
		{ w: "13%", label: () => __("Date") },
		{ w: "10%", label: () => __("In") },
		{ w: "10%", label: () => __("Out") },
		{ w: "7%", label: () => __("Hrs"), num: true },
		{ w: "14%", label: () => __("Status") },
		{ w: "12%", label: () => __("Visit") },
		{ w: "5%", label: () => __("PL"), num: true, title: () => __("Paid leave") },
		{ w: "9%", label: () => __("Late") },
		{ w: "8%", label: () => __("OT h:mm"), num: true },
		{ w: "7%", label: () => __("Cut") + " \u20b9", num: true },
	];

	// ---------------------------------------------------------------- overrides
	//
	// Summary-row overrides live here, keyed by employee, and are session only:
	// the durable record is the RAPL Overtime / Late Mark Processing draft the
	// values are written into. Day-level pins are different -- those persist on
	// the Attendance record itself via custom_overtime_manual /
	// custom_late_mark_manual, because that is what future saves recompute.

	function ov(employee, field, computed) {
		const o = state.overrides.get(employee);
		return o && field in o ? o[field] : computed;
	}

	function set_ov(employee, field, value) {
		const o = state.overrides.get(employee) || {};
		o[field] = value;
		state.overrides.set(employee, o);
	}

	function hhmm_from_seconds(seconds) {
		const s = Math.max(0, Math.round(flt(seconds)));
		if (!s) return "";
		const h = Math.floor(s / 3600);
		const m = Math.round((s % 3600) / 60);
		return `${h}:${String(m).padStart(2, "0")}`;
	}

	function render() {
		if (!state.data || !state.data.groups.length) {
			status(__("Nothing to show for this period."));
			render_footer();
			return;
		}
		status("");
		const bands = state.data.bands || [];
		const head = COLS.map(
			(c) => `<th style="width:${c.w}" class="${c.num ? "ac-num" : ""}"${c.title ? ` title="${c.title()}"` : ""}>${c.label ? c.label() : ""}</th>`
		).join("");
		const body = state.data.groups.map((g) => render_group(g, bands)).join("");
		$body.find(".ac-out").html(
			`<table class="ea-table ac-grid"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`
		);
		render_bulk();
		render_footer();
	}

	function render_group(g, bands) {
		const open = state.expanded.has(g.employee);
		const s = g.summary;
		const e = g.entry;
		const emp = g.employee;
		const ot_secs = ov(emp, "ot_hours_hhmm", e.ot_hours_hhmm);
		const ot_rate = ov(emp, "ot_rate", e.ot_rate);
		const ot_amount = ov(emp, "amount", e.ot_amount);
		const per_day = ov(emp, "per_day_rate", e.per_day_rate);
		const late_amount = ov(emp, "late_amount", e.late_amount);
		const dirty = state.overrides.has(emp);

		const band_inputs = bands
			.map((b, i) => {
				const v = ov(emp, `band_${i + 1}_count`, e.band_counts[b.label] || 0);
				return `<span class="ac-field"><label>${frappe.utils.escape_html(b.label)}</label>` +
					`<input class="ac-cell ac-sum ac-band" data-employee="${emp}" data-field="band_${i + 1}_count" value="${v || ""}" placeholder="0"></span>`;
			})
			.join("");

		const processed = [];
		if (e.ot_already_processed) processed.push(__("OT paid"));
		if (e.late_already_processed) processed.push(__("Late paid"));

		// The summary is a free-form BLOCK spanning the whole grid, not a table
		// row. It has no In, Out or Hrs, and its Late cell holds a count per
		// band rather than one label -- forcing it into the day columns made it
		// cramped and made the whole page read as misaligned.
		const rows = [`
			<tr class="ac-emp-row ${open ? "ac-open" : ""} ${dirty ? "ac-ov" : ""}" data-employee="${emp}">
				<td colspan="${COLS.length}" class="ac-emp-cell">
					<div class="ac-emp-top">
						<span class="ac-caret fa fa-chevron-${open ? "down" : "right"}"></span>
						<div class="ac-emp-id">
							<div class="ac-emp-name">${frappe.utils.escape_html(g.employee_name || "")} - ${emp}</div>
							<div class="ac-emp-sub">
								<span>${frappe.utils.escape_html(g.grade || "")}</span>
								${s.red_flags ? `<span class="ea-cut">&#9873; ${s.red_flags}</span>` : ""}
								${s.amber_flags ? `<span class="ea-late">&#9873; ${s.amber_flags}</span>` : ""}
								${g.employee_status && g.employee_status !== "Active"
									? `<span class="ea-dim">${__("left")} ${g.relieving_date ? frappe.datetime.str_to_user(g.relieving_date) : ""}</span>` : ""}
								${g.ot_eligible ? "" : `<span class="ea-dim" title="${__("Employee.custom_ot is off - automatic OT is not calculated, but you can still enter it by hand")}">${__("manual OT")}</span>`}
								${processed.length ? `<span class="ea-late">${processed.join(" &middot; ")}</span>` : ""}
							</div>
						</div>
						<div class="ac-emp-counts">
							${__("Present")} <b>${s.present}</b>${s.wfh ? ` <span class="ea-dim">(${__("incl. {0} WFH", [s.wfh])})</span>` : ""}
							&nbsp;&middot;&nbsp; ${__("Half day")} <b>${s.half_day}</b>
							&nbsp;&middot;&nbsp; ${__("Leave")} <b>${s.on_leave}</b>
							&nbsp;&middot;&nbsp; ${__("Absent")} <b>${s.absent}</b>
						</div>
						${render_leave_head(g)}
						${open ? "" : `<div class="ac-emp-money">
							<span class="ea-ot">+${format_currency(ot_amount || 0)}</span>
							<span class="ea-cut">&minus;${format_currency(late_amount || 0)}</span>
							<span class="ea-cut">&minus;${format_currency(advance_total(g))}</span>
							<b class="${(flt(ot_amount) - flt(late_amount) - advance_total(g)) < 0 ? "ea-cut" : "ea-ot"}">${
								format_currency(flt(ot_amount) - flt(late_amount) - advance_total(g))}</b>
						</div>`}
					</div>
					<div class="ac-emp-zones">
						<div class="ac-zone">
							<div class="ac-zone-h">${__("LATE MARK")}</div>
							<div class="ac-zone-body">
								${band_inputs}
								<span class="ac-field"><label>&#8377;/${__("day")}</label>
									<input class="ac-cell ac-sum" data-employee="${emp}" data-field="per_day_rate"
										value="${per_day || ""}" placeholder="0"
										title="${__("Monthly salary divided by calendar days in the period")}"></span>
								<span class="ac-spacer"></span>
								<input class="ac-cell ac-sum ea-cut" data-employee="${emp}" data-field="late_amount"
									value="${late_amount || ""}" placeholder="0" title="${__("Total late mark deduction")}">
							</div>
						</div>
						<div class="ac-zone">
							<div class="ac-zone-h">${__("OVERTIME")}</div>
							<div class="ac-zone-body">
								<span class="ac-field"><label>h:mm</label>
									<input class="ac-cell ac-sum" data-employee="${emp}" data-field="ot_hours_hhmm"
										value="${ot_secs ? hhmm_from_seconds(ot_secs) : ""}" placeholder="0:00"></span>
								<span class="ac-field"><label>&#8377;/${__("hr")}</label>
									<input class="ac-cell ac-sum" data-employee="${emp}" data-field="ot_rate"
										value="${ot_rate || ""}" placeholder="0"
										title="${__("Per-day rate divided by the overtime divisor. The denominator depends on grade.")}"></span>
								<span class="ac-spacer"></span>
								<input class="ac-cell ac-sum ea-ot" data-employee="${emp}" data-field="amount"
									value="${ot_amount || ""}" placeholder="0" title="${__("Overtime earned")}">
							</div>
						</div>
						${render_advance_zone(g)}
					</div>
					<div class="ac-effect">
						<span class="ac-zone-h">${__("EFFECT")}</span>
						<span class="ea-ot">${__("OT")} +${format_currency(ot_amount || 0)}</span>
						<span class="ea-cut">${__("Cut")} &minus;${format_currency(late_amount || 0)}</span>
						<span class="ea-cut">${__("Advance")} &minus;${format_currency(advance_total(g))}</span>
						<span class="ac-spacer"></span>
						<button class="btn btn-xs ac-netpay" data-employee="${emp}">${
							state.netpay.has(emp) ? __("Recompute") : __("Compute net pay")}</button>
					</div>
					${render_netpay(g)}
				</td>
			</tr>`];

		if (open) rows.push(g.rows.map((row) => render_day(g, row, bands)).join(""));
		return rows.join("");
	}

	// ---------------------------------------------------------------- advances
	//
	// One line per unrecovered Employee Advance, because the draft creates one
	// Additional Salary PER advance carrying ref_docname back to it. A single
	// lump sum could not be linked to anything.

	function advance_total(g) {
		const picked = state.adv.get(g.employee) || new Set();
		return (g.advances || [])
			.filter((a) => picked.has(a.name))
			.reduce((sum, a) => sum + flt(a.outstanding), 0);
	}

	function render_advance_zone(g) {
		const list = g.advances || [];
		if (!list.length) {
			return `<div class="ac-zone"><div class="ac-zone-h">${__("ADVANCE")}</div>
				<div class="ac-zone-body ea-dim">${__("Nothing to recover")}</div></div>`;
		}
		const picked = state.adv.get(g.employee) || new Set();
		const outstanding = list.reduce((s, a) => s + flt(a.outstanding), 0);
		const open = state.expanded.has("adv:" + g.employee);

		const lines = list.map((a) => `
			<div class="ac-adv-line">
				<input type="checkbox" class="ac-adv-pick" data-employee="${g.employee}"
					data-advance="${a.name}" ${picked.has(a.name) ? "checked" : ""}>
				<span class="ac-adv-name">${a.name}</span>
				<span class="ea-dim">${frappe.datetime.str_to_user(a.posting_date)}</span>
				<span class="ea-dim ac-adv-purpose">${frappe.utils.escape_html(a.purpose || "")}</span>
				<span class="ac-spacer"></span>
				<span class="ea-cut">${format_currency(a.outstanding)}</span>
			</div>`).join("");

		return `<div class="ac-zone">
			<div class="ac-zone-h">${__("ADVANCE")}</div>
			<div class="ac-zone-body ac-adv-head" data-employee="${g.employee}">
				<span class="ea-dim">${__("outstanding")} <b>${format_currency(outstanding)}</b></span>
				<span class="ac-spacer"></span>
				<span class="ea-cut"><b>${format_currency(advance_total(g))}</b></span>
				<span class="ac-adv-toggle">${list.length} ${open ? "&#9652;" : "&#9662;"}</span>
			</div>
			${open ? `<div class="ac-adv-list">${lines}</div>` : ""}
		</div>`;
	}

	// ---------------------------------------------------------------- net pay

	function render_netpay(g) {
		const res = state.netpay.get(g.employee);
		if (!res) return "";
		if (res.error) {
			return `<div class="ac-netpay-bar ea-cut">${frappe.utils.escape_html(res.error)}</div>`;
		}
		const open = state.netopen.has(g.employee);
		const rows = (list) => list.map((r) => `
			<div class="ac-pay-row ${r.injected ? "ac-pay-injected" : ""}">
				<span>${frappe.utils.escape_html(r.component)}</span>
				<span>${format_currency(r.amount)}</span>
			</div>`).join("");

		return `
			<div class="ac-netpay-bar" data-employee="${g.employee}">
				<b>${__("Computed pay")}</b>
				<span>${__("Net")} <b class="ac-net">${format_currency(res.net_pay)}</b></span>
				<span class="ea-dim">${__("provisional")} &middot; ${flt(res.payment_days, 2)}/${flt(res.total_working_days, 2)} ${__("days")}</span>
				<span class="ac-spacer"></span>
				<span class="ac-pay-toggle">${__("breakdown")} ${open ? "&#9652;" : "&#9662;"}</span>
			</div>
			${open ? `
			<div class="ac-pay-detail">
				<div class="ac-pay-cols">
					<div>
						<div class="ac-zone-h">${__("EARNINGS")}</div>
						${rows(res.earnings)}
						<div class="ac-pay-row ac-pay-total"><span>${__("Gross")}</span><span>${format_currency(res.gross_pay)}</span></div>
					</div>
					<div>
						<div class="ac-zone-h">${__("DEDUCTIONS")}</div>
						${rows(res.deductions)}
						<div class="ac-pay-row ac-pay-total"><span>${__("Total")}</span><span>${format_currency(res.total_deduction)}</span></div>
					</div>
				</div>
				<div class="ac-pay-net">
					<div>
						<div class="ac-zone-h">${__("NET")}</div>
						<div class="ac-net-big">${format_currency(res.net_pay)}</div>
					</div>
					<div class="ea-dim ac-pay-note">${__("Nothing is saved. Overtime, Late Mark and Advance are not yet submitted — this figure moves if attendance changes.")}</div>
				</div>
			</div>` : ""}`;
	}

	// ---------------------------------------------------------------- leave
	//
	// Only Absent days can become leave. Leave Application's own
	// validate_attendance() refuses a range covering any Present or Work From
	// Home day, so offering the tick anywhere else would only produce an error.
	//
	// Eligibility comes from LEAVE ALLOCATION, not the custom_paid_leave flag
	// on Employee -- that flag is wired to nothing.

	function leave_picked(employee) {
		return state.leave.get(employee) || new Set();
	}

	function render_leave_head(g) {
		const lv = g.leave || {};
		if (!lv.leave_type) return "";
		const picked = leave_picked(g.employee);
		if (!lv.eligible) {
			return `<span class="ac-leave-head ea-dim"
				title="${__("No leave allocation for this employee")}">${__("no")} ${frappe.utils.escape_html(lv.leave_type)}</span>`;
		}
		return `<span class="ac-leave-head">
				<span class="ea-dim">${__("Leave Balance")}</span> <b>${flt(lv.balance, 1)}</b>
				${lv.taken ? `<span class="ea-dim" style="margin-left:7px">${__("Leave Taken")}</span> <b>${lv.taken}</b>` : ""}
				${picked.size ? `<button class="btn btn-xs ac-leave-create" data-employee="${g.employee}" style="margin-left:7px">${__("Create leave")} (${picked.size})</button>` : ""}
			</span>`;
	}

	function leave_cell(g, row, att) {
		const lv = g.leave || {};
		if (!lv.leave_type) return '<span class="ea-dim">&mdash;</span>';
		if (att.leave_application) {
			return `<span class="ea-dim" title="${frappe.utils.escape_html(att.leave_application)}">&#10003;</span>`;
		}
		if (att.status !== "Absent") return '<span class="ea-dim">&mdash;</span>';
		if (!lv.eligible) {
			return `<span class="ea-dim" title="${__("No leave allocation")}">&#9633;</span>`;
		}
		const picked = leave_picked(g.employee);
		return `<input type="checkbox" class="ac-leave-pick" data-employee="${g.employee}"
			data-date="${row.date}" ${picked.has(row.date) ? "checked" : ""}>`;
	}

	function render_day(g, row, bands) {
		const att = row.attendance;
		const date_label = `${row.day_label} ${row.date.slice(8, 10)}`;
		const flag = top_flag(row.flags);
		const flag_cell = flag
			? `<td class="ac-num ${flag.level === "red" ? "ea-cut" : flag.level === "amber" ? "ea-late" : "ea-dim"}" title="${frappe.utils.escape_html(flag.message)}">&#9679;</td>`
			: '<td class="ac-num"></td>';
		const dash = '<td class="ac-num ea-dim">&mdash;</td>';

		if (!att) {
			if (row.is_holiday) {
				return `<tr class="ac-day ea-off" data-date="${row.date}" data-employee="${g.employee}"><td></td>${flag_cell}
					<td class="ea-dim ac-indent">${date_label}</td>
					<td colspan="9" class="ea-dim">${row.is_weekly_off ? __("Weekly off") : frappe.utils.escape_html(row.holiday_description || __("Holiday"))}
						${g.can_create ? `<button class="btn btn-xs ac-create ac-create-inline">${__("Create")}</button>` : ""}</td>
				</tr>`;
			}
			if (!row.in_service) return "";
			const cancelled = (row.flags || []).some((f) => f.code === "cancelled_only");
			const action = g.can_create
				? `<button class="btn btn-xs ac-create ac-create-inline">${__("Create")}</button>`
				: `<span class="ea-dim">${__("Left")}</span>`;
			return `<tr class="ac-day ea-missing" data-date="${row.date}" data-employee="${g.employee}">
					<td></td>${flag_cell}
					<td class="ac-indent">${date_label}</td>
					<td colspan="9">${cancelled ? __("Only a cancelled record exists") : __("No attendance record")} ${action}</td>
				</tr>`;
		}

		const pending = state.pending.get(att.name);
		const val = (f) => (pending && f in pending ? pending[f] : att[f]);
		// The pending map is keyed by the SERVER field names (apply_edits reads
		// custom_overtime_hours / custom_late_mark_band), but attendance_data
		// publishes them to the browser without the custom_ prefix. Reading the
		// prefixed name straight off att gave undefined, so the OT input
		// rendered blank on every row and the band never preselected.
		const pv = (pending_key, att_value) =>
			pending && pending_key in pending ? pending[pending_key] : att_value;
		const e = g.entry;
		// Per-day money is display only -- the payable figures live on the
		// summary row above, and now sit in the same columns as these.
		const ot_amount = flt(att.overtime_hours) * flt(ov(g.employee, "ot_rate", e.ot_rate));
		const band = bands.find((b) => b.label === att.late_mark_band);
		const cut = band ? flt(band.fraction) * flt(ov(g.employee, "per_day_rate", e.per_day_rate)) : 0;

		return `<tr class="ac-day ${pending ? "ac-dirty" : ""} ${row.is_holiday ? "ea-worked-off" : ""}" data-name="${att.name}">
				<td><input type="checkbox" class="ac-pick" ${state.selected.has(att.name) ? "checked" : ""}></td>
				${flag_cell}
				<td class="ac-indent">${date_label}</td>
				<td><input class="ac-in ac-cell" value="${hhmm(val("in_time"))}" placeholder="--:--"></td>
				<td><input class="ac-out ac-cell" value="${hhmm(val("out_time"))}" placeholder="--:--"></td>
				<td class="ac-num">${att.working_hours ? flt(att.working_hours, 2) : '<span class="ea-dim">&mdash;</span>'}</td>
				<td>${status_select(val("status"), att)}</td>
				<td>${visit_select(pv("custom_attendance_type", att.attendance_type), att, val)}</td>
				<td class="ac-num">${leave_cell(g, row, att)}</td>
				<td>${band_select(pv("custom_late_mark_band", att.late_mark_band), att, bands)}</td>
				<td class="ac-num"><input class="ac-cell ac-day-ot ${att.overtime_manual ? "ac-pinned" : ""}"
					value="${hhmm_from_seconds(flt(pv("custom_overtime_hours", att.overtime_hours)) * 3600)}" placeholder="0:00"
					title="${att.overtime_manual ? __("Set by hand - the rules will not recalculate this day") : __("Leave blank for automatic")}"></td>
				<td class="ac-num">${cut ? `<span class="ea-cut">${Math.round(cut)}</span>` : '<span class="ea-dim">&mdash;</span>'}</td>
			</tr>`;
	}

	function band_select(value, att, bands) {
		// Editable so a single day's late mark can be waived without falsifying
		// the punch. Choosing anything here sets custom_late_mark_manual, which
		// stops the rules recalculating this day; "auto" clears it again.
		// "none" must NOT send an empty string: the server treats empty as
		// "reset to automatic", so waiving a late mark would silently come back
		// on the next save. __none__ means "pinned, and the band is cleared".
		// "auto" is selected when nothing is pinned. Marking "none" selected made
		// every ordinary row look like a deliberately waived late mark.
		const opts = ['<option value="__none__"' + (att.late_mark_manual && !value ? " selected" : "") + ">" + __("none") + "</option>"]
			.concat(bands.map((b) =>
				`<option value="${frappe.utils.escape_html(b.label)}" ${b.label === value ? "selected" : ""}>${frappe.utils.escape_html(b.label)}</option>`))
			.join("");
		return `<select class="ac-cell ac-day-band ${att.late_mark_manual ? "ac-pinned" : ""}"
			title="${att.late_mark_manual ? __("Set by hand - the rules will not recalculate this day") : __("Automatic")}">
			<option value="__auto__" ${att.late_mark_manual ? "" : "selected"}>${__("auto")}</option>${opts}</select>`;
	}

	function visit_select(value, att, val) {
		// Required only when Present with BOTH punches empty. Without it every
		// site visit reads as a forgotten punch and the flag stops meaning
		// anything. Never required when a punch exists, or for Work From Home.
		const status = val("status");
		const required = status === "Present" && !val("in_time") && !val("out_time");
		const opts = ['<option value="">&mdash;</option>']
			.concat(VISIT_TYPES.map((t) =>
				`<option value="${t}" ${t === value ? "selected" : ""}>${__(t)}</option>`))
			.join("");
		return `<select class="ac-cell ac-visit ${required && !value ? "ac-required" : ""} ${value ? "" : "ea-dim"}"
			title="${required ? __("Required: Present with no punch times") : __("Optional")}">${opts}</select>`;
	}

	function status_select(value, att) {
		// Locked ONLY for a real leave -- one backed by a Leave Application, or
		// carrying a leave type the automation did not write. An auto-applied
		// half day also has a leave_type, and locking those made the punch
		// behind them impossible to correct.
		if (att.genuine_leave) {
			return `<span class="ea-dim" title="${frappe.utils.escape_html(att.leave_type || "")}">${frappe.utils.escape_html(value)} &middot; ${__("leave")}</span>`;
		}
		// "On Leave" is deliberately absent. Setting it here writes the status
		// by SQL with no leave_type or leave_application, because
		// check_leave_record() never runs -- payroll would find no leave to
		// deduct against. On Leave belongs to an approved Leave Application.
		const options = ["Present", "Absent", "Half Day", "Work From Home"];
		const pinned = att.status_manual;
		return `<select class="ac-status ac-cell ${pinned ? "ac-pinned" : ""}"
			title="${pinned ? __("Status set by hand - the rules will not re-apply Half Day for this day") : __("Automatic")}">${options
			.map((o) => `<option value="${o}" ${o === value ? "selected" : ""}>${__(o)}</option>`)
			.join("")}</select>`;
	}

	function top_flag(flags) {
		if (!flags || !flags.length) return null;
		return (
			flags.find((f) => f.level === "red") ||
			flags.find((f) => f.level === "amber") ||
			flags[0]
		);
	}

	function hhmm(value) {
		if (!value) return "";
		return String(value).length > 10 ? String(value).slice(11, 16) : String(value);
	}

	// ---------------------------------------------------------------- editing

	$body.on("click", ".ac-emp-row", function (e) {
		if ($(e.target).is("input, select, button")) return;
		const employee = $(this).data("employee");
		if (state.expanded.has(employee)) state.expanded.delete(employee);
		else state.expanded.add(employee);
		render();
	});

	$body.on("change", ".ac-sum", function () {
		// Cascade copied from the doctype forms so the Console and the created
		// draft can never produce different figures:
		//   OT h:mm changed -> ot_hours -> amount = round(hours x rate)
		//   rate    changed -> amount = round(hours x rate)
		//   amount  changed -> stands alone (not recomputed until h:mm or rate move)
		//   band count / per-day rate changed -> cut = round(sum(count x fraction) x per_day)
		const employee = $(this).data("employee");
		const field = $(this).data("field");
		const raw = $(this).val();
		const g = state.data.groups.find((x) => x.employee === employee);
		if (!g) return;
		const bands = state.data.bands || [];

		if (field === "ot_hours_hhmm") {
			const secs = seconds_from_hhmm(raw);
			if (raw && secs === null) {
				frappe.show_alert({ message: __("Use H:MM, e.g. 2:30"), indicator: "orange" });
				return render();
			}
			set_ov(employee, "ot_hours_hhmm", secs || 0);
		} else {
			set_ov(employee, field, flt(raw));
		}

		const o = state.overrides.get(employee) || {};
		const hours = flt(ov(employee, "ot_hours_hhmm", g.entry.ot_hours_hhmm)) / 3600;
		const rate = flt(ov(employee, "ot_rate", g.entry.ot_rate));

		if (field === "ot_hours_hhmm" || field === "ot_rate") {
			o.amount = Math.round(hours * rate);
		}
		if (field.startsWith("band_") || field === "per_day_rate") {
			const per_day = flt(ov(employee, "per_day_rate", g.entry.per_day_rate));
			let fraction = 0;
			bands.forEach((b, i) => {
				fraction += flt(b.fraction) * flt(ov(employee, `band_${i + 1}_count`, g.entry.band_counts[b.label] || 0));
			});
			o.late_amount = Math.round(fraction * per_day);
		}
		state.overrides.set(employee, o);
		render();
	});

	$body.on("change", ".ac-leave-pick", function (e) {
		e.stopPropagation();
		const emp = $(this).data("employee");
		const date = String($(this).data("date"));
		const g = state.data.groups.find((x) => x.employee === emp);
		const picked = leave_picked(emp);

		if (this.checked) {
			// Hard stop at the balance. validate_balance_leaves() would throw on
			// creation anyway, and a raw Frappe error after ticking eight days is
			// a worse experience than refusing the seventh.
			const balance = flt((g && g.leave && g.leave.balance) || 0);
			if (picked.size + 1 > balance) {
				this.checked = false;
				frappe.show_alert({
					message: __("Only {0} day(s) of leave available", [balance]),
					indicator: "orange",
				});
				return;
			}
			picked.add(date);
		} else {
			picked.delete(date);
		}
		state.leave.set(emp, picked);
		render();
	});

	$body.on("click", ".ac-leave-create", function (e) {
		e.stopPropagation();
		const emp = $(this).data("employee");
		const dates = Array.from(leave_picked(emp));
		if (!dates.length) return;
		frappe.confirm(
			__("Create approved leave for {0} day(s)? The attendance for those days becomes On Leave.", [dates.length]),
			() => frappe.call({
				method: "rapl_payroll_automation.api.attendance_console.create_leave_applications",
				args: { employee: emp, dates: JSON.stringify(dates) },
				freeze: true,
				callback(r) {
					const res = r.message || {};
					frappe.msgprint({
						title: __("{0} created, {1} failed",
								  [(res.created || []).length, (res.failed || []).length]),
						indicator: (res.failed || []).length ? "orange" : "green",
						message:
							(res.created || []).map((c) =>
								`<div><a href="${c.route}" target="_blank">${c.name}</a> &mdash; ${frappe.datetime.str_to_user(c.from_date)} to ${frappe.datetime.str_to_user(c.to_date)} (${c.days})</div>`).join("") +
							(res.failed || []).map((f) =>
								`<div class="text-muted">${f.from_date}: ${frappe.utils.escape_html(f.error)}</div>`).join(""),
					});
					state.leave.delete(emp);
					do_load(period());
				},
			})
		);
	});

	$body.on("click", ".ac-adv-head", function (e) {
		e.stopPropagation();
		const key = "adv:" + $(this).data("employee");
		if (state.expanded.has(key)) state.expanded.delete(key);
		else state.expanded.add(key);
		render();
	});

	$body.on("click", ".ac-adv-pick", function (e) {
		e.stopPropagation();
		const emp = $(this).data("employee");
		const adv = $(this).data("advance");
		const picked = state.adv.get(emp) || new Set();
		if (this.checked) picked.add(adv);
		else picked.delete(adv);
		state.adv.set(emp, picked);
		render();
	});

	$body.on("click", ".ac-netpay", function (e) {
		e.stopPropagation();
		const emp = $(this).data("employee");
		const g = state.data.groups.find((x) => x.employee === emp);
		if (!g) return;
		const range = period();
		frappe.call({
			method: "rapl_payroll_automation.api.attendance_console.compute_net_pay",
			args: {
				employee: emp,
				start_date: range.start,
				end_date: range.end,
				// The Console's pending amounts -- not yet submitted, so the
				// preview slip cannot see them on its own.
				overtime: ov(emp, "amount", g.entry.ot_amount) || 0,
				late_mark: ov(emp, "late_amount", g.entry.late_amount) || 0,
				advance: advance_total(g),
			},
			freeze: true,
			freeze_message: __("Computing..."),
			callback(r) {
				state.netpay.set(emp, r.message || {});
				state.netopen.add(emp);
				render();
			},
		});
	});

	$body.on("click", ".ac-pay-toggle", function (e) {
		e.stopPropagation();
		const emp = $(this).closest(".ac-netpay-bar").data("employee");
		if (state.netopen.has(emp)) state.netopen.delete(emp);
		else state.netopen.add(emp);
		render();
	});

	$body.on("change", ".ac-visit", function () {
		const $tr = $(this).closest("tr");
		const name = $tr.data("name");
		const att = find_attendance(name);
		if (!att) return;
		const entry = state.pending.get(name) || { name, modified: att.modified };
		entry.custom_attendance_type = $(this).val() || "";
		state.pending.set(name, entry);
		$tr.addClass("ac-dirty");
		render_footer();
	});

	$body.on("change", ".ac-day-ot, .ac-day-band", function () {
		const $tr = $(this).closest("tr");
		const name = $tr.data("name");
		const att = find_attendance(name);
		if (!att) return;
		const entry = state.pending.get(name) || { name, modified: att.modified };
		if ($(this).hasClass("ac-day-ot")) {
			const v = String($(this).val() || "").trim();
			// Blank resets this day to automatic; a value pins it.
			entry.custom_overtime_hours = v === "" ? "" : seconds_from_hhmm(v) / 3600;
		} else {
			const v = $(this).val();
			// "" resets to automatic; "__none__" pins a cleared band.
			entry.custom_late_mark_band = v === "__auto__" ? "" : v;
		}
		state.pending.set(name, entry);
		$tr.addClass("ac-dirty");
		render_footer();
	});

	function seconds_from_hhmm(v) {
		const parts = String(v).split(":");
		if (parts.length === 2) return (parseInt(parts[0], 10) || 0) * 3600 + (parseInt(parts[1], 10) || 0) * 60;
		return Math.round((parseFloat(v) || 0) * 3600);
	}

	$body.on("change", ".ac-in, .ac-out, .ac-status", function () {
		const $tr = $(this).closest("tr");
		const name = $tr.data("name");
		if (!name) return;
		const att = find_attendance(name);
		if (!att) return;

		const entry = state.pending.get(name) || { name, modified: att.modified };
		const $in = $tr.find(".ac-in");
		const $out = $tr.find(".ac-out");
		const $status = $tr.find(".ac-status");

		entry.in_time = combine($tr, $in.val());
		entry.out_time = combine($tr, $out.val());
		if ($status.length) entry.status = $status.val();

		state.pending.set(name, entry);
		$tr.addClass("ac-dirty");
		render_footer();
	});

	function combine($tr, time_value) {
		// Deliberately returns the bare "HH:MM". The server combines it with
		// the record's own attendance_date, so the browser never builds a
		// datetime and cannot get the date wrong.
		return time_value ? String(time_value).trim() : null;
	}

	function find_attendance(name) {
		for (const g of state.data.groups) {
			for (const row of g.rows) {
				if (row.attendance && row.attendance.name === name) return row.attendance;
			}
		}
		return null;
	}

	function find_row(name) {
		return row_for(name);
	}

	$body.on("change", ".ac-pick", function () {
		const name = $(this).closest("tr").data("name");
		if (!name) return;
		if (this.checked) state.selected.add(name);
		else state.selected.delete(name);
		render_bulk();
	});

	// ---------------------------------------------------------------- bulk

	function render_bulk() {
		const $bar = $body.find(".ac-bulk");
		if (!state.selected.size) return $bar.hide().empty();
		$bar.show().html(`
			<span class="ac-bulk-count">${__("{0} selected", [state.selected.size])}</span>
			<span>${__("Set out time")}</span>
			<input class="ac-bulk-out" placeholder="18:00" style="width:70px">
			<span>${__("Status")}</span>
			<select class="ac-bulk-status" style="width:auto">
				<option value="">${__("Leave unchanged")}</option>
				${["Present", "Absent", "Half Day"].map((o) => `<option>${o}</option>`).join("")}
			</select>
			<button class="btn btn-xs ac-bulk-apply">${__("Apply to selected")}</button>
			<button class="btn btn-xs ac-recalc">${__("Recalculate")}</button>
			<button class="btn btn-xs ac-clear-sel">${__("Clear")}</button>
		`);
	}

	$body.on("click", ".ac-clear-sel", () => { state.selected.clear(); render(); });

	$body.on("click", ".ac-bulk-apply", function () {
		const out = $body.find(".ac-bulk-out").val();
		const st = $body.find(".ac-bulk-status").val();
		if (!out && !st) return frappe.msgprint(__("Nothing to set."));

		state.selected.forEach((name) => {
			const att = find_attendance(name);
			if (!att) return;
			const entry = state.pending.get(name) || { name, modified: att.modified };
			if (out) entry.out_time = String(out).trim();
			if (st) entry.status = st;
			if (!("in_time" in entry)) entry.in_time = hhmm(att.in_time) || null;
			state.pending.set(name, entry);
		});
		render();
	});

	function row_for(name) {
		for (const g of state.data.groups) {
			for (const row of g.rows) {
				if (row.attendance && row.attendance.name === name) return row;
			}
		}
		return null;
	}

	$body.on("click", ".ac-recalc", function () {
		const names = Array.from(state.selected);
		frappe.call({
			method: "rapl_payroll_automation.api.attendance_console.recalculate",
			args: { names: JSON.stringify(names) },
			freeze: true,
			callback(r) { report(r.message); do_load(period()); },
		});
	});

	// ---------------------------------------------------------------- create

	$body.on("click", ".ac-create", function () {
		const $tr = $(this).closest("tr");
		const employee = $tr.data("employee");
		const date = $tr.data("date");

		const d = new frappe.ui.Dialog({
			title: __("Create attendance"),
			fields: [
				{ fieldname: "info", fieldtype: "HTML",
				  options: `<p class="text-muted">${employee} &middot; ${frappe.datetime.str_to_user(date)}</p>` },
				{ fieldname: "status", label: __("Status"), fieldtype: "Select",
				  options: ["Present", "Absent", "Half Day", "Work From Home"], default: "Present" },
				{ fieldname: "in_time", label: __("In"), fieldtype: "Data",
				  description: __("24-hour, e.g. 09:33") },
				{ fieldname: "out_time", label: __("Out"), fieldtype: "Data",
				  description: __("24-hour, e.g. 18:04") },
			],
			primary_action_label: __("Create"),
			primary_action(values) {
				d.hide();
				frappe.call({
					method: "rapl_payroll_automation.api.attendance_console.create_attendance",
					args: {
						// Bare "HH:MM" -- the server combines it with
						// attendance_date. Building the datetime here was the
						// source of silently-created records with no punches.
						rows: JSON.stringify([{
							employee,
							attendance_date: date,
							status: values.status,
							in_time: values.in_time || null,
							out_time: values.out_time || null,
						}]),
					},
					freeze: true,
					callback(r) {
						const res = r.message || {};
						(res.created || []).forEach((c) => {
							if ((values.in_time || values.out_time) && !c.in_time && !c.out_time) {
								frappe.msgprint({
									title: __("Created without punch times"),
									indicator: "orange",
									message: __("The times were not saved. Check the format is HH:MM."),
								});
							}
							if (c.status_changed) {
								frappe.msgprint(
									__("Created as {0} instead of what you chose &mdash; an approved Leave Application covers this day.", [c.status])
								);
							}
						});
						(res.failed || []).forEach((f) => frappe.msgprint({
							title: __("Could not create"), indicator: "red", message: f.error,
						}));
						do_load(period());
					},
				});
			},
		});
		d.show();
	});

	// ---------------------------------------------------------------- apply

	function render_footer() {
		const $foot = $body.find(".ac-foot");
		const count = state.pending.size;
		const loaded = state.data ? state.data.loaded_at : null;

		if (!state.data) return $foot.hide();
		const ovs = state.overrides.size;
		$foot.show().html(`
			<span class="${count ? "ac-dirty-text" : "ea-dim"}">${count ? __("{0} unsaved", [count]) : __("No unsaved changes")}</span>
			${ovs ? `<span class="ac-dirty-text">${__("{0} manual override(s)", [ovs])}</span>` : ""}
			<span class="ea-dim">${loaded ? __("Loaded {0}", [loaded.slice(11, 16)]) : ""}</span>
			<span style="flex:1"></span>
			<button class="btn btn-xs ac-draft-ot">${__("OT draft")}</button>
			<button class="btn btn-xs ac-draft-lm">${__("Late mark draft")}</button>
			<button class="btn btn-xs ac-draft-adv">${__("Advance draft")}</button>
			<button class="btn btn-xs ac-discard" ${count ? "" : "disabled"}>${__("Discard")}</button>
			<button class="btn btn-xs btn-primary ac-apply" ${count ? "" : "disabled"}>${__("Apply changes")}</button>
		`);
	}

	$body.on("click", ".ac-discard", () => {
		frappe.confirm(__("Discard {0} unsaved change(s)?", [state.pending.size]), () => {
			state.pending.clear();
			render();
		});
	});

	$body.on("click", ".ac-apply", () => apply(0));

	function apply(confirm_processed) {
		const changes = Array.from(state.pending.values());
		if (!changes.length) return;
		frappe.call({
			method: "rapl_payroll_automation.api.attendance_console.apply_edits",
			args: { changes: JSON.stringify(changes), confirm_processed },
			freeze: true,
			freeze_message: __("Applying..."),
			callback(r) {
				const res = r.message || {};
				(res.applied || []).forEach((a) => state.pending.delete(a.name));

				// Rows the server refused because that employee's OT or Late
				// Mark is already submitted for the period. Correcting them now
				// will not reach payroll -- get_employees() skips anyone already
				// paid -- so the user is told before it happens, not after.
				const needs = (res.failed || []).filter((f) => f.needs_confirmation);
				if (needs.length && !confirm_processed) {
					frappe.confirm(
						needs.map((f) => frappe.utils.escape_html(f.error)).join("<br><br>") +
							"<br><br><b>" + __("Edit anyway?") + "</b>",
						() => apply(1),
						() => { report(res); do_load(period()); }
					);
					return;
				}
				report(res);
				do_load(period());
			},
		});
	}

	function report(res) {
		const applied = (res.applied || []).length;
		const failed = res.failed || [];
		if (!failed.length) {
			frappe.show_alert({ message: __("{0} record(s) updated", [applied]), indicator: "green" });
			return;
		}
		frappe.msgprint({
			title: __("{0} applied, {1} failed", [applied, failed.length]),
			indicator: "orange",
			message: failed.map((f) => `<div><b>${f.name || ""}</b> &mdash; ${frappe.utils.escape_html(f.error)}</div>`).join(""),
		});
	}

	// ---------------------------------------------------------------- drafts

	function make_draft(kind) {
		if (state.pending.size) {
			return frappe.msgprint(
				__("Apply your {0} unsaved change(s) first &mdash; the draft is built from what is saved.", [state.pending.size])
			);
		}
		const range = period();

		// Every employee currently on screen, not just the filter box. With no
		// filter that is the whole loaded set, so one click covers everyone.
		const loaded = (state.data ? state.data.groups : []).map((g) => g.employee);

		// Manual overrides are keyed by employee; passing those employees
		// explicitly is also what lets an OT figure entered by hand survive for
		// someone whose Employee.custom_ot is off.
		const overrides = {};
		state.overrides.forEach((v, k) => { overrides[k] = v; });

		frappe.call({
			method: "rapl_payroll_automation.api.attendance_console.create_processing_draft",
			args: {
				kind,
				start_date: range.start,
				end_date: range.end,
				employees: loaded.length ? JSON.stringify(loaded) : null,
				overrides: Object.keys(overrides).length ? JSON.stringify(overrides) : null,
			},
			freeze: true,
			callback(r) {
				const res = r.message;
				if (!res) return;
				const errors = (res.result && res.result.errors) || [];
				const overridden = res.overridden || [];
				frappe.msgprint({
					title: res.reused ? __("Draft refreshed") : __("Draft created"),
					indicator: "blue",
					message:
						`<p><a href="${res.route}" target="_blank">${res.name}</a></p>` +
						`<p class="text-muted">${__("Rows: {0} &rarr; {1}", [res.rows_before, res.rows_after])}` +
						(res.filtered ? ` &middot; ${__("filtered")}` : "") +
						(overridden.length ? ` &middot; ${__("{0} manual override(s) applied", [overridden.length])}` : "") +
						"</p>" +
						(errors.length
							? `<div class="text-muted">${errors.map((e) => frappe.utils.escape_html(e)).join("<br>")}</div>`
							: ""),
				});
			},
		});
	}

	$body.on("click", ".ac-draft-adv", () => {
		const picked = [];
		(state.data.groups || []).forEach((g) => {
			(state.adv.get(g.employee) || new Set()).forEach((a) => picked.push(a));
		});
		if (!picked.length) return frappe.msgprint(__("No advances selected."));
		frappe.confirm(
			__("Create {0} draft recovery record(s)?", [picked.length]),
			() => frappe.call({
				method: "rapl_payroll_automation.api.attendance_console.create_advance_drafts",
				args: { advances: JSON.stringify(picked) },
				freeze: true,
				callback(r) {
					const res = r.message || {};
					frappe.msgprint({
						title: __("{0} created, {1} failed",
								  [(res.created || []).length, (res.failed || []).length]),
						indicator: (res.failed || []).length ? "orange" : "green",
						message:
							(res.created || []).map((c) =>
								`<div><a href="${c.route}" target="_blank">${c.name}</a> &mdash; ${c.advance} ${format_currency(c.amount)}</div>`).join("") +
							(res.failed || []).map((f) =>
								`<div class="text-muted">${f.advance}: ${frappe.utils.escape_html(f.error)}</div>`).join(""),
					});
					do_load(period());
				},
			})
		);
	});

	$body.on("click", ".ac-draft-ot", () => make_draft("overtime"));
	$body.on("click", ".ac-draft-lm", () => make_draft("late_mark"));
};
