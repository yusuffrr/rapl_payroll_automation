# Copyright (c) 2026, RAPL and contributors
#
# Pure payroll arithmetic -- NO frappe import, so it can be unit tested without
# a bench and cannot grow a hidden dependency on a live site.
#
# WHY THIS EXISTS
# ---------------
# The per-day / per-hour rate derivation lived in THREE places (RAPL Overtime
# Processing, the Attendance Console preview, the Employee Attendance
# statement), each calling Python's built-in round(). Two real problems:
#
#   1. round() is banker's rounding (round-half-to-even): round(340.5) == 340
#      but round(341.5) == 342. The Console's browser code uses Math.round
#      (half-up), so the same inputs gave a figure one rupee apart depending on
#      whether Python or the browser did the sum. round_half_up() is the single
#      rule now, and it matches Math.round for positive values.
#
#   2. The Console and the statement priced the LATE MARK per-day rate with the
#      OVERTIME denominator. For a grade that excludes weekly offs from the OT
#      denominator (Floor), that is monthly / (days - Sundays), while RAPL Late
#      Mark Processing -- the document that actually pays -- divides by ALL
#      calendar days. Late mark was therefore overstated on screen for exactly
#      those grades. pay_rates() returns the two rates separately.

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal


def round_half_up(value, ndigits=0):
	"""Commercial rounding: .5 always rounds away from zero.

	Goes through repr() so 2.675 is treated as the decimal 2.675 a person sees,
	not the binary float 2.67499999..., which is what built-in round() sees.
	"""
	if value is None:
		return 0 if ndigits <= 0 else 0.0
	quantum = Decimal(1).scaleb(-ndigits)
	rounded = Decimal(repr(float(value))).quantize(quantum, rounding=ROUND_HALF_UP)
	return int(rounded) if ndigits <= 0 else float(rounded)


# Rates are carried at RATE_DP decimals -- in effect exact (an error below
# 0.000001 rupee per hour). Only the final money AMOUNT is rounded, once, to
# whole rupees. Earlier the per-day rate was rounded to whole rupees and the
# hourly rate to paise before use, which made every employee's overtime a few
# rupees off, always in the same direction for that salary.
RATE_DP = 6


def pay_rates(monthly, total_days, weekly_off_days, exclude_weekly_off, ot_divisor):
	"""Per-day and per-hour rates, exactly as the processing documents use them.

	late_per_day  monthly / ALL calendar days                     (Late Mark, half day)
	ot_per_day    monthly / (days - weekly offs) for grades that exclude them,
	              otherwise monthly / calendar days               (Overtime)
	hourly        monthly / OT days / ot_divisor, from the EXACT per-day figure

	All three are carried at RATE_DP decimals, not rounded to rupees/paise.
	"""
	monthly = float(monthly or 0)
	total_days = int(total_days or 0)
	ot_days = total_days - int(weekly_off_days or 0) if exclude_weekly_off else total_days
	divisor = float(ot_divisor or 0)

	late_per_day = round_half_up(monthly / total_days, RATE_DP) if monthly and total_days > 0 else 0
	ot_per_day = round_half_up(monthly / ot_days, RATE_DP) if monthly and ot_days > 0 else 0
	hourly = (
		round_half_up(monthly / ot_days / divisor, RATE_DP)
		if monthly and ot_days > 0 and divisor > 0 else 0
	)
	return {
		"ot_days": ot_days,
		"late_per_day": late_per_day,
		"ot_per_day": ot_per_day,
		"hourly": hourly,
	}


def ot_amount(seconds, hourly):
	"""Overtime money from WHOLE SECONDS of overtime: the one rounding step."""
	return round_half_up(float(seconds or 0) / 3600 * float(hourly or 0))


