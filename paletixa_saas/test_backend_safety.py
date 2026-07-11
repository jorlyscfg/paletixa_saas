import json
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import frappe
import pytest

from paletixa_saas.config import infrastructure, platform_defaults
from paletixa_saas.paletixa_saas import api as saas_api
from paletixa_saas.paletixa_saas.doctype.saas_feature_config.saas_feature_config import SaaSFeatureConfig


def _unique_suffix():
	return uuid.uuid4().hex[:10]


def _cleanup_tenant_request(subdomain):
	if frappe.db.exists("SaaS Tenant Request", {"subdomain": subdomain}):
		name = frappe.db.get_value("SaaS Tenant Request", {"subdomain": subdomain}, "name")
		frappe.delete_doc("SaaS Tenant Request", name, ignore_permissions=True)
		frappe.db.commit()


class _FakeDoc:
	def __init__(self, doctype, name, docstatus=1):
		self.doctype = doctype
		self.name = name
		self.docstatus = docstatus
		self.cancel_calls = 0
		self.save_calls = 0
		self.items = []
		self.flags = SimpleNamespace()

	def cancel(self):
		self.cancel_calls += 1
		self.docstatus = 2

	def save(self, ignore_permissions=False):
		self.save_calls += 1
		return self

	def db_set(self, fieldname, value, update_modified=True):
		setattr(self, fieldname, value)
		return self

	def get(self, key, default=None):
		return getattr(self, key, default)


class _FakeInvoice:
	def __init__(self):
		self.name = "SINV-1"
		self.update_stock = 1
		self.outstanding_amount = 120.0
		self.grand_total = 120.0
		self.inserted = False
		self.submitted = False

	def insert(self, ignore_permissions=False):
		assert self.update_stock == 0
		self.inserted = True
		return self

	def submit(self):
		self.submitted = True
		self.docstatus = 1
		return self


class _FakeSaaSConfig:
	def __init__(self):
		self.primary_color = ""
		self.has_pos = 0
		self.has_production = 0
		self.has_logistics = 0
		self.has_reservations = 1
		self.has_wholesale = 1
		self.has_services = 1
		self.has_products = 1
		self.has_mexico_taxes = 0
		self.has_purchasing = 0
		self.reservation_item_code = ""
		self.max_reservation_assets = 0
		self.default_event_items = "[]"
		self.custom_country = ""
		self.custom_currency = ""
		self.company_name = ""
		self.company_abbr = ""
		self.default_distribution_warehouse = ""
		self.default_cash_account = ""
		self.default_bank_account = ""
		self.company_logo = ""
		self.client_logo = ""
		self.company_tax_id = ""
		self.company_address = ""
		self.company_phone = ""
		self.company_email = ""
		self.ticket_header = ""
		self.ticket_footer = ""
		self.print_logo = 1
		self.print_tax_id = 1
		self.print_address = 1
		self.print_contact = 1

	def get(self, key, default=None):
		return getattr(self, key, default)

	def save(self, ignore_permissions=False):
		return self

	def as_dict(self):
		return dict(self.__dict__)


class _FakeRuntimeConfig:
	def __init__(self):
		self.company_name = ""
		self.company_abbr = ""
		self.company_tax_id = ""
		self.company_address = ""
		self.company_phone = ""
		self.company_email = ""
		self.default_distribution_warehouse = ""
		self.default_cash_account = ""
		self.default_bank_account = ""
		self.custom_country = ""
		self.custom_currency = ""
		self.is_active = 0
		self.max_branches = 0
		self.print_logo = 0
		self.print_tax_id = 0
		self.print_address = 0
		self.print_contact = 0
		self.ticket_header = ""
		self.ticket_footer = ""
		self.saved_snapshots = []

	def get(self, key, default=None):
		return getattr(self, key, default)

	def save(self, ignore_permissions=False):
		self.saved_snapshots.append(
			{
				"company_name": self.company_name,
				"company_abbr": self.company_abbr,
				"default_distribution_warehouse": self.default_distribution_warehouse,
				"default_cash_account": self.default_cash_account,
				"default_bank_account": self.default_bank_account,
				"custom_country": self.custom_country,
				"custom_currency": self.custom_currency,
				"is_active": self.is_active,
				"max_branches": self.max_branches,
			}
		)
		return self


class _RecordingDoc:
	def __init__(self, doctype):
		self.doctype = doctype
		self._children = {}

	def append(self, table, row):
		self._children.setdefault(table, []).append(row)

	def insert(self, ignore_permissions=False):
		return self


def test_create_custom_field_requires_system_manager(monkeypatch):
	monkeypatch.setattr(frappe.session, "user", "cashier@example.test", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: [])
	monkeypatch.setattr(
		frappe.db,
		"exists",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected schema lookup")),
	)

	with pytest.raises(frappe.PermissionError, match="No tenés permisos para configurar el sistema"):
		saas_api.create_custom_field()


def test_generate_customer_access_pin_requires_system_manager(monkeypatch):
	monkeypatch.setattr(frappe.session, "user", "cashier@example.test", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: [])
	monkeypatch.setattr(
		frappe.db,
		"set_value",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected PIN write")),
	)

	with pytest.raises(frappe.PermissionError, match="No tenés permisos para generar un PIN de acceso"):
		saas_api.generate_customer_access_pin("CUST-001")


def test_create_pos_opening_requires_assigned_operator_or_system_manager(monkeypatch):
	monkeypatch.setattr(frappe.session, "user", "cashier@example.test", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: [])
	monkeypatch.setattr(frappe.db, "exists", lambda *args, **kwargs: False)
	monkeypatch.setattr(
		frappe,
		"new_doc",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected POS opening creation")),
	)

	with pytest.raises(frappe.PermissionError, match=r"No tenés permisos para realizar esta acción\."):
		saas_api.create_pos_opening(
			"Punto de Venta - Sucursal 4",
			"La Paletixa",
			[{"mode_of_payment": "Cash", "opening_amount": 100.0}],
		)


def test_close_pos_shift_requires_own_shift_or_system_manager(monkeypatch):
	monkeypatch.setattr(frappe.session, "user", "cashier@example.test", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: [])
	monkeypatch.setattr(
		frappe.db,
		"sql",
		lambda *args, **kwargs: [
			SimpleNamespace(
				status="Open",
				pos_closing_entry=None,
				user="other@example.test",
				pos_profile="Punto de Venta - Sucursal 4",
			)
		],
	)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected POS opening load")),
	)
	monkeypatch.setattr(
		frappe,
		"new_doc",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected POS closing creation")),
	)
	monkeypatch.setattr(
		frappe.db,
		"commit",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected commit")),
	)

	with pytest.raises(frappe.PermissionError, match=r"No tenés permisos para realizar esta acción\."):
		saas_api.close_pos_shift(
			"POS-OPE-2026-00003",
			[{"mode_of_payment": "Cash", "closing_amount": 100.0}],
		)


def test_notification_reads_require_system_manager(monkeypatch):
	monkeypatch.setattr(frappe.session, "user", "cashier@example.test", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: [])
	monkeypatch.setattr(saas_api, "ensure_saas_notification_doctype", lambda: None)
	monkeypatch.setattr(
		frappe.db,
		"count",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected notification count")),
	)

	with pytest.raises(frappe.PermissionError, match="No tenés permisos para acceder a esta información"):
		saas_api.get_unread_notifications()

	with pytest.raises(frappe.PermissionError, match="No tenés permisos para realizar esta acción"):
		saas_api.mark_notification_as_read("SAA-001")


def test_seed_demo_data_requires_tenant_admin_or_system_manager(monkeypatch):
	monkeypatch.setattr(frappe.session, "user", "cashier@example.test", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: [])
	monkeypatch.setattr(
		frappe.db,
		"exists",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected mutation lookup")),
	)
	monkeypatch.setattr(
		frappe.db,
		"set_single_value",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected settings mutation")),
	)
	monkeypatch.setattr(
		frappe.db,
		"sql",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected sql mutation")),
	)
	monkeypatch.setattr(
		frappe,
		"delete_doc",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected destructive mutation")),
	)

	with pytest.raises(frappe.PermissionError, match="No tenés permisos para realizar esta acción"):
		saas_api.seed_demo_data()


def test_provision_tenant_task_uses_safe_site_context_and_persists_status(monkeypatch, tmp_path):
	import erpnext.setup.setup_wizard.operations.install_fixtures as install_fixtures_module
	from frappe.core.doctype.user import user as user_module
	from frappe.utils import password as password_module

	request_id = f"provision-{_unique_suffix()}"
	master_site = "frontend"
	subdomain = f"tenant-{_unique_suffix()}"
	domain = f"{subdomain}.localhost"
	sites_path = tmp_path / "sites"
	site_dir = sites_path / domain
	site_dir.mkdir(parents=True)
	(site_dir / "site_config.json").write_text(json.dumps({"db_name": "tenant_db"}))

	saved_states = []
	entered_sites = []
	runtime_config = _FakeRuntimeConfig()
	company_doc = SimpleNamespace(
		abbr="TC",
		country="Mexico",
		default_currency="MXN",
		get=lambda key, default=None: {"country": "Mexico", "default_currency": "MXN"}.get(key, default),
	)

	class _FakeTenantRequest:
		def __init__(self):
			self.name = request_id
			self.subdomain = subdomain
			self.company_name = "Tenant Co"
			self.company_tax_id = "RFC-TENANT"
			self.company_address = "Calle Provisión 123"
			self.company_phone = "5550001234"
			self.company_email = "ops@tenant.test"
			self.admin_email = "admin@example.test"
			self.max_branches = 6
			self.status = "Pending"
			self.database_name = ""
			self.error_log = ""

		def get_password(self, fieldname):
			assert fieldname == "admin_password"
			return "SecretPassword123!"

		def save(self, ignore_permissions=False):
			saved_states.append((self.status, self.database_name, self.error_log))
			return self

	def _fake_get_all(doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs):
		filters = filters or {}
		company_filter = filters.get("company")
		if doctype == "Warehouse" and company_filter == request_doc.company_name:
			if filters.get("is_group", 0) == 0 and filters.get("disabled", 0) == 0:
				if filters.get("name") in {None, "Distribution - TC"}:
					return [{"name": "Distribution - TC"}]
		if doctype == "Account" and company_filter == request_doc.company_name:
			if filters.get("is_group", 0) == 0 and filters.get("disabled", 0) == 0:
				if filters.get("account_type") == "Cash" and filters.get("name") in {None, "Cash - TC"}:
					return [{"name": "Cash - TC"}]
				if filters.get("account_type") == "Bank" and filters.get("name") in {None, "Bank - TC"}:
					return [{"name": "Bank - TC"}]
				if filters.get("account_type") is None and filters.get("name") == "Cash - TC":
					return [{"name": "Cash - TC"}]
				if filters.get("account_type") is None and filters.get("name") == "Bank - TC":
					return [{"name": "Bank - TC"}]
		return []

	class _FakeWritableDoc:
		def __init__(self, doctype):
			self.doctype = doctype
			self.roles = []

		def insert(self, ignore_permissions=False):
			return self

		def add_roles(self, *roles):
			self.roles.extend(roles)

	class _FakeSafeSiteContext:
		def __init__(self, site):
			self.site = site
			self.previous_site = None

		def __enter__(self):
			self.previous_site = getattr(frappe.local, "site", None)
			entered_sites.append(self.site)
			frappe.local.site = self.site
			return frappe

		def __exit__(self, exc_type, exc_val, exc_tb):
			frappe.local.site = self.previous_site

	def _unexpected_raw_context_switch(*args, **kwargs):
		raise AssertionError("unexpected raw context switch")

	def _fake_get_doc(doctype, name=None, *args, **kwargs):
		if doctype == "SaaS Tenant Request" and name == request_id:
			return request_doc
		if doctype == "SaaS Feature Config" and name is None:
			return runtime_config
		raise AssertionError(f"Unexpected get_doc lookup: {doctype} {name}")

	def _fake_get_cached_doc(doctype, name=None, *args, **kwargs):
		if doctype == "Company" and name == request_doc.company_name:
			return company_doc
		if doctype == "SaaS Feature Config":
			return runtime_config
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} {name}")

	def _fake_new_doc(doctype):
		return _FakeWritableDoc(doctype)

	request_doc = _FakeTenantRequest()
	original_site = getattr(frappe.local, "site", None)
	role_permission_calls = []

	monkeypatch.setattr(saas_api, "SafeSiteContext", _FakeSafeSiteContext)
	monkeypatch.setattr(saas_api, "get_bench_path", lambda: str(tmp_path))
	monkeypatch.setattr(saas_api, "get_sites_path", lambda: str(sites_path))
	monkeypatch.setattr(saas_api, "get_db_root_credentials", lambda: ("root", "rootpass"))
	monkeypatch.setattr(frappe, "get_doc", _fake_get_doc)
	monkeypatch.setattr(frappe, "get_cached_doc", _fake_get_cached_doc)
	monkeypatch.setattr(frappe, "get_all", _fake_get_all)
	monkeypatch.setattr(frappe, "new_doc", _fake_new_doc)
	monkeypatch.setattr(frappe.db, "exists", lambda doctype, name=None, *args, **kwargs: False)
	monkeypatch.setattr(frappe.db, "set_default", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe.db, "set_single_value", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe.db, "commit", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe, "set_user", lambda user: setattr(frappe.session, "user", user))
	monkeypatch.setattr(frappe, "destroy", _unexpected_raw_context_switch)
	monkeypatch.setattr(frappe, "init", _unexpected_raw_context_switch)
	monkeypatch.setattr(frappe, "connect", _unexpected_raw_context_switch)
	monkeypatch.setattr(password_module, "update_password", lambda *args, **kwargs: None)
	monkeypatch.setattr(user_module, "generate_keys", lambda *args, **kwargs: None)
	monkeypatch.setattr(install_fixtures_module, "install", lambda *args, **kwargs: None)
	monkeypatch.setattr(
		saas_api, "setup_saas_role_permissions", lambda: role_permission_calls.append("called")
	)
	monkeypatch.setattr(
		subprocess,
		"run",
		lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
	)

	try:
		frappe.local.site = master_site
		saas_api.provision_tenant_task(request_id, base_domain="localhost")
	finally:
		frappe.local.site = original_site

	assert entered_sites == [domain]
	assert saved_states == [("In Progress", "", ""), ("Completed", "tenant_db", "")]
	assert request_doc.status == "Completed"
	assert request_doc.database_name == "tenant_db"
	assert request_doc.error_log == ""
	assert runtime_config.saved_snapshots
	assert runtime_config.company_name == "Tenant Co"
	assert runtime_config.company_abbr == "TC"
	assert runtime_config.default_distribution_warehouse == "Distribution - TC"
	assert runtime_config.default_cash_account == "Cash - TC"
	assert runtime_config.default_bank_account == "Bank - TC"
	assert role_permission_calls == ["called"]
	assert runtime_config.custom_country == "Mexico"
	assert runtime_config.custom_currency == "MXN"
	assert runtime_config.is_active == 1
	assert runtime_config.max_branches == 6
	assert frappe.local.site == original_site


