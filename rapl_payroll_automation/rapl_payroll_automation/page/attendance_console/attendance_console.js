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
			},
			freeze: true,
			freeze_message: __("Reading attendance..."),
			callback(r) {
				state.data = r.message;
				state.selected.clear();
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
	const COLS = [
		{ w: "3%" }, { w: "4%" },
		{ w: "16%", label: () => __("Employee") + " / " + __("Date") },
		{ w: "9%", label: () => __("In") },
		{ w: "9%", label: () => __("Out") },
		{ w: "7%", label: () => __("Hrs"), num: true },
		{ w: "13%", label: () => __("Status") },
		{ w: "10%", label: () => __("Late") },
		{ w: "9%", label: () => __("OT h:mm"), num: true },
		{ w: "6%", label: () => __("OT h"), num: true },
		{ w: "7%", label: () => __("Cut") + " \u20b9", num: true },
		{ w: "7%", label: () => __("OT") + " \u20b9", num: true },
	];

	function render() {
		if (!state.data || !state.data.groups.length) {
			status(__("Nothing to show for this period."));
			render_footer();
			return;
		}
		status("");
		const bands = state.data.bands || [];
		const head = COLS.map(
			(c) => `<th style="width:${c.w}" class="${c.num ? "ac-num" : ""}">${c.label ? c.label() : ""}</th>`
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
				return `<span class="ac-band-wrap"><label>${frappe.utils.escape_html(b.label)}</label>` +
					`<input class="ac-cell ac-sum ac-band" data-employee="${emp}" data-field="band_${i + 1}_count" value="${v || ""}" placeholder="0"></span>`;
			})
			.join("");

		const processed = [];
		if (e.ot_already_processed) processed.push(__("OT paid"));
		if (e.late_already_processed) processed.push(__("Late paid"));

		const rows = [`
			<tr class="ac-emp-row ${open ? "ac-open" : ""} ${dirty ? "ac-ov" : ""}" data-employee="${emp}">
				<td><i class="ac-caret fa fa-chevron-${open ? "down" : "right"}"></i></td>
				<td class="ac-num">${s.red_flags ? `<span class="ea-cut">${s.red_flags}</span>` : ""}${
					s.amber_flags ? `<span class="ea-late" style="margin-left:3px">${s.amber_flags}</span>` : ""
				}${!s.red_flags && !s.amber_flags ? '<span class="ea-dim">&mdash;</span>' : ""}</td>
				<td><b>${emp}</b> ${frappe.utils.escape_html(g.employee_name || "")}${
					g.employee_status && g.employee_status !== "Active"
						? ` <span class="ea-dim">&middot; ${__("left")} ${g.relieving_date ? frappe.datetime.str_to_user(g.relieving_date) : ""}</span>`
						: ""
				}${
					g.ot_eligible ? "" : ` <span class="ea-dim" title="${__("Employee.custom_ot is off - automatic OT is not calculated, but you can still enter it by hand")}">&middot; ${__("manual OT")}</span>`
				}${processed.length ? ` <span class="ea-late">&middot; ${processed.join(" &middot; ")}</span>` : ""}</td>
				<td colspan="4" class="ac-rates">
					<span class="ea-dim">${frappe.utils.escape_html(g.grade || "")} &middot; P ${s.present} &middot; HD ${s.half_day} &middot; L ${s.on_leave}</span>
					<span class="ac-band-wrap"><label>&#8377;/${__("day")}</label>
						<input class="ac-cell ac-sum" data-employee="${emp}" data-field="per_day_rate" value="${per_day || ""}" placeholder="0"></span>
					<span class="ac-band-wrap"><label>&#8377;/${__("hr")}</label>
						<input class="ac-cell ac-sum" data-employee="${emp}" data-field="ot_rate" value="${ot_rate || ""}" placeholder="0"></span>
				</td>
				<td class="ac-rates">${band_inputs}</td>
				<td class="ac-num"><input class="ac-cell ac-sum" data-employee="${emp}" data-field="ot_hours_hhmm" value="${ot_secs ? hhmm_from_seconds(ot_secs) : ""}" placeholder="0:00"></td>
				<td class="ac-num ea-dim">${flt(s.overtime_hours, 2) || "&mdash;"}</td>
				<td class="ac-num"><input class="ac-cell ac-sum ea-cut" data-employee="${emp}" data-field="late_amount" value="${late_amount || ""}" placeholder="0"></td>
				<td class="ac-num"><input class="ac-cell ac-sum ea-ot" data-employee="${emp}" data-field="amount" value="${ot_amount || ""}" placeholder="0"></td>
			</tr>`];

		if (open) rows.push(g.rows.map((row) => render_day(g, row, bands)).join(""));
		return rows.join("");
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
				return `<tr class="ac-day ea-off"><td></td>${flag_cell}
					<td class="ea-dim ac-indent">${date_label}</td>
					<td colspan="3" class="ea-dim">${row.is_weekly_off ? __("Weekly off") : frappe.utils.escape_html(row.holiday_description || __("Holiday"))}</td>
					<td class="ea-dim">&mdash;</td><td class="ea-dim">&mdash;</td>${dash}${dash}${dash}${dash}</tr>`;
			}
			if (!row.in_service) return "";
			const cancelled = (row.flags || []).some((f) => f.code === "cancelled_only");
			const action = g.can_create
				? `<button class="btn btn-xs ac-create">${__("Create")}</button>`
				: `<span class="ea-dim">${__("Left")}</span>`;
			return `<tr class="ac-day ea-missing" data-date="${row.date}" data-employee="${g.employee}">
					<td></td>${flag_cell}
					<td class="ac-indent">${date_label}</td>
					<td colspan="5">${cancelled ? __("Only a cancelled record exists") : __("No attendance record")}</td>
					<td colspan="4" class="ac-num">${action}</td>
				</tr>`;
		}

		const pending = state.pending.get(att.name);
		const val = (f) => (pending && f in pending ? pending[f] : att[f]);
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
				<td>${band_select(val("custom_late_mark_band"), att, bands)}</td>
				<td class="ac-num"><input class="ac-cell ac-day-ot ${att.overtime_manual ? "ac-pinned" : ""}"
					value="${hhmm_from_seconds(flt(val("custom_overtime_hours")) * 3600)}" placeholder="0:00"
					title="${att.overtime_manual ? __("Set by hand - the rules will not recalculate this day") : __("Leave blank for automatic")}"></td>
				<td class="ac-num ea-dim">${att.overtime_hours ? flt(att.overtime_hours, 2) : "&mdash;"}</td>
				<td class="ac-num">${cut ? `<span class="ea-cut">${Math.round(cut)}</span>` : '<span class="ea-dim">&mdash;</span>'}</td>
				<td class="ac-num">${ot_amount ? `<span class="ea-ot">${Math.round(ot_amount)}</span>` : '<span class="ea-dim">&mdash;</span>'}</td>
			</tr>`;
	}

	function band_select(value, att, bands) {
		// Editable so a single day's late mark can be waived without falsifying
		// the punch. Choosing anything here sets custom_late_mark_manual, which
		// stops the rules recalculating this day; "auto" clears it again.
		const opts = ['<option value="">' + __("none") + "</option>"]
			.concat(bands.map((b) =>
				`<option value="${frappe.utils.escape_html(b.label)}" ${b.label === value ? "selected" : ""}>${frappe.utils.escape_html(b.label)}</option>`))
			.join("");
		return `<select class="ac-cell ac-day-band ${att.late_mark_manual ? "ac-pinned" : ""}"
			title="${att.late_mark_manual ? __("Set by hand - the rules will not recalculate this day") : __("Automatic")}">
			<option value="__auto__">${__("auto")}</option>${opts}</select>`;
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
		const auto_hd = att.leave_type && !att.genuine_leave;
		return `<select class="ac-status ac-cell" ${auto_hd ? 'title="' + __("Auto half day - editable") + '"' : ""}>${options
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
			<button class="btn btn-xs ac-draft-ot">${__("Create OT draft")}</button>
			<button class="btn btn-xs ac-draft-lm">${__("Create late mark draft")}</button>
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

	$body.on("click", ".ac-draft-ot", () => make_draft("overtime"));
	$body.on("click", ".ac-draft-lm", () => make_draft("late_mark"));
};
