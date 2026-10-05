# Unit tests for the app's own rules, run WITHOUT a bench via tests/frappe_stub.
#
#   python3 -m unittest discover -s rapl_payroll_automation/tests -t .   (from the repo root)
#
# These cover the logic changed in the October 2026 fix round. Anything that
# needs a real database (permission checks, Salary Slip, queries) still has to
# be tested on a site.

import datetime as dt
import os
import sys
import types
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
	sys.path.insert(0, ROOT)

from rapl_payroll_automation.tests.frappe_stub import install, real_frappe_present  # noqa: E402

if real_frappe_present():
	# On a bench: these tests are for running WITHOUT Frappe. Never replace the
	# real framework inside a bench process.
	raise unittest.SkipTest("Stub-based tests; run them outside a bench")

install()

import frappe  # noqa: E402  (the stub)

from rapl_payroll_automation.api import (  # noqa: E402
	payroll_automation_utils as pau,
	attendance_automation,
	attendance_console as ac,
	attendance_data,
	ot_engine,
	processing_common,
	salary_slip_hooks,
)


def ns(**kw):
	return types.SimpleNamespace(**kw)


def settings(**kw):
	base = dict(
		ot_minimum_minutes=45, unpaid_break_minutes=0, ot_hours_divisor=8,
		half_day_leave_type="Auto Half Day", early_exit_cutoff=dt.timedelta(hours=17),
		late_mark_bands=[
			ns(label="L2", from_time=dt.timedelta(hours=10, minutes=1), to_time=dt.timedelta(hours=10, minutes=15), fraction=0.5),
			ns(label="L1", from_time=dt.timedelta(hours=9, minutes=46), to_time=dt.timedelta(hours=10), fraction=0.25),
		],
	)
	base.update(kw)
	s = ns(**base)
	s.get = lambda k, d=None: getattr(s, k, d)
	return s


SHIFT = ns(name="Regular", start_time=dt.timedelta(hours=9, minutes=30), end_time=dt.timedelta(hours=18))


class OvertimeEngine(unittest.TestCase):
	def test_holiday_ot_with_string_date(self):
		# Form / API saves pass attendance_date as a str; holiday_dates holds dates.
		hours = ot_engine.compute_day_ot(
			"2026-10-04 09:00:00", "2026-10-04 17:30:00", 8.5, "2026-10-04", "Present",
			SHIFT, settings(), {dt.date(2026, 10, 4)},
		)
		self.assertAlmostEqual(hours, 8.5)  # whole day, not minutes past 18:00

	def test_unpaid_break_only_on_holidays(self):
		s = settings(unpaid_break_minutes=30)
		hol = ot_engine.compute_day_ot(
			"2026-10-04 09:00:00", "2026-10-04 17:30:00", 8.5, dt.date(2026, 10, 4), "Present",
			SHIFT, s, {dt.date(2026, 10, 4)},
		)
		self.assertAlmostEqual(hol, 8.0)
		reg = ot_engine.compute_day_ot(
			"2026-10-05 09:30:00", "2026-10-05 20:00:00", 0, dt.date(2026, 10, 5), "Present",
			SHIFT, s, set(),
		)
		self.assertAlmostEqual(reg, 2.0)  # measured from shift end, break never enters

	def test_minimum_overrun(self):
		at_limit = ot_engine.compute_day_ot(
			"2026-10-05 09:30:00", "2026-10-05 18:45:00", 0, dt.date(2026, 10, 5), "Present",
			SHIFT, settings(), set(),
		)
		self.assertEqual(at_limit, 0.0)

	def test_half_day_and_ineligible_earn_nothing(self):
		args = ("2026-10-05 09:30:00", "2026-10-05 21:00:00", 0, dt.date(2026, 10, 5))
		self.assertEqual(ot_engine.compute_day_ot(*args, "Half Day", SHIFT, settings(), set()), 0.0)
		self.assertEqual(ot_engine.compute_day_ot(*args, "Present", SHIFT, settings(), set(), ot_eligible=False), 0.0)