def test_provision_tenant_task_blocks_completion_when_runtime_links_are_missing(monkeypatch, tmp_path):
	import erpnext.setup.setup_wizard.operations.install_fixtures as install_fixtures_module
	from frappe.core.doctype.user import user as user_module
	from frappe.utils import password as password_module

	request_id = f"provision-missing-{_unique_suffix()}"
	master_site = "frontend"
	subdomain = f"tenant-missing-{_unique_suffix()}"
	domain = f"{subdomain}.localhost"
	sites_path = tmp_path / "sites"
	site_dir = sites_path / domain
	site_dir.mkdir(parents=True)
	(site_dir / "site_config.json").write_text(json.dumps({"db_name": "tenant_db_missing"}))

	saved_states = []
	runtime_config = _FakeRuntimeConfig()
	company_doc = SimpleNamespace(
		abbr="TC",
		country="Mexico",
		default_currency="MXN",
		get=lambda key, default=None: {"country": "Mexico", "default_currency": "MXN"}.get(key, default),
	)

	class _FakeTenantRequest:
		def __init__(self):
			self.name = request_id
			self.subdomain = subdomain
			self.company_name = "Tenant Missing Co"
			self.company_tax_id = "RFC-MISSING"
			self.company_address = "Calle Falta 123"
			self.company_phone = "5550005678"
			self.company_email = "ops@missing.test"
			self.admin_email = "admin@missing.test"
			self.max_branches = 3
			self.status = "Pending"
			self.database_name = ""
			self.error_log = ""

		def get_password(self, fieldname):
			assert fieldname == "admin_password"
			return "SecretPassword123!"

		def save(self, ignore_permissions=False):
			saved_states.append((self.status, self.database_name, self.error_log))
			return self

	def _fake_get_all(doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs):
		filters = filters or {}
		company_filter = filters.get("company")
		if doctype == "Warehouse" and company_filter == request_doc.company_name:
			if filters.get("is_group", 0) == 0 and filters.get("disabled", 0) == 0:
				return [{"name": "Distribution - TC"}]
		if doctype == "Account" and company_filter == request_doc.company_name:
			if filters.get("is_group", 0) == 0 and filters.get("disabled", 0) == 0:
				if filters.get("account_type") == "Cash" and filters.get("name") in {None, "Cash - TC"}:
					return [{"name": "Cash - TC"}]
				if filters.get("account_type") == "Bank" and filters.get("name") in {None, "Bank - TC"}:
					return []
				if filters.get("account_type") is None and filters.get("name") == "Cash - TC":
					return [{"name": "Cash - TC"}]
				if filters.get("account_type") is None and filters.get("name") == "Bank - TC":
					return []
		return []

	class _FakeWritableDoc:
		def __init__(self, doctype):
			self.doctype = doctype
			self.roles = []

		def insert(self, ignore_permissions=False):
			return self

		def add_roles(self, *roles):
			self.roles.extend(roles)

	class _FakeSafeSiteContext:
		def __init__(self, site):
			self.site = site
			self.previous_site = None

		def __enter__(self):
			self.previous_site = getattr(frappe.local, "site", None)
			frappe.local.site = self.site
			return frappe

		def __exit__(self, exc_type, exc_val, exc_tb):
			frappe.local.site = self.previous_site

	def _fake_get_doc(doctype, name=None, *args, **kwargs):
		if doctype == "SaaS Tenant Request" and name == request_id:
			return request_doc
		if doctype == "SaaS Feature Config" and name is None:
			return runtime_config
		raise AssertionError(f"Unexpected get_doc lookup: {doctype} {name}")

	def _fake_get_cached_doc(doctype, name=None, *args, **kwargs):
		if doctype == "Company" and name == request_doc.company_name:
			return company_doc
		if doctype == "SaaS Feature Config":
			return runtime_config
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} {name}")

	def _fake_new_doc(doctype):
		return _FakeWritableDoc(doctype)

	request_doc = _FakeTenantRequest()
	original_site = getattr(frappe.local, "site", None)

	monkeypatch.setattr(saas_api, "SafeSiteContext", _FakeSafeSiteContext)
	monkeypatch.setattr(saas_api, "get_bench_path", lambda: str(tmp_path))
	monkeypatch.setattr(saas_api, "get_sites_path", lambda: str(sites_path))
	monkeypatch.setattr(saas_api, "get_db_root_credentials", lambda: ("root", "rootpass"))
	monkeypatch.setattr(frappe, "get_doc", _fake_get_doc)
	monkeypatch.setattr(frappe, "get_cached_doc", _fake_get_cached_doc)
	monkeypatch.setattr(frappe, "get_all", _fake_get_all)
	monkeypatch.setattr(frappe, "new_doc", _fake_new_doc)
	monkeypatch.setattr(frappe.db, "exists", lambda doctype, name=None, *args, **kwargs: False)
	monkeypatch.setattr(frappe.db, "set_default", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe.db, "set_single_value", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe.db, "commit", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe, "set_user", lambda user: setattr(frappe.session, "user", user))
	monkeypatch.setattr(frappe, "destroy", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe, "init", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe, "connect", lambda *args, **kwargs: None)
	monkeypatch.setattr(password_module, "update_password", lambda *args, **kwargs: None)
	monkeypatch.setattr(user_module, "generate_keys", lambda *args, **kwargs: None)
	monkeypatch.setattr(install_fixtures_module, "install", lambda *args, **kwargs: None)
	monkeypatch.setattr(saas_api, "setup_saas_role_permissions", lambda: None)
	monkeypatch.setattr(
		subprocess,
		"run",
		lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
	)

	try:
		frappe.local.site = master_site
		saas_api.provision_tenant_task(request_id, base_domain="localhost")
	finally:
		frappe.local.site = original_site

	assert saved_states[-1][0] == "Failed"
	assert request_doc.status == "Failed"
	assert request_doc.database_name == ""
	assert request_doc.error_log
	assert "runtime" in request_doc.error_log.lower() or "enlaces" in request_doc.error_log.lower()


def test_update_saas_config_uses_incoming_company_name_for_reservations():
	original_user = frappe.session.user
	original_get_roles = frappe.get_roles
	original_get_doc = frappe.get_doc
	original_exists = frappe.db.exists
	original_commit = frappe.db.commit
	original_clear_cache = frappe.clear_cache
	original_setup_fields = saas_api.setup_company_identity_fields
	original_sync_warehouses = saas_api.sync_event_warehouses
	original_master_gate = saas_api._is_platform_master_site

	fake_config = _FakeSaaSConfig()
	sync_calls = []

	try:
		frappe.session.user = "Administrator"
		frappe.get_roles = lambda user=None: ["System Manager"]
		frappe.get_doc = (
			lambda doctype, *args, **kwargs: fake_config if doctype == "SaaS Feature Config" else None
		)
		frappe.db.exists = lambda doctype, name=None, *args, **kwargs: False
		frappe.db.commit = lambda *args, **kwargs: None
		frappe.clear_cache = lambda *args, **kwargs: None
		saas_api.setup_company_identity_fields = lambda: None
		saas_api.sync_event_warehouses = lambda company_name, max_assets: sync_calls.append(
			(company_name, max_assets)
		)
		saas_api._is_platform_master_site = lambda: False

		result = saas_api.update_saas_config(
			company_name="Nueva Plataforma",
			max_reservation_assets=7,
		)

		assert result["success"] is True
		assert sync_calls == [("Nueva Plataforma", 7)]
		assert fake_config.company_name == "Nueva Plataforma"
	finally:
		frappe.session.user = original_user
		frappe.get_roles = original_get_roles
		frappe.get_doc = original_get_doc
		frappe.db.exists = original_exists
		frappe.db.commit = original_commit
		frappe.clear_cache = original_clear_cache
		saas_api.setup_company_identity_fields = original_setup_fields
		saas_api.sync_event_warehouses = original_sync_warehouses
		saas_api._is_platform_master_site = original_master_gate


def test_update_saas_config_ignores_invalid_max_reservation_assets():
	original_user = frappe.session.user
	original_get_roles = frappe.get_roles
	original_get_doc = frappe.get_doc
	original_exists = frappe.db.exists
	original_commit = frappe.db.commit
	original_clear_cache = frappe.clear_cache
	original_setup_fields = saas_api.setup_company_identity_fields
	original_sync_warehouses = saas_api.sync_event_warehouses
	original_master_gate = saas_api._is_platform_master_site

	fake_config = _FakeSaaSConfig()
	fake_config.max_reservation_assets = 5
	sync_calls = []

	try:
		frappe.session.user = "Administrator"
		frappe.get_roles = lambda user=None: ["System Manager"]
		frappe.get_doc = (
			lambda doctype, *args, **kwargs: fake_config if doctype == "SaaS Feature Config" else None
		)
		frappe.db.exists = lambda doctype, name=None, *args, **kwargs: False
		frappe.db.commit = lambda *args, **kwargs: None
		frappe.clear_cache = lambda *args, **kwargs: None
		saas_api.setup_company_identity_fields = lambda: None
		saas_api.sync_event_warehouses = lambda company_name, max_assets: sync_calls.append(
			(company_name, max_assets)
		)
		saas_api._is_platform_master_site = lambda: False

		result = saas_api.update_saas_config(
			company_name="Nueva Plataforma",
			max_reservation_assets="x",
		)

		assert result["success"] is True
		assert sync_calls == []
		assert fake_config.max_reservation_assets == 5
	finally:
		frappe.session.user = original_user
		frappe.get_roles = original_get_roles
		frappe.get_doc = original_get_doc
		frappe.db.exists = original_exists
		frappe.db.commit = original_commit
		frappe.clear_cache = original_clear_cache
		saas_api.setup_company_identity_fields = original_setup_fields
		saas_api.sync_event_warehouses = original_sync_warehouses
		saas_api._is_platform_master_site = original_master_gate


def test_update_saas_config_skips_reservation_validation_when_disabled():
	original_user = frappe.session.user
	original_get_roles = frappe.get_roles
	original_get_doc = frappe.get_doc
	original_exists = frappe.db.exists
	original_commit = frappe.db.commit
	original_clear_cache = frappe.clear_cache
	original_setup_fields = saas_api.setup_company_identity_fields
	original_sync_warehouses = saas_api.sync_event_warehouses
	original_master_gate = saas_api._is_platform_master_site

	class _DisabledReservationsConfig(_FakeSaaSConfig):
		def __init__(self):
			super().__init__()
			self.has_reservations = 0
			self.reservation_item_code = "Carrito Paletero"
			self.max_reservation_assets = 9
			self.default_event_items = '[{"item_code": "Carrito Paletero"}]'
			self.saved = False

		def save(self, ignore_permissions=False):
			assert self.reservation_item_code == ""
			assert self.max_reservation_assets == 0
			assert self.default_event_items == "[]"
			self.saved = True
			return self

	fake_config = _DisabledReservationsConfig()
	sync_calls = []

	try:
		frappe.session.user = "Administrator"
		frappe.get_roles = lambda user=None: ["System Manager"]
		frappe.get_doc = (
			lambda doctype, *args, **kwargs: fake_config if doctype == "SaaS Feature Config" else None
		)
		frappe.db.exists = lambda doctype, name=None, *args, **kwargs: False
		frappe.db.commit = lambda *args, **kwargs: None
		frappe.clear_cache = lambda *args, **kwargs: None
		saas_api.setup_company_identity_fields = lambda: None
		saas_api.sync_event_warehouses = lambda company_name, max_assets: sync_calls.append(
			(company_name, max_assets)
		)
		saas_api._is_platform_master_site = lambda: False

		result = saas_api.update_saas_config(
			company_name="Nueva Plataforma",
			has_reservations=0,
			reservation_item_code="Carrito Paletero",
			max_reservation_assets=7,
			default_event_items='[{"item_code": "Carrito Paletero"}]',
		)

		assert result["success"] is True
		assert fake_config.saved is True
		assert sync_calls == []
		assert fake_config.has_reservations == 0
	finally:
		frappe.session.user = original_user
		frappe.get_roles = original_get_roles
		frappe.get_doc = original_get_doc
		frappe.db.exists = original_exists
		frappe.db.commit = original_commit
		frappe.clear_cache = original_clear_cache
		saas_api.setup_company_identity_fields = original_setup_fields
		saas_api.sync_event_warehouses = original_sync_warehouses
		saas_api._is_platform_master_site = original_master_gate


def test_update_saas_config_skips_mexico_taxes_activation_when_disabled():
	original_user = frappe.session.user
	original_get_roles = frappe.get_roles
	original_get_doc = frappe.get_doc
	original_exists = frappe.db.exists
	original_commit = frappe.db.commit
	original_clear_cache = frappe.clear_cache
	original_setup_mexico_taxes = saas_api.setup_mexican_taxes_and_fields
	original_master_gate = saas_api._is_platform_master_site

	fake_config = _FakeSaaSConfig()
	fake_config.has_mexico_taxes = 0
	activation_calls = []

	try:
		frappe.session.user = "Administrator"
		frappe.get_roles = lambda user=None: ["System Manager"]
		frappe.get_doc = (
			lambda doctype, *args, **kwargs: fake_config if doctype == "SaaS Feature Config" else None
		)
		frappe.db.exists = lambda doctype, name=None, *args, **kwargs: False
		frappe.db.commit = lambda *args, **kwargs: None
		frappe.clear_cache = lambda *args, **kwargs: None
		saas_api.setup_mexican_taxes_and_fields = lambda company_name: activation_calls.append(company_name)
		saas_api._is_platform_master_site = lambda: False

		result = saas_api.update_saas_config(
			company_name="Nueva Plataforma",
			has_mexico_taxes=0,
		)

		assert result["success"] is True
		assert activation_calls == []
		assert fake_config.has_mexico_taxes == 0
	finally:
		frappe.session.user = original_user
		frappe.get_roles = original_get_roles
		frappe.get_doc = original_get_doc
		frappe.db.exists = original_exists
		frappe.db.commit = original_commit
		frappe.clear_cache = original_clear_cache
		saas_api.setup_mexican_taxes_and_fields = original_setup_mexico_taxes
		saas_api._is_platform_master_site = original_master_gate


def test_activate_mexican_taxes_handles_setup_outside_generic_save():
	original_user = frappe.session.user
	original_get_roles = frappe.get_roles
	original_get_doc = frappe.get_doc
	original_commit = frappe.db.commit
	original_clear_cache = frappe.clear_cache
	original_setup_mexico_taxes = saas_api.setup_mexican_taxes_and_fields
	original_master_gate = saas_api._is_platform_master_site

	fake_config = _FakeSaaSConfig()
	fake_config.has_mexico_taxes = 0
	activation_calls = []

	try:
		frappe.session.user = "Administrator"
		frappe.get_roles = lambda user=None: ["System Manager"]
		frappe.get_doc = (
			lambda doctype, *args, **kwargs: fake_config if doctype == "SaaS Feature Config" else None
		)
		frappe.db.commit = lambda *args, **kwargs: None
		frappe.clear_cache = lambda *args, **kwargs: None
		saas_api.setup_mexican_taxes_and_fields = lambda company_name: activation_calls.append(company_name)
		saas_api._is_platform_master_site = lambda: False

		result = saas_api.activate_mexican_taxes(company_name="Nueva Plataforma")

		assert result["success"] is True
		assert activation_calls == ["Nueva Plataforma"]
		assert fake_config.has_mexico_taxes == 1
	finally:
		frappe.session.user = original_user
		frappe.get_roles = original_get_roles
		frappe.get_doc = original_get_doc
		frappe.db.commit = original_commit
		frappe.clear_cache = original_clear_cache
		saas_api.setup_mexican_taxes_and_fields = original_setup_mexico_taxes
		saas_api._is_platform_master_site = original_master_gate


def test_update_saas_config_saves_when_wholesale_is_disabled(monkeypatch):
	fake_config = _FakeSaaSConfig()
	wholesale_setup_calls = []

	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="Administrator"), raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, *args, **kwargs: fake_config if doctype == "SaaS Feature Config" else None,
	)
	monkeypatch.setattr(frappe.db, "exists", lambda doctype, name=None, *args, **kwargs: False)
	monkeypatch.setattr(frappe.db, "commit", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe, "clear_cache", lambda *args, **kwargs: None)
	monkeypatch.setattr(saas_api, "setup_company_identity_fields", lambda: None)
	monkeypatch.setattr(
		saas_api, "setup_wholesale_custom_fields", lambda: wholesale_setup_calls.append("called")
	)
	monkeypatch.setattr(saas_api, "sync_event_warehouses", lambda *args, **kwargs: None)
	monkeypatch.setattr(saas_api, "_is_platform_master_site", lambda: False)

	result = saas_api.update_saas_config(company_name="Nueva Plataforma", has_wholesale=0)

	assert result["success"] is True
	assert fake_config.has_wholesale == 0
	assert wholesale_setup_calls == []


def test_update_saas_config_rejects_invalid_distribution_warehouse(monkeypatch):
	fake_config = _FakeSaaSConfig()
	fake_config.company_name = "Tenant Co"
	fake_config.company_abbr = "TC"
	fake_config.saved = False

	def _save(ignore_permissions=False):
		fake_config.saved = True
		return fake_config

	fake_config.save = _save

	def _get_doc(doctype, *args, **kwargs):
		if doctype == "SaaS Feature Config":
			return fake_config
		return None

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return fake_config
		if doctype == "Warehouse" and name == "Grupo - TC":
			return SimpleNamespace(company="Tenant Co", is_group=1, disabled=0)
		if doctype == "Company":
			return SimpleNamespace(abbr="TC")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="Administrator"), raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(frappe, "get_doc", _get_doc)
	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(frappe.db, "commit", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe, "clear_cache", lambda *args, **kwargs: None)
	monkeypatch.setattr(saas_api, "setup_company_identity_fields", lambda: None)
	monkeypatch.setattr(saas_api, "_is_platform_master_site", lambda: False)

	with pytest.raises(frappe.ValidationError, match="grupo"):
		saas_api.update_saas_config(default_distribution_warehouse="Grupo - TC")

	assert fake_config.saved is False


def test_update_saas_config_validates_distribution_warehouse_against_requested_company(monkeypatch):
	fake_config = _FakeSaaSConfig()
	fake_config.company_name = "Old Co"
	fake_config.company_abbr = "OLD"
	fake_config.saved = False

	def _save(ignore_permissions=False):
		fake_config.saved = True
		return fake_config

	fake_config.save = _save

	def _get_doc(doctype, *args, **kwargs):
		if doctype == "SaaS Feature Config":
			return fake_config
		return None

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return fake_config
		if doctype == "Warehouse" and name == "Fabrica - OLD":
			return SimpleNamespace(company="Old Co", is_group=0, disabled=0)
		if doctype == "Company" and name == "New Co":
			return SimpleNamespace(abbr="NEW")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="Administrator"), raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(frappe, "get_doc", _get_doc)
	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(
		frappe.db,
		"exists",
		lambda doctype, name=None, *args, **kwargs: doctype == "Company" and name == "New Co",
	)
	monkeypatch.setattr(frappe.db, "commit", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe, "clear_cache", lambda *args, **kwargs: None)
	monkeypatch.setattr(saas_api, "setup_company_identity_fields", lambda: None)
	monkeypatch.setattr(saas_api, "_is_platform_master_site", lambda: False)

	with pytest.raises(frappe.ValidationError, match="New Co"):
		saas_api.update_saas_config(company_name="New Co", default_distribution_warehouse="Fabrica - OLD")

	assert fake_config.saved is False


def test_saas_feature_config_validate_rejects_invalid_distribution_warehouse(monkeypatch):
	doc = SaaSFeatureConfig({"doctype": "SaaS Feature Config"})
	doc.company_name = "Tenant Co"
	doc.default_distribution_warehouse = "Grupo - TC"

	def _get_cached_doc(doctype, name=None):
		if doctype == "Warehouse" and name == "Grupo - TC":
			return SimpleNamespace(company="Tenant Co", is_group=1, disabled=0)
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)

	with pytest.raises(frappe.ValidationError, match="grupo"):
		doc.validate()


def test_get_event_warehouses_requires_system_manager(monkeypatch):
	monkeypatch.setattr(frappe.session, "user", "customer@example.test", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: [])
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected config lookup")),
	)

	with pytest.raises(frappe.PermissionError, match="No tenés permisos"):
		saas_api.get_event_warehouses()


