# Pure-logic tests: no bench needed.  Run:  python3 -m unittest rapl_payroll_automation.tests.test_payroll_math
import importlib.util, os, unittest

_p = os.path.join(os.path.dirname(__file__), "..", "api", "payroll_math.py")
_spec = importlib.util.spec_from_file_location("payroll_math", _p)
pm = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(pm)


def row(date, status=None, hol=False, ins=True, hds=None):
	att = {"status": status, "half_day_status": hds} if status else None
	return {"date": date, "in_service": ins, "is_holiday": hol, "attendance": att}


class Rounding(unittest.TestCase):
	def test_half_up_vs_bankers(self):
		# built-in round() gives 340 and 2; commercial rounding gives 341 and 3
		self.assertEqual(round(340.5), 340)
		self.assertEqual(pm.round_half_up(340.5), 341)
		self.assertEqual(pm.round_half_up(2.5), 3)
		self.assertEqual(pm.round_half_up(341.5), 342)

	def test_float_artifact(self):
		self.assertEqual(round(2.675, 2), 2.67)          # binary float trap
		self.assertEqual(pm.round_half_up(2.675, 2), 2.68)

	def test_hourly_tie(self):
		# 1355/8 = 169.375 -> 169.38 (what a person expects)
		self.assertEqual(pm.round_half_up(1355 / 8, 2), 169.38)

	def test_none_and_zero(self):
		self.assertEqual(pm.round_half_up(None), 0)
		self.assertEqual(pm.round_half_up(0, 2), 0.0)


class Rates(unittest.TestCase):
	def test_floor_grade_late_uses_calendar_days(self):
		r = pm.pay_rates(31200, 30, 4, True, 8)
		self.assertEqual(r["ot_per_day"], 1200)     # 31200 / 26
		self.assertEqual(r["late_per_day"], 1040)   # 31200 / 30  <- was 1200 on screen
		self.assertEqual(r["hourly"], 150.0)

	def test_office_grade_same_denominator(self):
		r = pm.pay_rates(31200, 30, 4, False, 8)
		self.assertEqual(r["ot_per_day"], r["late_per_day"])

	def test_rates_are_exact_not_rounded(self):
		# Rs 15,000, Floor grade, October: 31 days, 4 Sundays -> 27 OT days.
		r = pm.pay_rates(15000, 31, 4, True, 8)
		self.assertEqual(r["late_per_day"], 483.870968)   # was 484
		self.assertEqual(r["ot_per_day"], 555.555556)     # was 556
		self.assertEqual(r["hourly"], 69.444444)          # was 69.50 (from 556/8)

	def test_ot_amount_rounded_once(self):
		hourly = pm.pay_rates(15000, 31, 4, True, 8)["hourly"]
		self.assertEqual(pm.ot_amount(30 * 3600, hourly), 2083)        # old policy: 2085
		self.assertEqual(pm.ot_amount(int(12.25 * 3600), hourly), 851)  # 850.69
		self.assertEqual(pm.ot_amount(0, hourly), 0)

	def test_zero_salary(self):
		r = pm.pay_rates(0, 30, 4, True, 8)
		self.assertEqual((r["late_per_day"], r["ot_per_day"], r["hourly"]), (0, 0, 0))


