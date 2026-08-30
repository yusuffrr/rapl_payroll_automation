// Copyright (c) 2026, RAPL and contributors
//
// Employee Attendance -- read-only monthly statement.
// Replaces the hrms_custom "Attendance Export" page.
//
// Money columns (rate block, Cut, OT amount) are NOT hidden here. The server
// omits them entirely for non-HR callers -- see employee_attendance.py. This
// file only renders what it was given: if statement.show_money is false the
// fields are absent, not concealed.

frappe.pages["employee-attendance"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Employee Attendance"),
		single_column: true,
	});

	const state = { employees: [], statements: [], bulk: false };

	const month_field = page.add_field({
		fieldname: "month",
		label: __("Month"),
		fieldtype: "Select",
		options: [
			"January", "February", "March", "April", "May", "June",
			"July", "August", "September", "October", "November", "December",
		],
		default: frappe.datetime.get_today().split("-")[1],
	});

	const year_field = page.add_field({
		fieldname: "year",
		label: __("Year"),
		fieldtype: "Select",
		options: build_year_options(),
		default: String(new Date().getFullYear()),
	});

	const employee_field = page.add_field({
		fieldname: "employee",
		label: __("Employee"),
		fieldtype: "Select",
		options: [],
	});

	const $body = $(`
		<div class="ea-wrap" style="padding: 12px 0;">
			<div class="ea-status text-muted" style="padding: 24px 0; text-align: center;">
				${__("Choose a month and employee, then Load.")}
			</div>
			<div class="ea-out"></div>
		</div>
	`).appendTo(page.main);

	set_month_default(month_field);

	page.set_primary_action(__("Load"), load);
	page.add_inner_button(__("Print"), () => window.print());

	frappe.call({
		method: "rapl_payroll_automation.api.employee_attendance.get_selectable_employees",
		callback(r) {
			state.employees = r.message || [];
			const options = state.employees.map((e) => `${e.name} :: ${e.employee_name}`);
			if (state.employees.length > 1) options.unshift(__("All employees"));
			employee_field.df.options = options;
			employee_field.refresh();
			if (options.length) employee_field.set_value(options[0]);
		},
	});

	function build_year_options() {
		const now = new Date().getFullYear();
		const years = [];
		for (let y = now + 1; y >= now - 4; y--) years.push(String(y));
		return years;
	}

	function set_month_default(field) {
		const names = [
			"January", "February", "March", "April", "May", "June",
			"July", "August", "September", "October", "November", "December",
		];
		field.set_value(names[new Date().getMonth()]);
	}

	function period() {
		const names = [
			"January", "February", "March", "April", "May", "June",
			"July", "August", "September", "October", "November", "December",
		];
		const month_index = names.indexOf(month_field.get_value());
		const year = parseInt(year_field.get_value(), 10);
		if (month_index < 0 || !year) return null;
		const start = new Date(year, month_index, 1);
		const end = new Date(year, month_index + 1, 0);
		return { start: to_iso(start), end: to_iso(end) };
	}

	function to_iso(d) {
		const m = String(d.getMonth() + 1).padStart(2, "0");
		const day = String(d.getDate()).padStart(2, "0");
		return `${d.getFullYear()}-${m}-${day}`;
	}

	function status(message) {
		$body.find(".ea-status").text(message).toggle(Boolean(message));
	}

	function load() {
		const range = period();
		if (!range) {
			frappe.msgprint(__("Choose a month and year."));
			return;
		}

		const selected = employee_field.get_value();
		if (!selected) {
			frappe.msgprint(__("Choose an employee."));
			return;
		}

		$body.find(".ea-out").empty();
		status(__("Loading..."));

		const all = selected === __("All employees");
		if (all) {
			frappe.call({
				method: "rapl_payroll_automation.api.employee_attendance.get_bulk_statements",
				args: {
					employees: JSON.stringify(state.employees.map((e) => e.name)),
					start_date: range.start,
					end_date: range.end,
				},
				freeze: true,
				freeze_message: __("Building statements..."),
				callback(r) {
					state.statements = r.message || [];
					render(state.statements);
				},
			});
		} else {
			frappe.call({
				method: "rapl_payroll_automation.api.employee_attendance.get_statement",
				args: {
					employee: selected.split(" :: ")[0],
					start_date: range.start,
					end_date: range.end,
				},
				freeze: true,
				callback(r) {
					state.statements = r.message ? [r.message] : [];
					render(state.statements);
				},
			});
		}
	}

	function render(statements) {
		if (!statements.length) {
			status(__("Nothing to show for this period."));
			return;
		}
		status("");
		const html = statements
			.map((s, i) => render_statement(s, i < statements.length - 1))
			.join("");
		$body.find(".ea-out").html(html);
	}

	function money(value) {
		return format_currency(flt(value), frappe.defaults.get_default("currency"));
	}

	function render_statement(s, page_break) {
		const show = s.show_money;
		const pay = s.pay || {};
		const totals = s.totals || {};
		const brk = page_break ? "page-break-after: always;" : "";

		const rate_block = show
			? `<div class="ea-rates">
					<div><span>${__("Monthly salary")}</span><b>${money(pay.monthly_salary)}</b></div>
					<div><span>${__("Per day")} &middot; ${pay.ot_working_days} ${__("days")}</span><b>${money(pay.per_day_rate)}</b></div>
					<div><span>${__("Per hour")} &middot; &divide;${pay.ot_working_days ? "" : ""}8</span><b>${money(pay.hourly_rate)}</b></div>
				</div>`
			: "";

		const money_headers = show
			? `<th class="ea-num">${__("Cut")}</th><th class="ea-num">${__("OT")} &#8377;</th>`
			: "";

		const rows = s.rows.map((row) => render_row(row, show)).join("");

		return `
			<div class="ea-statement" style="${brk}">
				<div class="ea-head">
					<div>
						<span class="ea-name">${frappe.utils.escape_html(s.employee_name || s.employee)}</span>
						<span class="ea-meta">${s.employee}${s.grade ? " &middot; " + frappe.utils.escape_html(s.grade) : ""}</span>
					</div>
					<div class="ea-meta">${frappe.datetime.str_to_user(s.start_date)} &ndash; ${frappe.datetime.str_to_user(s.end_date)}</div>
				</div>
				${rate_block}
				<table class="ea-table">
					<thead>
						<tr>
							<th style="width:13%">${__("Date")}</th>
							<th style="width:11%">${__("In")}</th>
							<th style="width:11%">${__("Out")}</th>
							<th class="ea-num" style="width:9%">${__("Hrs")}</th>
							<th style="width:16%">${__("Status")}</th>
							<th style="width:10%">${__("Late")}</th>
							${show ? '<th class="ea-num" style="width:10%"></th>' : ""}
							<th class="ea-num" style="width:9%">${__("OT")}</th>
							${show ? '<th class="ea-num" style="width:11%"></th>' : ""}
						</tr>
					</thead>
					<tbody>${rows}</tbody>
				</table>
				${render_footer(s, show, totals)}
			</div>`;
	}

	function render_row(row, show) {
		const att = row.attendance;
		const date_label = `${row.day_label} ${row.date.slice(8, 10)}`;
		const money_cells = show ? '<td class="ea-num ea-dim">&mdash;</td><td class="ea-num ea-dim">&mdash;</td>' : "";

		if (!att) {
			if (row.is_holiday) {
				const label = row.is_weekly_off
					? __("Weekly off")
					: frappe.utils.escape_html(row.holiday_description || __("Holiday"));
				return `<tr class="ea-off">
						<td class="ea-dim">${date_label}</td>
						<td colspan="3" class="ea-dim">${label}</td>
						<td class="ea-dim">${row.is_weekly_off ? __("Weekly off") : __("Holiday")}</td>
						<td class="ea-dim">&mdash;</td>
						${show ? '<td class="ea-num ea-dim">&mdash;</td>' : ""}
						<td class="ea-num ea-dim">&mdash;</td>
						${show ? '<td class="ea-num ea-dim">&mdash;</td>' : ""}
					</tr>`;
			}
			if (!row.in_service) return "";
			return `<tr class="ea-missing">
					<td>${date_label}</td>
					<td colspan="3">${__("No attendance record")}</td>
					<td>&mdash;</td><td class="ea-dim">&mdash;</td>
					${show ? '<td class="ea-num ea-dim">&mdash;</td>' : ""}
					<td class="ea-num ea-dim">&mdash;</td>
					${show ? '<td class="ea-num ea-dim">&mdash;</td>' : ""}
				</tr>`;
		}

		const worked_off_day = row.is_holiday;
		const cls = worked_off_day ? "ea-worked-off" : "";
		const status_label = worked_off_day
			? __("Off &middot; worked")
			: frappe.utils.escape_html(att.leave_type ? att.status + " &middot; " + att.leave_type : att.status);

		const cut = show
			? att.late_deduction_amount
				? `<td class="ea-num ea-cut">${money(att.late_deduction_amount)}</td>`
				: '<td class="ea-num ea-dim">&mdash;</td>'
			: "";
		const ot_amount = show
			? att.overtime_amount
				? `<td class="ea-num ea-ot">${money(att.overtime_amount)}</td>`
				: '<td class="ea-num ea-dim">&mdash;</td>'
			: "";

		return `<tr class="${cls}">
				<td>${date_label}</td>
				<td>${short_time(att.in_time)}</td>
				<td>${short_time(att.out_time)}</td>
				<td class="ea-num">${att.working_hours ? flt(att.working_hours, 2) : '<span class="ea-dim">&mdash;</span>'}</td>
				<td>${status_label}</td>
				<td>${att.late_mark_band ? '<b class="ea-late">' + frappe.utils.escape_html(att.late_mark_band) + "</b>" : '<span class="ea-dim">&mdash;</span>'}</td>
				${cut}
				<td class="ea-num">${att.overtime_hours ? "<b>" + flt(att.overtime_hours, 2) + "</b>" : '<span class="ea-dim">&mdash;</span>'}</td>
				${ot_amount}
			</tr>`;
	}

	function short_time(value) {
		if (!value) return '<span class="ea-dim">&mdash;</span>';
		return value.slice(11, 16);
	}

	function render_footer(s, show, totals) {
		const bands = s.bands || [];
		const counts = s.summary.band_counts || {};

		const band_lines = bands
			.map((b) => {
				const count = counts[b.label] || 0;
				if (!count) return "";
				const amount = show && s.pay
					? `<span class="ea-cut">${money(Math.round(b.fraction * count * s.pay.per_day_rate))}</span>`
					: "";
				return `<div><span>${count} &times; ${frappe.utils.escape_html(b.label)} ${__("day")}</span>${amount}</div>`;
			})
			.join("");

		return `
			<div class="ea-foot">
				<div>
					<div class="ea-foot-h">${__("Attendance")}</div>
					<div><span>${__("Present")}</span><b>${s.summary.present}</b></div>
					<div><span>${__("Half days")}</span><b>${s.summary.half_day}</b></div>
					<div><span>${__("On leave")}</span><b>${s.summary.on_leave}</b></div>
					<div><span>${__("Weekly off / holiday")}</span><b>${s.summary.holiday}</b></div>
				</div>
				<div>
					<div class="ea-foot-h">${__("Late marks")}</div>
					${band_lines || '<div class="ea-dim">' + __("None") + "</div>"}
					${show ? `<div class="ea-total"><span>${__("Deducted")}</span><b class="ea-cut">${money(totals.late_deduction_amount)}</b></div>` : ""}
				</div>
				<div>
					<div class="ea-foot-h">${__("Overtime")}</div>
					<div><span>${__("Hours")}</span><b>${flt(s.summary.overtime_hours, 2)}</b></div>
					${show ? `<div><span>${flt(s.summary.overtime_hours, 2)} &times; ${money(s.pay.hourly_rate)}</span></div>` : ""}
					${show ? `<div class="ea-total"><span>${__("Earned")}</span><b class="ea-ot">${money(totals.overtime_amount)}</b></div>` : ""}
				</div>
			</div>`;
	}
};
