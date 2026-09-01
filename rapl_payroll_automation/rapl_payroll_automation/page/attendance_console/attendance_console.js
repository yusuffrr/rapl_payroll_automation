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
		$body.find(".ac-out").html(`<div class="ac-groups">${html}</div>`);
		render_bulk();
		render_footer();
	}

	function render_group(g, bands) {
		const open = state.expanded.has(g.employee);
		const s = g.summary;
		const e = g.entry;
		const band_cells = bands
			.map((b) => `<td class="ac-num">${e.band_counts[b.label] || '<span class="ea-dim">&mdash;</span>'}</td>`)
			.join("");

		const processed = [];
		if (e.ot_already_processed) processed.push(__("OT paid"));
		if (e.late_already_processed) processed.push(__("Late paid"));

		return `
			<div class="ac-group" data-employee="${g.employee}">
				<table class="ea-table ac-emp">
					<tr class="${open ? "ac-open" : ""}">
						<td style="width:4%"><i class="ac-caret fa fa-chevron-${open ? "down" : "right"}"></i></td>
						<td style="width:20%"><b>${g.employee}</b> ${frappe.utils.escape_html(g.employee_name || "")}</td>
						<td style="width:8%" class="ea-dim">${frappe.utils.escape_html(g.grade || "")}</td>
						<td style="width:14%" class="ea-dim">P ${s.present} &middot; HD ${s.half_day} &middot; L ${s.on_leave}</td>
						<td style="width:8%" class="ac-num">${g.ot_eligible ? flt(e.ot_hours, 2) : '<span class="ea-dim">n/a</span>'}</td>
						<td style="width:9%" class="ac-num ea-dim">${g.ot_eligible ? format_currency(e.ot_rate) : "&mdash;"}</td>
						<td style="width:10%" class="ac-num ea-ot">${g.ot_eligible && e.ot_amount ? format_currency(e.ot_amount) : '<span class="ea-dim">&mdash;</span>'}</td>
						${band_cells}
						<td style="width:10%" class="ac-num ea-cut">${e.late_amount ? format_currency(e.late_amount) : '<span class="ea-dim">&mdash;</span>'}</td>
						<td style="width:9%" class="ac-num">
							${s.red_flags ? `<span class="ea-cut">${s.red_flags}</span>` : ""}
							${s.amber_flags ? `<span class="ea-late" style="margin-left:4px">${s.amber_flags}</span>` : ""}
							${!s.red_flags && !s.amber_flags ? '<span class="ea-dim">clean</span>' : ""}
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
			return `<tr class="ea-missing" data-date="${row.date}" data-employee="${g.employee}">
					<td></td>${flag_cell}
					<td>${date_label}</td>
					<td colspan="4">${__("No attendance record")}</td>
					<td colspan="2" class="ac-num"><button class="btn btn-xs ac-create">${__("Create")}</button></td>
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
		const options = ["Present", "Absent", "Half Day", "On Leave", "Work From Home"];
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

	$body.on("change", ".ac-cell", function () {
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
		if (!time_value) return null;
		const row = find_row($tr.closest(".ac-group").data("employee"), $tr.data("name"));
		if (!row) return null;
		const t = time_value.length === 5 ? time_value + ":00" : time_value;
		return `${row.date} ${t}`;
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
				${["Present", "Absent", "Half Day", "On Leave"].map((o) => `<option>${o}</option>`).join("")}
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
			if (out) {
				const row = row_for(name);
				entry.out_time = row ? `${row.date} ${out.length === 5 ? out + ":00" : out}` : entry.out_time;
			}
			if (st) entry.status = st;
			if (!("in_time" in entry)) entry.in_time = att.in_time;
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
				  options: ["Present", "Absent", "Half Day", "On Leave", "Work From Home"], default: "Present" },
				{ fieldname: "in_time", label: __("In"), fieldtype: "Time" },
				{ fieldname: "out_time", label: __("Out"), fieldtype: "Time" },
			],
			primary_action_label: __("Create"),
			primary_action(values) {
				d.hide();
				frappe.call({
					method: "rapl_payroll_automation.api.attendance_console.create_attendance",
					args: {
						rows: JSON.stringify([{
							employee,
							attendance_date: date,
							status: values.status,
							in_time: values.in_time ? `${date} ${values.in_time}` : null,
							out_time: values.out_time ? `${date} ${values.out_time}` : null,
						}]),
					},
					freeze: true,
					callback(r) {
						const res = r.message || {};
						(res.created || []).forEach((c) => {
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
		$foot.show().html(`
			<span class="${count ? "ac-dirty-text" : "ea-dim"}">${count ? __("{0} unsaved", [count]) : __("No unsaved changes")}</span>
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

	$body.on("click", ".ac-apply", () => {
		const changes = Array.from(state.pending.values());
		if (!changes.length) return;
		frappe.call({
			method: "rapl_payroll_automation.api.attendance_console.apply_edits",
			args: { changes: JSON.stringify(changes) },
			freeze: true,
			freeze_message: __("Applying..."),
			callback(r) {
				const res = r.message || {};
				(res.applied || []).forEach((a) => state.pending.delete(a.name));
				report(res);
				do_load(period());
			},
		});
	});

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
		const employee = employee_field.get_value();
		frappe.call({
			method: "rapl_payroll_automation.api.attendance_console.create_processing_draft",
			args: {
				kind,
				start_date: range.start,
				end_date: range.end,
				employees: employee ? JSON.stringify([employee]) : null,
			},
			freeze: true,
			callback(r) {
				const res = r.message;
				if (!res) return;
				const errors = (res.result && res.result.errors) || [];
				frappe.msgprint({
					title: res.reused ? __("Draft refreshed") : __("Draft created"),
					indicator: "blue",
					message:
						`<p><a href="${res.route}" target="_blank">${res.name}</a></p>` +
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