class AttendanceRules(unittest.TestCase):
	def derive(self, **kw):
		base = dict(
			employee="E1", attendance_date=dt.date(2026, 10, 5),
			in_time=dt.datetime(2026, 10, 5, 9, 30), out_time=dt.datetime(2026, 10, 5, 18, 0),
			working_hours=0, status="Present", leave_type=None, shift="Regular",
			settings=settings(), holiday_dates=set(), ot_eligible=True,
		)
		base.update(kw)
		orig = ot_engine.resolve_shift
		attendance_automation.resolve_shift = lambda name, s: SHIFT
		try:
			return attendance_automation.derive_attendance_fields(**base)
		finally:
			attendance_automation.resolve_shift = orig

	def test_manual_half_day_costs_half_a_day(self):
		d = self.derive(status="Half Day", status_manual=1, half_day_status=None)
		self.assertEqual(d["status"], "Half Day")
		self.assertEqual(d["half_day_status"], "Absent")

	def test_half_day_without_punches_still_costs(self):
		d = self.derive(status="Half Day", in_time=None, out_time=None, status_manual=1)
		self.assertEqual(d["half_day_status"], "Absent")

	def test_half_day_on_holiday_still_costs(self):
		d = self.derive(status="Half Day", holiday_dates={dt.date(2026, 10, 5)}, status_manual=1)
		self.assertEqual(d["half_day_status"], "Absent")

	def test_half_day_from_leave_application_left_to_hrms(self):
		d = self.derive(status="Half Day", leave_type="Casual Leave",
						leave_application="LA-1", half_day_status="Present")
		self.assertEqual(d["half_day_status"], "Present")

	def test_manual_present_left_alone(self):
		d = self.derive(in_time=dt.datetime(2026, 10, 5, 11, 0), status="Present", status_manual=1)
		self.assertEqual(d["status"], "Present")

	def test_late_past_all_bands_is_half_day(self):
		d = self.derive(in_time=dt.datetime(2026, 10, 5, 10, 30))
		self.assertEqual(d["status"], "Half Day")
		self.assertEqual(d["half_day_status"], "Absent")

	def test_band_match_and_early_exit(self):
		d = self.derive(in_time=dt.datetime(2026, 10, 5, 9, 50))
		self.assertEqual(d["custom_late_mark_band"], "L1")
		d = self.derive(out_time=dt.datetime(2026, 10, 5, 16, 30))
		self.assertEqual(d["early_exit"], 1)
		self.assertEqual(d["status"], "Half Day")

	def test_absent_never_carries_a_band(self):
		d = self.derive(in_time=dt.datetime(2026, 10, 5, 9, 50), status="Absent")
		self.assertIsNone(d["custom_late_mark_band"])

	def test_stored_ot_uses_half_up(self):
		# Holiday span 2h 07m 30s = 2.125 h: round-to-even would store 2.12.
		d = self.derive(in_time=dt.datetime(2026, 10, 4, 9, 0, 0),
						out_time=dt.datetime(2026, 10, 4, 11, 7, 30),
						attendance_date=dt.date(2026, 10, 4), holiday_dates={dt.date(2026, 10, 4)})
		self.assertEqual(d["custom_overtime_hours"], 2.13)

	def test_pinned_ot_survives(self):
		d = self.derive(overtime_manual=1, current_overtime_hours=3.25,
						out_time=dt.datetime(2026, 10, 5, 23, 0))
		self.assertEqual(d["custom_overtime_hours"], 3.25)


class BandOrder(unittest.TestCase):
	def test_bands_sorted_by_time_not_text(self):
		# str(timedelta(9:46)) == "9:46:00" sorts AFTER "10:01:00" as text.
		defs = attendance_data.get_band_definitions(settings())
		self.assertEqual([b["label"] for b in defs], ["L1", "L2"])


