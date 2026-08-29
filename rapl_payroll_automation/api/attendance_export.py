# Copyright (c) 2026, RAPL and contributors
#
# Monthly attendance export -- backend for the "RAPL Attendance Export" page.
#
# MIGRATED from hrms_custom.api.attendance (app now uninstalled). The SQL and
# the returned shape are unchanged; the only structural difference is that the
# recursive-CTE query, which was previously pasted verbatim into both
# get_bulk_attendance_data() and get_bulk_attendance_pdf(), now lives in a
# single _get_attendance_rows() helper called by both. The two copies were
# already identical apart from the trailing `a.name AS reference` column, which
# the PDF path did not select; that column is now always selected and simply
# ignored by the PDF renderer.
#
# KNOWN GAP (not addressed here -- deliberate, see merge notes):
#   The `ot` column reads Attendance.custom_overtime_hours, which is written by
#   the legacy "OT Calculation - Attendance Records" scheduled Server Script,
#   NOT by this app's RAPL Overtime Processing doctype. The two use different
#   rules (per-record shift vs one reference shift; working_hours vs raw span
#   on holidays; no docstatus filter vs docstatus=1/Present only), so the OT
#   hours shown on this report will not reconcile against the OT actually paid
#   via Additional Salary. Unifying these is tracked separately.

import frappe

# Holiday resolution here is intentionally left as-is from the original app:
# it COALESCEs Employee.holiday_list -> latest submitted Shift Assignment's
# Shift Type holiday list -> Company.default_holiday_list. Note this differs
# from the payroll side, which uses erpnext's get_holiday_list_for_employee().
# The two can disagree; changing it would alter what this report shows, so it
# is out of scope for the app merge.
_ATTENDANCE_SQL = """
	WITH RECURSIVE dates AS (
		SELECT STR_TO_DATE(CONCAT('01 ', %(month)s, ' ', %(year)s), '%%d %%M %%Y') AS dt
		UNION ALL
		SELECT dt + INTERVAL 1 DAY
		FROM dates
		WHERE dt < LAST_DAY(
			STR_TO_DATE(CONCAT('01 ', %(month)s, ' ', %(year)s), '%%d %%M %%Y')
		)
	)
	SELECT
		DATE_FORMAT(d.dt, '%%d-%%m-%%Y') AS attendance_date,
		DATE_FORMAT(d.dt, '%%a') AS day_label,
		DATE_FORMAT(a.in_time, '%%H:%%i') AS in_time,
		DATE_FORMAT(a.out_time, '%%H:%%i') AS out_time,
		COALESCE(a.working_hours, 0) AS working_hours,
		COALESCE(a.custom_overtime_hours, 0) AS ot,
		COALESCE(a.status, '') AS status,
		CONCAT_WS('-',
			CASE
				WHEN (a.in_time IS NULL AND a.out_time IS NOT NULL)
				  OR (a.in_time IS NOT NULL AND a.out_time IS NULL)
				THEN 'IE'
			END,
			CASE WHEN a.early_exit = 1 THEN 'EO' END,
			CASE WHEN a.late_entry = 1 THEN 'LM' END,
			CASE
				WHEN EXISTS (
					SELECT 1 FROM `tabHoliday` h
					WHERE h.parent = COALESCE(
						emp_t.holiday_list,
						(
							SELECT st.holiday_list
							FROM `tabShift Assignment` sa
							JOIN `tabShift Type` st ON st.name = sa.shift_type
							WHERE sa.employee = %(employee)s
							  AND sa.start_date <= d.dt
							  AND sa.docstatus = 1
							ORDER BY sa.start_date DESC
							LIMIT 1
						),
						co.default_holiday_list
					)
					AND h.holiday_date = d.dt
				)
				THEN 'HY'
			END,
			CASE
				WHEN EXISTS (
					SELECT 1 FROM `tabLeave Application` la
					WHERE la.employee = %(employee)s
					  AND la.from_date <= d.dt
					  AND la.to_date >= d.dt
					  AND la.status = 'Approved'
					  AND la.docstatus = 1
				)
				THEN 'LV'
			END
		) AS remarks,
		a.name AS reference
	FROM dates d
	LEFT JOIN `tabAttendance` a
		ON a.attendance_date = d.dt
		AND a.employee = %(employee)s
		AND a.docstatus = 1
	LEFT JOIN `tabEmployee` emp_t ON emp_t.name = %(employee)s
	LEFT JOIN `tabCompany` co ON co.name = emp_t.company
	ORDER BY d.dt
"""


