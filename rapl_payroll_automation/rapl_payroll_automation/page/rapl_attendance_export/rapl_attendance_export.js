frappe.pages["rapl-attendance-export"].on_page_load = function (wrapper) {
    let page = frappe.ui.make_app_page({
        parent: wrapper,
        title: "Monthly Attendance Report",
        single_column: true
    });

    window._bulk_attendance_page = page;

    let state = {
        is_bulk: false,
        employees: [],
        data: {}
    };

    // ─── Filters ─────────────────────────────────────────────────────────────

    let months = ["January","February","March","April","May","June",
                  "July","August","September","October","November","December"];
    let now = new Date();

    let month_field = page.add_field({
        fieldname: "month",
        label: "Month",
        fieldtype: "Select",
        options: months.join("\n"),
        default: months[now.getMonth()],
        change: function() {}
    });

    let year_field = page.add_field({
        fieldname: "year",
        label: "Year",
        fieldtype: "Int",
        default: now.getFullYear(),
        change: function() {}
    });

    let employee_field = page.add_field({
        fieldname: "employee",
        label: "Employee",
        fieldtype: "Link",
        options: "Employee",
        change: function() {}
    });

    // ─── Bulk Toggle Button ───────────────────────────────────────────────────

    let bulk_btn = page.add_field({
        fieldname: "bulk_toggle",
        label: "Switch to Bulk",
        fieldtype: "Button",
        click: function() {
            state.is_bulk = !state.is_bulk;
            toggle_bulk_mode(state, page, employee_field, bulk_btn);
        }
    });

    // ─── Action Buttons ───────────────────────────────────────────────────────

    page.add_inner_button(__("Load Report"), function () {
        load_report(state, page, month_field, year_field, employee_field);
    });

    page.add_inner_button(__("Export PDF"), function () {
        export_pdf(state, month_field, year_field);
    });

    page.add_inner_button(__("Export Excel"), function () {
        export_excel(state, month_field, year_field);
    });

    // ─── Content Area ─────────────────────────────────────────────────────────

    $(wrapper).find(".page-content").append(`
        <div id="bulk-emp-selector" style="display:none; padding:12px; 
            border:1px solid #d1d8dd; border-radius:6px; margin:12px 0;">
            <div style="display:flex; gap:8px; margin-bottom:8px;">
                <button class="btn btn-xs btn-default" id="btn-select-all">Select All</button>
                <button class="btn btn-xs btn-default" id="btn-deselect-all">Deselect All</button>
            </div>
            <input type="text" id="emp-search" class="form-control form-control-sm"
                placeholder="Search employee..." style="margin-bottom:8px;">
            <div id="emp-list" style="max-height:250px; overflow-y:auto;
                border:1px solid #eee; border-radius:4px; padding:8px;">
                <div class="text-muted">Loading employees...</div>
            </div>
        </div>
        <div id="attendance-report-area" style="margin-top:16px;"></div>
    `);

    // Load employee list for bulk mode
    frappe.call({
        method: "frappe.client.get_list",
        args: {
            doctype: "Employee",
            fields: ["name", "employee_name"],
            filters: [["status", "=", "Active"]],
            limit_page_length: 0,
            order_by: "employee_name asc"
        },
        callback: function(r) {
            if (!r.message) return;
            let emp_html = r.message.map(e => `
                <div class="emp-row" style="padding:3px 2px;">
                    <label style="font-weight:normal; cursor:pointer; margin:0;">
                        <input type="checkbox" class="emp-checkbox" value="${esc(e.name)}"
                            data-name="${esc(e.employee_name)}">
                        &nbsp;${esc(e.employee_name)}
                        <span class="text-muted">(${esc(e.name)})</span>
                    </label>
                </div>
            `).join("");
            $("#emp-list").html(emp_html);

            $("#emp-search").on("input", function() {
                let q = $(this).val().toLowerCase();
                $(".emp-row").each(function() {
                    $(this).toggle($(this).text().toLowerCase().includes(q));
                });
            });

            $("#btn-select-all").on("click", function() {
                $(".emp-row:visible .emp-checkbox").prop("checked", true);
            });

            $("#btn-deselect-all").on("click", function() {
                $(".emp-checkbox").prop("checked", false);
            });
        }
    });
};

// ─── Toggle Bulk Mode ─────────────────────────────────────────────────────────

function toggle_bulk_mode(state, page, employee_field, bulk_btn) {
    // The report on screen is cleared, so nothing from it may be exported.
    state.data = {};
    state.employees = [];
    if (state.is_bulk) {
        $(employee_field.wrapper).hide();
        $("#bulk-emp-selector").show();
        $(bulk_btn.wrapper).find("button").text("Switch to Single");
    } else {
        $(employee_field.wrapper).show();
        $("#bulk-emp-selector").hide();
        $(bulk_btn.wrapper).find("button").text("Switch to Bulk");
        $(".emp-checkbox").prop("checked", false);
    }
    $("#attendance-report-area").html("");
}