class OvertimeTotals(unittest.TestCase):
	"""Hours are added EXACTLY and rounded once -- no per-day rounding bias."""

	def rows(self, minutes_each, days, pinned=None):
		out = []
		for i in range(days):
			exact = minutes_each / 60
			att = {"status": "Present", "overtime_hours": round(exact, 2), "docstatus": 1,
				   "expected_overtime_exact": exact, "overtime_manual": False,
				   "overtime_seconds": round(minutes_each * 60),
				   "late_mark_band": None, "half_day_status": None}
			if pinned and i == 0:
				att.update(overtime_manual=True, overtime_hours=pinned,
						   overtime_seconds=round(pinned * 3600))
			out.append({"date": f"2026-09-{i + 1:02d}", "flags": [], "is_holiday": False,
						"in_service": True, "is_future": False, "attendance": att})
		return out

	def test_47_minutes_for_26_days(self):
		s = attendance_data.summarise(self.rows(47, 26), settings(), dt.date(2026, 10, 5))
		# 26 x 47 min = 1222 min = 20.3667 h. Per-day rounding gave 20.28.
		self.assertEqual(s["overtime_hours"], 20.37)

	def test_drafts_and_leave_day_bands_not_counted(self):
		rows = self.rows(60, 3)
		rows[0]["attendance"]["docstatus"] = 0                      # draft: shown, not paid
		rows[1]["attendance"].update(status="On Leave", late_mark_band="L1")
		rows[2]["attendance"].update(late_mark_band="L1")
		s = attendance_data.summarise(rows, settings(), dt.date(2026, 10, 5))
		self.assertEqual(s["overtime_hours"], 2.0)                   # drafts excluded
		self.assertEqual(s["band_counts"], {"L1": 1})                # leave-day band excluded

	def test_pinned_day_uses_pinned_value(self):
		s = attendance_data.summarise(self.rows(60, 3, pinned=2.5), settings(), dt.date(2026, 10, 5))
		self.assertEqual(s["overtime_hours"], 4.5)


class ConsoleHelpers(unittest.TestCase):
	def test_clean_overrides(self):
		self.assertEqual(ac._clean_overrides(None), {})
		out = ac._clean_overrides({"E1": {"amount": "120", "junk": 5, "ot_rate": ""}})
		self.assertEqual(out, {"E1": {"amount": 120.0}})
		with self.assertRaises(frappe.ValidationError):
			ac._clean_overrides({"E1": {"amount": "abc"}})
		with self.assertRaises(frappe.ValidationError):
			ac._clean_overrides({"E1": {"band_1_count": -1}})
		with self.assertRaises(frappe.ValidationError):
			ac._clean_overrides(["E1"])

	def test_combine(self):
		self.assertEqual(ac._combine("2026-10-05", "9:30"), dt.datetime(2026, 10, 5, 9, 30))
		self.assertIsNone(ac._combine("2026-10-05", ""))
		with self.assertRaises(frappe.ValidationError):
			ac._combine("2026-10-05", "9.30")

	def test_contiguous_blocks(self):
		blocks = ac._contiguous_blocks(["2026-10-07", "2026-10-05", "2026-10-06", "2026-10-09"])
		self.assertEqual([(str(b[0]), str(b[-1])) for b in blocks],
						 [("2026-10-05", "2026-10-07"), ("2026-10-09", "2026-10-09")])

	def test_period_bounds(self):
		self.assertEqual(ac._period_bounds("2026-10-01", "2026-10-31"),
						 (dt.date(2026, 10, 1), dt.date(2026, 10, 31)))
		for bad in (("2026-10-31", "2026-10-01"), ("2026-01-01", "2026-12-31"), (None, "2026-10-31")):
			with self.assertRaises(frappe.ValidationError):
				ac._period_bounds(*bad)

	def test_employee_list(self):
		self.assertIsNone(ac._employee_list(None))
		self.assertEqual(ac._employee_list('["E1","E1","E2"]'), ["E1", "E2"])
		with self.assertRaises(frappe.ValidationError):
			ac._employee_list('{"a":1}')
		with self.assertRaises(frappe.ValidationError):
			ac._employee_list([f"E{i}" for i in range(ac.MAX_EMPLOYEES + 1)])


