from types import SimpleNamespace

import frappe
import pytest

from paletixa_saas.config import platform_defaults


class _FakeConfig:
	def __init__(self, values):
		self._values = values

	def get(self, key, default=None):
		return self._values.get(key, default)


class _FakeModeOfPayment:
	def __init__(self):
		self.mode_of_payment = ""
		self.type = ""
		self.enabled = 0
		self.accounts = []
		self.insert_calls = 0
		self.save_calls = 0

	def append(self, table, row):
		assert table == "accounts"
		child = SimpleNamespace(**row)
		self.accounts.append(child)
		return child

	def insert(self, ignore_permissions=False):
		self.insert_calls += 1
		return self

	def save(self, ignore_permissions=False):
		self.save_calls += 1
		return self

	def get(self, key, default=None):
		return getattr(self, key, default)


class _FakeAccount:
	def __init__(self, created_accounts, created_name="Bank - TC"):
		self._created_accounts = created_accounts
		self._created_name = created_name
		self.account_name = ""
		self.parent_account = ""
		self.company = ""
		self.account_type = ""
		self.root_type = ""
		self.report_type = ""
		self.is_group = 0
		self.disabled = 0
		self.insert_calls = 0
		self.name = ""

	def insert(self, ignore_permissions=False):
		self.insert_calls += 1
		self.name = self._created_name
		self._created_accounts[self.name] = SimpleNamespace(
			company=self.company,
			disabled=self.disabled,
			is_group=self.is_group,
			account_type=self.account_type,
			root_type=self.root_type,
			report_type=self.report_type,
			parent_account=self.parent_account,
			account_name=self.account_name,
		)
		return self


def test_configured_distribution_warehouse_is_returned_only_when_valid(monkeypatch):
	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig(
				{
					"company_name": "Tenant Co",
					"company_abbr": "TC",
					"default_distribution_warehouse": "Distribucion - TC",
				}
			)
		if doctype == "Warehouse" and name == "Distribucion - TC":
			return SimpleNamespace(company="Tenant Co", is_group=0, disabled=0)
		if doctype == "Company":
			return SimpleNamespace(abbr="TC")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)

	assert platform_defaults.get_platform_distribution_warehouse() == "Distribucion - TC"


@pytest.mark.parametrize(
	"warehouse_doc,match",
	[
		(None, "no existe"),
		(SimpleNamespace(company="Tenant Co", is_group=0, disabled=1), "deshabilitado"),
		(SimpleNamespace(company="Tenant Co", is_group=1, disabled=0), "grupo"),
		(SimpleNamespace(company="Other Co", is_group=0, disabled=0), "compañía Tenant Co"),
	],
)
def test_invalid_configured_distribution_warehouse_raises_clean_validation_error(
	monkeypatch, warehouse_doc, match
):
	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig(
				{
					"company_name": "Tenant Co",
					"company_abbr": "TC",
					"default_distribution_warehouse": "Invalid - TC",
				}
			)
		if doctype == "Warehouse" and name == "Invalid - TC":
			if warehouse_doc is None:
				raise frappe.DoesNotExistError
			return warehouse_doc
		if doctype == "Company":
			return SimpleNamespace(abbr="TC")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)

	with pytest.raises(frappe.ValidationError, match=match):
		platform_defaults.get_platform_distribution_warehouse()


def test_platform_defaults_fail_closed_in_production_when_defaults_are_missing():
	original_get_cached_doc = frappe.get_cached_doc
	original_get_all = frappe.get_all
	original_developer_mode = frappe.conf.get("developer_mode")
	original_in_test = getattr(frappe.flags, "in_test", False)

	try:
		frappe.conf.developer_mode = 0
		frappe.flags.in_test = False
		frappe.get_cached_doc = (
			lambda doctype, name=None: _FakeConfig({}) if doctype == "SaaS Feature Config" else None
		)
		frappe.get_all = lambda doctype, *args, **kwargs: []

		with pytest.raises(frappe.ValidationError):
			platform_defaults.get_platform_company_name()

		with pytest.raises(frappe.ValidationError):
			platform_defaults.get_platform_company_abbr()

		with pytest.raises(frappe.ValidationError):
			platform_defaults.get_platform_distribution_warehouse()

		with pytest.raises(frappe.ValidationError):
			platform_defaults.get_platform_payment_account("Cash")
	finally:
		frappe.get_cached_doc = original_get_cached_doc
		frappe.get_all = original_get_all
		frappe.conf.developer_mode = original_developer_mode
		frappe.flags.in_test = original_in_test