// ─── Load Report ──────────────────────────────────────────────────────────────

function load_report(state, page, month_field, year_field, employee_field) {
    let month = month_field.get_value();
    let year = year_field.get_value();

    if (!month || !year) {
        frappe.msgprint(__("Please select Month and Year."));
        return;
    }

    let employees = [];

    if (state.is_bulk) {
        $(".emp-checkbox:checked").each(function() {
            employees.push({ id: $(this).val(), name: String($(this).attr("data-name") || "") });
        });
        if (!employees.length) {
            frappe.msgprint(__("Please select at least one employee."));
            return;
        }
    } else {
        let emp = employee_field.get_value();
        if (!emp) {
            frappe.msgprint(__("Please select an Employee."));
            return;
        }
        employees.push({ id: emp, name: emp });
    }

    // The report on screen belongs to THIS month/year; exports use it, not
    // whatever the dropdowns say later. A request id drops a slower, older
    // response that arrives after a newer Load.
    const request_id = (state.request_id || 0) + 1;
    state.request_id = request_id;

    // Show loading indicator in report area
    $("#attendance-report-area").html(`
        <div style="text-align:center; padding:40px; color:#888;">
            <div>Loading attendance data...</div>
        </div>
    `);

    frappe.call({
        method: "rapl_payroll_automation.api.attendance_export.get_bulk_attendance_data",
        args: {
            employees: JSON.stringify(employees.map(e => e.id)),
            month: month,
            year: String(year)
        },
        error: function() {
            if (state.request_id !== request_id) return;
            // Nothing exportable until a load succeeds -- otherwise Export
            // would quietly re-export the PREVIOUS report.
            state.data = {};
            $("#attendance-report-area").html(`
                <div style="text-align:center; padding:40px; color:#c0392b;">
                    ${esc(__("Could not load the report. You may not have access to some of the selected employees."))}
                </div>
            `);
        },
        callback: function(r) {
            if (state.request_id !== request_id) return;
            if (!r.message) {
                state.data = {};
                $("#attendance-report-area").html(`
                    <div style="text-align:center; padding:40px; color:#888;">
                        No data returned.
                    </div>
                `);
                return;
            }

            state.data = r.message;
            state.employees = employees;
            state.month = month;
            state.year = year;

            employees.forEach(e => {
                if (r.message[e.id]) {
                    e.name = r.message[e.id].employee_name;
                }
            });

            render_report(state, month, year);
        }
    });
}

// ─── Render Report ────────────────────────────────────────────────────────────

function render_report(state, month, year) {
    let html = state.employees.map(e => {
        let emp_data = state.data[e.id];
        if (!emp_data) return "";

        let rows = (emp_data.rows || []).map(r => {
            let is_holiday = (r.remarks || "").includes("HY");
            let is_leave = (r.remarks || "").includes("LV");
            let row_bg = is_holiday ? "#fff9e6" : is_leave ? "#e8f4e8" : "";

            return `<tr style="background:${row_bg}">
                <td>${esc(r.attendance_date || "")}</td>
                <td>${esc(r.day_label || "")}</td>
                <td>${esc(r.in_time || "-")}</td>
                <td>${esc(r.out_time || "-")}</td>
                <td>${esc(r.working_hours || 0)}</td>
                <td>${esc(r.ot || 0)}</td>
                <td>${esc(r.status || "")}</td>
                <td>${esc(r.remarks || "")}</td>
            </tr>`;
        }).join("");

        return `
            <div class="emp-section" data-emp="${esc(e.id)}"
                style="margin-bottom:32px; border:1px solid #d1d8dd;
                border-radius:6px; overflow:hidden;">
                <div style="background:#4a5568; color:#fff; padding:10px 16px;
                    font-size:14px; font-weight:600;">
                    ${esc(emp_data.employee_name)}
                    <span style="font-weight:normal; font-size:12px; opacity:0.8;">
                        &nbsp;— ${month} ${year}
                    </span>
                </div>
                <div style="overflow-x:auto;">
                    <table style="width:100%; border-collapse:collapse; font-size:12px;">
                        <thead>
                            <tr style="background:#edf2f7;">
                                <th style="${th}">Date</th>
                                <th style="${th}">Day</th>
                                <th style="${th}">In Time</th>
                                <th style="${th}">Out Time</th>
                                <th style="${th}">Working Hrs</th>
                                <th style="${th}">OT</th>
                                <th style="${th}">Status</th>
                                <th style="${th}">Remarks</th>
                            </tr>
                        </thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>
                <div style="padding:8px 16px; font-size:10px; color:#888; border-top:1px solid #eee;">
                    <strong>Remarks:</strong>
                    IE = Incomplete Entry &nbsp;|&nbsp; EO = Early Out &nbsp;|&nbsp;
                    LM = Late Entry &nbsp;|&nbsp;
                    <span style="background:#fff9e6; padding:1px 4px;">HY = Holiday</span> &nbsp;|&nbsp;
                    <span style="background:#e8f4e8; padding:1px 4px;">LV = Leave</span>
                </div>
            </div>
        `;
    }).join("");

    $("#attendance-report-area").html(html);
}

