# Minimal stand-in for frappe / erpnext so the app's pure-ish functions can be
# unit tested WITHOUT a bench. Only what those functions touch is provided.
# Database access is NOT emulated: tests must only call code paths that do
# not reach frappe.db / frappe.get_all (those raise loudly here).
#
# Usage (from a test module):
#     from tests.frappe_stub import install; install()
#     from rapl_payroll_automation.api import ...

import datetime as _dt
import html as _html
import sys
import types


class ValidationError(Exception):
	pass


class PermissionError(Exception):  # noqa: A001 -- mirrors frappe.PermissionError
	pass


def _not_available(*args, **kwargs):
	raise RuntimeError("Database access is not available in unit tests")


def real_frappe_present():
	"""True on a bench, where the real frappe is importable or already loaded."""
	mod = sys.modules.get("frappe")
	if mod is not None:
		return not getattr(mod, "_rapl_stub", False)
	import importlib.util

	return importlib.util.find_spec("frappe") is not None


def install():
	"""Install the stand-in -- NEVER over a real frappe (e.g. bench run-tests)."""
	if "frappe" in sys.modules and getattr(sys.modules["frappe"], "_rapl_stub", False):
		return
	if real_frappe_present():
		raise RuntimeError("Real frappe is available; refusing to install the test stub")

	frappe = types.ModuleType("frappe")
	frappe._rapl_stub = True
	frappe.ValidationError = ValidationError
	frappe.PermissionError = PermissionError
	frappe.messages = []

	def throw(msg, exc=ValidationError, *a, **k):
		raise exc(msg)

	frappe.throw = throw
	frappe.msgprint = lambda *a, **k: frappe.messages.append(a[0] if a else k.get("msg"))
	frappe.clear_messages = lambda: frappe.messages.clear()
	frappe._ = lambda s, *a: s
	frappe.whitelist = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
	frappe.log_error = lambda *a, **k: None
	frappe.get_traceback = lambda: ""
	frappe.parse_json = __import__("json").loads
	frappe.scrub = lambda s: s.replace(" ", "_").replace("-", "_").lower()
	for name in ("get_all", "get_list", "get_doc", "get_cached_doc", "get_single", "new_doc",
				 "get_roles", "has_permission", "get_cached_value", "get_attr"):
		setattr(frappe, name, _not_available)
	frappe.db = types.SimpleNamespace(
		get_value=_not_available, exists=_not_available, sql=_not_available,
		count=_not_available, set_value=_not_available, savepoint=_not_available,
		rollback=_not_available, get_single_value=_not_available,
	)
	frappe.session = types.SimpleNamespace(user="test@example.com")
	frappe.local = types.SimpleNamespace(response={})

	# ---- frappe.utils
	utils = types.ModuleType("frappe.utils")

	def getdate(v=None):
		if v is None:
			return _dt.date.today()
		if isinstance(v, _dt.datetime):
			return v.date()
		if isinstance(v, _dt.date):
			return v
		return _dt.date.fromisoformat(str(v)[:10])

	def get_datetime(v=None):
		if v is None:
			return _dt.datetime.now()
		if isinstance(v, _dt.datetime):
			return v
		if isinstance(v, _dt.date):
			return _dt.datetime.combine(v, _dt.time())
		try:
			return _dt.datetime.fromisoformat(str(v))
		except ValueError:
			# frappe uses dateutil, which accepts an unpadded hour ("9:30:00")
			return _dt.datetime.strptime(str(v), "%Y-%m-%d %H:%M:%S")

	def flt(v, precision=None):
		try:
			f = float(v or 0)
		except (TypeError, ValueError):
			f = 0.0
		return round(f, precision) if precision is not None else f

	def cint(v):
		try:
			return int(float(v or 0))
		except (TypeError, ValueError):
			return 0

	def add_days(d, n):
		return getdate(d) + _dt.timedelta(days=n)

	def date_diff(a, b):
		return (getdate(a) - getdate(b)).days

	def get_first_day(d):
		return getdate(d).replace(day=1)

	def get_last_day(d):
		d = getdate(d)
		nxt = (d.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
		return nxt - _dt.timedelta(days=1)

	def add_months(d, n):
		d = getdate(d)
		m = d.month - 1 + n
		y, m = d.year + m // 12, m % 12 + 1
		return d.replace(year=y, month=m, day=min(d.day, get_last_day(_dt.date(y, m, 1)).day))

	def time_diff_in_hours(a, b):
		return (get_datetime(a) - get_datetime(b)).total_seconds() / 3600

	utils.getdate = getdate
	utils.get_datetime = get_datetime
	utils.flt = flt
	utils.cint = cint
	utils.add_days = add_days
	utils.date_diff = date_diff
	utils.get_first_day = get_first_day
	utils.get_last_day = get_last_day
	utils.add_months = add_months
	utils.time_diff_in_hours = time_diff_in_hours
	utils.now = lambda: str(_dt.datetime.now())
	utils.escape_html = lambda s: _html.escape(str(s), quote=True)
	utils.strip_html = lambda s: str(s)
	utils.get_holiday_dates_between = _not_available
	frappe.utils = utils

	rate_limiter = types.ModuleType("frappe.rate_limiter")
	rate_limiter.rate_limit = lambda *a, **k: (lambda f: f)
	frappe.rate_limiter = rate_limiter

	model = types.ModuleType("frappe.model")
	document = types.ModuleType("frappe.model.document")
	document.Document = type("Document", (), {})
	naming = types.ModuleType("frappe.model.naming")
	naming.append_number_if_name_exists = lambda dt, name, **k: name
	model.document, model.naming = document, naming

	custom = types.ModuleType("frappe.custom")

	# ---- erpnext / hrms bits imported at module level
	erp = types.ModuleType("erpnext")
	employee_mod = types.ModuleType("erpnext.setup.doctype.employee.employee")
	employee_mod.get_holiday_list_for_employee = _not_available
	holiday_mod = types.ModuleType("erpnext.setup.doctype.holiday_list.holiday_list")
	holiday_mod.get_holiday_dates_between = _not_available

	hrms_hl = types.ModuleType("hrms.utils.holiday_list")
	hrms_hl.get_holiday_dates_between = _not_available

	mods = {
		"hrms": types.ModuleType("hrms"), "hrms.utils": types.ModuleType("hrms.utils"),
		"hrms.utils.holiday_list": hrms_hl,
		"frappe": frappe, "frappe.utils": utils, "frappe.rate_limiter": rate_limiter,
		"frappe.model": model, "frappe.model.document": document,
		"frappe.model.naming": naming, "frappe.custom": custom,
		"erpnext": erp, "erpnext.setup": types.ModuleType("erpnext.setup"),
		"erpnext.setup.doctype": types.ModuleType("erpnext.setup.doctype"),
		"erpnext.setup.doctype.employee": types.ModuleType("erpnext.setup.doctype.employee"),
		"erpnext.setup.doctype.employee.employee": employee_mod,
		"erpnext.setup.doctype.holiday_list": types.ModuleType("erpnext.setup.doctype.holiday_list"),
		"erpnext.setup.doctype.holiday_list.holiday_list": holiday_mod,
	}
	sys.modules.update(mods)