def esi_employee_contribution(wages, rate="0.0075"):
	"""ESI employee contribution, rounded UP to the next whole rupee.

	Wages are taken to paise first (they are money; this also removes float
	artefacts such as 20000.000000000004 from adding salary lines), then
	multiplied EXACTLY in decimal, then any fraction of a rupee rounds up:
	  15,075.00 x 0.75% = 113.0625  -> 114
	  13,333.34 x 0.75% = 100.00005 -> 101  (a real fraction, however small)
	  20,000.00 x 0.75% = 150.00    -> 150
	"""
	from decimal import ROUND_CEILING

	if not wages or float(wages) <= 0:
		return 0
	paise = Decimal(repr(float(wages))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
	exact = paise * Decimal(str(rate))
	return int(exact.quantize(Decimal("1"), rounding=ROUND_CEILING))


def _to_date(value):
	if isinstance(value, date):
		return value
	return date.fromisoformat(str(value)[:10])


def day_totals(rows, today):
	"""Payable-day arithmetic for one employee-period from build_month_rows() rows.

	Counts, for every in-service day up to and including `today`:
	  worked   Present / Work From Home on a working day
	  off      weekly offs and holidays (paid, whether or not anyone punched in)
	  half     Half Day whose other half is unpaid -> counts 0.5
	  missing  a working day with no attendance record at all
	  payable  worked + off + 0.5 x half

	A holiday that somebody worked is ONE day (off), never two. A Half Day is
	0.5 here and 1 in the dedicated Half Day tally -- the tally stays a plain
	count of half days.

	Days after `today` are not counted at all (month in progress). Today itself
	with no record yet is "pending", not missing: the day is not over.
	"""
	today = _to_date(today)
	t = {
		"worked_days": 0, "off_days": 0, "half_days_unpaid": 0,
		"missing_days": 0, "pending_today": 0, "future_days": 0,
		"payable_days": 0.0, "through_date": None,
	}
	last_seen = None

	for row in rows:
		day = _to_date(row["date"])
		if not row.get("in_service", True):
			continue
		if day > today:
			t["future_days"] += 1
			continue
		last_seen = day

		att = row.get("attendance")
		if row.get("is_holiday"):
			t["off_days"] += 1
		elif att:
			status = att.get("status")
			if status in ("Present", "Work From Home"):
				t["worked_days"] += 1
			elif status == "Half Day":
				if (att.get("half_day_status") or "Absent") == "Absent":
					t["half_days_unpaid"] += 1
				else:
					t["worked_days"] += 1
		elif day == today:
			t["pending_today"] += 1
		else:
			t["missing_days"] += 1

	t["payable_days"] = (
		t["worked_days"] + t["off_days"] + 0.5 * t["half_days_unpaid"]
	)
	t["through_date"] = str(last_seen) if last_seen else None
	return t


def month_cutoff(start_date, end_date, today, today_has_attendance):
	"""Last day that counts when pricing a month that may not be over.

	Returns (cutoff_date_or_None, in_progress).
	  - month already finished  -> (end_date, False)
	  - month not started yet   -> (None, True)   nothing can be earned yet
	  - month in progress       -> today if today already has attendance,
	                               otherwise yesterday (today is not decided)
	"""
	start, end, today = _to_date(start_date), _to_date(end_date), _to_date(today)
	if today > end:
		return end, False
	if today < start:
		return None, True
	cutoff = today if today_has_attendance else today - timedelta(days=1)
	if cutoff < start:
		return None, True
	return min(cutoff, end), True


def expected_payment_days(
	base_days, lwp, absent_days, half_absent_days, half_fraction,
	missing_days, future_days,
):
	"""Payment days for a salary preview, built from the ground up.

	Does NOT lean on Payroll Settings 'Consider Unmarked Attendance As' or on
	payroll_based_on: with 'Present' HRMS pays a day nobody marked, and with
	'Leave' it never looks at Absent at all. HR wants every shortfall to cost
	something, so each one is subtracted explicitly:

	  base_days          calendar days in the employee's active period (less
	                     holidays when HRMS is set not to count them)
	  lwp                leave without pay, as HRMS already works it out
	  absent_days        Absent on a working day
	  half_absent_days   Half Day with the absent half unpaid -> half_fraction each
	  missing_days       working days up to the cut-off with NO attendance record
	  future_days        payable days after the cut-off (month in progress) --
	                     not earned yet, so not paid in a month-to-date figure
	"""
	paid = (
		float(base_days)
		- float(lwp or 0)
		- float(absent_days or 0)
		- float(half_absent_days or 0) * float(half_fraction)
		- float(missing_days or 0)
		- float(future_days or 0)
	)
	return max(paid, 0.0)


# --- Late-mark waivers -------------------------------------------------------
#
# A waiver forgives a NUMBER of late marks for one month. It never goes below
# zero: waiving 5 marks from a band that has 4 waives 4, and the 5th is simply
# unused -- it does not carry to another band (unless "any band" was chosen),
# to another month, or turn into a payment.

WAIVER_MODES = ("band", "costliest", "cheapest")


def _int0(value):
	try:
		return max(int(value or 0), 0)
	except (TypeError, ValueError):
		return 0


def clamp_waived(counts, waived):
	"""Each band's waived count kept between 0 and that band's own count."""
	waived = list(waived or [])
	waived += [0] * (len(counts) - len(waived))
	return [min(_int0(w), _int0(c)) for w, c in zip(waived, counts)]


def allocate_waiver(counts, fractions, marks, mode, band_index=None, existing=None):
	"""Per-band waived counts after forgiving `marks` late marks.

	counts     marks in each band, in band order
	fractions  each band's day fraction, same order
	mode       "band"      only band `band_index` (0-based); never spills over
	           "costliest" highest-fraction band first, then the next
	           "cheapest"  lowest-fraction band first, then the next
	existing   waived counts already on the row, to ADD to; None replaces them
	"""
	if mode not in WAIVER_MODES:
		raise ValueError(f"Unknown waiver mode: {mode}")
	n = len(counts)
	counts = [_int0(c) for c in counts]
	fractions = [float(f or 0) for f in list(fractions) + [0] * (n - len(fractions))]
	waived = clamp_waived(counts, existing) if existing is not None else [0] * n
	remaining = _int0(marks)

	if mode == "band":
		if band_index is None or not 0 <= int(band_index) < n:
			raise ValueError("Choose a valid band.")
		order = [int(band_index)]
	elif mode == "costliest":
		# Ties: the later (later-in-the-morning) band first.
		order = sorted(range(n), key=lambda i: (-fractions[i], -i))
	else:
		order = sorted(range(n), key=lambda i: (fractions[i], i))

	for i in order:
		if remaining <= 0:
			break
		take = min(counts[i] - waived[i], remaining)
		waived[i] += take
		remaining -= take
	return waived


def late_amounts(counts, waived, fractions, per_day_rate):
	"""(amount before waiver, amount after waiver), each rounded ONCE.

	Both are priced from day-units the same way RAPL Late Mark Processing
	prices a row, so "before" equals the normal amount and the saving is
	exactly before - after.
	"""
	n = len(counts)
	fractions = [float(f or 0) for f in list(fractions) + [0] * (n - len(fractions))]
	waived = clamp_waived(counts, waived)
	gross = sum(_int0(c) * f for c, f in zip(counts, fractions))
	net = sum((_int0(c) - w) * f for c, w, f in zip(counts, waived, fractions))
	rate = float(per_day_rate or 0)
	return round_half_up(gross * rate), round_half_up(net * rate)


def waived_row_amount(counts, waived, fractions, per_day_rate, base=None):
	"""(amount before waiver, amount after waiver) for one row.

	`base` is what the row deducted with no waiver. Normally that is the priced
	amount, and the result is priced once from (count - waived). When someone
	set the amount by hand (or the Console typed one), the waiver takes the
	waived marks' value OFF that amount instead -- so a waiver can only ever
	lower a deduction, and never below zero.
	"""
	gross, net = late_amounts(counts, waived, fractions, per_day_rate)
	if base is None:
		return gross, net
	base = max(round_half_up(float(base or 0)), 0)
	if base == gross:
		return gross, net
	n = len(counts)
	fractions = [float(f or 0) for f in list(fractions) + [0] * (n - len(fractions))]
	units = sum(w * f for w, f in zip(clamp_waived(counts, waived), fractions))
	value = round_half_up(units * float(per_day_rate or 0))
	return base, max(base - value, 0)
