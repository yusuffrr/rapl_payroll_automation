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


class Waiver(unittest.TestCase):
	# Band 1 = 0.25 day, band 2 = 0.5 day
	F = [0.25, 0.5]

	def test_single_band_caps_at_its_count(self):
		# 4 marks in band 2, waive 5 from band 2 -> 4 waived, never -1
		self.assertEqual(pm.allocate_waiver([4, 4], self.F, 5, "band", 1), [0, 4])

	def test_single_band_never_spills(self):
		self.assertEqual(pm.allocate_waiver([3, 1], self.F, 5, "band", 1), [0, 1])

	def test_any_band_costliest_first_spills(self):
		self.assertEqual(pm.allocate_waiver([4, 4], self.F, 5, "costliest"), [1, 4])

	def test_any_band_cheapest_first(self):
		self.assertEqual(pm.allocate_waiver([4, 4], self.F, 5, "cheapest"), [4, 1])

	def test_more_than_all_marks_goes_to_zero_not_below(self):
		w = pm.allocate_waiver([4, 4], self.F, 50, "costliest")
		self.assertEqual(w, [4, 4])
		self.assertEqual(pm.late_amounts([4, 4], w, self.F, 500), (1500, 0))

	def test_add_vs_replace(self):
		self.assertEqual(pm.allocate_waiver([4, 4], self.F, 2, "band", 0, existing=[1, 3]), [3, 3])
		self.assertEqual(pm.allocate_waiver([4, 4], self.F, 2, "band", 0), [2, 0])
		# adding past the count still stops at the count
		self.assertEqual(pm.allocate_waiver([4, 4], self.F, 9, "band", 0, existing=[3, 0]), [4, 0])

	def test_no_marks_nothing_waived(self):
		self.assertEqual(pm.allocate_waiver([0, 0], self.F, 5, "costliest"), [0, 0])

	def test_bad_inputs(self):
		with self.assertRaises(ValueError):
			pm.allocate_waiver([1, 1], self.F, 1, "band", 2)
		with self.assertRaises(ValueError):
			pm.allocate_waiver([1, 1], self.F, 1, "everything")
		self.assertEqual(pm.allocate_waiver([2, 2], self.F, -3, "costliest"), [0, 0])

	def test_amounts_rounded_once(self):
		# 3 x 0.25 x 333.333333 = 249.99999975 -> 250; after waiving 1 -> 166.67 -> 167
		before, after = pm.late_amounts([3, 0], [1, 0], self.F, 333.333333)
		self.assertEqual((before, after), (250, 167))

	def test_clamp(self):
		self.assertEqual(pm.clamp_waived([2, 1], [5, -1]), [2, 0])
		self.assertEqual(pm.clamp_waived([2, 1], None), [0, 0])


class WaivedRowAmount(unittest.TestCase):
	F = [0.25, 0.5]

	def test_normal_row_priced_once(self):
		self.assertEqual(pm.waived_row_amount([3, 0], [1, 0], self.F, 333.333333, 250), (250, 167))

	def test_hand_set_zero_never_increases(self):
		self.assertEqual(pm.waived_row_amount([0, 3], [0, 1], self.F, 400, 0), (0, 0))

	def test_hand_amount_reduced_by_waived_value(self):
		# computed would be 600; HR typed 500; waiving one 0.5 mark at 400 takes 200 off
		self.assertEqual(pm.waived_row_amount([0, 3], [0, 1], self.F, 400, 500), (500, 300))
		self.assertEqual(pm.waived_row_amount([0, 3], [0, 3], self.F, 400, 500), (500, 0))
