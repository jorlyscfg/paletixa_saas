from types import SimpleNamespace

import frappe

from paletixa_saas.config import platform_defaults


class _FakeConfig:
	def __init__(self, values):
		self._values = values

	def get(self, key, default=None):
		return self._values.get(key, default)


def run_tests():
	print("🚀 Verificando resolución explícita de defaults de plataforma...")

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