@pytest.mark.parametrize(
	"call, kwargs",
	[
		(saas_api.get_event_reservations, {}),
		(saas_api.get_pending_event_bookings, {}),
		(saas_api.get_event_warehouses, {}),
		(saas_api.get_event_reservation_production_demand, {"event_date": "2026-07-07"}),
		(saas_api.complete_event_booking, {"sales_order_name": "SO-RES-0001"}),
		(saas_api.cancel_event_booking, {"sales_order_name": "SO-RES-0001"}),
		(saas_api.release_event_booking, {"sales_order_name": "SO-RES-0001"}),
	],
)
def test_event_lifecycle_admin_apis_reject_non_admin_users(monkeypatch, call, kwargs):
	monkeypatch.setattr(frappe.session, "user", "employee@example.test", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["Employee"])
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected config lookup")),
	)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected document lookup")),
	)
	monkeypatch.setattr(
		frappe.db,
		"sql",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected SQL lookup")),
	)

	with pytest.raises(frappe.PermissionError, match=r"No tenés permisos|Iniciá sesión"):
		call(**kwargs)


def test_get_event_reservations_includes_pending_and_confirmed(monkeypatch):
	class _ReservationItem:
		def __init__(self, payload):
			self._payload = payload

		def as_dict(self):
			return self._payload

	pending = SimpleNamespace(
		name="SO-RES-0001",
		sales_order="SO-RES-0001",
		customer="CUST-0001",
		event_date="2026-07-07",
		company="Tenant Co",
		grand_total=120.0,
		advance_paid=20.0,
		outstanding_amount=100.0,
		assigned_cart_warehouse="",
		reservation_item_code="Carrito Paletero",
		state="Pending Confirmation",
		sales_invoice="",
		payment_entry="",
		delivery_note="",
		cancel_reason="",
		release_notes="",
		items=[
			_ReservationItem(
				{"item_code": "ITEM-A", "item_name": "Item A", "qty": 1, "rate": 120.0, "amount": 120.0}
			)
		],
	)
	confirmed = SimpleNamespace(
		name="SO-RES-0002",
		sales_order="SO-RES-0002",
		customer="CUST-0002",
		event_date="2026-07-08",
		company="Tenant Co",
		grand_total=200.0,
		advance_paid=200.0,
		outstanding_amount=0.0,
		assigned_cart_warehouse="Carrito - TC",
		reservation_item_code="Carrito Paletero",
		state="Confirmed",
		sales_invoice="SI-RES-0002",
		payment_entry="PE-RES-0002",
		delivery_note="",
		cancel_reason="",
		release_notes="",
		items=[
			_ReservationItem(
				{"item_code": "ITEM-B", "item_name": "Item B", "qty": 2, "rate": 100.0, "amount": 200.0}
			)
		],
	)
	reservation_docs = {
		pending.name: pending,
		confirmed.name: confirmed,
	}
	value_map = {
		("Customer", "CUST-0001", "customer_name"): "Cliente Pendiente",
		("Customer", "CUST-0001", "mobile_no"): "5550000001",
		("Customer", "CUST-0002", "customer_name"): "Cliente Confirmado",
		("Customer", "CUST-0002", "mobile_no"): "5550000002",
		("Sales Order", "SO-RES-0001", "transaction_date"): "2026-07-03",
		("Sales Order", "SO-RES-0002", "transaction_date"): "2026-07-04",
	}

	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="Administrator"), raising=False)
	monkeypatch.setattr(
		frappe,
		"local",
		SimpleNamespace(flags=SimpleNamespace(in_test=True, mute_messages=False)),
		raising=False,
	)
	monkeypatch.setattr(frappe, "flags", SimpleNamespace(in_test=True, mute_messages=False), raising=False)
	monkeypatch.setattr(frappe, "_", lambda text, *args, **kwargs: text, raising=False)
	monkeypatch.setattr(
		frappe,
		"msgprint",
		lambda msg, *args, **kwargs: (_ for _ in ()).throw(frappe.ValidationError(msg)),
		raising=False,
	)
	monkeypatch.setattr(
		frappe,
		"throw",
		lambda msg, *args, **kwargs: (_ for _ in ()).throw(frappe.ValidationError(msg)),
		raising=False,
	)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe,
		"db",
		SimpleNamespace(
			exists=lambda doctype, name=None, *args, **kwargs: doctype == "Event Cart Reservation"
			and name in reservation_docs,
			get_value=lambda doctype, name, fieldname, *args, **kwargs: value_map.get(
				(doctype, name, fieldname)
			),
			commit=lambda *args, **kwargs: None,
			begin=lambda *args, **kwargs: None,
			rollback=lambda *args, **kwargs: None,
		),
		raising=False,
	)

	def _fake_get_all(doctype, filters=None, fields=None, order_by=None, pluck=None, *args, **kwargs):
		if doctype != "Event Cart Reservation":
			return []

		requested_states = (filters or {}).get("state")
		if isinstance(requested_states, (list, tuple)) and requested_states[:1] == ["in"]:
			allowed_states = set(requested_states[1])
		elif requested_states:
			allowed_states = {requested_states}
		else:
			allowed_states = {pending.state, confirmed.state}

		rows = []
		if confirmed.state in allowed_states:
			rows.append(SimpleNamespace(name=confirmed.name))
		if pending.state in allowed_states:
			rows.append(SimpleNamespace(name=pending.name))
		return rows

	monkeypatch.setattr(frappe, "get_all", _fake_get_all)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, name=None, *args, **kwargs: reservation_docs[name]
		if doctype == "Event Cart Reservation"
		else None,
	)
	reservations = saas_api.get_event_reservations()
	pending_only = saas_api.get_pending_event_bookings()

	assert [reservation.name for reservation in reservations] == ["SO-RES-0002", "SO-RES-0001"]
	assert {reservation.state for reservation in reservations} == {"Pending Confirmation", "Confirmed"}
	assert reservations[0]["items"][0]["item_code"] == "ITEM-B"
	assert reservations[0].customer_name == "Cliente Confirmado"
	assert [reservation.name for reservation in pending_only] == ["SO-RES-0001"]
	assert pending_only[0].state == "Pending Confirmation"


def test_complete_event_booking_rejects_invalid_supplied_warehouse(monkeypatch):
	fake_config = _FakeSaaSConfig()
	fake_config.company_name = "Tenant Co"
	fake_config.company_abbr = "TC"
	reservation = _FakeDoc("Event Cart Reservation", "SO-RES-0001", docstatus=0)
	reservation.state = "Confirmed"
	reservation.sales_order = "SO-RES-0001"
	reservation.company = "Tenant Co"
	reservation.event_date = "2026-07-07"
	reservation.reservation_item_code = "Carrito Paletero"
	so = SimpleNamespace(name="SO-RES-0001", docstatus=1, company="Tenant Co")

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return fake_config
		if doctype == "Warehouse" and name == "Grupo - TC":
			return SimpleNamespace(company="Tenant Co", is_group=1, disabled=0)
		if doctype == "Company" and name == "Tenant Co":
			return SimpleNamespace(abbr="TC")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="Administrator"), raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe.db,
		"exists",
		lambda doctype, name=None, *args, **kwargs: doctype in {"Sales Order", "Event Cart Reservation"},
	)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, name=None: reservation if doctype == "Event Cart Reservation" else so,
	)
	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)

	with pytest.raises(frappe.ValidationError, match="grupo"):
		saas_api.complete_event_booking("SO-RES-0001", warehouse="Grupo - TC")


def test_complete_event_booking_rejects_warehouse_outside_event_allowlist(monkeypatch):
	fake_config = _FakeSaaSConfig()
	fake_config.company_name = "Tenant Co"
	fake_config.company_abbr = "TC"
	fake_config.default_distribution_warehouse = "Fabrica - TC"
	reservation = _FakeDoc("Event Cart Reservation", "SO-RES-0001", docstatus=0)
	reservation.state = "Confirmed"
	reservation.sales_order = "SO-RES-0001"
	reservation.company = "Tenant Co"
	reservation.event_date = "2026-07-07"
	reservation.reservation_item_code = "Carrito Paletero"
	so = SimpleNamespace(name="SO-RES-0001", docstatus=1, company="Tenant Co")

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return fake_config
		if doctype == "Warehouse" and name in {"Fabrica - TC", "Sucursal - TC"}:
			return SimpleNamespace(company="Tenant Co", is_group=0, disabled=0)
		if doctype == "Company" and name == "Tenant Co":
			return SimpleNamespace(abbr="TC")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	def _exists(doctype, name=None, *args, **kwargs):
		return doctype in {"Sales Order", "Event Cart Reservation"} or (
			doctype == "Warehouse" and name == "Carritos de Eventos - TC"
		)

	def _get_all(doctype, *args, **kwargs):
		if doctype == "Warehouse":
			return [SimpleNamespace(name="Carrito Evento 1 - TC", warehouse_name="Carrito Evento 1 - TC")]
		return []

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(frappe.db, "exists", _exists)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, name=None: reservation if doctype == "Event Cart Reservation" else so,
	)
	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(frappe, "get_all", _get_all)

	with pytest.raises(frappe.ValidationError, match="no está habilitado"):
		saas_api.complete_event_booking("SO-RES-0001", warehouse="Sucursal - TC")


def test_check_cart_availability_counts_event_reservation_rows(monkeypatch):
	fake_config = _FakeSaaSConfig()
	fake_config.has_reservations = 1
	fake_config.max_reservation_assets = 3
	fake_config.reservation_item_code = "Carrito Paletero"

	monkeypatch.setattr(frappe, "get_cached_doc", lambda doctype, name=None: fake_config)
	monkeypatch.setattr(saas_api, "get_platform_company_name", lambda: "Tenant Co")

	def _get_all(doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs):
		assert doctype == "Event Cart Reservation"
		assert filters["event_date"] == "2026-07-07"
		assert filters["company"] == "Tenant Co"
		assert filters["state"] == ["in", ["Pending Confirmation", "Confirmed"]]
		return [{"name": "RES-1"}, {"name": "RES-2"}]

	monkeypatch.setattr(frappe, "get_all", _get_all)

	result = saas_api.check_cart_availability("2026-07-07")

	assert result["enabled"] is True
	assert result["active_reserved"] == 2
	assert result["already_booked"] == 2
	assert result["available_qty"] == 1


def test_get_event_reservation_production_demand_requires_admin_access_and_delegates(monkeypatch):
	monkeypatch.setattr(frappe.session, "user", "employee@example.test", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["Employee"])
	monkeypatch.setattr(
		saas_api,
		"_get_event_reservation_production_demand",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected service delegation")),
	)

	with pytest.raises(frappe.PermissionError, match=r"No tenés permisos|Iniciá sesión"):
		saas_api.get_event_reservation_production_demand("2026-07-07")

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])

	calls = []

	def _fake_service(event_date, company=None):
		calls.append((event_date, company))
		return {"date": event_date, "items": [{"item_code": "ITEM-A", "item_name": "Item A", "qty": 2.0}]}

	monkeypatch.setattr(saas_api, "_get_event_reservation_production_demand", _fake_service)

	result = saas_api.get_event_reservation_production_demand("2026-07-07", company="Tenant Co")
	default_result = saas_api.get_event_reservation_production_demand("2026-07-07")

	assert calls == [("2026-07-07", "Tenant Co"), ("2026-07-07", None)]
	assert result == {
		"date": "2026-07-07",
		"items": [{"item_code": "ITEM-A", "item_name": "Item A", "qty": 2.0}],
	}
	assert default_result == {
		"date": "2026-07-07",
		"items": [{"item_code": "ITEM-A", "item_name": "Item A", "qty": 2.0}],
	}


def test_complete_event_booking_rejects_company_mismatch_before_warehouse_validation(monkeypatch):
	class _FakeInvoice:
		def __init__(self):
			self.name = "SI-1"
			self.update_stock = 1
			self.posting_date = None
			self.set_posting_time = 0
			self.currency = "MXN"
			self.grand_total = 120.0
			self.outstanding_amount = 40.0
			self.items = [
				SimpleNamespace(item_code="Carrito Paletero", warehouse="Fabrica - TC", name="SI-ITEM-1"),
				SimpleNamespace(item_code="ITEM-A", warehouse="Fabrica - TC", name="SI-ITEM-2"),
			]
			self.inserted = False
			self.submitted = False

		def insert(self, ignore_permissions=False):
			assert self.update_stock == 0
			self.inserted = True
			return self

		def submit(self):
			self.submitted = True
			self.docstatus = 1
			return self

	class _FakePaymentEntry:
		def __init__(self):
			self.name = "PE-1"
			self.references = [SimpleNamespace(allocated_amount=0)]
			self.mode_of_payment = ""
			self.reference_no = ""
			self.reference_date = None
			self.paid_to = ""
			self.paid_amount = 0.0
			self.received_amount = 0.0
			self.inserted = False
			self.submitted = False

		def insert(self, ignore_permissions=False):
			self.inserted = True
			return self

		def submit(self):
			self.submitted = True
			self.docstatus = 1
			return self

	reservation = _FakeDoc("Event Cart Reservation", "SO-RES-0001", docstatus=0)
	reservation.state = "Pending Confirmation"
	reservation.sales_order = "SO-RES-0001"
	reservation.customer = "Customer A"
	reservation.event_date = "2026-07-07"
	reservation.company = "Other Co"
	reservation.reservation_item_code = "Carrito Paletero"
	reservation.assigned_cart_warehouse = ""
	reservation.sales_invoice = ""
	reservation.payment_entry = ""
	reservation.grand_total = 0.0
	reservation.base_grand_total = 0.0
	reservation.advance_paid = 0.0
	reservation.outstanding_amount = 0.0
	reservation.items = []

	so = _FakeDoc("Sales Order", "SO-RES-0001", docstatus=1)
	so.company = "Tenant Co"
	so.currency = "MXN"
	so.status = "To Deliver and Bill"
	so.items = [
		SimpleNamespace(
			name="SO-ITEM-1", item_code="ITEM-A", item_name="Item A", qty=2, rate=60.0, amount=120.0
		)
	]
	so.grand_total = 120.0

	invoice = _FakeInvoice()
	payment = _FakePaymentEntry()

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe,
		"db",
		SimpleNamespace(
			exists=lambda doctype, name=None, *args, **kwargs: doctype
			in {"Sales Order", "Event Cart Reservation"},
			commit=lambda *args, **kwargs: None,
			begin=lambda *args, **kwargs: None,
			rollback=lambda *args, **kwargs: None,
			get_value=lambda doctype, name, fieldname, *args, **kwargs: 40.0
			if doctype == "Sales Invoice"
			else None,
		),
	)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, name=None, *args, **kwargs: reservation
		if doctype == "Event Cart Reservation"
		else so,
	)
	monkeypatch.setattr(
		"erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
		lambda sales_order_name: invoice,
	)
	monkeypatch.setattr(
		"erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
		lambda doctype, name, bank_amount=None: payment,
	)
	monkeypatch.setattr(
		saas_api,
		"validate_confirmed_allocation_warehouse",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("warehouse validation must not run on company mismatch")
		),
	)
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("config lookup must not run on company mismatch")
		),
	)

	with pytest.raises(frappe.ValidationError, match="compañía"):
		saas_api.complete_event_booking(
			"SO-RES-0001", register_payment=True, payment_mode="Cash", warehouse="Other Co Cart"
		)


def test_complete_event_booking_fails_before_invoice_creation_when_cash_account_missing(monkeypatch):
	fake_config = _FakeSaaSConfig()
	fake_config.company_name = "Tenant Co"
	fake_config.company_abbr = "TC"
	fake_config.default_distribution_warehouse = "Fabrica - TC"
	reservation = _FakeDoc("Event Cart Reservation", "SO-RES-0001", docstatus=0)
	reservation.state = "Pending Confirmation"
	reservation.sales_order = "SO-RES-0001"
	reservation.customer = "Customer A"
	reservation.event_date = "2026-07-07"
	reservation.company = "Tenant Co"
	reservation.reservation_item_code = "Carrito Paletero"
	reservation.assigned_cart_warehouse = ""
	reservation.sales_invoice = ""
	reservation.payment_entry = ""
	reservation.grand_total = 120.0
	reservation.base_grand_total = 120.0
	reservation.advance_paid = 20.0
	reservation.outstanding_amount = 100.0
	reservation.items = []

	so = _FakeDoc("Sales Order", "SO-RES-0001", docstatus=1)
	so.company = "Tenant Co"
	so.currency = "MXN"
	so.status = "To Deliver and Bill"
	so.items = [
		SimpleNamespace(
			name="SO-ITEM-1", item_code="ITEM-A", item_name="Item A", qty=2, rate=50.0, amount=100.0
		)
	]
	so.grand_total = 100.0

	make_sales_invoice_calls = []

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return fake_config
		if doctype == "Company" and name == "Tenant Co":
			return SimpleNamespace(abbr="TC")
		if doctype == "Warehouse" and name == "Fabrica - TC":
			return SimpleNamespace(company="Tenant Co", is_group=0, disabled=0)
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe,
		"db",
		SimpleNamespace(
			exists=lambda doctype, name=None, *args, **kwargs: doctype
			in {"Sales Order", "Event Cart Reservation"},
			commit=lambda *args, **kwargs: None,
			begin=lambda *args, **kwargs: None,
			rollback=lambda *args, **kwargs: None,
			get_value=lambda *args, **kwargs: 0.0,
		),
	)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, name=None, *args, **kwargs: reservation
		if doctype == "Event Cart Reservation"
		else so,
	)
	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(saas_api, "get_platform_distribution_warehouse", lambda: "Fabrica - TC")
	monkeypatch.setattr(
		saas_api, "_validate_event_booking_warehouse", lambda warehouse, company_name=None: warehouse
	)
	monkeypatch.setattr(
		"erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
		lambda sales_order_name: make_sales_invoice_calls.append(sales_order_name) or _FakeInvoice(),
	)
	monkeypatch.setattr(
		saas_api,
		"get_platform_payment_account",
		lambda payment_mode: (_ for _ in ()).throw(
			frappe.ValidationError(
				"Configurá la cuenta contable por defecto para efectivo en SaaS Feature Config."
			)
		),
	)

	with pytest.raises(frappe.ValidationError, match="cuenta contable por defecto para efectivo"):
		saas_api.complete_event_booking(
			"SO-RES-0001", register_payment=True, payment_mode="Cash", warehouse="Fabrica - TC"
		)

	assert make_sales_invoice_calls == []
	assert reservation.save_calls == 0
	assert reservation.state == "Pending Confirmation"
	assert reservation.assigned_cart_warehouse == ""
	assert reservation.sales_invoice == ""
	assert reservation.payment_entry == ""


