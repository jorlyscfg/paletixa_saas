import frappe

from paletixa_saas.paletixa_saas import api as saas_api


def test_setup_company_identity_fields_adds_warehouse_options():
	original_exists = frappe.db.exists
	original_get_doc = frappe.get_doc
	original_cache = frappe.cache
	original_commit = frappe.db.commit
	original_clear_cache = frappe.clear_cache
	original_role_permissions = saas_api.setup_saas_role_permissions
	original_site = getattr(frappe.local, "site", None)

	class _FakeCache:
		def get_value(self, key):
			return None

		def set_value(self, key, value):
			self.key = key
			self.value = value

	class _FakeDoc:
		def __init__(self, payload):
			self.payload = payload

		def insert(self, ignore_permissions=False):
			return self

	created_docs = []
	fake_cache = _FakeCache()

	try:
		frappe.local.site = "test.localhost"
		frappe.db.exists = lambda doctype, name=None: doctype == "Custom Field" and name != "SaaS Feature Config-default_distribution_warehouse"
		frappe.get_doc = lambda payload: created_docs.append(payload) or _FakeDoc(payload)
		frappe.cache = lambda: fake_cache
		frappe.db.commit = lambda *args, **kwargs: None
		frappe.clear_cache = lambda *args, **kwargs: None
		saas_api.setup_saas_role_permissions = lambda: None

		sa_api_result = saas_api.setup_company_identity_fields()
		assert sa_api_result is None

		target_doc = next(doc for doc in created_docs if doc["fieldname"] == "default_distribution_warehouse")
		assert target_doc["doctype"] == "Custom Field"
		assert target_doc["fieldtype"] == "Link"
		assert target_doc["options"] == "Warehouse"
	finally:
		frappe.db.exists = original_exists
		frappe.get_doc = original_get_doc
		frappe.cache = original_cache
		frappe.db.commit = original_commit
		frappe.clear_cache = original_clear_cache
		saas_api.setup_saas_role_permissions = original_role_permissions
		frappe.local.site = original_site
