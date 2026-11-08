"""9 Nov update for sites that ran order_cost_fields with the 8 Nov version (the test
site): there, Paid By was made read-only and filled automatically. Decision 6 removes it;
hide the field. Old values stay. On a site running both for the first time, this repeats
what order_cost_fields already did."""

from rapl_payroll_automation.patches.v0_3.order_cost_fields import _hide_paid_by


def execute():
	_hide_paid_by()