def test_configured_payment_account_wins_without_fallback_lookup(monkeypatch):
	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig(
				{
					"company_name": "Tenant Co",
					"default_cash_account": "Cash - TC",
				}
			)
		if doctype == "Account" and name == "Cash - TC":
			return SimpleNamespace(company="Tenant Co", disabled=0, is_group=0, account_type="Cash")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(
		frappe,
		"get_all",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected fallback lookup")),
	)

	assert platform_defaults.get_platform_payment_account("Cash") == "Cash - TC"


@pytest.mark.parametrize(
	"payment_mode, config_values, account_doc, match",
	[
		(
			"Cash",
			{"company_name": "Tenant Co", "default_cash_account": "Cash - TC"},
			SimpleNamespace(company="Other Co", disabled=0, is_group=0, account_type="Cash"),
			"compañía Tenant Co",
		),
		(
			"Cash",
			{"company_name": "Tenant Co", "default_cash_account": "Cash - TC"},
			SimpleNamespace(company="Tenant Co", disabled=1, is_group=0, account_type="Cash"),
			"deshabilitada",
		),
		(
			"Cash",
			{"company_name": "Tenant Co", "default_cash_account": "Cash - TC"},
			SimpleNamespace(company="Tenant Co", disabled=0, is_group=1, account_type="Cash"),
			"cuenta grupo",
		),
		(
			"Transferencia",
			{"company_name": "Tenant Co", "default_bank_account": "Bank - TC"},
			SimpleNamespace(company="Tenant Co", disabled=0, is_group=0, account_type="Cash"),
			"tipo Bank",
		),
	],
)
def test_invalid_configured_payment_account_raises_clean_validation_error(
	monkeypatch, payment_mode, config_values, account_doc, match
):
	account_name = config_values.get("default_cash_account") or config_values.get("default_bank_account")

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig(config_values)
		if doctype == "Account" and name == account_name:
			return account_doc
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(
		frappe,
		"get_all",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected fallback lookup")),
	)

	with pytest.raises(frappe.ValidationError, match=match):
		platform_defaults.get_platform_payment_account(payment_mode)


def test_missing_cash_config_falls_back_to_active_company_cash_account(monkeypatch):
	account_docs = {
		"Cash - TC": SimpleNamespace(company="Tenant Co", disabled=0, is_group=0, account_type="Cash"),
	}

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig({"company_name": "Tenant Co"})
		if doctype == "Account" and name in account_docs:
			return account_docs[name]
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	def _get_all(doctype, *args, **kwargs):
		if doctype == "Account":
			return [{"name": "Cash - TC"}]
		raise AssertionError(f"Unexpected get_all lookup: {doctype}")

	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(frappe, "get_all", _get_all)

	assert platform_defaults.get_platform_payment_account("Cash") == "Cash - TC"


def test_missing_bank_config_falls_back_to_active_company_bank_account(monkeypatch):
	account_docs = {
		"Bank - TC": SimpleNamespace(company="Tenant Co", disabled=0, is_group=0, account_type="Bank"),
	}

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig({"company_name": "Tenant Co"})
		if doctype == "Account" and name in account_docs:
			return account_docs[name]
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	def _get_all(doctype, *args, **kwargs):
		if doctype == "Account":
			return [{"name": "Bank - TC"}]
		raise AssertionError(f"Unexpected get_all lookup: {doctype}")

	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(frappe, "get_all", _get_all)

	assert platform_defaults.get_platform_payment_account("Transferencia") == "Bank - TC"


