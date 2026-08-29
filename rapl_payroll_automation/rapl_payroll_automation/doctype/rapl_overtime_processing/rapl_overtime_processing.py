# Copyright (c) 2026, RAPL and contributors
#
# RAPL Overtime Processing -- submittable document replacing the old
# hand-rolled Dashboard Page. Uses the native Frappe Grid for the `entries`
# child table, which gives add-row / delete-row / multi-select-bulk-delete /
# full inline editing for free -- none of that needed to be custom-built.
#
# Workflow: create a new document, set the period, click "Get Employees"
# (populates `entries` -- either eligible employees only, or every employee
# if "Get All Employees" is used), freely add/remove/edit rows using the
# native grid controls, then Submit to create the actual Additional Salary
# records.

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import append_number_if_name_exists

from rapl_payroll_automation.api.payroll_automation_utils import (
	additional_salary_already_exists,
	create_and_submit_additional_salary,
	get_all_holiday_dates,
	get_automation_settings,
	get_grade_ot_rule,
	get_total_working_days,
	get_weekly_off_dates,
)
from rapl_payroll_automation.api.overtime_automation import get_attendance_for_employee
from rapl_payroll_automation.api.ot_engine import compute_day_ot, resolve_shift
from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee
from frappe.utils import flt, getdate


class RAPLOvertimeProcessing(Document):
	def autoname(self):
		"""
		Name from the document's own start_date -- e.g. "May 2026 - Overtime".
		A second document for the same month gets "-1", "-2" etc. appended
		automatically (via append_number_if_name_exists, the same utility
		Frappe itself uses for this exact collision case) -- so multiple
		Overtime Processing documents for one month are fully supported,
		not blocked or silently overwritten.
		"""
		if not self.start_date:
			frappe.throw(_("Set Start Date before saving (required to generate the name)."))
		base_name = getdate(self.start_date).strftime("%B %Y") + " - Overtime"
		self.name = append_number_if_name_exists("RAPL Overtime Processing", base_name, separator="-")
	def on_submit(self):
		settings = get_automation_settings()
		errors = []
		for row in self.entries:
			if not row.amount or row.amount <= 0:
				continue
			if additional_salary_already_exists(row.employee, settings.overtime_salary_component, self.end_date):
				errors.append(f"{row.employee}: already processed for this period, skipped")
				continue
			doc = create_and_submit_additional_salary(
				row.employee, settings.overtime_salary_component, row.amount, self.start_date, self.end_date
			)
			row.db_set("additional_salary", doc.name, update_modified=False)

		if errors:
			frappe.msgprint(
				_("Some rows were skipped:") + "<br>" + "<br>".join(errors),
				indicator="orange",
				title=_("Overtime Processing -- Notes"),
			)


@frappe.whitelist()
def get_employee_ot_details(docname, employee):
	"""
	Computes OT Hours/Rate/Amount for ONE employee, used by the child table's
	own client script (rapl_overtime_processing_entry.js) when a row is added
	via the native grid's own "Add Row" -- not through "Select Employees
	Manually" or either "Get Employees" button, which already compute this
	via get_employees() above. Without this, a row added the native-grid way
	had Employee set but nothing else auto-populated.
	"""
	doc = frappe.get_doc("RAPL Overtime Processing", docname)
	settings = get_automation_settings()
	errors = []
	result = _compute_employee_overtime(employee, doc.start_date, doc.end_date, settings, errors)
	if not result:
		return {"ot_hours": 0, "ot_hours_seconds": 0, "ot_rate": 0, "ot_amount": 0, "errors": errors}
	return {
		"ot_hours": result["ot_hours"],
		"ot_hours_seconds": result["ot_hours_seconds"],
		"ot_rate": result["ot_rate"],
		"ot_amount": result["ot_amount"],
		"errors": errors,
	}


@frappe.whitelist()
def get_employees(docname, all_employees=False, employees=None):
	"""
	Populates the `entries` child table on a RAPL Overtime Processing document.

	Three mutually exclusive modes (checked in this priority order):
	  1. `employees` given (list of Employee IDs, from the manual multi-select
	     picker) -- use exactly this list, no other filter applied.
	  2. all_employees=True -- every active Employee, regardless of attendance
	     or custom_ot.
	  3. all_employees=False (default), employees=None -- only employees with
	     (a) Present attendance in the period AND (b) Employee.custom_ot = 1 --
	     the actual OT-eligible workforce.
	"""
	doc = frappe.get_doc("RAPL Overtime Processing", docname)
	settings = get_automation_settings()
	start_date, end_date = doc.start_date, doc.end_date

	if isinstance(employees, str):
		employees = frappe.parse_json(employees)
	if isinstance(all_employees, str):
		all_employees = all_employees.lower() in ("1", "true", "yes")

	if employees:
		employees = list(employees)  # explicit manual selection -- use as-is, no filtering
	elif all_employees:
		employees = frappe.get_all("Employee", filters={"status": "Active"}, pluck="name")
	else:
		attendance_employees = set(
			frappe.get_all(
				"Attendance",
				filters={
					"attendance_date": ["between", [start_date, end_date]],
					"docstatus": 1,
					"status": "Present",
				},
				pluck="employee",
			)
		)
		ot_eligible_employees = set(
			frappe.get_all("Employee", filters={"custom_ot": 1, "status": "Active"}, pluck="name")
		)
		employees = sorted(attendance_employees & ot_eligible_employees)

	# Preserve any existing rows (manual additions/edits, or a previous
	# "Get Employees" run) -- only append rows for employees NOT already
	# present. Previously this did `doc.entries = []` unconditionally,
	# which silently destroyed manual entries every time any "Get
	# Employees" button was clicked again. Fixed.
	existing_employees = {row.employee for row in doc.entries}
	errors = []
	for emp in employees:
		if emp in existing_employees:
			continue  # already in the table (manual or previous fetch) -- don't touch it
		if additional_salary_already_exists(emp, settings.overtime_salary_component, end_date):
			errors.append(f"{emp}: already processed for this period, excluded")
			continue
		result = _compute_employee_overtime(emp, start_date, end_date, settings, errors)
		row = doc.append("entries", {})
		row.employee = emp
		if result:
			row.ot_hours = result["ot_hours"]
			row.ot_hours_hhmm = result["ot_hours_seconds"]
			row.ot_rate = result["ot_rate"]
			row.amount = result["ot_amount"]
		else:
			row.ot_hours = 0
			row.ot_hours_hhmm = 0
			row.ot_rate = 0
			row.amount = 0

	doc.save()

	if errors:
		frappe.msgprint(
			"<br>".join(errors), indicator="orange", title=_("Get Employees -- Notes")
		)

	return doc.name