def _get_attendance_rows(employee, month, year):
	"""One calendar month of rows for one employee, one row per day whether or
	not an Attendance record exists for it."""
	return frappe.db.sql(
		_ATTENDANCE_SQL,
		{"employee": employee, "month": month, "year": year},
		as_dict=True,
	)


def _serialize(rows):
	"""date/time objects are not JSON-serialisable; stringify them in place."""
	out = []
	for row in rows:
		r = dict(row)
		for key, val in r.items():
			if hasattr(val, "strftime"):
				r[key] = str(val)
		out.append(r)
	return out


@frappe.whitelist()
def get_bulk_attendance_data(employees, month, year):
	employees = frappe.parse_json(employees)
	results = {}

	for emp in employees:
		emp_details = frappe.db.get_value("Employee", emp, ["employee_name"], as_dict=True)
		rows = _get_attendance_rows(emp, month, year)

		results[emp] = {
			"employee_name": emp_details.employee_name if emp_details else emp,
			"rows": _serialize(rows),
		}

	return results


@frappe.whitelist()
def get_bulk_attendance_pdf(employees, month, year):
	employees = frappe.parse_json(employees)
	from frappe.utils.pdf import get_pdf

	html_parts = []

	for i, emp in enumerate(employees):
		emp_details = frappe.db.get_value("Employee", emp, ["employee_name"], as_dict=True)
		rows = _get_attendance_rows(emp, month, year)
		emp_name = emp_details.employee_name if emp_details else emp

		table_rows = ""
		for row in rows:
			attendance_date = str(row.attendance_date) if row.attendance_date else ""
			table_rows += f"""
				<tr>
					<td>{attendance_date}</td>
					<td>{row.day_label or ""}</td>
					<td>{row.in_time or "-"}</td>
					<td>{row.out_time or "-"}</td>
					<td>{row.working_hours or 0}</td>
					<td>{row.ot or 0}</td>
					<td>{row.status or ""}</td>
					<td>{row.remarks or ""}</td>
				</tr>
			"""

		page_break = "page-break-after: always;" if i < len(employees) - 1 else ""

		html_parts.append(f"""
			<div style="{page_break} padding: 20px;">
				<h2 style="text-align:center; color:black; margin-bottom:4px;">Monthly Attendance Report</h2>
				<p style="text-align:center; color:#555; margin-bottom:12px;">{month} {year}</p>
				<div style="background:#4a5568; color:white; padding:8px; font-size:13px; font-weight:bold; margin-bottom:8px;">
					{emp_name}
				</div>
				<table border="1" cellpadding="4" cellspacing="0"
					style="width:100%; border-collapse:collapse; font-size:9px;">
					<thead>
						<tr style="background:#4a5568; color:white;">
							<th>Date</th>
							<th>Day</th>
							<th>In Time</th>
							<th>Out Time</th>
							<th>Working Hrs</th>
							<th>OT</th>
							<th>Status</th>
							<th>Remarks</th>
						</tr>
					</thead>
					<tbody>
						{table_rows}
					</tbody>
				</table>
				<div style="margin-top:8px; font-size:9px; color:#555;">
					IE = Incomplete Entry | EO = Early Out | LM = Late Entry | HY = Holiday | LV = Leave
				</div>
			</div>
		""")

	full_html = f"""
		<html>
		<head>
		<style>
			body {{ font-family: Arial, sans-serif; color: black; }}
			table {{ width: 100%; border-collapse: collapse; }}
			th, td {{ border: 1px solid #ccc; padding: 4px; font-size: 9px; color: black; }}
			th {{ background-color: #4a5568; color: white; }}
		</style>
		</head>
		<body>
			{"".join(html_parts)}
		</body>
		</html>
	"""

	pdf_content = get_pdf(full_html)

	frappe.local.response.filename = f"Attendance_{month}_{year}.pdf"
	frappe.local.response.filecontent = pdf_content
	frappe.local.response.type = "pdf"