def test_complete_event_booking_fails_before_invoice_creation_when_cash_account_missing_even_if_reservation_outstanding_is_stale(
	monkeypatch,
):
	fake_config = _FakeSaaSConfig()
	fake_config.company_name = "Tenant Co"
	fake_config.company_abbr = "TC"
	fake_config.default_distribution_warehouse = "Fabrica - TC"
	reservation = _FakeDoc("Event Cart Reservation", "SO-RES-0001", docstatus=0)
	reservation.state = "Pending Confirmation"
	reservation.sales_order = "SO-RES-0001"
	reservation.customer = "Customer A"
	reservation.event_date = "2026-07-07"
	reservation.company = "Tenant Co"
	reservation.reservation_item_code = "Carrito Paletero"
	reservation.assigned_cart_warehouse = ""
	reservation.sales_invoice = ""
	reservation.payment_entry = ""
	reservation.grand_total = 0.0
	reservation.base_grand_total = 0.0
	reservation.advance_paid = 0.0
	reservation.outstanding_amount = 0.0
	reservation.items = []

	so = _FakeDoc("Sales Order", "SO-RES-0001", docstatus=1)
	so.company = "Tenant Co"
	so.currency = "MXN"
	so.status = "To Deliver and Bill"
	so.items = [
		SimpleNamespace(
			name="SO-ITEM-1", item_code="ITEM-A", item_name="Item A", qty=2, rate=50.0, amount=100.0
		)
	]
	so.grand_total = 120.0
	so.outstanding_amount = 120.0

	make_sales_invoice_calls = []

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe,
		"db",
		SimpleNamespace(
			exists=lambda doctype, name=None, *args, **kwargs: doctype
			in {"Sales Order", "Event Cart Reservation"},
			commit=lambda *args, **kwargs: None,
			begin=lambda *args, **kwargs: None,
			rollback=lambda *args, **kwargs: None,
			get_value=lambda *args, **kwargs: 0.0,
		),
	)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, name=None, *args, **kwargs: reservation
		if doctype == "Event Cart Reservation"
		else so,
	)
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: fake_config
		if doctype == "SaaS Feature Config"
		else (_ for _ in ()).throw(AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")),
	)
	monkeypatch.setattr(saas_api, "get_platform_distribution_warehouse", lambda: "Fabrica - TC")
	monkeypatch.setattr(
		saas_api, "_validate_event_booking_warehouse", lambda warehouse, company_name=None: warehouse
	)
	monkeypatch.setattr(
		"erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
		lambda sales_order_name: make_sales_invoice_calls.append(sales_order_name) or _FakeInvoice(),
	)
	monkeypatch.setattr(
		saas_api,
		"get_platform_payment_account",
		lambda payment_mode: (_ for _ in ()).throw(
			frappe.ValidationError(
				"Configurá la cuenta contable por defecto para efectivo en SaaS Feature Config."
			)
		),
	)

	with pytest.raises(frappe.ValidationError, match="cuenta contable por defecto para efectivo"):
		saas_api.complete_event_booking(
			"SO-RES-0001", register_payment=True, payment_mode="Cash", warehouse="Fabrica - TC"
		)

	assert make_sales_invoice_calls == []
	assert reservation.save_calls == 0
	assert reservation.state == "Pending Confirmation"
	assert reservation.assigned_cart_warehouse == ""
	assert reservation.sales_invoice == ""
	assert reservation.payment_entry == ""


@pytest.mark.parametrize("register_payment", [False, 0, "0"])
def test_complete_event_booking_rejects_unpaid_confirmation_requests(monkeypatch, register_payment):
	reservation = _FakeDoc("Event Cart Reservation", "SO-RES-0001", docstatus=0)
	reservation.state = "Pending Confirmation"
	reservation.sales_order = "SO-RES-0001"
	reservation.customer = "Customer A"
	reservation.event_date = "2026-07-07"
	reservation.company = "Tenant Co"
	reservation.reservation_item_code = "Carrito Paletero"
	reservation.assigned_cart_warehouse = ""
	reservation.sales_invoice = ""
	reservation.payment_entry = ""
	reservation.grand_total = 0.0
	reservation.base_grand_total = 0.0
	reservation.advance_paid = 0.0
	reservation.outstanding_amount = 0.0
	reservation.items = []

	so = _FakeDoc("Sales Order", "SO-RES-0001", docstatus=1)
	so.company = "Tenant Co"
	so.currency = "MXN"
	so.status = "To Deliver and Bill"
	so.items = [
		SimpleNamespace(
			name="SO-ITEM-1", item_code="ITEM-A", item_name="Item A", qty=2, rate=50.0, amount=100.0
		)
	]
	so.grand_total = 120.0

	make_sales_invoice_calls = []

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe,
		"db",
		SimpleNamespace(
			exists=lambda doctype, name=None, *args, **kwargs: doctype
			in {"Sales Order", "Event Cart Reservation"},
			commit=lambda *args, **kwargs: None,
			begin=lambda *args, **kwargs: None,
			rollback=lambda *args, **kwargs: None,
			get_value=lambda *args, **kwargs: 0.0,
		),
	)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, name=None, *args, **kwargs: reservation
		if doctype == "Event Cart Reservation"
		else so,
	)
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("config lookup must not run when payment registration is disabled")
		),
	)
	monkeypatch.setattr(
		saas_api,
		"get_platform_distribution_warehouse",
		lambda: (_ for _ in ()).throw(
			AssertionError("warehouse lookup must not run when payment registration is disabled")
		),
	)
	monkeypatch.setattr(
		"erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
		lambda sales_order_name: make_sales_invoice_calls.append(sales_order_name) or _FakeInvoice(),
	)

	with pytest.raises(frappe.ValidationError, match="requiere registrar el pago"):
		saas_api.complete_event_booking(
			"SO-RES-0001", register_payment=register_payment, payment_mode="Cash", warehouse="Fabrica - TC"
		)

	assert make_sales_invoice_calls == []
	assert reservation.save_calls == 0
	assert reservation.state == "Pending Confirmation"
	assert reservation.assigned_cart_warehouse == ""
	assert reservation.sales_invoice == ""
	assert reservation.payment_entry == ""


def test_release_event_booking_creates_delivery_note_once(monkeypatch):
	class _FakeDeliveryNote:
		def __init__(self):
			self.name = "DN-1"
			self.items = [
				SimpleNamespace(item_code="Carrito Paletero", warehouse="Fabrica - TC"),
				SimpleNamespace(item_code="ITEM-A", warehouse="Fabrica - TC"),
			]
			self.inserted = False
			self.submitted = False

		def insert(self, ignore_permissions=False):
			self.inserted = True
			return self

		def submit(self):
			self.submitted = True
			self.docstatus = 1
			return self

	reservation = _FakeDoc("Event Cart Reservation", "SO-RES-0002", docstatus=0)
	reservation.state = "Confirmed"
	reservation.sales_order = "SO-RES-0002"
	reservation.customer = "Customer A"
	reservation.event_date = "2026-07-08"
	reservation.company = "Tenant Co"
	reservation.reservation_item_code = "Carrito Paletero"
	reservation.assigned_cart_warehouse = "Other Co Cart"
	reservation.delivery_note = ""
	reservation.release_notes = ""
	reservation.released_at = None
	reservation.released_by = None
	reservation.items = []

	so = _FakeDoc("Sales Order", "SO-RES-0002", docstatus=1)
	so.company = "Tenant Co"
	so.currency = "MXN"
	so.items = [
		SimpleNamespace(
			name="SO-ITEM-1", item_code="ITEM-A", item_name="Item A", qty=2, rate=60.0, amount=120.0
		)
	]

	delivery_note = _FakeDeliveryNote()
	make_delivery_note_calls = []
	sql_calls = []
	warehouse_validation_calls = []

	def _sql(query, params=None, *args, **kwargs):
		sql_calls.append((query, params))
		if "GET_LOCK" in query or "RELEASE_LOCK" in query:
			return [(1,)]
		raise AssertionError(f"Unexpected SQL: {query}")

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return SimpleNamespace(reservation_item_code="Carrito Paletero")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe,
		"db",
		SimpleNamespace(
			exists=lambda doctype, name=None, *args, **kwargs: doctype
			in {"Sales Order", "Event Cart Reservation"},
			commit=lambda *args, **kwargs: None,
			begin=lambda *args, **kwargs: None,
			rollback=lambda *args, **kwargs: None,
			sql=_sql,
		),
	)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, name=None, *args, **kwargs: reservation
		if doctype == "Event Cart Reservation"
		else so,
	)
	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(
		saas_api,
		"validate_confirmed_allocation_warehouse",
		lambda warehouse, company_name=None: warehouse_validation_calls.append(company_name) or warehouse,
	)
	monkeypatch.setattr(
		"erpnext.selling.doctype.sales_order.sales_order.make_delivery_note",
		lambda sales_order_name: make_delivery_note_calls.append(sales_order_name) or delivery_note,
	)

	first = saas_api.release_event_booking("SO-RES-0002", release_notes="Release after event return")
	second = saas_api.release_event_booking("SO-RES-0002", release_notes="Release after event return")

	assert first["success"] is True
	assert first["delivery_note"] == "DN-1"
	assert second["success"] is True
	assert second["delivery_note"] == "DN-1"
	assert make_delivery_note_calls == ["SO-RES-0002"]
	assert (
		sql_calls.count(("SELECT GET_LOCK(%s, %s)", ("event_cart_reservation_release:SO-RES-0002", 5))) == 2
	)
	assert sql_calls.count(("SELECT RELEASE_LOCK(%s)", ("event_cart_reservation_release:SO-RES-0002",))) == 2
	assert warehouse_validation_calls == ["Tenant Co", "Tenant Co"]
	assert delivery_note.inserted is True
	assert delivery_note.submitted is True
	assert reservation.state == "Released"
	assert reservation.delivery_note == "DN-1"
	assert reservation.assigned_cart_warehouse == "Other Co Cart"


def test_release_event_booking_rejects_company_mismatch(monkeypatch):
	reservation = _FakeDoc("Event Cart Reservation", "SO-RES-0004", docstatus=0)
	reservation.state = "Confirmed"
	reservation.sales_order = "SO-RES-0004"
	reservation.customer = "Customer A"
	reservation.event_date = "2026-07-10"
	reservation.company = "Other Co"
	reservation.reservation_item_code = "Carrito Paletero"
	reservation.assigned_cart_warehouse = "Other Co Cart"
	reservation.delivery_note = ""
	reservation.release_notes = ""
	reservation.released_at = None
	reservation.released_by = None
	reservation.items = []

	so = _FakeDoc("Sales Order", "SO-RES-0004", docstatus=1)
	so.company = "Tenant Co"
	so.currency = "MXN"
	so.items = [
		SimpleNamespace(
			name="SO-ITEM-1", item_code="ITEM-A", item_name="Item A", qty=1, rate=60.0, amount=60.0
		)
	]

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe,
		"db",
		SimpleNamespace(
			exists=lambda doctype, name=None, *args, **kwargs: doctype
			in {"Sales Order", "Event Cart Reservation"},
			commit=lambda *args, **kwargs: None,
			begin=lambda *args, **kwargs: None,
			rollback=lambda *args, **kwargs: None,
			sql=lambda query, params=None, *args, **kwargs: [(1,)]
			if "GET_LOCK" in query or "RELEASE_LOCK" in query
			else [],
		),
	)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, name=None, *args, **kwargs: reservation
		if doctype == "Event Cart Reservation"
		else so,
	)
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("config lookup must not run on company mismatch")
		),
	)
	monkeypatch.setattr(
		saas_api,
		"validate_confirmed_allocation_warehouse",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("warehouse validation must not run on company mismatch")
		),
	)
	monkeypatch.setattr(
		"erpnext.selling.doctype.sales_order.sales_order.make_delivery_note",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("delivery note must not be created on company mismatch")
		),
	)

	with pytest.raises(frappe.ValidationError, match="compañía"):
		saas_api.release_event_booking("SO-RES-0004", release_notes="Release after event return")


def test_cancel_event_booking_requires_refund_evidence_for_confirmed_reservations(monkeypatch):
	reservation = _FakeDoc("Event Cart Reservation", "SO-RES-0003", docstatus=0)
	reservation.state = "Confirmed"
	reservation.sales_order = "SO-RES-0003"
	reservation.customer = "Customer A"
	reservation.event_date = "2026-07-09"
	reservation.company = "Tenant Co"
	reservation.reservation_item_code = "Carrito Paletero"
	reservation.sales_invoice = "SI-RES-0003"
	reservation.payment_entry = "PE-RES-0003"
	reservation.credit_note = ""
	reservation.refund_payment_entry = ""
	reservation.reconciliation_notes = ""
	reservation.cancel_reason = ""
	reservation.items = []

	so = _FakeDoc("Sales Order", "SO-RES-0003", docstatus=1)
	so.company = "Tenant Co"
	invoice = _FakeDoc("Sales Invoice", "SI-RES-0003", docstatus=1)
	payment = _FakeDoc("Payment Entry", "PE-RES-0003", docstatus=1)

	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="Administrator"), raising=False)
	monkeypatch.setattr(
		frappe,
		"local",
		SimpleNamespace(flags=SimpleNamespace(in_test=True, mute_messages=False)),
		raising=False,
	)
	monkeypatch.setattr(frappe, "_", lambda text, *args, **kwargs: text, raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe,
		"db",
		SimpleNamespace(
			exists=lambda doctype, name=None, *args, **kwargs: doctype
			in {"Sales Order", "Event Cart Reservation"},
			commit=lambda *args, **kwargs: None,
			begin=lambda *args, **kwargs: None,
			rollback=lambda *args, **kwargs: None,
		),
		raising=False,
	)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, name=None, *args, **kwargs: {
			("Event Cart Reservation", "SO-RES-0003"): reservation,
			("Sales Order", "SO-RES-0003"): so,
			("Sales Invoice", "SI-RES-0003"): invoice,
			("Payment Entry", "PE-RES-0003"): payment,
		}.get((doctype, name), so),
	)
	monkeypatch.setattr(
		frappe,
		"get_all",
		lambda doctype, filters=None, pluck=None, **kwargs: ["SI-RES-0003"]
		if doctype == "Sales Invoice Item"
		else ["PE-RES-0003"],
	)

	fake_frappe = SimpleNamespace(
		session=SimpleNamespace(user="Administrator"),
		get_roles=lambda user=None: ["System Manager"],
		db=SimpleNamespace(
			exists=lambda doctype, name=None, *args, **kwargs: doctype
			in {"Sales Order", "Event Cart Reservation", "Sales Invoice", "Payment Entry"},
			commit=lambda *args, **kwargs: None,
			begin=lambda *args, **kwargs: None,
			rollback=lambda *args, **kwargs: None,
		),
		utils=SimpleNamespace(
			now_datetime=lambda: "2026-07-09 00:00:00", cint=lambda value: int(bool(value))
		),
		get_doc=lambda doctype, name=None, *args, **kwargs: {
			("Event Cart Reservation", "SO-RES-0003"): reservation,
			("Sales Order", "SO-RES-0003"): so,
			("Sales Invoice", "SI-RES-0003"): invoice,
			("Payment Entry", "PE-RES-0003"): payment,
		}.get((doctype, name), so),
		get_all=lambda doctype, filters=None, pluck=None, **kwargs: ["SI-RES-0003"]
		if doctype == "Sales Invoice Item"
		else ["PE-RES-0003"],
		_=lambda text, *args, **kwargs: text,
		ValidationError=frappe.ValidationError,
		throw=lambda msg, *args, **kwargs: (_ for _ in ()).throw(frappe.ValidationError(msg)),
	)
	monkeypatch.setattr(saas_api, "frappe", fake_frappe, raising=False)

	with pytest.raises(frappe.ValidationError, match="evidencia"):
		saas_api.cancel_event_booking("SO-RES-0003")

	result = saas_api.cancel_event_booking(
		"SO-RES-0003",
		refund_evidence="Refunded via credit note",
		reversal_evidence="PE reversed",
	)

	assert result["success"] is True
	assert reservation.state == "Cancelled"
	assert reservation.cancel_calls == 0
	assert reservation.save_calls == 1
	assert so.cancel_calls == 1
	assert invoice.cancel_calls == 1
	assert payment.cancel_calls == 1


def test_get_admin_dashboard_metrics_does_not_bootstrap_optional_fields(monkeypatch):
	fake_config = _FakeSaaSConfig()
	wholesale_setup_calls = []
	reservation_setup_calls = []
	custom_field_creations = []

	def _sql_stub(query, *args, **kwargs):
		if kwargs.get("as_dict"):
			return []
		if "COUNT(" in query and "SUM(" in query:
			return [(0, 0.0)]
		if "SUM(grand_total)" in query:
			return [(0.0,)]
		return []

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: fake_config if doctype == "SaaS Feature Config" else None,
	)
	monkeypatch.setattr(frappe.db, "sql", _sql_stub)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, *args, **kwargs: custom_field_creations.append((doctype, args, kwargs))
		or SimpleNamespace(),
	)
	monkeypatch.setattr(
		saas_api, "setup_wholesale_custom_fields", lambda: wholesale_setup_calls.append("called")
	)
	monkeypatch.setattr(
		saas_api, "setup_reservation_fields", lambda: reservation_setup_calls.append("called")
	)
	monkeypatch.setattr(saas_api, "get_platform_company_name", lambda: "Nueva Plataforma")
	monkeypatch.setattr(saas_api, "get_platform_company_abbr", lambda company=None: "NP")

	result = saas_api.get_admin_dashboard_metrics()

	assert result["success"] is True
	assert wholesale_setup_calls == []
	assert reservation_setup_calls == []
	assert custom_field_creations == []


