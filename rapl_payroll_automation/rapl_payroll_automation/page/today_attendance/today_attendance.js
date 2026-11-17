// Copyright (c) 2026, RAPL and contributors
//
// Today's Attendance -- live board from biometric Employee Checkin.
// Read-only. Follows the Regular Shift Type window, so it matches the
// Attendance that HRMS auto-attendance creates the next day.

frappe.pages["today-attendance"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Today's Attendance"),
		single_column: true,
	});
	wrapper.today_attendance = new TodayAttendance(page);
};

frappe.pages["today-attendance"].on_page_show = function (wrapper) {
	const board = wrapper.today_attendance;
	if (!board) return;
	board.apply_route_options();
	board.refresh(true);
};

const TA_METHOD = "rapl_payroll_automation.api.live_attendance.get_status";
const TA_ADD_METHOD = "rapl_payroll_automation.api.live_attendance.add_missing_punch";
const TA_REFRESH_MS = 2 * 60 * 1000;

const TA_CARDS = [
	{ key: "Absent", color: "red" },
	{ key: "On Leave", color: "blue" },
	{ key: "Present", color: "green" },
	{ key: "Overnight", color: "cyan" },
	{ key: "Total", color: "gray" },
];
// statuses without their own card still get a coloured pill in the Total view
const TA_PILL = { "On Duty": "purple", Holiday: "gray" };

class TodayAttendance {
	constructor(page) {
		this.page = page;
		this.active = "Absent";
		this.apply_route_options();
		this.data = null;
		this.refresh_debounced = frappe.utils.debounce(() => this.refresh(), 300);

		this.make_filters();
		this.make_layout();

		this.page.set_primary_action(__("Refresh"), () => this.refresh(), "refresh");

		this.refresh();
		this.timer = setInterval(() => {
			if (frappe.get_route()[0] === "today-attendance") {
				this.refresh(true);
			}
		}, TA_REFRESH_MS);
	}

	// A number card (or a link like /app/today-attendance?status=Present) can choose the card.
	apply_route_options() {
		const opts = frappe.route_options || {};
		const qs = new URLSearchParams(window.location.search);
		const wanted = opts.status || qs.get("status");
		if (wanted && TA_CARDS.some((c) => c.key === wanted)) {
			this.active = wanted;
			if (this.data) this.render();
		}
		if (opts.status) delete frappe.route_options.status;
	}

	make_filters() {
		this.f_grade = this.page.add_field({
			fieldname: "grade", label: __("Grade"), fieldtype: "Link", options: "Employee Grade",
			change: () => this.refresh_debounced(),
		});
		$(`<button class="btn btn-default btn-sm ta-copy">${__("Copy Absent List")}</button>`)
			.on("click", () => this.copy_list("Absent"))
			.insertAfter(this.f_grade.$wrapper);
	}

	make_layout() {
		this.$wrap = $(`
			<div class="ta-wrap">
				<style>
					.ta-wrap { padding: var(--padding-md) 0; }
					.ta-meta { margin-bottom: var(--margin-md); color: var(--text-muted); font-size: var(--text-sm); }
					.ta-meta .ta-stale { color: var(--red-600, #e03636); font-weight: 600; }
					.ta-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: var(--margin-sm); margin-bottom: var(--margin-lg); }
					.ta-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: var(--border-radius-md);
						padding: 12px 14px; cursor: pointer; transition: box-shadow .15s, border-color .15s; }
					.ta-card:hover { box-shadow: var(--shadow-sm); }
					.ta-card.active { border-color: var(--primary); box-shadow: 0 0 0 1px var(--primary); }
					.ta-card .ta-num { font-size: 26px; font-weight: 700; line-height: 1.2; color: var(--heading-color); }
					.ta-card .ta-lbl { font-size: var(--text-sm); }
					.ta-table { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: var(--border-radius-md); overflow-x: auto; }
					.ta-table table { margin: 0; }
					.ta-table th { font-size: var(--text-sm); color: var(--text-muted); font-weight: 500; white-space: nowrap; background: var(--subtle-fg); }
					.ta-table td { font-size: var(--text-md); vertical-align: middle; }
					.ta-dept { background: var(--subtle-accent); font-weight: 600; font-size: var(--text-sm); }
					.indicator-pill.cyan { background: var(--cyan-50, #e6f7fa); color: var(--cyan-700, #0b7285); }
					.ta-add { margin-left: 6px; }
					.ta-copy { align-self: center; margin-left: var(--margin-sm); }
					.ta-empty { padding: 40px; text-align: center; color: var(--text-muted); }
				</style>
				<div class="ta-meta"></div>
				<div class="ta-cards"></div>
				<div class="ta-table"></div>
			</div>`).appendTo(this.page.main);

		this.$meta = this.$wrap.find(".ta-meta");
		this.$cards = this.$wrap.find(".ta-cards");
		this.$table = this.$wrap.find(".ta-table");

		this.$table.on("click", ".ta-add", (e) => {
			const $b = $(e.currentTarget);
			this.add_missing_punch($b.attr("data-emp"), $b.attr("data-name"));
		});

		this.$cards.on("click", ".ta-card", (e) => {
			this.active = $(e.currentTarget).attr("data-key");
			this.render();
		});
	}