def test_missing_bank_leaf_creates_operational_bank_account_under_active_group(monkeypatch):
	created_accounts = {}
	created_doc = _FakeAccount(created_accounts)
	state = {"leaf_requests": 0}

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig({"company_name": "Tenant Co", "company_abbr": "TC"})
		if doctype == "Account" and name == "Bank Accounts - TC":
			return SimpleNamespace(
				company="Tenant Co",
				disabled=0,
				is_group=1,
				account_type="Bank",
				root_type="Asset",
				report_type="Balance Sheet",
			)
		if doctype == "Account" and name in created_accounts:
			return created_accounts[name]
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	def _get_all(doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs):
		filters = filters or {}
		if doctype != "Account":
			raise AssertionError(f"Unexpected get_all lookup: {doctype}")

		if filters.get("company") != "Tenant Co":
			return []

		if filters.get("account_type") == "Bank" and filters.get("is_group", 0) == 0:
			state["leaf_requests"] += 1
			if created_accounts:
				return [{"name": "Bank - TC"}]
			return []

		if filters.get("account_type") == "Bank" and filters.get("is_group", 0) == 1:
			return [{"name": "Bank Accounts - TC"}]

		return []

	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(frappe, "get_all", _get_all)
	monkeypatch.setattr(
		frappe,
		"new_doc",
		lambda doctype: created_doc
		if doctype == "Account"
		else (_ for _ in ()).throw(AssertionError(f"Unexpected new_doc lookup: {doctype}")),
	)

	first = platform_defaults.get_platform_payment_account("Transferencia")
	second = platform_defaults.get_platform_payment_account("Transferencia")

	assert first == "Bank - TC"
	assert second == "Bank - TC"
	assert created_doc.insert_calls == 1
	assert state["leaf_requests"] == 2
	assert created_accounts["Bank - TC"].parent_account == "Bank Accounts - TC"
	assert created_accounts["Bank - TC"].company == "Tenant Co"
	assert created_accounts["Bank - TC"].account_type == "Bank"
	assert created_accounts["Bank - TC"].is_group == 0
	assert created_accounts["Bank - TC"].disabled == 0


def test_missing_bank_leaf_returns_concurrently_created_bank_account_after_insert_failure(monkeypatch):
	state = {"leaf_requests": 0, "insert_calls": 0}
	logged_errors = []

	class _RacingAccount:
		def __init__(self):
			self.account_name = ""
			self.parent_account = ""
			self.company = ""
			self.account_type = ""
			self.root_type = ""
			self.report_type = ""
			self.is_group = 0
			self.disabled = 0
			self.name = ""

		def insert(self, ignore_permissions=False):
			state["insert_calls"] += 1
			raise Exception("MySQL record changed / duplicate tree update condition")

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig({"company_name": "Tenant Co", "company_abbr": "TC"})
		if doctype == "Account" and name == "Bank Accounts - TC":
			return SimpleNamespace(
				company="Tenant Co",
				disabled=0,
				is_group=1,
				account_type="Bank",
				root_type="Asset",
				report_type="Balance Sheet",
			)
		if doctype == "Account" and name == "Bank - TC":
			return SimpleNamespace(
				company="Tenant Co",
				disabled=0,
				is_group=0,
				account_type="Bank",
				root_type="Asset",
				report_type="Balance Sheet",
			)
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	def _get_all(doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs):
		filters = filters or {}
		if doctype != "Account":
			raise AssertionError(f"Unexpected get_all lookup: {doctype}")

		if filters.get("company") != "Tenant Co":
			return []

		if filters.get("account_type") == "Bank" and filters.get("is_group", 0) == 0:
			state["leaf_requests"] += 1
			if state["leaf_requests"] == 1:
				return []
			return [{"name": "Bank - TC"}]

		if filters.get("account_type") == "Bank" and filters.get("is_group", 0) == 1:
			return [{"name": "Bank Accounts - TC"}]

		return []

	def _log_error(message=None, title=None, **kwargs):
		logged_errors.append({"title": title, "message": message})

	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(frappe, "get_all", _get_all)
	monkeypatch.setattr(
		frappe,
		"new_doc",
		lambda doctype: _RacingAccount()
		if doctype == "Account"
		else (_ for _ in ()).throw(AssertionError(f"Unexpected new_doc lookup: {doctype}")),
	)
	monkeypatch.setattr(frappe, "log_error", _log_error)

	assert platform_defaults.get_platform_payment_account("Transferencia") == "Bank - TC"
	assert state["insert_calls"] == 1
	assert state["leaf_requests"] == 2
	assert logged_errors == [
		{
			"title": "Error creating platform bank account",
			"message": "MySQL record changed / duplicate tree update condition",
		}
	]