def test_get_sales_report_data_does_not_bootstrap_reservation_setup_when_disabled(monkeypatch):
	fake_config = _FakeSaaSConfig()
	fake_config.has_reservations = 0
	wholesale_setup_calls = []
	reservation_setup_calls = []

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(frappe.db, "sql", lambda *args, **kwargs: [])
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: fake_config if doctype == "SaaS Feature Config" else None,
	)
	monkeypatch.setattr(
		saas_api, "setup_wholesale_custom_fields", lambda: wholesale_setup_calls.append("called")
	)
	monkeypatch.setattr(
		saas_api, "setup_reservation_fields", lambda: reservation_setup_calls.append("called")
	)
	monkeypatch.setattr(saas_api, "get_platform_company_name", lambda: "Nueva Plataforma")
	monkeypatch.setattr(saas_api, "get_platform_company_abbr", lambda company=None: "NP")

	result = saas_api.get_sales_report_data()

	assert result["success"] is True
	assert result["sales_trend"] == []
	assert wholesale_setup_calls == []
	assert reservation_setup_calls == []


def test_get_stock_report_data_does_not_bootstrap_reservation_setup_when_disabled(monkeypatch):
	fake_config = _FakeSaaSConfig()
	fake_config.has_reservations = 0
	wholesale_setup_calls = []
	reservation_setup_calls = []

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(frappe.db, "sql", lambda *args, **kwargs: [])
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: fake_config if doctype == "SaaS Feature Config" else None,
	)
	monkeypatch.setattr(
		saas_api, "setup_wholesale_custom_fields", lambda: wholesale_setup_calls.append("called")
	)
	monkeypatch.setattr(
		saas_api, "setup_reservation_fields", lambda: reservation_setup_calls.append("called")
	)
	monkeypatch.setattr(saas_api, "get_platform_company_name", lambda: "Nueva Plataforma")
	monkeypatch.setattr(saas_api, "get_platform_company_abbr", lambda company=None: "NP")

	result = saas_api.get_stock_report_data()

	assert result["success"] is True
	assert result["stock_data"] == []
	assert wholesale_setup_calls == []
	assert reservation_setup_calls == []


def test_get_all_customers_omits_wholesale_pin_when_field_is_missing(monkeypatch):
	requested_fields = []

	class _CustomerMeta:
		def has_field(self, fieldname):
			return False

	def _fake_get_all(doctype, *args, **kwargs):
		requested_fields.extend(kwargs.get("fields") or [])
		return [{"name": "CUST-0001", "customer_name": "Cliente Prueba"}]

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe, "get_meta", lambda doctype: _CustomerMeta() if doctype == "Customer" else None
	)
	monkeypatch.setattr(frappe, "get_all", _fake_get_all)

	customers = saas_api.get_all_customers()

	assert "custom_wholesale_access_pin" not in requested_fields
	assert customers == [
		{"name": "CUST-0001", "customer_name": "Cliente Prueba", "custom_wholesale_access_pin": None}
	]


def test_get_customer_orders_history_blocks_cajero_users_with_mismatched_profiles(monkeypatch):
	fetch_calls = []

	monkeypatch.setattr(frappe.session, "user", "cajero.s1.t1@lapaletixa.com", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: [])
	monkeypatch.setattr(
		saas_api,
		"get_customer_wholesale_profile",
		lambda: {"success": True, "customer": "CUST-0001"},
	)
	monkeypatch.setattr(frappe, "get_all", lambda *args, **kwargs: fetch_calls.append((args, kwargs)) or [])

	with pytest.raises(frappe.PermissionError, match="No tenés permisos para ver este historial\\."):
		saas_api.get_customer_orders_history("CUST-9999")

	assert fetch_calls == []


def test_get_customer_orders_history_allows_matching_customer_profile(monkeypatch):
	fetch_calls = []

	monkeypatch.setattr(frappe.session, "user", "cliente@example.com", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: [])
	monkeypatch.setattr(
		saas_api,
		"get_customer_wholesale_profile",
		lambda: {"success": True, "customer": "CUST-0001"},
	)

	def _fake_get_all(doctype, *args, **kwargs):
		fetch_calls.append((doctype, kwargs))
		if doctype == "Sales Order":
			return [{"name": "SO-0001"}]
		if doctype == "Sales Invoice":
			return [{"name": "SI-0001"}]
		return []

	monkeypatch.setattr(frappe, "get_all", _fake_get_all)

	result = saas_api.get_customer_orders_history("CUST-0001")

	assert result == {"orders": [{"name": "SO-0001"}], "invoices": [{"name": "SI-0001"}]}
	assert [doctype for doctype, _ in fetch_calls] == ["Sales Order", "Sales Invoice"]


def test_get_all_customers_rejects_non_tenant_admin_users(monkeypatch):
	monkeypatch.setattr(frappe.session, "user", "cliente@example.com", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: [])

	with pytest.raises(frappe.PermissionError, match="No tenés permisos para acceder a esta información\\."):
		saas_api.get_all_customers()


def test_create_service_invoice_rejects_non_service_operators_before_invoice_creation(monkeypatch):
	begin_calls = []
	exists_calls = []
	new_doc_calls = []
	get_roles_calls = []

	monkeypatch.setattr(frappe.session, "user", "support.admin@example.com", raising=False)
	monkeypatch.setattr(
		frappe, "get_roles", lambda user=None: get_roles_calls.append(user or frappe.session.user) or []
	)
	monkeypatch.setattr(frappe.db, "begin", lambda *args, **kwargs: begin_calls.append((args, kwargs)))
	monkeypatch.setattr(
		frappe.db, "exists", lambda *args, **kwargs: exists_calls.append((args, kwargs)) or True
	)
	monkeypatch.setattr(
		frappe, "new_doc", lambda *args, **kwargs: new_doc_calls.append((args, kwargs)) or SimpleNamespace()
	)

	with pytest.raises(frappe.PermissionError, match="No tenés permisos para registrar servicios"):
		saas_api.create_service_invoice(
			customer="CUST-0001",
			items=[{"item_code": "ITEM-0001", "qty": 1, "rate": 10}],
			payment_amount=10,
			payment_mode="Cash",
		)

	assert get_roles_calls == []
	assert begin_calls == []
	assert exists_calls == []
	assert new_doc_calls == []


def test_create_notification_on_order_does_not_bootstrap_wholesale_setup_for_core_flow(monkeypatch):
	fake_config = _FakeSaaSConfig()
	fake_config.has_wholesale = 0
	fake_config.reservation_item_code = "Carrito Paletero"
	wholesale_setup_calls = []
	notification_doctype_calls = []
	new_doc_calls = []

	def _fake_new_doc(doctype, *args, **kwargs):
		new_doc_calls.append(doctype)
		return SimpleNamespace(insert=lambda *a, **k: None)

	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: fake_config if doctype == "SaaS Feature Config" else None,
	)
	monkeypatch.setattr(frappe, "new_doc", _fake_new_doc)
	monkeypatch.setattr(frappe.db, "commit", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe, "log_error", lambda *args, **kwargs: None)
	monkeypatch.setattr(
		saas_api, "setup_wholesale_custom_fields", lambda: wholesale_setup_calls.append("called")
	)
	monkeypatch.setattr(
		saas_api, "ensure_saas_notification_doctype", lambda: notification_doctype_calls.append("called")
	)

	doc = SimpleNamespace(
		get=lambda key, default=None: None,
		items=[SimpleNamespace(item_code="ITEM-0001")],
		customer_name="Cliente Prueba",
		name="SO-0001",
	)

	saas_api.create_notification_on_order(doc)

	assert wholesale_setup_calls == []
	assert notification_doctype_calls == []
	assert new_doc_calls == []


def test_wholesale_endpoints_short_circuit_when_disabled(monkeypatch):
	fake_config = _FakeSaaSConfig()
	fake_config.has_wholesale = 0
	wholesale_setup_calls = []
	item_get_all_calls = []
	warehouse_sql_calls = []

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: fake_config if doctype == "SaaS Feature Config" else None,
	)
	monkeypatch.setattr(frappe.db, "commit", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe.db, "begin", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe.db, "rollback", lambda *args, **kwargs: None)
	monkeypatch.setattr(
		frappe,
		"get_all",
		lambda doctype, *args, **kwargs: item_get_all_calls.append((doctype, args, kwargs)) or [],
	)
	monkeypatch.setattr(
		frappe.db,
		"sql",
		lambda query, *args, **kwargs: warehouse_sql_calls.append((query, args, kwargs)) or [],
	)
	monkeypatch.setattr(
		frappe, "throw", lambda *args, **kwargs: (_ for _ in ()).throw(frappe.PermissionError(*args))
	)
	monkeypatch.setattr(
		saas_api, "setup_wholesale_custom_fields", lambda: wholesale_setup_calls.append("called")
	)

	profile = saas_api.get_customer_wholesale_profile()
	assert profile == {"success": False, "error": "El módulo de mayoristas está deshabilitado."}

	access = saas_api.validate_wholesale_access(phone="55 4433 2211", pin="123456")
	assert access == {"success": False, "error": "El módulo de mayoristas está deshabilitado."}

	orders = saas_api.get_pending_wholesale_orders()
	assert orders == []

	completed_orders = saas_api.get_completed_wholesale_orders()
	assert completed_orders == []

	create_res = saas_api.create_wholesale_order(items=[{"item_code": "ITEM-0001", "qty": 1}])
	assert create_res == {"success": False, "error": "El módulo de mayoristas está deshabilitado."}

	complete_res = saas_api.complete_wholesale_order("SO-0001")
	assert complete_res == {"success": False, "error": "El módulo de mayoristas está deshabilitado."}

	cancel_res = saas_api.cancel_wholesale_order("SO-0001")
	assert cancel_res == {"success": False, "error": "El módulo de mayoristas está deshabilitado."}

	items_res = saas_api.get_active_items_with_prices(warehouse="WH-0001")
	assert items_res == {"success": False, "error": "El módulo de mayoristas está deshabilitado."}

	warehouses_res = saas_api.get_active_warehouses_with_stock()
	assert warehouses_res == {"success": False, "error": "El módulo de mayoristas está deshabilitado."}

	assert wholesale_setup_calls == []
	assert item_get_all_calls == []
	assert warehouse_sql_calls == []


def test_get_completed_wholesale_orders_includes_tracking_data(monkeypatch):
	fake_config = _FakeSaaSConfig()
	fake_config.has_wholesale = 1
	get_all_calls = []

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: fake_config if doctype == "SaaS Feature Config" else None,
	)
	monkeypatch.setattr(saas_api, "setup_wholesale_custom_fields", lambda: None)

	def _fake_get_all(
		doctype,
		filters=None,
		fields=None,
		order_by=None,
		pluck=None,
		limit=None,
		or_filters=None,
		*args,
		**kwargs,
	):
		get_all_calls.append(
			{
				"doctype": doctype,
				"filters": filters,
				"fields": fields,
				"order_by": order_by,
				"pluck": pluck,
				"limit": limit,
				"or_filters": or_filters,
			}
		)
		if doctype == "Sales Order":
			assert limit == 50
			assert order_by == "modified desc"
			assert filters["docstatus"] == 1
			return [
				{
					"name": "SO-COMP-0001",
					"customer": "CUST-0001",
					"customer_name": "Cliente Completado",
					"transaction_date": "2026-07-07",
					"delivery_date": "2026-07-08",
					"grand_total": 250.0,
					"custom_metodo_pago": "Transferencia",
					"custom_metodo_entrega": "Domicilio",
					"status": "Completed",
					"modified": "2026-07-08 14:30:00",
					"per_billed": 100,
					"per_delivered": 100,
				},
			]
		if doctype == "Sales Order Item":
			assert filters == {"parent": "SO-COMP-0001"}
			return [
				{
					"item_code": "ITEM-0001",
					"item_name": "Paleta Mango",
					"qty": 10,
					"rate": 25.0,
					"amount": 250.0,
				},
			]
		if doctype == "Sales Invoice Item":
			assert filters == {"sales_order": "SO-COMP-0001"}
			assert pluck == "parent"
			return ["SI-COMP-0001"]
		if doctype == "Payment Entry Reference":
			assert filters == {
				"reference_doctype": "Sales Invoice",
				"reference_name": "SI-COMP-0001",
				"docstatus": ["!=", 2],
			}
			assert pluck == "parent"
			return ["PE-COMP-0001"]
		return []

	def _fake_get_value(doctype, name, fieldname, *args, **kwargs):
		if doctype == "Customer" and name == "CUST-0001" and fieldname == "mobile_no":
			return "+525500000001"
		if doctype == "Sales Invoice" and name == "SI-COMP-0001" and fieldname == "status":
			return "Paid"
		if doctype == "Sales Invoice" and name == "SI-COMP-0001" and fieldname == "modified":
			return "2026-07-08 14:31:00"
		if doctype == "Sales Invoice" and name == "SI-COMP-0001" and fieldname == "outstanding_amount":
			return 0.0
		return None

	monkeypatch.setattr(frappe, "get_all", _fake_get_all)
	monkeypatch.setattr(frappe.db, "get_value", _fake_get_value)

	orders = saas_api.get_completed_wholesale_orders()

	assert len(orders) == 1
	order = orders[0]
	assert order["name"] == "SO-COMP-0001"
	assert order["customer_name"] == "Cliente Completado"
	assert order["contact_phone"] == "+525500000001"
	assert order["sales_invoice"] == "SI-COMP-0001"
	assert order["payment_entry"] == "PE-COMP-0001"
	assert order["invoice_status"] == "Paid"
	assert order["completed_on"] == "2026-07-08 14:31:00"
	assert order["outstanding_amount"] == 0.0
	assert order["items"][0]["item_code"] == "ITEM-0001"
	assert any(call["doctype"] == "Sales Order" for call in get_all_calls)


@pytest.mark.parametrize(
	"requested_limit, expected_limit",
	[
		(0, 50),
		(-1, 1),
		(1, 1),
		(100, 100),
		(101, 100),
		("abc", 50),
	],
)
def test_get_completed_wholesale_orders_clamps_limit(monkeypatch, requested_limit, expected_limit):
	fake_config = _FakeSaaSConfig()
	fake_config.has_wholesale = 1
	seen_limits = []

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: fake_config if doctype == "SaaS Feature Config" else None,
	)
	monkeypatch.setattr(saas_api, "setup_wholesale_custom_fields", lambda: None)

	def _fake_get_all(doctype, *args, **kwargs):
		if doctype == "Sales Order":
			seen_limits.append(kwargs.get("limit"))
			return []
		return []

	monkeypatch.setattr(frappe, "get_all", _fake_get_all)

	orders = saas_api.get_completed_wholesale_orders(limit=requested_limit)

	assert orders == []
	assert seen_limits == [expected_limit]


def test_get_active_items_with_prices_hides_stock_for_non_admins(monkeypatch):
	fake_config = _FakeSaaSConfig()
	fake_config.has_wholesale = 1
	item_group_calls = []
	item_price_calls = []
	bin_calls = []

	monkeypatch.setattr(frappe.session, "user", "employee@tenant.test", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["Employee"])
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: fake_config if doctype == "SaaS Feature Config" else None,
	)

	def _fake_get_all(doctype, *args, **kwargs):
		if doctype == "Item Group":
			item_group_calls.append((args, kwargs))
			return [{"name": "Products"}]
		if doctype == "Item":
			return [
				{
					"name": "ITEM-0001",
					"item_name": "Item 1",
					"item_group": "Products",
					"standard_rate": 10,
					"image": None,
				}
			]
		if doctype == "Item Price":
			item_price_calls.append((args, kwargs))
			return [{"item_code": "ITEM-0001", "price_list": "Standard Selling", "price_list_rate": 12}]
		if doctype == "Bin":
			bin_calls.append((args, kwargs))
			return []
		return []

	monkeypatch.setattr(frappe, "get_all", _fake_get_all)

	items = saas_api.get_active_items_with_prices(warehouse="WH-0001")

	assert bin_calls == []
	assert items[0]["actual_qty"] is None
	assert items[0]["retail_price"] == 12
	assert item_group_calls
	assert item_price_calls


def test_get_active_warehouses_with_stock_requires_admin_access(monkeypatch):
	fake_config = _FakeSaaSConfig()
	fake_config.has_wholesale = 1

	monkeypatch.setattr(frappe.session, "user", "employee@tenant.test", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["Employee"])
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: fake_config if doctype == "SaaS Feature Config" else None,
	)
	monkeypatch.setattr(
		frappe, "throw", lambda *args, **kwargs: (_ for _ in ()).throw(frappe.PermissionError(*args))
	)

	with pytest.raises(frappe.PermissionError, match="No tenés permisos para acceder a este recurso"):
		saas_api.get_active_warehouses_with_stock()


def test_get_item_barcodes_requires_auth_and_operator_access(monkeypatch):
	monkeypatch.setattr(frappe.session, "user", "employee@tenant.test", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["Employee"])
	monkeypatch.setattr(
		frappe,
		"get_all",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected barcode read")),
	)

	with pytest.raises(frappe.PermissionError, match="No tenés permisos para acceder a este recurso"):
		saas_api.get_item_barcodes()


def test_create_wholesale_order_rejects_spoofed_guest_customer_without_token_before_document_creation(
	monkeypatch,
):
	fake_config = _FakeSaaSConfig()
	fake_config.has_wholesale = 1
	new_doc_calls = []

	monkeypatch.setattr(frappe.session, "user", "Guest", raising=False)
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: fake_config if doctype == "SaaS Feature Config" else None,
	)
	monkeypatch.setattr(saas_api, "setup_wholesale_custom_fields", lambda: None)
	monkeypatch.setattr(frappe.db, "exists", lambda *args, **kwargs: True)
	monkeypatch.setattr(
		frappe,
		"new_doc",
		lambda *args, **kwargs: new_doc_calls.append((args, kwargs))
		or (_ for _ in ()).throw(AssertionError("Sales Order should not be created")),
	)

	with pytest.raises(frappe.PermissionError, match="Sesión mayorista inválida"):
		saas_api.create_wholesale_order(
			items=[{"item_code": "ITEM-0001", "qty": 1}],
			metodo_pago="Transferencia",
			metodo_entrega="Domicilio",
			customer="CUST-SPOOFED",
		)

	assert new_doc_calls == []