class Row:
	def __init__(self, idx, **kw):
		self.idx = idx
		self.__dict__.update(kw)
		self.meta = ns(has_field=lambda f: f in ("amount", "ot_hours", "ot_rate"),
					   get_label=lambda f: f)

	def get(self, k, d=None):
		return getattr(self, k, d)


class ProcessingDocs(unittest.TestCase):
	def doc(self, rows, start="2026-10-01", end="2026-10-31"):
		return ns(start_date=start, end_date=end, entries=rows)

	def test_valid(self):
		processing_common.validate_processing_doc(
			self.doc([Row(1, employee="E1", amount=100), Row(2, employee="E2", amount=0)]))

	def test_rejects(self):
		for d in (
			self.doc([], start="2026-10-31", end="2026-10-01"),
			self.doc([Row(1, employee="E1", amount=1), Row(2, employee="E1", amount=2)]),
			self.doc([Row(1, employee="E1", amount=-5)]),
			self.doc([Row(1, employee=None, amount=5)]),
		):
			with self.assertRaises(frappe.ValidationError):
				processing_common.validate_processing_doc(d)

	def test_before_submit(self):
		with self.assertRaises(frappe.ValidationError):
			processing_common.before_submit_processing_doc(self.doc([]))
		with self.assertRaises(frappe.ValidationError):
			processing_common.before_submit_processing_doc(self.doc([Row(1, employee="E1", amount=0)]))
		processing_common.before_submit_processing_doc(self.doc([Row(1, employee="E1", amount=10)]))


class Statutory(unittest.TestCase):
	def test_pt_slabs(self):
		pt = salary_slip_hooks._compute_pt
		self.assertEqual(pt("Male", 8000, 0, 0, 0, "2026-10-01"), 175)
		self.assertEqual(pt("Male", 9000, 2000, 0, 0, "2026-10-01"), 200)
		self.assertEqual(pt("Male", 9000, 2000, 0, 0, "2026-02-01"), 300)
		self.assertEqual(pt("Female", 20000, 0, 0, 0, "2026-10-01"), 0)
		frappe.messages.clear()
		self.assertEqual(pt(None, 50000, 0, 0, 0, "2026-10-01"), 0)
		self.assertTrue(frappe.messages)  # blank gender is reported, not silent

	def test_esi_rounds_up_to_next_rupee(self):
		from rapl_payroll_automation.api.payroll_math import esi_employee_contribution
		doc = ns(deductions=[ns(salary_component="ESI", amount=0)])
		salary_slip_hooks._set_deduction_amount(
			doc, "ESI", esi_employee_contribution(15075), rounding=int)
		self.assertEqual(doc.deductions[0].amount, 114)  # 113.06 -> 114, not 113

	def test_whole_hook_pf_pt_esi(self):
		"""correct_statutory_deductions end to end on a fake slip."""
		class Slip(types.SimpleNamespace):
			def set_net_pay(self):
				self.net_called = True

		slip = Slip(
			employee="E1", start_date="2026-10-01",
			custom_conveyance_for_deductions=0, custom_overtime_for_pt=75,
			earnings=[ns(salary_component="Basic", amount=12000),
					  ns(salary_component="HRA", amount=3000)],
			deductions=[ns(salary_component="PF", amount=0),
						ns(salary_component="Professional Tax", amount=0),
						ns(salary_component="ESI", amount=0)],
		)
		emp = types.SimpleNamespace(gender="Male", custom_pf=1, custom_pt=1, custom_esi=1)
		cfg = settings(pf_salary_component="PF", pt_salary_component="Professional Tax",
					   esi_salary_component="ESI", basic_salary_component=None,
					   hra_salary_component=None)
		orig_db, orig_cfg = frappe.db.get_value, salary_slip_hooks.get_automation_settings
		frappe.db.get_value = lambda *a, **k: emp
		salary_slip_hooks.get_automation_settings = lambda: cfg
		try:
			salary_slip_hooks.correct_statutory_deductions(slip, "validate")
		finally:
			frappe.db.get_value, salary_slip_hooks.get_automation_settings = orig_db, orig_cfg
		amounts = {d.salary_component: d.amount for d in slip.deductions}
		self.assertEqual(amounts["PF"], 1440)                 # 12% of 12,000
		self.assertEqual(amounts["Professional Tax"], 200)    # Male, > 10,000, not Feb
		self.assertEqual(amounts["ESI"], 114)                 # 15,075 x 0.75% = 113.06 -> up
		self.assertTrue(slip.net_called)

	def test_deduction_rounding_is_half_up(self):
		doc = ns(deductions=[ns(salary_component="PF", amount=0)])
		salary_slip_hooks._set_deduction_amount(doc, "PF", 1234.5)
		self.assertEqual(doc.deductions[0].amount, 1235)  # round() would give 1234
		salary_slip_hooks._set_deduction_amount(doc, "PF", 1235.5)
		self.assertEqual(doc.deductions[0].amount, 1236)