def test_missing_bank_leaf_raises_clean_error_when_insert_failure_has_no_concurrent_account(monkeypatch):
	state = {"leaf_requests": 0, "insert_calls": 0}
	logged_errors = []

	class _RacingAccount:
		def __init__(self):
			self.account_name = ""
			self.parent_account = ""
			self.company = ""
			self.account_type = ""
			self.root_type = ""
			self.report_type = ""
			self.is_group = 0
			self.disabled = 0
			self.name = ""

		def insert(self, ignore_permissions=False):
			state["insert_calls"] += 1
			raise Exception("MySQL record changed / duplicate tree update condition")

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig({"company_name": "Tenant Co", "company_abbr": "TC"})
		if doctype == "Account" and name == "Bank Accounts - TC":
			return SimpleNamespace(
				company="Tenant Co",
				disabled=0,
				is_group=1,
				account_type="Bank",
				root_type="Asset",
				report_type="Balance Sheet",
			)
		if doctype == "Account" and name == "Bank - TC":
			return SimpleNamespace(
				company="Tenant Co",
				disabled=0,
				is_group=0,
				account_type="Bank",
				root_type="Asset",
				report_type="Balance Sheet",
			)
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	def _get_all(doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs):
		filters = filters or {}
		if doctype != "Account":
			raise AssertionError(f"Unexpected get_all lookup: {doctype}")

		if filters.get("company") != "Tenant Co":
			return []

		if filters.get("account_type") == "Bank" and filters.get("is_group", 0) == 0:
			state["leaf_requests"] += 1
			return []

		if filters.get("account_type") == "Bank" and filters.get("is_group", 0) == 1:
			return [{"name": "Bank Accounts - TC"}]

		return []

	def _log_error(message=None, title=None, **kwargs):
		logged_errors.append({"title": title, "message": message})

	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(frappe, "get_all", _get_all)
	monkeypatch.setattr(
		frappe,
		"new_doc",
		lambda doctype: _RacingAccount()
		if doctype == "Account"
		else (_ for _ in ()).throw(AssertionError(f"Unexpected new_doc lookup: {doctype}")),
	)
	monkeypatch.setattr(frappe, "log_error", _log_error)

	with pytest.raises(frappe.ValidationError, match="No se pudo crear la cuenta bancaria operativa"):
		platform_defaults.get_platform_payment_account("Transferencia")

	assert state["insert_calls"] == 1
	assert state["leaf_requests"] == 2
	assert logged_errors == [
		{
			"title": "Error creating platform bank account",
			"message": "MySQL record changed / duplicate tree update condition",
		}
	]


def test_missing_bank_group_raises_clean_error_and_does_not_create(monkeypatch):
	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig({"company_name": "Tenant Co", "company_abbr": "TC"})
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	def _get_all(doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs):
		filters = filters or {}
		if doctype == "Account" and filters.get("company") == "Tenant Co":
			return []
		raise AssertionError(f"Unexpected get_all lookup: {doctype}")

	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(frappe, "get_all", _get_all)
	monkeypatch.setattr(
		frappe,
		"new_doc",
		lambda doctype: (_ for _ in ()).throw(AssertionError(f"Unexpected new_doc lookup: {doctype}")),
	)

	with pytest.raises(frappe.ValidationError, match="grupo de cuentas bancarias"):
		platform_defaults.get_platform_payment_account("Transferencia")


def test_invalid_configured_bank_account_rejects_without_auto_create(monkeypatch):
	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig({"company_name": "Tenant Co", "default_bank_account": "Bank - TC"})
		if doctype == "Account" and name == "Bank - TC":
			return SimpleNamespace(company="Tenant Co", disabled=0, is_group=0, account_type="Cash")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(
		frappe,
		"get_all",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected fallback lookup")),
	)
	monkeypatch.setattr(
		frappe,
		"new_doc",
		lambda doctype: (_ for _ in ()).throw(AssertionError(f"Unexpected new_doc lookup: {doctype}")),
	)

	with pytest.raises(frappe.ValidationError, match="tipo Bank"):
		platform_defaults.get_platform_payment_account("Transferencia")