def test_create_wholesale_sale_blocks_non_privileged_authenticated_users_before_document_creation(
	monkeypatch,
):
	fake_config = _FakeSaaSConfig()
	fake_config.has_wholesale = 1
	create_calls = []

	monkeypatch.setattr(frappe.session, "user", "employee@tenant.test", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["Employee"])
	monkeypatch.setattr(saas_api, "_is_platform_master_site", lambda: False)
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: fake_config if doctype == "SaaS Feature Config" else None,
	)
	monkeypatch.setattr(
		saas_api,
		"get_platform_company_name",
		lambda: (_ for _ in ()).throw(AssertionError("Company lookup should not happen")),
	)
	monkeypatch.setattr(
		saas_api,
		"get_platform_distribution_warehouse",
		lambda: (_ for _ in ()).throw(AssertionError("Warehouse lookup should not happen")),
	)
	monkeypatch.setattr(
		frappe,
		"new_doc",
		lambda *args, **kwargs: create_calls.append((args, kwargs))
		or (_ for _ in ()).throw(AssertionError("Sales Invoice should not be created")),
	)
	monkeypatch.setattr(
		frappe.db,
		"commit",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("No writes should happen")),
	)

	with pytest.raises(frappe.PermissionError, match="No tenés permisos para acceder a este recurso\\."):
		saas_api.create_wholesale_sale(
			customer="CUST-0001",
			items=[{"item_code": "ITEM-0001", "qty": 1, "rate": 10}],
			payment_amount=10,
			payment_mode="Cash",
			warehouse="WH-0001",
		)

	assert create_calls == []


def _setup_wholesale_completion(
	monkeypatch,
	warehouse_doc,
	payment_mode_exists=None,
	sales_order_status="To Deliver and Bill",
	custom_metodo_pago="Transferencia",
	per_billed=0,
	per_delivered=0,
):
	fake_config = _FakeSaaSConfig()
	fake_config.company_name = "Tenant Co"
	fake_config.company_abbr = "TC"
	fake_config.default_distribution_warehouse = "Fabrica - TC"

	so = _FakeDoc("Sales Order", "SO-0001", docstatus=1)
	so.company = "Tenant Co"
	so.currency = "MXN"
	so.status = sales_order_status
	so.custom_metodo_pago = custom_metodo_pago
	so.per_billed = per_billed
	so.per_delivered = per_delivered
	so.items = [
		SimpleNamespace(
			name="SO-ITEM-1", item_code="ITEM-A", item_name="Item A", qty=2, rate=50.0, amount=100.0
		),
	]
	so.grand_total = 100.0

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return fake_config
		if doctype == "Warehouse" and name == "Fabrica - TC":
			return warehouse_doc
		if doctype == "Company" and name == "Tenant Co":
			return SimpleNamespace(abbr="TC")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, name=None, *args, **kwargs: so
		if doctype == "Sales Order"
		else (_ for _ in ()).throw(AssertionError(f"Unexpected get_doc lookup: {doctype} / {name}")),
	)
	monkeypatch.setattr(
		frappe,
		"db",
		SimpleNamespace(
			exists=lambda doctype, name=None, *args, **kwargs: doctype == "Sales Order"
			or (doctype == "Mode of Payment" and payment_mode_exists and payment_mode_exists(name)),
			commit=lambda *args, **kwargs: None,
			begin=lambda *args, **kwargs: None,
			rollback=lambda *args, **kwargs: None,
			get_value=lambda doctype, name, fieldname, *args, **kwargs: 40.0
			if doctype == "Sales Invoice"
			else None,
			sql=lambda query, params=None, *args, **kwargs: [(1,)]
			if "GET_LOCK" in query or "RELEASE_LOCK" in query
			else [],
		),
	)

	return so


def test_complete_wholesale_order_rejects_unknown_payment_mode_before_invoice_creation(monkeypatch):
	warehouse_doc = SimpleNamespace(company="Tenant Co", is_group=0, disabled=0)
	_setup_wholesale_completion(monkeypatch, warehouse_doc)
	make_sales_invoice_calls = []

	monkeypatch.setattr(
		"erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
		lambda sales_order_name: make_sales_invoice_calls.append(sales_order_name)
		or (_ for _ in ()).throw(AssertionError("Sales Invoice should not be created")),
	)
	monkeypatch.setattr(
		saas_api,
		"get_platform_payment_account",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("payment account lookup should not run for invalid payment modes")
		),
	)
	monkeypatch.setattr(
		frappe,
		"new_doc",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("Mode of Payment should not be created for invalid payment modes")
		),
	)

	with pytest.raises(frappe.ValidationError, match="Método de pago inválido"):
		saas_api.complete_wholesale_order(
			"SO-0001",
			register_payment=True,
			payment_mode="Foo",
			warehouse="Fabrica - TC",
		)

	assert make_sales_invoice_calls == []


def test_complete_wholesale_order_rejects_missing_payment_account_before_invoice_creation(monkeypatch):
	warehouse_doc = SimpleNamespace(company="Tenant Co", is_group=0, disabled=0)
	_setup_wholesale_completion(monkeypatch, warehouse_doc, payment_mode_exists=lambda name: False)
	make_sales_invoice_calls = []

	monkeypatch.setattr(
		"erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
		lambda sales_order_name: make_sales_invoice_calls.append(sales_order_name)
		or (_ for _ in ()).throw(AssertionError("Sales Invoice should not be created")),
	)
	monkeypatch.setattr(
		platform_defaults,
		"get_platform_payment_account",
		lambda payment_mode: (_ for _ in ()).throw(
			frappe.ValidationError(
				"Configurá la cuenta contable por defecto para transferencias en SaaS Feature Config."
			)
		),
	)
	monkeypatch.setattr(
		frappe,
		"new_doc",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("Mode of Payment should not be created when the account is missing")
		),
	)

	with pytest.raises(frappe.ValidationError, match="cuenta contable por defecto para transferencias"):
		saas_api.complete_wholesale_order(
			"SO-0001",
			register_payment=True,
			payment_mode="Transferencia",
			warehouse="Fabrica - TC",
		)

	assert make_sales_invoice_calls == []


@pytest.mark.parametrize(
	"sales_order_status,custom_metodo_pago,per_billed,per_delivered,match",
	[
		("To Deliver and Bill", "", 0, 0, "método de pago mayorista válido"),
		("Completed", "Transferencia", 0, 0, "ya fue procesado"),
		("To Deliver and Bill", "Transferencia", 100, 100, "ya fue procesado"),
	],
)
def test_complete_wholesale_order_rejects_ineligible_sales_order_before_invoice_creation(
	monkeypatch,
	sales_order_status,
	custom_metodo_pago,
	per_billed,
	per_delivered,
	match,
):
	warehouse_doc = SimpleNamespace(company="Tenant Co", is_group=0, disabled=0)
	_setup_wholesale_completion(
		monkeypatch,
		warehouse_doc,
		sales_order_status=sales_order_status,
		custom_metodo_pago=custom_metodo_pago,
		per_billed=per_billed,
		per_delivered=per_delivered,
	)
	make_sales_invoice_calls = []

	monkeypatch.setattr(
		"erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
		lambda sales_order_name: make_sales_invoice_calls.append(sales_order_name)
		or (_ for _ in ()).throw(AssertionError("Sales Invoice should not be created")),
	)

	with pytest.raises(frappe.ValidationError, match=match):
		saas_api.complete_wholesale_order(
			"SO-0001",
			register_payment=True,
			payment_mode="Transferencia",
			warehouse="Fabrica - TC",
		)

	assert make_sales_invoice_calls == []


def test_complete_wholesale_order_rejects_invalid_warehouse_before_invoice_creation(monkeypatch):
	warehouse_doc = SimpleNamespace(company="Other Co", is_group=0, disabled=0)
	_setup_wholesale_completion(monkeypatch, warehouse_doc)
	make_sales_invoice_calls = []

	monkeypatch.setattr(
		"erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
		lambda sales_order_name: make_sales_invoice_calls.append(sales_order_name)
		or (_ for _ in ()).throw(AssertionError("Sales Invoice should not be created")),
	)
	monkeypatch.setattr(
		saas_api,
		"get_platform_payment_account",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("payment account lookup should not run when warehouse validation fails")
		),
	)
	monkeypatch.setattr(
		frappe,
		"new_doc",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("Mode of Payment should not be created when warehouse validation fails")
		),
	)

	with pytest.raises(frappe.ValidationError, match="debe pertenecer a la compañía Tenant Co"):
		saas_api.complete_wholesale_order(
			"SO-0001",
			register_payment=True,
			payment_mode="Transferencia",
			warehouse="Fabrica - TC",
		)

	assert make_sales_invoice_calls == []


@pytest.mark.parametrize("register_payment", [0, "0"])
def test_complete_wholesale_order_supports_register_payment_zero_without_payment_validation(
	monkeypatch, register_payment
):
	warehouse_doc = SimpleNamespace(company="Tenant Co", is_group=0, disabled=0)
	_setup_wholesale_completion(monkeypatch, warehouse_doc)
	make_sales_invoice_calls = []

	class _FakeInvoice:
		def __init__(self):
			self.name = "SI-1"
			self.grand_total = 100.0
			self.outstanding_amount = 40.0
			self.items = [SimpleNamespace(warehouse="", item_code="ITEM-A")]
			self.update_stock = 1
			self.posting_date = None
			self.set_posting_time = 0
			self.currency = "MXN"

		def insert(self, ignore_permissions=False):
			return self

		def submit(self):
			self.docstatus = 1
			return self

	monkeypatch.setattr(
		"erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
		lambda sales_order_name: make_sales_invoice_calls.append(sales_order_name) or _FakeInvoice(),
	)
	monkeypatch.setattr(
		saas_api,
		"get_platform_payment_account",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("payment account lookup should not run when register_payment=0")
		),
	)
	monkeypatch.setattr(
		saas_api,
		"ensure_platform_payment_mode",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("payment mode resolution should not run when register_payment=0")
		),
	)

	result = saas_api.complete_wholesale_order(
		"SO-0001",
		register_payment=register_payment,
		payment_mode="Transferencia",
		warehouse="Fabrica - TC",
	)

	assert result["success"] is True
	assert result["advance_paid"] == 0.0
	assert result["outstanding_amount"] == 40.0
	assert make_sales_invoice_calls == ["SO-0001"]


def test_complete_wholesale_order_reloads_sales_order_under_named_lock_before_mutation(monkeypatch):
	warehouse_doc = SimpleNamespace(company="Tenant Co", is_group=0, disabled=0)
	so = _setup_wholesale_completion(
		monkeypatch,
		warehouse_doc,
		sales_order_status="To Deliver and Bill",
		custom_metodo_pago="Transferencia",
		per_billed=0,
		per_delivered=0,
	)
	locked_so = _FakeDoc("Sales Order", "SO-0001", docstatus=1)
	locked_so.company = "Tenant Co"
	locked_so.currency = "MXN"
	locked_so.status = "Completed"
	locked_so.custom_metodo_pago = "Transferencia"
	locked_so.per_billed = 100
	locked_so.per_delivered = 100
	locked_so.items = list(so.items)
	sales_order_reads = 0
	lock_sql_calls = []

	def _sql(query, params=None, *args, **kwargs):
		lock_sql_calls.append((query, params))
		if "GET_LOCK" in query or "RELEASE_LOCK" in query:
			return [(1,)]
		raise AssertionError(f"Unexpected SQL: {query}")

	def _get_doc(doctype, name=None, *args, **kwargs):
		nonlocal sales_order_reads
		if doctype == "Sales Order" and name == "SO-0001":
			sales_order_reads += 1
			return so if sales_order_reads == 1 else locked_so
		raise AssertionError(f"Unexpected get_doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe.db, "sql", _sql)
	monkeypatch.setattr(
		"erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
		lambda sales_order_name: (_ for _ in ()).throw(AssertionError("Sales Invoice should not be created")),
	)
	monkeypatch.setattr(frappe, "get_doc", _get_doc)

	with pytest.raises(frappe.ValidationError, match="ya fue procesado"):
		saas_api.complete_wholesale_order("SO-0001", register_payment=False, warehouse="Fabrica - TC")

	assert lock_sql_calls == [
		("SELECT GET_LOCK(%s, %s)", ("wholesale_order_completion:SO-0001", 10)),
		("SELECT RELEASE_LOCK(%s)", ("wholesale_order_completion:SO-0001",)),
	]
	assert sales_order_reads == 2


def test_complete_wholesale_order_uses_resolved_transferencia_payment_mode_when_bank_draft_is_absent(
	monkeypatch,
):
	warehouse_doc = SimpleNamespace(company="Tenant Co", is_group=0, disabled=0)
	so = _setup_wholesale_completion(
		monkeypatch,
		warehouse_doc,
		payment_mode_exists=lambda name: name == "Transferencia",
		sales_order_status="To Deliver and Bill",
		custom_metodo_pago="Transferencia",
		per_billed=0,
		per_delivered=0,
	)

	class _FakeModeOfPayment:
		def __init__(self):
			self.mode_of_payment = "Transferencia"
			self.type = "Bank"
			self.enabled = 1
			self.accounts = [SimpleNamespace(company="Tenant Co", default_account="Old Bank - TC")]
			self.save_calls = 0

		def append(self, table, row):
			assert table == "accounts"
			child = SimpleNamespace(**row)
			self.accounts.append(child)
			return child

		def get(self, key, default=None):
			return getattr(self, key, default)

		def save(self, ignore_permissions=False):
			self.save_calls += 1
			return self

	class _FakeInvoice:
		def __init__(self):
			self.name = "SI-1"
			self.grand_total = 100.0
			self.outstanding_amount = 0.0
			self.items = [SimpleNamespace(warehouse="", item_code="ITEM-A")]
			self.update_stock = 1
			self.posting_date = None
			self.set_posting_time = 0
			self.currency = "MXN"

		def insert(self, ignore_permissions=False):
			return self

		def submit(self):
			self.docstatus = 1
			return self

	class _FakePaymentEntry:
		def __init__(self):
			self.references = [SimpleNamespace(allocated_amount=0)]
			self.mode_of_payment = ""
			self.reference_no = ""
			self.reference_date = None
			self.paid_to = ""
			self.paid_amount = 0.0
			self.received_amount = 0.0

		def insert(self, ignore_permissions=False):
			return self

		def submit(self):
			self.docstatus = 1
			return self

	mode_doc = _FakeModeOfPayment()
	payment_entry = _FakePaymentEntry()
	payment_account_calls = []

	def _get_doc(doctype, name=None, *args, **kwargs):
		if doctype == "Sales Order" and name == "SO-0001":
			return so
		if doctype == "Mode of Payment" and name == "Transferencia":
			return mode_doc
		raise AssertionError(f"Unexpected get_doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "get_doc", _get_doc)
	monkeypatch.setattr(
		platform_defaults,
		"get_platform_payment_account",
		lambda payment_mode: payment_account_calls.append(payment_mode) or "Bank - TC",
	)
	monkeypatch.setattr(
		"erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
		lambda sales_order_name: _FakeInvoice(),
	)
	monkeypatch.setattr(
		"erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
		lambda doctype, reference_name, bank_amount=None: payment_entry,
	)
	monkeypatch.setattr(
		frappe.db,
		"get_value",
		lambda doctype, name, fieldname, *args, **kwargs: 0.0 if doctype == "Sales Invoice" else None,
	)

	result = saas_api.complete_wholesale_order(
		"SO-0001",
		register_payment=True,
		payment_mode="Transferencia",
		warehouse="Fabrica - TC",
	)

	assert result["success"] is True
	assert result["advance_paid"] == 100.0
	assert result["outstanding_amount"] == 0.0
	assert payment_account_calls == ["Transferencia"]
	assert mode_doc.save_calls == 1
	assert mode_doc.accounts[0].default_account == "Bank - TC"
	assert payment_entry.mode_of_payment == "Transferencia"
	assert payment_entry.paid_to == "Bank - TC"
	assert payment_entry.paid_amount == 100.0
	assert payment_entry.received_amount == 100.0
	assert payment_entry.references[0].allocated_amount == 100.0


def test_get_features_omits_reservation_defaults_when_disabled(monkeypatch):
	class _DisabledFeatureConfig:
		primary_color = "#1abc9c"
		has_pos = 0
		has_production = 0
		has_logistics = 0
		has_reservations = 0
		has_wholesale = 0
		has_mexico_taxes = 0
		has_services = 0
		has_products = 0
		has_purchasing = 0
		reservation_item_code = "Carrito Paletero"
		max_reservation_assets = 12
		default_event_items = '[{"item_code": "Carrito Paletero"}]'
		custom_country = "Mexico"
		custom_currency = "MXN"
		company_name = ""
		company_logo = ""
		company_tax_id = ""
		company_address = ""
		company_phone = ""
		company_email = ""
		ticket_header = ""
		ticket_footer = ""
		print_logo = 0
		print_tax_id = 0
		print_address = 0
		print_contact = 0
		is_active = 0
		max_branches = 0

		def get(self, key, default=None):
			return getattr(self, key, default)

	monkeypatch.setattr(saas_api, "setup_company_identity_fields", lambda: None)
	monkeypatch.setattr(frappe, "get_cached_doc", lambda doctype, name=None: _DisabledFeatureConfig())

	response = saas_api.get_features()

	assert response["features"]["reservations"] is False
	assert response["reservation_item_code"] == ""
	assert response["max_reservation_assets"] == 0
	assert response["default_event_items"] == "[]"


def test_get_features_does_not_bootstrap_inventory_permissions_when_products_disabled(monkeypatch):
	created_docs = []

	class _CoreFeatureMeta:
		def has_field(self, fieldname):
			return fieldname in {
				"company_name",
				"company_tax_id",
				"company_address",
				"company_phone",
				"company_email",
				"company_abbr",
				"default_distribution_warehouse",
				"default_cash_account",
				"default_bank_account",
				"ticket_header",
				"ticket_footer",
				"print_logo",
				"print_tax_id",
				"print_address",
				"print_contact",
			}

	class _FakeCache:
		def get_value(self, key):
			return None

		def set_value(self, key, value):
			return None

	fake_config = _FakeSaaSConfig()
	fake_config.has_products = 0
	fake_config.company_name = "Nueva Plataforma"
	fake_config.company_abbr = "NP"

	monkeypatch.setattr(saas_api, "setup_service_role_permissions", lambda: None)
	monkeypatch.setattr(
		frappe, "get_meta", lambda doctype: _CoreFeatureMeta() if doctype == "SaaS Feature Config" else None
	)
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: fake_config if doctype == "SaaS Feature Config" else None,
	)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda payload: created_docs.append(payload)
		or SimpleNamespace(insert=lambda ignore_permissions=False: None),
	)
	monkeypatch.setattr(frappe, "cache", lambda: _FakeCache())

	response = saas_api.get_features()

	assert response["features"]["products"] is False
	assert created_docs == []