class DayTotals(unittest.TestCase):
	def test_full_month_with_sundays_and_half(self):
		# 7 days: Mon-Thu present, Fri half, Sat absent, Sun off
		rows = [row(f"2026-09-0{d}", "Present") for d in (1, 2, 3, 4)]
		rows += [row("2026-09-05", "Half Day", hds="Absent"),
				 row("2026-09-06", "Absent"),
				 row("2026-09-07", hol=True)]
		t = pm.day_totals(rows, "2026-09-30")
		self.assertEqual(t["worked_days"], 4)
		self.assertEqual(t["off_days"], 1)
		self.assertEqual(t["half_days_unpaid"], 1)
		self.assertEqual(t["payable_days"], 5.5)

	def test_holiday_worked_counts_once(self):
		t = pm.day_totals([row("2026-09-06", "Present", hol=True)], "2026-09-30")
		self.assertEqual(t["off_days"], 1)
		self.assertEqual(t["worked_days"], 0)
		self.assertEqual(t["payable_days"], 1)

	def test_wfh_is_worked(self):
		t = pm.day_totals([row("2026-09-01", "Work From Home")], "2026-09-30")
		self.assertEqual(t["payable_days"], 1)

	def test_month_in_progress(self):
		rows = [row("2026-10-01", "Present"), row("2026-10-02", "Present"),
				row("2026-10-03", "Present"), row("2026-10-04", hol=True),
				row("2026-10-05"),                     # today, nothing yet
				row("2026-10-06"), row("2026-10-07")]  # future
		t = pm.day_totals(rows, "2026-10-05")
		self.assertEqual(t["pending_today"], 1)
		self.assertEqual(t["missing_days"], 0)
		self.assertEqual(t["future_days"], 2)
		self.assertEqual(t["payable_days"], 4)
		self.assertEqual(t["through_date"], "2026-10-05")

	def test_past_unmarked_is_missing(self):
		t = pm.day_totals([row("2026-10-01"), row("2026-10-02", "Present")], "2026-10-05")
		self.assertEqual(t["missing_days"], 1)

	def test_out_of_service_ignored(self):
		t = pm.day_totals([row("2026-10-01", ins=False, hol=True), row("2026-10-02", hol=True)], "2026-10-31")
		self.assertEqual(t["off_days"], 1)

	def test_half_day_with_paid_other_half(self):
		t = pm.day_totals([row("2026-10-01", "Half Day", hds="Present")], "2026-10-31")
		self.assertEqual(t["payable_days"], 1)


class Cutoff(unittest.TestCase):
	def test_finished_month(self):
		self.assertEqual(pm.month_cutoff("2026-09-01", "2026-09-30", "2026-10-05", False)[1], False)

	def test_in_progress_yesterday_when_today_empty(self):
		c, prog = pm.month_cutoff("2026-10-01", "2026-10-31", "2026-10-05", False)
		self.assertEqual((str(c), prog), ("2026-10-04", True))

	def test_in_progress_today_when_marked(self):
		c, _ = pm.month_cutoff("2026-10-01", "2026-10-31", "2026-10-05", True)
		self.assertEqual(str(c), "2026-10-05")

	def test_future_month(self):
		self.assertEqual(pm.month_cutoff("2026-11-01", "2026-11-30", "2026-10-05", False), (None, True))

	def test_first_day_nothing_yet(self):
		self.assertEqual(pm.month_cutoff("2026-10-01", "2026-10-31", "2026-10-01", False), (None, True))


class PaymentDays(unittest.TestCase):
	def test_finished_month_deducts_everything(self):
		# 30 days, 1 absent, 1 half (0.5), 2 missing records
		self.assertEqual(pm.expected_payment_days(30, 0, 1, 1, 0.5, 2, 0), 26.5)

	def test_month_to_date(self):
		# 31-day month, 5 elapsed: 26 days not earned yet
		self.assertEqual(pm.expected_payment_days(31, 0, 0, 0, 0.5, 0, 26), 5)

	def test_never_negative(self):
		self.assertEqual(pm.expected_payment_days(10, 0, 20, 0, 0.5, 0, 0), 0.0)


if __name__ == "__main__":
	unittest.main()


class EsiRoundUp(unittest.TestCase):
	def test_next_higher_rupee(self):
		self.assertEqual(pm.esi_employee_contribution(15075), 114)     # 113.0625
		self.assertEqual(pm.esi_employee_contribution(20000), 150)     # exactly 150
		self.assertEqual(pm.esi_employee_contribution(14000), 105)     # exactly 105
		self.assertEqual(pm.esi_employee_contribution(0), 0)

	def test_tiny_real_fraction_rounds_up(self):
		self.assertEqual(pm.esi_employee_contribution(13333.34), 101)  # 100.00005

	def test_float_artefact_in_wages_ignored(self):
		wages = 20000.000000000004           # what adding salary lines can produce
		self.assertEqual(pm.esi_employee_contribution(wages), 150)