	refresh(silent) {
		return frappe
			.call({
				method: TA_METHOD,
				args: { grade: this.f_grade.get_value() },
				freeze: !silent,
			})
			.then((r) => {
				this.data = r.message || { rows: [], summary: {} };
				this.render();
			});
	}

	render() {
		const d = this.data;
		if (!d) return;
		const s = d.summary || {};

		// show nothing unless the biometric sync looks stuck
		let warn = "";
		if (d.last_checkin && moment().diff(moment(d.last_checkin), "minutes") > 60) {
			warn = `${__("No punches synced since")} ${frappe.datetime.prettyDate(d.last_checkin)} — ${__("biometric sync may be down, numbers may be wrong")}`;
		}
		this.$meta.html(warn ? `<span class="ta-stale">${warn}</span>` : "").toggle(!!warn);

		// cards
		this.$cards.html(
			TA_CARDS.map(
				(c) => `
				<div class="ta-card ${this.active === c.key ? "active" : ""}" data-key="${c.key}">
					<div class="ta-num">${s[c.key] || 0}</div>
					<div class="ta-lbl"><span class="indicator-pill no-indicator-dot ${c.color}">${__(c.key)}</span></div>
				</div>`
			).join("")
		);

		// table
		const rows = this.filtered_rows();
		if (!rows.length) {
			this.$table.html(`<div class="ta-empty">${__("No employees in")} "${__(this.active)}"</div>`);
			return;
		}

		const esc = frappe.utils.escape_html;
		const color_of = { ...TA_PILL, ...Object.fromEntries(TA_CARDS.map((c) => [c.key, c.color])) };
		let last_dept = null;
		const body = rows
			.map((r) => {
				let head = "";
				if (r.department !== last_dept) {
					last_dept = r.department;
					const n = rows.filter((x) => x.department === r.department).length;
					head = `<tr><td colspan="4" class="ta-dept">${esc(r.department || __("No Department"))} (${n})</td></tr>`;
				}
				const notes = [];
				if (r.late_by) notes.push(`<span class="text-danger">${__("Late by {0} min", [r.late_by])}</span>`);
				if (r.note) notes.push(esc(r.note));
				const add_btn = r.status === "Absent"
					? `<button class="btn btn-xs btn-default ta-add" data-emp="${esc(r.employee)}" data-name="${esc(r.employee_name)}">+ ${__("Add missing punch")}</button>`
					: "";
				return `${head}<tr>
					<td><a href="/app/employee/${encodeURIComponent(r.employee)}">${esc(r.employee_name)}</a></td>
					<td><span class="indicator-pill ${color_of[r.status] || "gray"}">${__(r.status)}</span></td>
					<td style="white-space:nowrap">${r.first_in || ""}</td>
					<td>${notes.join(" · ")} ${add_btn}</td>
				</tr>`;
			})
			.join("");

		this.$table.html(`
			<table class="table table-hover">
				<thead><tr>
					<th>${__("Employee")}</th><th>${__("Status")}</th><th>${__("In Time")}</th><th>${__("Note")}</th>
				</tr></thead>
				<tbody>${body}</tbody>
			</table>`);
	}

	// Someone is at work but the board says Absent: they forgot to punch in.
	// HR adds the arrival time; tomorrow's auto-attendance then builds the day correctly.
	add_missing_punch(employee, employee_name) {
		frappe.prompt(
			[{
				fieldname: "time", fieldtype: "Time", label: __("Arrival time"), reqd: 1,
				default: "09:30:00",
				description: __("When {0} actually arrived today.", [employee_name]),
			}],
			(values) => {
				frappe.call({
					method: TA_ADD_METHOD,
					args: { employee, time: values.time },
					freeze: true,
				}).then((r) => {
					frappe.show_alert({
						message: __("Punch added for {0} at {1}", [employee_name, r.message.time]),
						indicator: "green",
					});
					this.refresh(true);
				});
			},
			__("Add missing punch: {0}", [employee_name]),
			__("Add punch")
		);
	}

	filtered_rows() {
		const rows = (this.data && this.data.rows) || [];
		if (this.active === "Total") return rows;
		if (this.active === "Overnight") return rows.filter((r) => r.overnight);
		return rows.filter((r) => r.status === this.active);
	}

	copy_list(status) {
		const rows = ((this.data && this.data.rows) || []).filter((r) => r.status === status);
		if (!rows.length) {
			frappe.show_alert({ message: __("Nobody in {0}", [__(status)]), indicator: "green" });
			return;
		}
		const text =
			`*${status} – ${frappe.datetime.str_to_user(this.data.date)}* (${rows.length})\n` +
			rows.map((r, i) => `${i + 1}. ${r.employee_name}${r.department ? " – " + r.department : ""}`).join("\n");
		frappe.utils.copy_to_clipboard(text);
	}
}