def test_get_features_does_not_bootstrap_service_permissions_when_services_disabled(monkeypatch):
	class _CoreFeatureMeta:
		def has_field(self, fieldname):
			return fieldname in {
				"company_name",
				"company_tax_id",
				"company_address",
				"company_phone",
				"company_email",
				"company_abbr",
				"default_distribution_warehouse",
				"default_cash_account",
				"default_bank_account",
				"ticket_header",
				"ticket_footer",
				"print_logo",
				"print_tax_id",
				"print_address",
				"print_contact",
			}

	class _FakeCache:
		def get_value(self, key):
			return None

		def set_value(self, key, value):
			return None

	fake_config = _FakeSaaSConfig()
	fake_config.has_services = 0
	fake_config.company_name = "Nueva Plataforma"
	fake_config.company_abbr = "NP"
	service_setup_calls = []

	monkeypatch.setattr(frappe.local, "site", "test.localhost", raising=False)
	monkeypatch.setattr(
		saas_api, "setup_service_role_permissions", lambda: service_setup_calls.append("called")
	)
	monkeypatch.setattr(
		frappe, "get_meta", lambda doctype: _CoreFeatureMeta() if doctype == "SaaS Feature Config" else None
	)
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: fake_config if doctype == "SaaS Feature Config" else None,
	)
	monkeypatch.setattr(frappe, "cache", lambda: _FakeCache())

	response = saas_api.get_features()

	assert response["features"]["services"] is False
	assert service_setup_calls == []


def test_update_saas_config_saves_without_inventory_bootstrap_when_products_disabled(monkeypatch):
	created_docs = []

	class _CoreFeatureMeta:
		def has_field(self, fieldname):
			return fieldname in {
				"company_name",
				"company_tax_id",
				"company_address",
				"company_phone",
				"company_email",
				"company_abbr",
				"default_distribution_warehouse",
				"default_cash_account",
				"default_bank_account",
				"ticket_header",
				"ticket_footer",
				"print_logo",
				"print_tax_id",
				"print_address",
				"print_contact",
			}

	class _FakeCache:
		def get_value(self, key):
			return None

		def set_value(self, key, value):
			return None

	class _ProductsDisabledConfig(_FakeSaaSConfig):
		def __init__(self):
			super().__init__()
			self.has_products = 0
			self.saved = False

		def save(self, ignore_permissions=False):
			self.saved = True
			return self

	fake_config = _ProductsDisabledConfig()

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe, "get_meta", lambda doctype: _CoreFeatureMeta() if doctype == "SaaS Feature Config" else None
	)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, *args, **kwargs: fake_config
		if doctype == "SaaS Feature Config"
		else created_docs.append((doctype, args, kwargs))
		or SimpleNamespace(insert=lambda ignore_permissions=False: None),
	)
	monkeypatch.setattr(frappe.db, "exists", lambda *args, **kwargs: False)
	monkeypatch.setattr(frappe.db, "commit", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe, "clear_cache", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe, "cache", lambda: _FakeCache())
	monkeypatch.setattr(saas_api, "setup_service_role_permissions", lambda: None)
	monkeypatch.setattr(saas_api, "_is_platform_master_site", lambda: False)

	result = saas_api.update_saas_config(company_name="Nueva Plataforma", has_products=0)

	assert result["success"] is True
	assert fake_config.saved is True
	assert fake_config.has_products == 0
	assert created_docs == []


def test_update_saas_config_does_not_bootstrap_service_permissions_when_services_disabled(monkeypatch):
	created_docs = []

	class _CoreFeatureMeta:
		def has_field(self, fieldname):
			return fieldname in {
				"company_name",
				"company_tax_id",
				"company_address",
				"company_phone",
				"company_email",
				"company_abbr",
				"default_distribution_warehouse",
				"default_cash_account",
				"default_bank_account",
				"ticket_header",
				"ticket_footer",
				"print_logo",
				"print_tax_id",
				"print_address",
				"print_contact",
			}

	class _FakeCache:
		def get_value(self, key):
			return None

		def set_value(self, key, value):
			return None

	class _ServicesDisabledConfig(_FakeSaaSConfig):
		def __init__(self):
			super().__init__()
			self.has_services = 0
			self.saved = False

		def save(self, ignore_permissions=False):
			self.saved = True
			return self

	fake_config = _ServicesDisabledConfig()
	service_setup_calls = []

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe.local, "site", "test.localhost", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(
		frappe, "get_meta", lambda doctype: _CoreFeatureMeta() if doctype == "SaaS Feature Config" else None
	)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, *args, **kwargs: fake_config
		if doctype == "SaaS Feature Config"
		else created_docs.append((doctype, args, kwargs))
		or SimpleNamespace(insert=lambda ignore_permissions=False: None),
	)
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: fake_config if doctype == "SaaS Feature Config" else None,
	)
	monkeypatch.setattr(
		frappe.db, "exists", lambda doctype, name=None, *args, **kwargs: doctype == "Custom Field"
	)
	monkeypatch.setattr(frappe.db, "commit", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe, "clear_cache", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe, "cache", lambda: _FakeCache())
	monkeypatch.setattr(
		saas_api, "setup_service_role_permissions", lambda: service_setup_calls.append("called")
	)
	monkeypatch.setattr(saas_api, "_is_platform_master_site", lambda: False)

	result = saas_api.update_saas_config(company_name="Nueva Plataforma", has_services=0)

	assert result["success"] is True
	assert fake_config.saved is True
	assert fake_config.has_services == 0
	assert service_setup_calls == []
	assert created_docs == []


def test_get_reservations_activation_contract_reports_requirements():
	class _DisabledReservationConfig:
		has_reservations = 0

		def get(self, key, default=None):
			return getattr(self, key, default)

	original_user = frappe.session.user
	original_get_roles = frappe.get_roles
	original_get_cached_doc = frappe.get_cached_doc
	original_master_gate = saas_api._is_platform_master_site

	try:
		frappe.session.user = "Administrator"
		frappe.get_roles = lambda user=None: ["System Manager"]
		saas_api._is_platform_master_site = lambda: False
		frappe.get_cached_doc = lambda doctype, name=None: _DisabledReservationConfig()

		response = saas_api.get_reservations_activation_contract()

		assert response["module"] == "reservations"
		assert response["activation"]["current_status"]["enabled"] is False
		assert response["activation"]["current_status"]["state"] == "disabled"
		assert [field["fieldname"] for field in response["activation"]["required_fields"]] == [
			"reservation_item_code",
			"max_reservation_assets",
			"default_event_items",
		]
		assert any(
			dependency["feature"] == "has_products" and dependency["required"] is True
			for dependency in response["activation"]["suggested_dependencies"]
		)
	finally:
		frappe.session.user = original_user
		frappe.get_roles = original_get_roles
		frappe.get_cached_doc = original_get_cached_doc
		saas_api._is_platform_master_site = original_master_gate


def test_setup_mexican_taxes_falls_back_when_direct_liabilities_missing():
	original_get_cached_doc = frappe.get_cached_doc
	original_exists = frappe.db.exists
	original_new_doc = frappe.new_doc
	original_commit = frappe.db.commit
	original_logger = frappe.logger

	created_accounts = []
	created_templates = []
	logger_messages = []
	fake_logger = type(
		"_FakeLogger",
		(),
		{"warning": lambda self, message: logger_messages.append(message)},
	)()

	class _FakeCompany:
		abbr = "LP"

	def _fake_get_cached_doc(doctype, name=None, *args, **kwargs):
		if doctype == "Company" and name == "La Paletixa":
			return _FakeCompany()
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} {name}")

	def _fake_exists(doctype, name=None, *args, **kwargs):
		if doctype == "Account":
			return name in {"Current Liabilities - LP", "Current Assets - LP"}
		if doctype in {
			"Sales Taxes and Charges Template",
			"Purchase Taxes and Charges Template",
			"Custom Field",
		}:
			return False
		if doctype == "Company":
			return name == "La Paletixa"
		return False

	def _fake_new_doc(doctype):
		doc = _RecordingDoc(doctype)

		def _insert(ignore_permissions=False):
			if doctype == "Account":
				doc.name = f"{doc.account_name} - LP"
				created_accounts.append((doc.name, doc.parent_account, doc.company, doc.account_type))
			elif doctype in {"Sales Taxes and Charges Template", "Purchase Taxes and Charges Template"}:
				created_templates.append(
					(doctype, doc.title, doc._children.get("taxes", [{}])[0].get("account_head"))
				)
			return doc

		doc.insert = _insert
		return doc

	try:
		frappe.get_cached_doc = _fake_get_cached_doc
		frappe.db.exists = _fake_exists
		frappe.new_doc = _fake_new_doc
		frappe.db.commit = lambda *args, **kwargs: None
		frappe.logger = lambda name=None: fake_logger

		saas_api.setup_mexican_taxes_and_fields("La Paletixa")

		assert ("IVA 16% Cobrado - LP", "Current Liabilities - LP", "La Paletixa", "Tax") in created_accounts
		assert ("IVA 16% Pagado - LP", "Current Assets - LP", "La Paletixa", "Tax") in created_accounts
		assert (
			"Sales Taxes and Charges Template",
			"IVA 16% México",
			"IVA 16% Cobrado - LP",
		) in created_templates
		assert (
			"Purchase Taxes and Charges Template",
			"IVA 16% México Compras",
			"IVA 16% Pagado - LP",
		) in created_templates
		assert logger_messages == []
	finally:
		frappe.get_cached_doc = original_get_cached_doc
		frappe.db.exists = original_exists
		frappe.new_doc = original_new_doc
		frappe.db.commit = original_commit
		frappe.logger = original_logger


def test_tenant_status_token_and_redaction():
	subdomain = f"safety-token-{_unique_suffix()}"
	original_user = frappe.session.user
	original_enqueue = frappe.enqueue
	original_rate_limit = saas_api._enforce_tenant_request_rate_limit
	original_base_domain = saas_api.get_base_domain

	try:
		saas_api._enforce_tenant_request_rate_limit = lambda *args, **kwargs: None
		saas_api.get_base_domain = lambda: "localhost"
		frappe.enqueue = lambda *args, **kwargs: None

		result = saas_api.request_tenant(
			subdomain=subdomain,
			company_name="Safety Test Company",
			company_tax_id="RFC-SAFETY",
			company_address="Calle Seguridad 123",
			company_phone="5551234567",
			company_email="ops@safety.test",
			admin_email="admin@safety.test",
			admin_password="SecretPassword123!",
		)

		assert result.get("success") is True
		assert result.get("request_id") == subdomain
		token = result.get("request_token")
		assert token

		pending_status = saas_api.get_tenant_status(subdomain, token=token)
		assert pending_status.get("status") == "Pending"
		assert pending_status.get("error_log") == ""

		unauthorized_status = saas_api.get_tenant_status(subdomain)
		assert unauthorized_status.get("status") == "NotFound"

		doc = frappe.get_doc("SaaS Tenant Request", {"subdomain": subdomain})
		doc.status = "Failed"
		doc.error_log = "Traceback: secret infra details"
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		failed_status = saas_api.get_tenant_status(subdomain, token=token)
		assert failed_status.get("status") == "Failed"
		assert failed_status.get("error_log") == "El aprovisionamiento falló. Contactá al administrador."
		assert "Traceback" not in (failed_status.get("error_log") or "")
	finally:
		_cleanup_tenant_request(subdomain)
		frappe.enqueue = original_enqueue
		saas_api._enforce_tenant_request_rate_limit = original_rate_limit
		saas_api.get_base_domain = original_base_domain
		frappe.set_user(original_user)


def test_tenant_status_accepts_workspace_id_alias():
	workspace_id = f"safety-alias-{_unique_suffix()}"
	original_user = frappe.session.user
	original_enqueue = frappe.enqueue
	original_rate_limit = saas_api._enforce_tenant_request_rate_limit
	original_base_domain = saas_api.get_base_domain

	try:
		saas_api._enforce_tenant_request_rate_limit = lambda *args, **kwargs: None
		saas_api.get_base_domain = lambda: "localhost"
		frappe.enqueue = lambda *args, **kwargs: None

		result = saas_api.request_tenant(
			workspace_id=workspace_id,
			company_name="Safety Test Company",
			company_tax_id="RFC-SAFETY",
			company_address="Calle Seguridad 123",
			company_phone="5551234567",
			company_email="ops@safety.test",
			admin_email="admin@safety.test",
			admin_password="SecretPassword123!",
		)

		assert result.get("success") is True
		assert result.get("request_id") == workspace_id
		token = result.get("request_token")
		assert token

		pending_status = saas_api.get_tenant_status(workspace_id=workspace_id, token=token)
		assert pending_status.get("status") == "Pending"
		assert pending_status.get("error_log") == ""

		unauthorized_status = saas_api.get_tenant_status(workspace_id=workspace_id)
		assert unauthorized_status.get("status") == "NotFound"
	finally:
		_cleanup_tenant_request(workspace_id)
		frappe.enqueue = original_enqueue
		saas_api._enforce_tenant_request_rate_limit = original_rate_limit
		saas_api.get_base_domain = original_base_domain
		frappe.set_user(original_user)


def test_get_features_fails_closed_without_platform_config(monkeypatch):
	class _EmptyFeatureConfig:
		primary_color = ""
		has_pos = 0
		has_production = 0
		has_logistics = 0
		has_reservations = 0
		has_wholesale = 0
		has_mexico_taxes = 0
		has_services = 0
		has_products = 0
		has_purchasing = 0
		reservation_item_code = ""
		max_reservation_assets = 0
		default_event_items = "[]"
		custom_country = ""
		custom_currency = ""
		company_name = ""
		company_logo = ""
		company_tax_id = ""
		company_address = ""
		company_phone = ""
		company_email = ""
		ticket_header = ""
		ticket_footer = ""
		print_logo = 0
		print_tax_id = 0
		print_address = 0
		print_contact = 0
		is_active = 0
		max_branches = 0

		def get(self, key, default=None):
			return getattr(self, key, default)

	monkeypatch.setattr(saas_api, "setup_company_identity_fields", lambda: None)
	monkeypatch.setattr(frappe, "get_cached_doc", lambda doctype, name=None: _EmptyFeatureConfig())

	response = saas_api.get_features()

	assert response["setup_required"] is True
	assert response["client_name"] == ""
	assert response["company_name"] == ""
	assert "La Paletixa" not in response["error"]


def test_get_features_includes_distribution_warehouse_for_admin(monkeypatch):
	config = _FakeSaaSConfig()
	config.company_name = "Tenant Co"
	config.company_abbr = "TC"
	config.default_distribution_warehouse = "Distribucion - TC"

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return config
		if doctype == "Warehouse" and name == "Distribucion - TC":
			return SimpleNamespace(company="Tenant Co", is_group=0, disabled=0)
		if doctype == "Company":
			return SimpleNamespace(abbr="TC")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(saas_api, "setup_company_identity_fields", lambda: None)
	monkeypatch.setattr(frappe.session, "user", "admin@example.test", raising=False)
	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)

	response = saas_api.get_features()

	assert response["default_distribution_warehouse"] == "Distribucion - TC"


def test_get_features_omits_distribution_warehouse_for_guest(monkeypatch):
	config = _FakeSaaSConfig()
	config.company_name = "Tenant Co"
	config.company_abbr = "TC"
	config.default_distribution_warehouse = "Distribucion - TC"

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return config
		if doctype == "Company":
			return SimpleNamespace(abbr="TC")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(saas_api, "setup_company_identity_fields", lambda: None)
	monkeypatch.setattr(frappe.session, "user", "Guest", raising=False)
	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)

	response = saas_api.get_features()

	assert "default_distribution_warehouse" not in response
	assert "Distribucion - TC" not in json.dumps(response, ensure_ascii=False)


def test_get_pos_profile_fails_closed_when_pos_is_disabled(monkeypatch):
	class _DisabledPosConfig:
		has_pos = 0

		def get(self, key, default=None):
			return getattr(self, key, default)

	def _unexpected(*args, **kwargs):
		raise AssertionError("POS data should not be queried when POS is disabled")

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_cached_doc", lambda doctype, name=None: _DisabledPosConfig())
	monkeypatch.setattr(frappe, "get_roles", _unexpected)
	monkeypatch.setattr(frappe, "get_all", _unexpected)
	monkeypatch.setattr(frappe, "get_doc", _unexpected)

	with pytest.raises(frappe.PermissionError):
		saas_api.get_pos_profile()


def test_is_platform_master_site_raises_when_platform_sites_are_missing(monkeypatch):
	def _missing_platform_sites():
		frappe.throw("missing platform sites", frappe.ValidationError)

	monkeypatch.setattr(infrastructure, "resolve_platform_master_sites", _missing_platform_sites)

	with pytest.raises(frappe.ValidationError):
		infrastructure.is_platform_master_site("frontend")