def test_payment_account_fallback_ignores_disabled_group_and_wrong_company_accounts(monkeypatch):
	account_docs = {
		"Disabled Cash - TC": SimpleNamespace(
			company="Tenant Co", disabled=1, is_group=0, account_type="Cash"
		),
		"Group Cash - TC": SimpleNamespace(company="Tenant Co", disabled=0, is_group=1, account_type="Cash"),
		"Other Cash - OC": SimpleNamespace(company="Other Co", disabled=0, is_group=0, account_type="Cash"),
		"Cash - TC": SimpleNamespace(company="Tenant Co", disabled=0, is_group=0, account_type="Cash"),
	}

	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig({"company_name": "Tenant Co"})
		if doctype == "Account" and name in account_docs:
			return account_docs[name]
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	def _get_all(doctype, *args, **kwargs):
		if doctype == "Account":
			return [
				{"name": "Disabled Cash - TC"},
				{"name": "Group Cash - TC"},
				{"name": "Other Cash - OC"},
				{"name": "Cash - TC"},
			]
		raise AssertionError(f"Unexpected get_all lookup: {doctype}")

	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(frappe, "get_all", _get_all)

	assert platform_defaults.get_platform_payment_account("Cash") == "Cash - TC"


def test_missing_payment_fallback_raises_clean_error_when_no_account_exists(monkeypatch):
	def _get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig({"company_name": "Tenant Co"})
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	def _get_all(doctype, *args, **kwargs):
		if doctype == "Account":
			return []
		raise AssertionError(f"Unexpected get_all lookup: {doctype}")

	monkeypatch.setattr(frappe, "get_cached_doc", _get_cached_doc)
	monkeypatch.setattr(frappe, "get_all", _get_all)

	with pytest.raises(frappe.ValidationError, match="cuenta contable Cash"):
		platform_defaults.get_platform_payment_account("Cash")


def test_transferencia_payment_mode_is_created_when_bank_draft_is_missing(monkeypatch):
	fake_mode = _FakeModeOfPayment()
	payment_account_calls = []

	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: _FakeConfig({"company_name": "Tenant Co"})
		if doctype == "SaaS Feature Config"
		else (_ for _ in ()).throw(AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")),
	)
	monkeypatch.setattr(
		frappe.db,
		"exists",
		lambda doctype, name=None, *args, **kwargs: False,
	)
	monkeypatch.setattr(
		frappe,
		"new_doc",
		lambda doctype: fake_mode
		if doctype == "Mode of Payment"
		else (_ for _ in ()).throw(AssertionError(f"Unexpected new_doc lookup: {doctype}")),
	)
	monkeypatch.setattr(
		platform_defaults,
		"get_platform_payment_account",
		lambda payment_mode: payment_account_calls.append(payment_mode) or "Bank - TC",
	)

	mode_name, payment_account = platform_defaults.ensure_platform_payment_mode(
		"Bank Draft", company_name="Tenant Co"
	)

	assert mode_name == "Transferencia"
	assert payment_account == "Bank - TC"
	assert payment_account_calls == ["Transferencia"]
	assert fake_mode.mode_of_payment == "Transferencia"
	assert fake_mode.type == "Bank"
	assert fake_mode.enabled == 1
	assert fake_mode.insert_calls == 1
	assert fake_mode.save_calls == 0
	assert fake_mode.accounts[0].company == "Tenant Co"
	assert fake_mode.accounts[0].default_account == "Bank - TC"


def test_transferencia_payment_mode_prefers_existing_bank_draft(monkeypatch):
	fake_mode = _FakeModeOfPayment()
	fake_mode.mode_of_payment = "Bank Draft"
	fake_mode.type = "Bank"
	fake_mode.enabled = 1
	fake_mode.accounts = [SimpleNamespace(company="Tenant Co", default_account="Old Bank - TC")]
	payment_account_calls = []

	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: _FakeConfig({"company_name": "Tenant Co"})
		if doctype == "SaaS Feature Config"
		else fake_mode
		if doctype == "Mode of Payment" and name == "Bank Draft"
		else (_ for _ in ()).throw(AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")),
	)
	monkeypatch.setattr(
		frappe.db,
		"exists",
		lambda doctype, name=None, *args, **kwargs: doctype == "Mode of Payment" and name == "Bank Draft",
	)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, name=None, *args, **kwargs: fake_mode
		if doctype == "Mode of Payment"
		else (_ for _ in ()).throw(AssertionError(f"Unexpected get_doc lookup: {doctype} / {name}")),
	)
	monkeypatch.setattr(
		platform_defaults,
		"get_platform_payment_account",
		lambda payment_mode: payment_account_calls.append(payment_mode) or "Bank - TC",
	)

	mode_name, payment_account = platform_defaults.ensure_platform_payment_mode(
		"Transferencia", company_name="Tenant Co"
	)

	assert mode_name == "Bank Draft"
	assert payment_account == "Bank - TC"
	assert payment_account_calls == ["Transferencia"]
	assert fake_mode.accounts[0].default_account == "Bank - TC"
	assert fake_mode.save_calls == 1