if __name__ == "__main__":
	unittest.main()


class HolidayListForPeriod(unittest.TestCase):
	"""The holiday list in force DURING the period -- not the one assigned today."""

	def assignment(self, day):
		# HL-2026 until 31 Dec 2026, HL-2027 from 1 Jan 2027.
		return "HL-2026" if day < dt.date(2027, 1, 1) else "HL-2027"

	def setUp(self):
		self.calls = []
		self.orig = pau.get_holiday_list_for_employee

		def fake(employee, raise_exception=True, as_on=None):
			self.calls.append(as_on)
			return self.assignment(as_on)

		pau.get_holiday_list_for_employee = fake
		hl = sys.modules["hrms.utils.holiday_list"]
		self.had_ranges = hasattr(hl, "get_holiday_list_ranges_for_employee")
		if self.had_ranges:
			self.orig_ranges = hl.get_holiday_list_ranges_for_employee
			del hl.get_holiday_list_ranges_for_employee

	def tearDown(self):
		pau.get_holiday_list_for_employee = self.orig
		if self.had_ranges:
			sys.modules["hrms.utils.holiday_list"].get_holiday_list_ranges_for_employee = self.orig_ranges

	def test_past_month_uses_its_own_list(self):
		# Processing December 2026 -- whatever "today" is.
		r = pau.get_holiday_list_ranges("E1", "2026-12-01", "2026-12-31")
		self.assertEqual(r, [("HL-2026", dt.date(2026, 12, 1), dt.date(2026, 12, 31))])

	def test_change_inside_period_is_split_at_the_right_day(self):
		r = pau.get_holiday_list_ranges("E1", "2026-12-20", "2027-01-10")
		self.assertEqual(r, [("HL-2026", dt.date(2026, 12, 20), dt.date(2026, 12, 31)),
							 ("HL-2027", dt.date(2027, 1, 1), dt.date(2027, 1, 10))])
		self.assertLess(len(self.calls), 10)   # bisection, not one lookup per day

	def test_single_day(self):
		self.assertEqual(pau.get_holiday_list_ranges("E1", "2027-01-05", "2027-01-05"),
						 [("HL-2027", dt.date(2027, 1, 5), dt.date(2027, 1, 5))])

	def test_uses_hrms_ranges_when_available(self):
		hl = sys.modules["hrms.utils.holiday_list"]
		hl.get_holiday_list_ranges_for_employee = lambda e, s, en: [
			{"holiday_list": "HL-A", "from_date": dt.date(2026, 11, 25), "to_date": dt.date(2026, 12, 9)},
			{"holiday_list": "HL-B", "from_date": dt.date(2026, 12, 10), "to_date": dt.date(2027, 2, 1)},
		]
		try:
			r = pau.get_holiday_list_ranges("E1", "2026-12-01", "2026-12-31")
		finally:
			del hl.get_holiday_list_ranges_for_employee
		self.assertEqual(r, [("HL-A", dt.date(2026, 12, 1), dt.date(2026, 12, 9)),
							 ("HL-B", dt.date(2026, 12, 10), dt.date(2026, 12, 31))])
		self.assertEqual(self.calls, [])        # HRMS 16.20+: no fallback lookups


