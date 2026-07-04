from types import SimpleNamespace

import frappe
import pytest

from paletixa_saas.config import platform_defaults


class _FakeConfig:
	def __init__(self, values):
		self._values = values

	def get(self, key, default=None):
		return self._values.get(key, default)


def test_platform_defaults_fail_closed_in_production_when_defaults_are_missing():
	original_get_cached_doc = frappe.get_cached_doc
	original_get_all = frappe.get_all
	original_developer_mode = frappe.conf.get("developer_mode")
	original_in_test = getattr(frappe.flags, "in_test", False)

	try:
		frappe.conf.developer_mode = 0
		frappe.flags.in_test = False
		frappe.get_cached_doc = lambda doctype, name=None: _FakeConfig({}) if doctype == "SaaS Feature Config" else None
		frappe.get_all = lambda doctype, *args, **kwargs: [] if doctype == "Company" else []

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


def test_platform_defaults_fall_back_to_single_company_when_config_identity_is_missing():
	original_get_cached_doc = frappe.get_cached_doc
	original_get_all = frappe.get_all

	try:
		frappe.get_cached_doc = lambda doctype, name=None: _FakeConfig({}) if doctype == "SaaS Feature Config" else SimpleNamespace(abbr="TC")
		frappe.get_all = lambda doctype, *args, **kwargs: ([{"name": "Tenant Co"}] if doctype == "Company" else [])

		assert platform_defaults.get_platform_company_name() == "Tenant Co"
		assert platform_defaults.get_platform_company_abbr() == "TC"
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
