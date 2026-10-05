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
	get_employee_holiday_dates,
	get_automation_settings,
	get_grade_ot_rule,
	get_employee_holiday_rows,
	get_salary_month,
	get_total_working_days,
	get_employee_weekly_off_dates,
)
from rapl_payroll_automation.api.overtime_automation import get_attendance_for_employee
from rapl_payroll_automation.api.ot_engine import compute_day_ot, resolve_shift
from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee
from frappe.utils import cint, flt, getdate

from rapl_payroll_automation.api.processing_common import (
	before_submit_processing_doc,
	cancel_additional_salaries,
	validate_processing_doc,
)
from rapl_payroll_automation.api.payroll_math import ot_amount, pay_rates, round_half_up


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
		# " #" not "-": Frappe names an amendment "<name>-1", which collided
		# with a second same-month document named "<name>-1".
		self.name = append_number_if_name_exists("RAPL Overtime Processing", base_name, separator=" #")
	def validate(self):
		validate_processing_doc(self)

	def before_submit(self):
		before_submit_processing_doc(self)

	def on_cancel(self):
		cancel_additional_salaries(self)

	def on_submit(self):
		settings = get_automation_settings()
		errors = []
		for row in self.entries:
			if not row.amount or row.amount <= 0:
				continue
			if additional_salary_already_exists(row.employee, settings.overtime_salary_component, self.end_date, self.start_date):
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
	# get_doc does not check permission; rates expose salary.
	doc.check_permission("write")
	if not frappe.has_permission("Employee", "read", doc=employee):
		frappe.throw(_("Not permitted for employee {0}").format(employee), frappe.PermissionError)
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
	# get_doc does not check permission; rates expose salary.
	doc.check_permission("write")
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
	# Only employees the caller may see (User Permissions). get_all above and a
	# client-sent list both ignore them, and each row exposes pay rates.
	if employees:
		allowed = set(frappe.get_list(
			"Employee", filters={"name": ["in", list(employees)]}, pluck="name", limit_page_length=0
		))
		employees = [e for e in employees if e in allowed]

	existing_employees = {row.employee for row in doc.entries}
	errors = []
	for emp in employees:
		if emp in existing_employees:
			continue  # already in the table (manual or previous fetch) -- don't touch it
		if additional_salary_already_exists(emp, settings.overtime_salary_component, end_date, start_date):
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
	grade, monthly_salary = frappe.db.get_value(
		"Employee", emp, ["grade", settings.ot_rate_base_fieldname]
	) or (None, None)

	if not monthly_salary:
		errors.append(f"{emp}: missing '{settings.ot_rate_base_fieldname}', added with 0 (edit manually)")
		return None

	rule = get_grade_ot_rule(settings, grade)
	if rule is None:
		errors.append(f"{emp}: no OT rule configured for grade '{grade}', added with 0 (edit manually)")
		return None

	# Rates: the salary MONTH's days and weekly offs (not the period's).
	# Holidays used to price each day: the lists in force DURING the period.
	month_first, month_last, month_days = get_salary_month(start_date)
	holiday_rows = get_employee_holiday_rows(emp, month_first, month_last)  # one lookup
	all_holidays = {
		getdate(r.holiday_date) for r in holiday_rows
		if getdate(start_date) <= getdate(r.holiday_date) <= getdate(end_date)
	}
	sunday_dates = {getdate(r.holiday_date) for r in holiday_rows if r.weekly_off}

	rates = pay_rates(
		monthly_salary, month_days, len(sunday_dates),
		bool(rule),
		settings.ot_hours_divisor,
	)
	ot_working_days = rates["ot_days"]
	if ot_working_days <= 0:
		errors.append(f"{emp}: computed OT working days <= 0, added with 0 (edit manually)")
		return None

	# Per explicit design decision: round the PER-DAY amount to whole rupees
	# (0 decimals) FIRST -- this is the "per day salary" reference point.
	# hourly_rate is then derived from that whole-rupee figure and rounded
	# to 2 decimals for display/use (keeping the earlier fix's principle:
	# whatever's shown in Rate/hr must be the exact value used in the
	# Amount calculation, or the manual-vs-automatic mismatch bug returns).
	hourly_rate = rates["hourly"]

	# get_attendance_for_employee() already filters docstatus=1 AND
	# status='Present', so every row here is a Present day.
	total_ot_hours = 0.0
	for day in get_attendance_for_employee(emp, start_date, end_date):
		if cint(day.get("custom_overtime_manual")):
			# HR pinned this day in the Console -- pay the pinned figure, and
			# do it BEFORE computing, so a compute error cannot drop it.
			total_ot_hours += max(flt(day.get("custom_overtime_hours")), 0)
			continue
		try:
			ot_hours = compute_day_ot(
				in_time=day.in_time,
				out_time=day.out_time,
				working_hours=day.working_hours,
				attendance_date=day.attendance_date,
				status="Present",  # pinned non-Present days are overridden above
				shift=resolve_shift(day.shift, settings),
				settings=settings,
				holiday_dates=all_holidays,
			)
			# Add EXACT hours; round once, below. Rounding each day first
			# (2 dp) loses up to 18 seconds a day, and for a repeated duration
			# always in the same direction: 26 days of 47 min paid 20.28 h
			# instead of 20.37 h.
			total_ot_hours += max(flt(ot_hours), 0)
		except Exception as day_err:
			errors.append(f"{emp} / {day.attendance_date}: {day_err} -- day skipped")

	# Exact to the second. The Duration field (OT Hours HH:MM) holds these
	# seconds, the Amount is priced from them with the exact hourly rate and
	# rounded ONCE to whole rupees. ot_hours (2 dp) is for display only.
	seconds = round_half_up(total_ot_hours * 3600)
	return {
		"ot_hours": round_half_up(seconds / 3600, 2),
		"ot_hours_seconds": seconds,
		"ot_rate": hourly_rate,
		"ot_amount": ot_amount(seconds, hourly_rate),
	}