class DisplayText(unittest.TestCase):
	def test_holiday_description_is_plain_text(self):
		frappe.utils.strip_html = lambda t: __import__("re").sub(r"<[^>]*>", "", t)
		raw = '<div class="ql-editor read-mode"><p>Krishna Janmashtami</p></div>'
		self.assertEqual(attendance_data.plain_text(raw), "Krishna Janmashtami")
		self.assertEqual(attendance_data.plain_text("<p>Eid &amp; Diwali</p>"), "Eid & Diwali")
		self.assertIsNone(attendance_data.plain_text(None))


class DraftOverrides(unittest.TestCase):
	"""Console overrides land on the right draft and only there."""

	class Doc(types.SimpleNamespace):
		def save(self):
			self.saved = True

	def late_doc(self):
		row = types.SimpleNamespace(employee="E1", band_1_count=2, band_2_count=0,
									per_day_rate=500.0, amount=250.0)
		return self.Doc(entries=[row])

	def test_ot_amount_never_becomes_late_deduction(self):
		doc = self.late_doc()
		bands = [{"fraction": 0.25}, {"fraction": 0.5}]
		touched = ac._apply_overrides(doc, "late_mark", {"E1": {"amount": 2083.0}}, bands)
		self.assertEqual(touched, [])                 # an OT-only edit is not a late-mark edit
		self.assertEqual(doc.entries[0].amount, 250.0)

	def test_late_override_applies(self):
		doc = self.late_doc()
		bands = [{"fraction": 0.25}, {"fraction": 0.5}]
		ac._apply_overrides(doc, "late_mark", {"E1": {"band_1_count": 4}}, bands)
		self.assertEqual(doc.entries[0].amount, 500)  # 4 x 0.25 x 500, rounded once


class SalaryMonth(unittest.TestCase):
	def test_rates_use_the_month_not_the_period(self):
		self.assertEqual(pau.get_salary_month("2026-10-15"),
						 (dt.date(2026, 10, 1), dt.date(2026, 10, 31), 31))
		self.assertEqual(pau.get_salary_month("2028-02-10")[2], 29)

	def test_processing_period_must_be_one_month(self):
		doc = ns(start_date="2026-10-20", end_date="2026-11-05", entries=[])
		with self.assertRaises(frappe.ValidationError):
			processing_common.validate_processing_doc(doc)
		processing_common.validate_processing_doc(ns(start_date="2026-10-01", end_date="2026-10-15", entries=[]))


class AuditRound3(unittest.TestCase):
	def test_band_gap_seconds_no_longer_free(self):
		# Bands 09:46-10:00 and 10:01-10:15: 10:00:40 used to fall between them.
		band, past = attendance_automation.match_late_band(dt.datetime(2026, 10, 5, 10, 0, 40), settings())
		self.assertEqual((band, past), ("L1", False))
		band, past = attendance_automation.match_late_band(dt.datetime(2026, 10, 5, 10, 15, 30), settings())
		self.assertEqual((band, past), ("L2", False))     # shown as 10:15 -> band, not Half Day
		band, past = attendance_automation.match_late_band(dt.datetime(2026, 10, 5, 10, 16, 0), settings())
		self.assertEqual((band, past), (None, True))

	def test_pin_limits(self):
		bands = [{"label": "L1"}, {"label": "L2"}]
		self.assertIsNone(ac._check_pin("custom_overtime_hours", 2.5, bands))
		self.assertIsNone(ac._check_pin("custom_overtime_hours", "", bands))
		self.assertIsNotNone(ac._check_pin("custom_overtime_hours", -2, bands))
		self.assertIsNotNone(ac._check_pin("custom_overtime_hours", 9999, bands))
		self.assertIsNotNone(ac._check_pin("custom_overtime_hours", "abc", bands))
		self.assertIsNone(ac._check_pin("custom_late_mark_band", "__none__", bands))
		self.assertIsNotNone(ac._check_pin("custom_late_mark_band", "L9", bands))