let th = "padding:8px; text-align:left; border-bottom:2px solid #d1d8dd; white-space:nowrap;";

// ─── Export PDF ───────────────────────────────────────────────────────────────
function export_pdf(state, month_field, year_field) {
    if (!Object.keys(state.data).length) {
        frappe.msgprint(__("Please load the report first."));
        return;
    }

    // The period that was LOADED, not the current dropdowns.
    let month = state.month;
    let year = state.year;
    let employees = state.employees.map(e => e.id);

    if (!employees.length) {
        frappe.msgprint(__("No employees to export."));
        return;
    }

    window.open(
        `/api/method/rapl_payroll_automation.api.attendance_export.get_bulk_attendance_pdf?employees=${encodeURIComponent(JSON.stringify(employees))}&month=${encodeURIComponent(month)}&year=${encodeURIComponent(year)}`,
        "_blank"
    );
}
// ─── Export Excel ─────────────────────────────────────────────────────────────

function export_excel(state, month_field, year_field) {
    if (!Object.keys(state.data).length) {
        frappe.msgprint(__("Please load the report first."));
        return;
    }

    let month = state.month;
    let year = state.year;

    load_sheetjs(function() {
        let wb = XLSX.utils.book_new();

        state.employees.forEach(e => {
            let emp_data = state.data[e.id];
            if (!emp_data) return;

            let sheet_data = [
                ["Date", "Day", "In Time", "Out Time", "Working Hrs", "OT", "Status", "Remarks"]
            ];

            (emp_data.rows || []).forEach(r => {
                sheet_data.push([
                    r.attendance_date || "",
                    r.day_label || "",
                    r.in_time || "",
                    r.out_time || "",
                    r.working_hours || 0,
                    r.ot || 0,
                    r.status || "",
                    r.remarks || ""
                ]);
            });

            let ws = XLSX.utils.aoa_to_sheet(sheet_data);

            // Column widths
            ws["!cols"] = [
                {wch:12},{wch:6},{wch:8},{wch:8},
                {wch:12},{wch:6},{wch:10},{wch:16}
            ];

            // Sheet names: max 31 chars, unique, none of []:*?/\ -- SheetJS
            // throws on any of these, and on a blank employee_name.
            let base = String(emp_data.employee_name || e.id)
                .replace(/[\[\]:*?\/\\]/g, "").substring(0, 26) || String(e.id).substring(0, 26);
            let sheet_name = base, k = 1;
            while (wb.SheetNames.includes(sheet_name)) sheet_name = `${base}~${k++}`;
            XLSX.utils.book_append_sheet(wb, ws, sheet_name);
        });

        let filename = state.employees.length === 1
            ? `Attendance_${state.employees[0].name}_${month}_${year}.xlsx`
            : `Attendance_Bulk_${month}_${year}.xlsx`;

        XLSX.writeFile(wb, filename);
        frappe.show_alert({ message: __("Excel downloaded!"), indicator: "green" });
    });
}

// ─── Load Libraries ───────────────────────────────────────────────────────────

function load_html2pdf(callback) {
    // No longer needed - PDF generated server side
    callback();
}

function load_sheetjs(callback) {
    if (window.XLSX) { callback(); return; }
    // One download at a time: a second click while loading used to add a
    // second script tag and save the file twice.
    if (window._rapl_sheetjs_loading) return;
    window._rapl_sheetjs_loading = true;
    let script = document.createElement("script");
    script.src = "https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js";
    script.onload = function () { window._rapl_sheetjs_loading = false; callback(); };
    script.onerror = function () {
        window._rapl_sheetjs_loading = false;
        script.remove();
        frappe.msgprint(__("Could not load the Excel library. Check the internet connection and try again."));
    };
    document.head.appendChild(script);
}


// Everything rendered with .html() goes through this -- employee names and
// remarks come from the database and must never be treated as markup.
function esc(v) {
    return frappe.utils.escape_html(String(v === undefined || v === null ? "" : v));
}