def test_transferencia_payment_mode_skips_disabled_bank_draft_and_creates_safe_alternative(monkeypatch):
	disabled_mode = _FakeModeOfPayment()
	disabled_mode.mode_of_payment = "Bank Draft"
	disabled_mode.type = "Bank"
	disabled_mode.enabled = 0
	disabled_mode.accounts = [SimpleNamespace(company="Tenant Co", default_account="Old Bank - TC")]
	created_mode = _FakeModeOfPayment()
	payment_account_calls = []

	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: _FakeConfig({"company_name": "Tenant Co"})
		if doctype == "SaaS Feature Config"
		else disabled_mode
		if doctype == "Mode of Payment" and name == "Bank Draft"
		else (_ for _ in ()).throw(AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")),
	)
	monkeypatch.setattr(
		frappe.db,
		"exists",
		lambda doctype, name=None, *args, **kwargs: doctype == "Mode of Payment" and name == "Bank Draft",
	)
	monkeypatch.setattr(
		frappe,
		"new_doc",
		lambda doctype: created_mode
		if doctype == "Mode of Payment"
		else (_ for _ in ()).throw(AssertionError(f"Unexpected new_doc lookup: {doctype}")),
	)
	monkeypatch.setattr(
		platform_defaults,
		"get_platform_payment_account",
		lambda payment_mode: payment_account_calls.append(payment_mode) or "Bank - TC",
	)

	mode_name, payment_account = platform_defaults.ensure_platform_payment_mode(
		"Transferencia", company_name="Tenant Co"
	)

	assert mode_name == "Transferencia"
	assert payment_account == "Bank - TC"
	assert payment_account_calls == ["Transferencia"]
	assert disabled_mode.enabled == 0
	assert disabled_mode.save_calls == 0
	assert created_mode.mode_of_payment == "Transferencia"
	assert created_mode.type == "Bank"
	assert created_mode.enabled == 1
	assert created_mode.insert_calls == 1
	assert created_mode.accounts[0].company == "Tenant Co"
	assert created_mode.accounts[0].default_account == "Bank - TC"


def test_platform_defaults_fall_back_to_single_company_when_config_identity_is_missing():
	original_get_cached_doc = frappe.get_cached_doc
	original_get_all = frappe.get_all

	try:
		frappe.get_cached_doc = (
			lambda doctype, name=None: _FakeConfig({})
			if doctype == "SaaS Feature Config"
			else SimpleNamespace(abbr="TC")
		)
		frappe.get_all = lambda doctype, *args, **kwargs: (
			[{"name": "Tenant Co"}] if doctype == "Company" else []
		)

		assert platform_defaults.get_platform_company_name() == "Tenant Co"
		assert platform_defaults.get_platform_company_abbr() == "TC"
	finally:
		frappe.get_cached_doc = original_get_cached_doc
		frappe.get_all = original_get_all


def test_distribution_warehouse_falls_back_to_factory_warehouse():
	original_get_cached_doc = frappe.get_cached_doc
	original_get_all = frappe.get_all

	try:
		frappe.get_cached_doc = (
			lambda doctype, name=None: _FakeConfig({"company_name": "Tenant Co", "company_abbr": "TC"})
			if doctype == "SaaS Feature Config"
			else SimpleNamespace(abbr="TC")
		)

		def _get_all(doctype, *args, **kwargs):
			if doctype == "Warehouse":
				return [
					{"name": "Central - TC", "warehouse_name": "Central"},
					{"name": "Materia Prima - TC", "warehouse_name": "Materia Prima Fabrica"},
					{"name": "Fábrica - TC", "warehouse_name": "Fábrica"},
				]
			return []

		frappe.get_all = _get_all

		assert platform_defaults.get_platform_distribution_warehouse() == "Fábrica - TC"
	finally:
		frappe.get_cached_doc = original_get_cached_doc
		frappe.get_all = original_get_all


