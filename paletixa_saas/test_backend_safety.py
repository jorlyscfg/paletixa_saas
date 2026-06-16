# -*- coding: utf-8 -*-

import uuid

import frappe

from paletixa_saas.paletixa_saas import api as saas_api


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

	def cancel(self):
		self.cancel_calls += 1
		self.docstatus = 2


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


class _RecordingDoc:
	def __init__(self, doctype):
		self.doctype = doctype
		self._children = {}

	def append(self, table, row):
		self._children.setdefault(table, []).append(row)

	def insert(self, ignore_permissions=False):
		return self


def test_update_saas_config_uses_incoming_company_name_for_reservations():
	original_user = frappe.session.user
	original_get_roles = frappe.get_roles
	original_get_doc = frappe.get_doc
	original_exists = frappe.db.exists
	original_commit = frappe.db.commit
	original_clear_cache = frappe.clear_cache
	original_setup_fields = saas_api.setup_company_identity_fields
	original_sync_warehouses = saas_api.sync_event_warehouses

	fake_config = _FakeSaaSConfig()
	sync_calls = []

	try:
		frappe.session.user = "Administrator"
		frappe.get_roles = lambda user=None: ["System Manager"]
		frappe.get_doc = lambda doctype, *args, **kwargs: fake_config if doctype == "SaaS Feature Config" else None
		frappe.db.exists = lambda doctype, name=None, *args, **kwargs: False
		frappe.db.commit = lambda *args, **kwargs: None
		frappe.clear_cache = lambda *args, **kwargs: None
		saas_api.setup_company_identity_fields = lambda: None
		saas_api.sync_event_warehouses = lambda company_name, max_assets: sync_calls.append((company_name, max_assets))

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


def test_update_saas_config_ignores_invalid_max_reservation_assets():
	original_user = frappe.session.user
	original_get_roles = frappe.get_roles
	original_get_doc = frappe.get_doc
	original_exists = frappe.db.exists
	original_commit = frappe.db.commit
	original_clear_cache = frappe.clear_cache
	original_setup_fields = saas_api.setup_company_identity_fields
	original_sync_warehouses = saas_api.sync_event_warehouses

	fake_config = _FakeSaaSConfig()
	fake_config.max_reservation_assets = 5
	sync_calls = []

	try:
		frappe.session.user = "Administrator"
		frappe.get_roles = lambda user=None: ["System Manager"]
		frappe.get_doc = lambda doctype, *args, **kwargs: fake_config if doctype == "SaaS Feature Config" else None
		frappe.db.exists = lambda doctype, name=None, *args, **kwargs: False
		frappe.db.commit = lambda *args, **kwargs: None
		frappe.clear_cache = lambda *args, **kwargs: None
		saas_api.setup_company_identity_fields = lambda: None
		saas_api.sync_event_warehouses = lambda company_name, max_assets: sync_calls.append((company_name, max_assets))

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
		if doctype in {"Sales Taxes and Charges Template", "Purchase Taxes and Charges Template", "Custom Field"}:
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
		assert ("Sales Taxes and Charges Template", "IVA 16% México", "IVA 16% Cobrado - LP") in created_templates
		assert ("Purchase Taxes and Charges Template", "IVA 16% México Compras", "IVA 16% Pagado - LP") in created_templates
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
			subdomain,
			"Safety Test Company",
			"admin@safety.test",
			"SecretPassword123!",
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


def test_tenant_active_gate_fails_closed():
	original_site = frappe.local.site
	original_form_dict = getattr(frappe.local, "form_dict", None)
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

	def _raise_config_error(*args, **kwargs):
		raise Exception("boom")

	try:
		frappe.local.site = "tenant.localhost"
		frappe.local.form_dict = frappe._dict({"cmd": "paletixa_saas.paletixa_saas.api.some_protected_call"})
		saas_api._is_platform_master_site = lambda: False

		frappe.get_cached_doc = _raise_config_error
		try:
			saas_api.validate_tenant_is_active()
			raise AssertionError("Expected PermissionError when SaaS Feature Config lookup fails")
		except frappe.PermissionError:
			pass

		frappe.get_cached_doc = lambda *args, **kwargs: _InactiveConfig()
		try:
			saas_api.validate_tenant_is_active()
			raise AssertionError("Expected PermissionError when tenant is inactive")
		except frappe.PermissionError:
			pass

		frappe.get_cached_doc = lambda *args, **kwargs: _ActiveConfig()
		saas_api.validate_tenant_is_active()
	finally:
		saas_api._is_platform_master_site = original_master_gate
		frappe.get_cached_doc = original_get_cached_doc
		frappe.local.site = original_site
		frappe.local.form_dict = original_form_dict


def test_tenant_admin_permission_allows_tenant_admin_and_blocks_master_site():
	original_user = frappe.session.user
	original_get_roles = frappe.get_roles
	original_master_gate = saas_api._is_platform_master_site

	try:
		frappe.session.user = "admin@tenant.test"
		frappe.get_roles = lambda user=None: []
		saas_api._is_platform_master_site = lambda: False

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


def _assert_audit_safe_cancellation(cancel_fn, sales_order_name, invoice_name, payment_name):
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

		result = cancel_fn(sales_order_name)
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
	from paletixa_saas.paletixa_saas.api import cancel_event_booking

	_assert_audit_safe_cancellation(cancel_event_booking, "SO-SAFETY-2", "SI-SAFETY-2", "PE-SAFETY-2")


def run():
	print("Running backend safety regression checks...")
	test_update_saas_config_uses_incoming_company_name_for_reservations()
	test_update_saas_config_ignores_invalid_max_reservation_assets()
	test_setup_mexican_taxes_falls_back_when_direct_liabilities_missing()
	test_tenant_status_token_and_redaction()
	test_tenant_active_gate_fails_closed()
	test_audit_safe_wholesale_cancellation()
	test_audit_safe_event_cancellation()
	print("All backend safety regression checks passed.")


if __name__ == "__main__":
	run()