def _compute_employee_overtime(emp, start_date, end_date, settings, errors):
	"""OT hours are now READ from Attendance.custom_overtime_hours, which is
	written by attendance_automation.py's validate hook via ot_engine.

	Previously this function recomputed OT per day from in_time/out_time,
	while a nightly Server Script independently wrote custom_overtime_hours
	using different rules. The attendance export read the field; payroll paid
	this recomputation; the two did not reconcile. Both now come from
	ot_engine.compute_day_ot().

	The rate calculation below is UNCHANGED -- per-day amount rounded to whole
	rupees first, hourly rate derived from that and rounded to 2dp, so the
	displayed Rate/hr multiplied by the displayed Hours reconciles by hand.

	Payroll deliberately does NOT read Attendance.custom_overtime_hours. That
	field is written by the same engine on validate and exists for the monthly
	attendance export, but reading it here would make payroll depend on when
	validate last ran -- and direct SQL corrections to Attendance bypass
	validate entirely. Computing from in_time/out_time on every run keeps
	payroll correct against current punch data no matter how a record was
	edited, which is how this doctype always behaved."""
	grade = frappe.db.get_value("Employee", emp, "grade")
	monthly_salary = frappe.db.get_value("Employee", emp, settings.ot_rate_base_fieldname)

	if not monthly_salary:
		errors.append(f"{emp}: missing '{settings.ot_rate_base_fieldname}', added with 0 (edit manually)")
		return None

	rule = get_grade_ot_rule(settings, grade)
	if rule is None:
		errors.append(f"{emp}: no OT rule configured for grade '{grade}', added with 0 (edit manually)")
		return None

	holiday_list = get_holiday_list_for_employee(emp)
	all_holidays = get_all_holiday_dates(holiday_list, start_date, end_date)
	sunday_dates = get_weekly_off_dates(holiday_list, start_date, end_date)

	base_working_days = get_total_working_days(start_date, end_date)
	ot_working_days = base_working_days - len(sunday_dates) if rule else base_working_days
	if ot_working_days <= 0:
		errors.append(f"{emp}: computed OT working days <= 0, added with 0 (edit manually)")
		return None

	# Per explicit design decision: round the PER-DAY amount to whole rupees
	# (0 decimals) FIRST -- this is the "per day salary" reference point.
	# hourly_rate is then derived from that whole-rupee figure and rounded
	# to 2 decimals for display/use (keeping the earlier fix's principle:
	# whatever's shown in Rate/hr must be the exact value used in the
	# Amount calculation, or the manual-vs-automatic mismatch bug returns).
	per_day_amount = round(flt(monthly_salary) / ot_working_days)
	hourly_rate = round(per_day_amount / flt(settings.ot_hours_divisor), 2)

	# get_attendance_for_employee() already filters docstatus=1 AND
	# status='Present', so every row here is a Present day.
	total_ot_hours = 0.0
	for day in get_attendance_for_employee(emp, start_date, end_date):
		try:
			ot_hours = compute_day_ot(
				in_time=day.in_time,
				out_time=day.out_time,
				working_hours=day.working_hours,
				attendance_date=day.attendance_date,
				status="Present",
				shift=resolve_shift(day.shift, settings),
				settings=settings,
				holiday_dates=all_holidays,
			)
			total_ot_hours += max(ot_hours, 0)
		except Exception as day_err:
			errors.append(f"{emp} / {day.attendance_date}: {day_err} -- day skipped")

	# REVERSED per updated instruction: total_ot_hours now rounds to 2
	# decimals (previously deliberately left exact/unrounded). This is the
	# actual value used in the Amount calculation below, not just a display
	# rounding -- so it's a genuine, if small, precision change, not cosmetic.
	# In practice this has negligible effect on the final Amount, since
	# Amount is separately rounded to whole rupees at the very end anyway --
	# that final rounding already absorbs far more precision loss than this
	# 2-decimal-vs-exact difference on Hours ever could.
	total_ot_hours = round(total_ot_hours, 2)
	return {
		"ot_hours": total_ot_hours,
		"ot_hours_seconds": round(total_ot_hours * 3600),  # for the Duration field (OT Hours HH:MM)
		"ot_rate": hourly_rate,
		"ot_amount": round(total_ot_hours * hourly_rate),
	}
