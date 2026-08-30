app_name = "rapl_payroll_automation"
app_title = "Rinix HR"
app_publisher = "Rinix Automation Pvt Ltd"
app_description = (
	"Unified HR app for RAPL: Overtime, Late Mark, Half Day and Early Exit payroll "
	"automation, monthly attendance export, and Employee Advance outstanding tracking. "
	"Built on Frappe HRMS (version-16)."
)
app_email = "admin@rinix.example"
app_license = "mit"

# This app reads/writes Employee, Attendance, Shift Type, Leave Type, Salary
# Slip, Salary Component, Additional Salary, Salary Structure Assignment,
# Employee Advance, Expense Claim, Payment Entry, Journal Entry --
# all from ERPNext/HRMS. Both must be installed first.
required_apps = ["erpnext", "hrms"]

# Doc events -----------------------------------------------------------------
# Attendance: Late Mark bands, Half Day (arrival past the last configured band
#   OR pre-cutoff exit), Sunday/holiday guard (Holiday-List-driven), paid-leave
#   guard.
# Salary Slip: two hooks --
#   before_validate: pre-fetches Overtime + Conveyance totals into
#     custom_overtime_for_pt / custom_conveyance_for_deductions -- reference/
#     audit fields on the slip only.
#   validate: runs AFTER Salary Slip's own calculate_net_pay() completes and
#     overwrites PF/PT/ESI deduction amounts in plain Python.
#     NOTE: this is currently INERT by design -- pf_salary_component /
#     pt_salary_component / esi_salary_component are intentionally left blank
#     in RAPL Payroll Automation Settings, so _set_deduction_amount() matches
#     no row and returns False. PF/PT/ESI are computed by the Salary Component
#     formulas instead. Do not populate those three Settings fields without
#     first reviewing salary_slip_hooks.py -- switching this on changes live
#     payroll figures.
# Employee Advance + the four documents that move money against it: maintain
#   the custom_outstanding_balance field (paid - claimed - returned).
#   Migrated from frappe_attachment_plus; logic unchanged.
doc_events = {
	"Attendance": {
		"validate": "rapl_payroll_automation.api.attendance_automation.apply_attendance_deduction_logic",
	},
	"Salary Slip": {
		"before_validate": "rapl_payroll_automation.api.salary_slip_hooks.set_precomputed_fields",
		"validate": "rapl_payroll_automation.api.salary_slip_hooks.correct_statutory_deductions",
	},
	"Employee Advance": {
		"validate": "rapl_payroll_automation.api.employee_advance.update_outstanding_balance",
		"on_update_after_submit": "rapl_payroll_automation.api.employee_advance.update_outstanding_balance",
	},
	"Payment Entry": {
		"on_submit": "rapl_payroll_automation.api.employee_advance.update_outstanding_balance_from_payment_entry",
		"on_cancel": "rapl_payroll_automation.api.employee_advance.update_outstanding_balance_from_payment_entry",
	},
	"Expense Claim": {
		"on_submit": "rapl_payroll_automation.api.employee_advance.update_outstanding_balance_from_expense_claim",
		"on_cancel": "rapl_payroll_automation.api.employee_advance.update_outstanding_balance_from_expense_claim",
	},
	"Journal Entry": {
		"on_submit": "rapl_payroll_automation.api.employee_advance.update_outstanding_balance_from_journal_entry",
		"on_cancel": "rapl_payroll_automation.api.employee_advance.update_outstanding_balance_from_journal_entry",
	},
	"Additional Salary": {
		"on_submit": "rapl_payroll_automation.api.employee_advance.update_outstanding_balance_from_additional_salary",
		"on_cancel": "rapl_payroll_automation.api.employee_advance.update_outstanding_balance_from_additional_salary",
	},
}

# Assets ---------------------------------------------------------------------
# Shared stylesheet for the Employee Attendance statement and the Attendance
# Console. Both render the same table shape, so the colour meanings (amber =
# late mark, red = money out, green = money in) must be defined in one place.
app_include_css = "/assets/rapl_payroll_automation/css/rapl_attendance.css"

# Fixtures ---------------------------------------------------------------
# Filtered by explicit dt + fieldname rather than by module. The previous
# hrms_custom app filtered Custom Field on module = "Hrms Custom", but its
# only module was "Hrms Custom Attendance", so the filter matched nothing and
# no field was ever exported.
#
# These fields are created via Customize Form on the target site; listing them
# here means `bench --site [site] export-fixtures` captures them for
# redeployment. Adding this block does not change anything on migrate.
fixtures = [
	{
		"dt": "Custom Field",
		"filters": [
			[
				"name",
				"in",
				[
					"Attendance-custom_late_mark_band",
					"Attendance-custom_overtime",
					"Attendance-custom_overtime_hours",
					"Employee-custom_ot",
					"Employee-custom_monthly_salary",
					"Employee-custom_pf",
					"Employee-custom_pt",
					"Employee-custom_esi",
					"Salary Slip-custom_overtime_for_pt",
					"Salary Slip-custom_conveyance_for_deductions",
					"Salary Slip-custom_overtime_rate",
					"Salary Slip-custom_overtime_hours",
					"Employee Advance-custom_outstanding_balance",
				],
			]
		],
	},
]