def test_distribution_warehouse_falls_back_to_unaccented_factory_warehouse(monkeypatch):
	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: _FakeConfig({"company_name": "Tenant Co", "company_abbr": "TC"})
		if doctype == "SaaS Feature Config"
		else SimpleNamespace(abbr="TC"),
	)

	def _get_all(doctype, *args, **kwargs):
		if doctype == "Warehouse":
			return [
				{"name": "Central - TC", "warehouse_name": "Central"},
				{"name": "Fabrica - TC", "warehouse_name": "Fabrica"},
			]
		return []

	monkeypatch.setattr(frappe, "get_all", _get_all)

	assert platform_defaults.get_platform_distribution_warehouse() == "Fabrica - TC"


def test_distribution_warehouse_throws_clean_factory_message_when_missing():
	original_get_cached_doc = frappe.get_cached_doc
	original_get_all = frappe.get_all

	try:
		frappe.get_cached_doc = (
			lambda doctype, name=None: _FakeConfig({"company_name": "Tenant Co", "company_abbr": "TC"})
			if doctype == "SaaS Feature Config"
			else SimpleNamespace(abbr="TC")
		)
		frappe.get_all = lambda doctype, *args, **kwargs: []

		with pytest.raises(frappe.ValidationError, match="almacén de fábrica"):
			platform_defaults.get_platform_distribution_warehouse()
	finally:
		frappe.get_cached_doc = original_get_cached_doc
		frappe.get_all = original_get_all


def run_tests():
	print("🚀 Verificando resolución explícita de defaults de plataforma sin fallback demo...")

	original_get_cached_doc = frappe.get_cached_doc
	try:
		fake_config = _FakeConfig(
			{
				"company_name": "La Paletixa",
				"company_abbr": "LP",
				"default_distribution_warehouse": "Distribucion - LP",
				"default_cash_account": "Cash - LP",
				"default_bank_account": "Bank Accounts - LP",
			}
		)

		def _get_cached_doc(doctype, name=None):
			if doctype == "SaaS Feature Config":
				return fake_config
			if doctype == "Company":
				return SimpleNamespace(abbr="LP")
			if doctype == "Account" and name == "Cash - LP":
				return SimpleNamespace(company="La Paletixa", disabled=0, is_group=0, account_type="Cash")
			if doctype == "Account" and name == "Bank Accounts - LP":
				return SimpleNamespace(company="La Paletixa", disabled=0, is_group=0, account_type="Bank")
			raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

		frappe.get_cached_doc = _get_cached_doc

		assert platform_defaults.get_platform_company_name() == "La Paletixa"
		assert platform_defaults.get_platform_company_abbr() == "LP"
		assert platform_defaults.get_platform_distribution_warehouse() == "Distribucion - LP"
		assert platform_defaults.get_platform_payment_account("Cash") == "Cash - LP"
		assert platform_defaults.get_platform_payment_account("Transferencia") == "Bank Accounts - LP"
		print("✅ PASSED: company, warehouse and payment defaults resolve from config")

		missing_config = _FakeConfig({"company_name": "La Paletixa", "company_abbr": "LP"})

		def _get_cached_doc_missing(doctype, name=None):
			if doctype == "SaaS Feature Config":
				return missing_config
			if doctype == "Company":
				return SimpleNamespace(abbr="LP")
			raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

		frappe.get_cached_doc = _get_cached_doc_missing

		try:
			platform_defaults.get_platform_distribution_warehouse()
			print("❌ FAILED: missing warehouse default did not fail closed")
			return
		except frappe.ValidationError:
			print("✅ PASSED: missing warehouse default fails closed")

		try:
			platform_defaults.get_platform_payment_account("Cash")
			print("❌ FAILED: missing cash account default did not fail closed")
			return
		except frappe.ValidationError:
			print("✅ PASSED: missing cash account default fails closed")

	finally:
		frappe.get_cached_doc = original_get_cached_doc


if __name__ == "__main__":
	run_tests()