def test_tenant_active_gate_fails_closed():
	original_site = frappe.local.site
	original_form_dict = getattr(frappe.local, "form_dict", None)
	original_request = getattr(frappe.local, "request", None)
	original_get_cached_doc = frappe.get_cached_doc
	original_master_gate = saas_api._is_platform_master_site

	class _InactiveConfig:
		def get(self, key, default=None):
			if key == "is_active":
				return 0
			return default

	class _ActiveConfig:
		def get(self, key, default=None):
			if key == "is_active":
				return 1
			return default

	try:
		frappe.local.site = "tenant.localhost"
		frappe.local.form_dict = frappe._dict({"cmd": "paletixa_saas.paletixa_saas.api.some_protected_call"})
		saas_api._is_platform_master_site = lambda: False

		frappe.get_cached_doc = lambda *args, **kwargs: _InactiveConfig()
		try:
			saas_api.validate_tenant_is_active()
			raise AssertionError("Expected PermissionError when tenant is inactive")
		except frappe.PermissionError:
			pass

		frappe.get_cached_doc = lambda *args, **kwargs: _ActiveConfig()
		saas_api.validate_tenant_is_active()

		def _missing_master_site_lookup(*args, **kwargs):
			raise Exception("missing platform sites")

		saas_api._is_platform_master_site = _missing_master_site_lookup
		frappe.get_cached_doc = lambda *args, **kwargs: _ActiveConfig()
		frappe.local.form_dict = frappe._dict({"cmd": "paletixa_saas.paletixa_saas.api.get_features"})
		saas_api.validate_tenant_is_active()

		frappe.local.form_dict = frappe._dict({"cmd": "paletixa_saas.paletixa_saas.api.some_protected_call"})
		saas_api.validate_tenant_is_active()
	finally:
		saas_api._is_platform_master_site = original_master_gate
		frappe.get_cached_doc = original_get_cached_doc
		frappe.local.site = original_site
		frappe.local.form_dict = original_form_dict
		frappe.local.request = original_request


def test_primary_master_site_prefers_frontend_over_erpadmin_in_dev():
	original_resolve = saas_api._resolve_platform_master_sites

	try:
		saas_api._resolve_platform_master_sites = lambda: {"frontend", "erpadmin"}
		assert saas_api._get_primary_master_site() == "frontend"
	finally:
		saas_api._resolve_platform_master_sites = original_resolve


def test_tenant_admin_permission_allows_tenant_admin_and_blocks_master_site():
	original_user = frappe.session.user
	original_get_roles = frappe.get_roles
	original_master_gate = saas_api._is_platform_master_site

	try:
		frappe.session.user = "admin@tenant.test"
		frappe.get_roles = lambda user=None: []
		saas_api._is_platform_master_site = lambda: False

		saas_api.check_tenant_admin_permission()

		def _missing_platform_sites():
			raise Exception("missing platform sites")

		saas_api._is_platform_master_site = _missing_platform_sites
		saas_api.check_tenant_admin_permission()

		saas_api._is_platform_master_site = lambda: True
		try:
			saas_api.check_tenant_admin_permission()
			raise AssertionError("Expected PermissionError on master site")
		except frappe.PermissionError:
			pass
	finally:
		frappe.session.user = original_user
		frappe.get_roles = original_get_roles
		saas_api._is_platform_master_site = original_master_gate


def _assert_audit_safe_cancellation(
	cancel_fn,
	sales_order_name,
	invoice_name,
	payment_name,
	extra_docs=None,
	cancel_kwargs=None,
):
	original_user = frappe.session.user
	original_get_all = frappe.get_all
	original_get_doc = frappe.get_doc
	original_exists = frappe.db.exists
	original_delete_doc = frappe.delete_doc
	original_commit = frappe.db.commit
	original_begin = frappe.db.begin
	original_rollback = frappe.db.rollback
	original_get_roles = frappe.get_roles

	fake_docs = {
		("Sales Order", sales_order_name): _FakeDoc("Sales Order", sales_order_name, docstatus=1),
		("Sales Invoice", invoice_name): _FakeDoc("Sales Invoice", invoice_name, docstatus=1),
		("Payment Entry", payment_name): _FakeDoc("Payment Entry", payment_name, docstatus=1),
	}
	if extra_docs:
		fake_docs.update(extra_docs)
	deleted_docs = []

	def _fake_exists(doctype, name=None, *args, **kwargs):
		return (doctype, name) in fake_docs

	def _fake_get_all(doctype, filters=None, pluck=None, **kwargs):
		if doctype == "Sales Invoice Item":
			return [invoice_name]
		if doctype == "Payment Entry Reference":
			return [payment_name]
		return []

	def _fake_get_doc(doctype, name=None, *args, **kwargs):
		return fake_docs[(doctype, name)]

	def _fake_delete_doc(doctype, name, ignore_permissions=False):
		deleted_docs.append((doctype, name))

	try:
		frappe.session.user = "Administrator"
		frappe.get_roles = lambda user=None: ["System Manager"]
		frappe.db.exists = _fake_exists
		frappe.get_all = _fake_get_all
		frappe.get_doc = _fake_get_doc
		frappe.delete_doc = _fake_delete_doc
		frappe.db.commit = lambda *args, **kwargs: None
		frappe.db.begin = lambda *args, **kwargs: None
		frappe.db.rollback = lambda *args, **kwargs: None

		result = cancel_fn(sales_order_name, **(cancel_kwargs or {}))
		assert result.get("success") is True
		assert deleted_docs == []
		assert fake_docs[("Sales Order", sales_order_name)].docstatus == 2
		assert fake_docs[("Sales Order", sales_order_name)].cancel_calls == 1
		assert fake_docs[("Sales Invoice", invoice_name)].docstatus == 2
		assert fake_docs[("Sales Invoice", invoice_name)].cancel_calls == 1
		assert fake_docs[("Payment Entry", payment_name)].docstatus == 2
		assert fake_docs[("Payment Entry", payment_name)].cancel_calls == 1
	finally:
		frappe.session.user = original_user
		frappe.get_roles = original_get_roles
		frappe.db.exists = original_exists
		frappe.get_all = original_get_all
		frappe.get_doc = original_get_doc
		frappe.delete_doc = original_delete_doc
		frappe.db.commit = original_commit
		frappe.db.begin = original_begin
		frappe.db.rollback = original_rollback


def test_audit_safe_wholesale_cancellation():
	from paletixa_saas.paletixa_saas.api import cancel_wholesale_order

	_assert_audit_safe_cancellation(cancel_wholesale_order, "SO-SAFETY-1", "SI-SAFETY-1", "PE-SAFETY-1")


def test_audit_safe_event_cancellation():
	reservation = _FakeDoc("Event Cart Reservation", "SO-SAFETY-2", docstatus=0)
	reservation.state = "Confirmed"
	reservation.sales_order = "SO-SAFETY-2"
	reservation.sales_invoice = "SI-SAFETY-2"
	reservation.payment_entry = "PE-SAFETY-2"
	reservation.credit_note = "SI-RETURN-2"
	reservation.refund_payment_entry = "PE-REFUND-2"
	reservation.credit_note_amount = 0
	reservation.refund_amount = 0
	reservation.cancel_reason = ""
	credit_note = SimpleNamespace(name="SI-RETURN-2", grand_total=-100)
	refund = SimpleNamespace(paid_amount=100, received_amount=0)

	with (
		patch.object(frappe.session, "user", "Administrator"),
		patch.object(frappe, "get_roles", return_value=["System Manager"]),
		patch.object(saas_api, "_get_event_reservation", return_value=reservation),
		patch.object(saas_api, "_event_reservation_named_lock", return_value=nullcontext()),
		patch.object(
			saas_api,
			"_validate_submitted_event_reversals",
			return_value=(credit_note, [SimpleNamespace(name="PE-SAFETY-2")], [refund], 100),
		),
		patch.object(
			saas_api,
			"_cancel_sales_order_transaction_chain",
			side_effect=AssertionError("submitted accounting history must be preserved"),
		),
	):
		result = saas_api.cancel_event_booking("SO-SAFETY-2", cancel_reason="Customer request")

	assert result["success"] is True
	assert reservation.state == "Cancelled"
	assert reservation.credit_note_amount == 100
	assert reservation.refund_amount == 100
	assert reservation.flags.event_reservation_service_operation is True


def test_event_cancellation_accepts_verified_document_names_from_service_request(monkeypatch):
	reservation = _FakeDoc("Event Cart Reservation", "SO-1", docstatus=0)
	reservation.state = "Confirmed"
	reservation.sales_invoice = "SI-1"
	reservation.payment_entry = "PE-1"
	reservation.credit_note = ""
	reservation.refund_payment_entry = ""
	reservation.credit_note_amount = 0
	reservation.refund_amount = 0
	reservation.cancel_reason = ""
	credit_note = SimpleNamespace(name="SI-RETURN-1", grand_total=-100)
	refund = SimpleNamespace(paid_amount=100, received_amount=0)

	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(saas_api, "_get_event_reservation", lambda name: reservation)
	monkeypatch.setattr(saas_api, "_event_reservation_named_lock", lambda *args, **kwargs: nullcontext())
	monkeypatch.setattr(
		saas_api,
		"_validate_submitted_event_reversals",
		lambda doc: (credit_note, [SimpleNamespace(name="PE-1")], [refund], 100)
		if (doc.credit_note, doc.refund_payment_entry) == ("SI-RETURN-1", "PE-REFUND-1")
		else (_ for _ in ()).throw(AssertionError("document names were not assigned before validation")),
	)

	result = saas_api.cancel_event_booking(
		"SO-1", credit_note="SI-RETURN-1", refund_payment_entry="PE-REFUND-1"
	)

	assert result["success"] is True
	assert reservation.credit_note == "SI-RETURN-1"
	assert reservation.refund_payment_entry == "PE-REFUND-1"


def test_event_cancellation_rejects_arbitrary_free_text_evidence(monkeypatch):
	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])

	with pytest.raises(frappe.ValidationError, match="texto libre"):
		saas_api.cancel_event_booking("SO-1", refund_evidence="Refunded over the phone")


def _setup_event_reversal_documents(monkeypatch, original_amounts, refund_amounts, duplicate_refund=False):
	reservation = SimpleNamespace(
		sales_order="SO-1",
		sales_invoice="SI-1",
		payment_entry=f"PE-{len(original_amounts)}",
		credit_note="SI-RETURN-1",
		refund_payment_entry="PE-REFUND-1" if refund_amounts else "",
		get=lambda key, default=None: getattr(reservation, key, default),
	)
	original_invoice = frappe._dict(
		name="SI-1",
		docstatus=1,
		company="Tenant Co",
		customer="CUST-1",
		currency="MXN",
		grand_total=sum(original_amounts),
	)
	credit_note = frappe._dict(
		name="SI-RETURN-1",
		docstatus=1,
		is_return=1,
		return_against="SI-1",
		company="Tenant Co",
		customer="CUST-1",
		currency="MXN",
		grand_total=-sum(original_amounts),
	)
	docs = {
		("Sales Invoice", "SI-1"): original_invoice,
		("Sales Invoice", "SI-RETURN-1"): credit_note,
	}
	for index, amount in enumerate(original_amounts, 1):
		reference_doctype = "Sales Order" if index == 1 and len(original_amounts) > 1 else "Sales Invoice"
		reference_name = "SO-1" if reference_doctype == "Sales Order" else "SI-1"
		docs[("Payment Entry", f"PE-{index}")] = frappe._dict(
			name=f"PE-{index}",
			docstatus=1,
			payment_type="Receive",
			company="Tenant Co",
			party_type="Customer",
			party="CUST-1",
			paid_to_account_currency="MXN",
			paid_amount=amount,
			received_amount=amount,
			references=[
				frappe._dict(
					reference_doctype=reference_doctype,
					reference_name=reference_name,
					allocated_amount=amount,
				)
			],
		)
	for index, amount in enumerate(refund_amounts, 1):
		references = [
			frappe._dict(
				reference_doctype="Sales Invoice",
				reference_name="SI-RETURN-1",
				allocated_amount=-amount,
			)
		]
		if duplicate_refund and index == 1:
			references.append(references[0].copy())
		docs[("Payment Entry", f"PE-REFUND-{index}")] = frappe._dict(
			name=f"PE-REFUND-{index}",
			docstatus=1,
			payment_type="Pay",
			party_type="Customer",
			party="CUST-1",
			company="Tenant Co",
			paid_from_account_currency="MXN",
			paid_amount=amount,
			received_amount=amount,
			references=references,
		)

	def fake_get_all(doctype, filters=None, fields=None, **kwargs):
		assert doctype == "Payment Entry Reference"
		if filters["reference_name"][1] == ["SO-1", "SI-1"]:
			return [{"parent": f"PE-{index}"} for index in range(1, len(original_amounts) + 1)]
		return [{"parent": f"PE-REFUND-{index}"} for index in range(1, len(refund_amounts) + 1)]

	monkeypatch.setattr(frappe, "get_doc", lambda doctype, name: docs[(doctype, name)])
	monkeypatch.setattr(frappe, "get_all", fake_get_all)
	return reservation, credit_note


def test_event_reversal_rejects_refund_of_only_final_payment(monkeypatch):
	reservation, _credit_note = _setup_event_reversal_documents(monkeypatch, [30, 70], [70])

	with pytest.raises(frappe.ValidationError, match="todos los pagos"):
		saas_api._validate_submitted_event_reversals(reservation)


def test_event_reversal_accepts_advance_and_final_payment_fully_refunded(monkeypatch):
	reservation, credit_note = _setup_event_reversal_documents(monkeypatch, [30, 70], [30, 70])

	result = saas_api._validate_submitted_event_reversals(reservation)

	assert result[0] == credit_note
	assert [payment.name for payment in result[1]] == ["PE-1", "PE-2"]
	assert [payment.name for payment in result[2]] == ["PE-REFUND-1", "PE-REFUND-2"]
	assert result[3] == 100


def test_event_reversal_rejects_duplicate_refund_references(monkeypatch):
	reservation, _credit_note = _setup_event_reversal_documents(
		monkeypatch, [100], [100], duplicate_refund=True
	)

	with pytest.raises(frappe.ValidationError, match="única referencia"):
		saas_api._validate_submitted_event_reversals(reservation)


def test_event_reversal_preserves_single_payment_backward_compatibility(monkeypatch):
	reservation, credit_note = _setup_event_reversal_documents(monkeypatch, [100], [100])

	result = saas_api._validate_submitted_event_reversals(reservation)

	assert result[0] == credit_note
	assert [payment.name for payment in result[1]] == ["PE-1"]
	assert [payment.name for payment in result[2]] == ["PE-REFUND-1"]
	assert result[3] == 100


def test_confirm_event_reservation_uses_site_safe_identity_lock(monkeypatch):
	seen = []

	@contextmanager
	def fake_lock(key, timeout_seconds=5):
		seen.append(("enter", key, timeout_seconds))
		yield
		seen.append(("exit", key, timeout_seconds))

	monkeypatch.setattr(frappe.local, "site", "tenant-a.test", raising=False)
	monkeypatch.setattr(frappe.session, "user", "Administrator", raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"])
	monkeypatch.setattr(saas_api, "_event_reservation_named_lock", fake_lock)
	monkeypatch.setattr(
		saas_api,
		"_confirm_event_reservation_locked",
		lambda *args, **kwargs: seen.append(("confirm", args[0])) or {"sales_invoice": "SI-1"},
	)

	first = saas_api.confirm_event_reservation("SO-1")
	second = saas_api.confirm_event_reservation("SO-1")

	assert first == second == {"sales_invoice": "SI-1"}
	assert seen[0][0] == "enter"
	assert seen[1] == ("confirm", "SO-1")
	assert seen[2][0] == "exit"
	assert seen[0][1] == seen[3][1]
	assert "tenant-a.test" not in seen[0][1]
	assert len(seen[0][1]) <= 64


def test_concurrent_event_confirmation_creates_accounting_chain_once(monkeypatch):
	mutex = threading.Lock()
	start = threading.Barrier(2)
	created = []

	@contextmanager
	def fake_lock(key, timeout_seconds=5):
		with mutex:
			yield

	def fake_confirm(*args, **kwargs):
		if not created:
			created.append(("SI-1", "PE-1"))
		return {"sales_invoice": created[0][0], "payment_entry": created[0][1]}

	def run_confirmation():
		start.wait()
		return saas_api.confirm_event_reservation("SO-1")

	monkeypatch.setattr(saas_api, "_require_event_lifecycle_admin_access", lambda: None)
	monkeypatch.setattr(saas_api, "_event_reservation_named_lock", fake_lock)
	monkeypatch.setattr(saas_api, "_confirm_event_reservation_locked", fake_confirm)

	with ThreadPoolExecutor(max_workers=2) as executor:
		results = list(executor.map(lambda _: run_confirmation(), range(2)))

	assert created == [("SI-1", "PE-1")]
	assert results == [
		{"sales_invoice": "SI-1", "payment_entry": "PE-1"},
		{"sales_invoice": "SI-1", "payment_entry": "PE-1"},
	]


def run():
	print("Running backend safety regression checks...")
	test_update_saas_config_uses_incoming_company_name_for_reservations()
	test_update_saas_config_ignores_invalid_max_reservation_assets()
	test_update_saas_config_skips_reservation_validation_when_disabled()
	test_get_features_omits_reservation_defaults_when_disabled()
	test_get_reservations_activation_contract_reports_requirements()
	test_setup_mexican_taxes_falls_back_when_direct_liabilities_missing()
	test_tenant_status_token_and_redaction()
	test_tenant_active_gate_fails_closed()
	test_create_wholesale_sale_blocks_non_privileged_authenticated_users_before_document_creation()
	test_audit_safe_wholesale_cancellation()
	test_audit_safe_event_cancellation()
	print("All backend safety regression checks passed.")


if __name__ == "__main__":
	run()
