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

	function render() {
		if (!state.data || !state.data.groups.length) {
			status(__("Nothing to show for this period."));
			render_footer();
			return;
		}
		status("");
		const bands = state.data.bands || [];
		const html = state.data.groups.map((g) => render_group(g, bands)).join("");
		$body.find(".ac-out").html(
			`<div class="ac-groups">${render_emp_header(bands)}${html}</div>`
		);
		render_bulk();
		render_footer();
	}

	function render_emp_header(bands) {
		// The summary row previously had no header, so its numbers read as
		// unexplained gaps. These are the RAPL Overtime / Late Mark Processing
		// Entry columns, in the same order the entry rows carry them.
		const band_th = bands
			.map((b) => `<th class="ac-num" style="width:5%">${frappe.utils.escape_html(b.label)}</th>`)
			.join("");
		return `
			<table class="ea-table ac-emp-head">
				<thead><tr>
					<th style="width:3%"></th>
					<th style="width:17%">${__("Employee")}</th>
					<th style="width:7%">${__("Grade")}</th>
					<th style="width:12%">${__("Attendance")}</th>
					<th class="ac-num" style="width:8%">${__("OT h:mm")}</th>
					<th class="ac-num" style="width:6%">${__("OT h")}</th>
					<th class="ac-num" style="width:8%">${__("Rate/hr")}</th>
					<th class="ac-num" style="width:9%">${__("OT")} &#8377;</th>
					${band_th}
					<th class="ac-num" style="width:8%">${__("Per day")} &#8377;</th>
					<th class="ac-num" style="width:9%">${__("Cut")} &#8377;</th>
					<th class="ac-num" style="width:5%">&#9873;</th>
				</tr></thead>
			</table>`;
	}

	function ov(employee, field, fallback) {
		const o = state.overrides.get(employee);
		return o && field in o ? o[field] : fallback;
	}

	function set_ov(employee, field, value) {
		const o = state.overrides.get(employee) || {};
		o[field] = value;
		state.overrides.set(employee, o);
	}

	function hhmm_from_seconds(seconds) {
		const s = Math.max(0, Math.round(flt(seconds)));
		return `${String(Math.floor(s / 3600)).padStart(2, "0")}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}`;
	}

	function seconds_from_hhmm(text) {
		const m = String(text || "").trim().match(/^(\d{1,3}):([0-5]?\d)$/);
		if (!m) return null;
		return parseInt(m[1], 10) * 3600 + parseInt(m[2], 10) * 60;
	}

	function render_group(g, bands) {
		const open = state.expanded.has(g.employee);
		const s = g.summary;
		const e = g.entry;
		const emp = g.employee;
		const ot_secs = ov(emp, "ot_hours_hhmm", e.ot_hours_hhmm);
		const ot_hours = flt(ot_secs) / 3600;
		const ot_rate = ov(emp, "ot_rate", e.ot_rate);
		const ot_amount = ov(emp, "amount", e.ot_amount);
		const per_day = ov(emp, "per_day_rate", e.per_day_rate);
		const late_amount = ov(emp, "late_amount", e.late_amount);
		const dirty = state.overrides.has(emp);

		const band_cells = bands
			.map((b, i) => {
				const v = ov(emp, `band_${i + 1}_count`, e.band_counts[b.label] || 0);
				return `<td class="ac-num"><input class="ac-cell ac-sum ac-band" data-employee="${emp}" data-field="band_${i + 1}_count" value="${v || ""}" placeholder="0"></td>`;
			})
			.join("");

		const processed = [];
		if (e.ot_already_processed) processed.push(__("OT paid"));
		if (e.late_already_processed) processed.push(__("Late paid"));

		return `
			<div class="ac-group" data-employee="${g.employee}">
				<table class="ea-table ac-emp">
					<tr class="${open ? "ac-open" : ""} ${dirty ? "ac-ov" : ""}">
						<td style="width:3%"><i class="ac-caret fa fa-chevron-${open ? "down" : "right"}"></i></td>
						<td style="width:17%"><b>${g.employee}</b> ${frappe.utils.escape_html(g.employee_name || "")}${
							g.employee_status && g.employee_status !== "Active"
								? ` <span class="ea-dim">&middot; ${__("left")} ${g.relieving_date ? frappe.datetime.str_to_user(g.relieving_date) : ""}</span>`
								: ""
						}
							${g.ot_eligible ? "" : `<span class="ea-dim" title="${__("Employee.custom_ot is off -- automatic OT is not calculated, but you can still enter it by hand")}">&middot; ${__("manual OT")}</span>`}</td>
						<td style="width:7%" class="ea-dim">${frappe.utils.escape_html(g.grade || "")}</td>
						<td style="width:12%" class="ea-dim">P ${s.present} &middot; HD ${s.half_day} &middot; L ${s.on_leave}</td>
						<td style="width:8%" class="ac-num"><input class="ac-cell ac-sum" data-employee="${emp}" data-field="ot_hours_hhmm" value="${ot_secs ? hhmm_from_seconds(ot_secs) : ""}" placeholder="0:00"></td>
						<td style="width:6%" class="ac-num ea-dim">${ot_hours ? flt(ot_hours, 2) : "&mdash;"}</td>
						<td style="width:8%" class="ac-num"><input class="ac-cell ac-sum" data-employee="${emp}" data-field="ot_rate" value="${ot_rate || ""}" placeholder="0"></td>
						<td style="width:9%" class="ac-num"><input class="ac-cell ac-sum ea-ot" data-employee="${emp}" data-field="amount" value="${ot_amount || ""}" placeholder="0"></td>
						${band_cells}
						<td style="width:8%" class="ac-num"><input class="ac-cell ac-sum" data-employee="${emp}" data-field="per_day_rate" value="${per_day || ""}" placeholder="0"></td>
						<td style="width:9%" class="ac-num"><input class="ac-cell ac-sum ea-cut" data-employee="${emp}" data-field="late_amount" value="${late_amount || ""}" placeholder="0"></td>
						<td style="width:5%" class="ac-num">
							${s.red_flags ? `<span class="ea-cut">${s.red_flags}</span>` : ""}
							${s.amber_flags ? `<span class="ea-late" style="margin-left:4px">${s.amber_flags}</span>` : ""}
							${!s.red_flags && !s.amber_flags ? '<span class="ea-dim">&mdash;</span>' : ""}
						</td>
					</tr>
				</table>
				${processed.length ? `<div class="ac-note">${processed.join(" &middot; ")} &mdash; ${__("already in Additional Salary for this period")}</div>` : ""}
				<div class="ac-days" style="display:${open ? "block" : "none"}">${open ? render_days(g) : ""}</div>
			</div>`;
	}

	function render_days(g) {
		const rows = g.rows.map((row) => render_day(g, row)).join("");
		return `
			<table class="ea-table ac-day-table">
				<thead><tr>
					<th style="width:5%"></th><th style="width:5%"></th>
					<th style="width:14%">${__("Date")}</th>
					<th style="width:13%">${__("In")}</th>
					<th style="width:13%">${__("Out")}</th>
					<th style="width:9%" class="ac-num">${__("Hrs")}</th>
					<th style="width:17%">${__("Status")}</th>
					<th style="width:12%">${__("Late")}</th>
					<th style="width:12%" class="ac-num">${__("OT")}</th>
				</tr></thead>
				<tbody>${rows}</tbody>
			</table>`;
	}

	function render_day(g, row) {
		const att = row.attendance;
		const date_label = `${row.day_label} ${row.date.slice(8, 10)}`;
		const flag = top_flag(row.flags);
		const flag_cell = flag
			? `<td class="${flag.level === "red" ? "ea-cut" : flag.level === "amber" ? "ea-late" : "ea-dim"}" title="${frappe.utils.escape_html(flag.message)}">&#9679;</td>`
			: "<td></td>";

		if (!att) {
			if (row.is_holiday) {
				return `<tr class="ea-off"><td></td>${flag_cell}
					<td class="ea-dim">${date_label}</td>
					<td colspan="3" class="ea-dim">${row.is_weekly_off ? __("Weekly off") : frappe.utils.escape_html(row.holiday_description || __("Holiday"))}</td>
					<td class="ea-dim">&mdash;</td><td class="ea-dim">&mdash;</td><td class="ac-num ea-dim">&mdash;</td></tr>`;
			}
			if (!row.in_service) return "";
			const cancelled = (row.flags || []).some((f) => f.code === "cancelled_only");
			// Attendance.validate() calls validate_active_employee(), which throws
			// on insert for a non-Active employee. Creation is not offered rather
			// than offered and then rejected.
			const action = g.can_create
				? `<button class="btn btn-xs ac-create">${__("Create")}</button>`
				: `<span class="ea-dim">${__("Left")}</span>`;
			return `<tr class="ea-missing" data-date="${row.date}" data-employee="${g.employee}">
					<td></td>${flag_cell}
					<td>${date_label}</td>
					<td colspan="4">${cancelled ? __("Only a cancelled record exists") : __("No attendance record")}</td>
					<td colspan="2" class="ac-num">${action}</td>
				</tr>`;
		}

		const pending = state.pending.get(att.name);
		const dirty = Boolean(pending);
		const val = (f) => (pending && f in pending ? pending[f] : att[f]);
		const checked = state.selected.has(att.name);

		return `<tr class="${dirty ? "ac-dirty" : ""} ${row.is_holiday ? "ea-worked-off" : ""}" data-name="${att.name}">
				<td><input type="checkbox" class="ac-pick" ${checked ? "checked" : ""}></td>
				${flag_cell}
				<td>${date_label}</td>
				<td><input class="ac-in ac-cell" value="${hhmm(val("in_time"))}" placeholder="--:--"></td>
				<td><input class="ac-out ac-cell" value="${hhmm(val("out_time"))}" placeholder="--:--"></td>
				<td class="ac-num">${att.working_hours ? flt(att.working_hours, 2) : '<span class="ea-dim">&mdash;</span>'}</td>
				<td>${status_select(val("status"), att.leave_type)}</td>
				<td>${att.late_mark_band ? `<b class="ea-late">${frappe.utils.escape_html(att.late_mark_band)}</b>` : '<span class="ea-dim">&mdash;</span>'}</td>
				<td class="ac-num">${att.overtime_hours ? `<b>${flt(att.overtime_hours, 2)}</b>` : '<span class="ea-dim">&mdash;</span>'}</td>
			</tr>`;
	}

	function status_select(value, leave_type) {
		if (leave_type) {
			return `<span class="ea-dim" title="${frappe.utils.escape_html(leave_type)}">${frappe.utils.escape_html(value)} &middot; ${__("leave")}</span>`;
		}
		// "On Leave" is deliberately absent. Setting it here writes the status by
		// SQL with no leave_type or leave_application, because check_leave_record()
		// never runs -- payroll would find no leave to deduct against. On Leave
		// belongs to an approved Leave Application.
		const options = ["Present", "Absent", "Half Day", "Work From Home"];
		return `<select class="ac-status ac-cell">${options
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

	$body.on("click", ".ac-emp tr", function (e) {
		if ($(e.target).is("input, select, button")) return;
		const employee = $(this).closest(".ac-group").data("employee");
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

	$body.on("change", ".ac-cell:not(.ac-sum)", function () {
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

	function find_row(employee, name) {
		const g = state.data.groups.find((x) => x.employee === employee);
		if (!g) return null;
		return g.rows.find((r) => r.attendance && r.attendance.name === name);
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
