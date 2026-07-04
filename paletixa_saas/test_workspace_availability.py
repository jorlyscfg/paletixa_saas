# -*- coding: utf-8 -*-

from pathlib import Path
import tempfile
import uuid

import frappe
import pytest

from paletixa_saas.paletixa_saas import api as saas_api


def _unique_workspace_id(prefix="workspace"):
	return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _cleanup_tenant_request(subdomain):
	if frappe.db.exists("SaaS Tenant Request", {"subdomain": subdomain}):
		name = frappe.db.get_value("SaaS Tenant Request", {"subdomain": subdomain}, "name")
		frappe.delete_doc("SaaS Tenant Request", name, ignore_permissions=True)
		frappe.db.commit()


def test_check_tenant_availability_returns_structured_results_for_common_states():
	workspace_id = _unique_workspace_id()
	original_get_sites_path = saas_api.get_sites_path
	original_get_base_domain = saas_api.get_base_domain

	with tempfile.TemporaryDirectory() as sites_dir:
		try:
			saas_api.get_sites_path = lambda: sites_dir
			saas_api.get_base_domain = lambda: "localhost"

			available = saas_api.check_tenant_availability(workspace_id=workspace_id)
			assert available["available"] is True
			assert available["reason"] == "available"
			assert available["workspace_id"] == workspace_id

			invalid = saas_api.check_tenant_availability(workspace_id="bad id")
			assert invalid["available"] is False
			assert invalid["reason"] == "invalid"

			reserved = saas_api.check_tenant_availability(workspace_id="master")
			assert reserved["available"] is False
			assert reserved["reason"] == "reserved"

			record = frappe.get_doc(
				{
					"doctype": "SaaS Tenant Request",
					"subdomain": workspace_id,
					"company_name": "Workspace Test Co",
					"company_tax_id": "RFC-WORKSPACE",
					"company_address": "Calle Principal 123",
					"company_phone": "5551234567",
					"company_email": "ops@workspace.test",
					"admin_email": "admin@test.local",
					"admin_password": "SecretPassword123!",
					"status": "Pending",
				}
			)
			record.insert(ignore_permissions=True)
			frappe.db.commit()

			duplicate = saas_api.check_tenant_availability(workspace_id=workspace_id)
			assert duplicate["available"] is False
			assert duplicate["reason"] == "duplicate"

			frappe.db.set_value("SaaS Tenant Request", workspace_id, "status", "Failed")
			frappe.db.commit()

			retry = saas_api.check_tenant_availability(workspace_id=workspace_id)
			assert retry["available"] is True
			assert retry["reason"] == "available"

			site_path = Path(sites_dir) / f"{workspace_id}.localhost"
			site_path.mkdir()

			site_exists = saas_api.check_tenant_availability(workspace_id=workspace_id)
			assert site_exists["available"] is False
			assert site_exists["reason"] == "site_exists"
		finally:
			_cleanup_tenant_request(workspace_id)
			saas_api.get_sites_path = original_get_sites_path
			saas_api.get_base_domain = original_get_base_domain


def test_request_tenant_still_rejects_duplicate_workspace_ids():
	workspace_id = _unique_workspace_id("duplicate")
	original_enqueue = frappe.enqueue
	original_rate_limit = saas_api._enforce_tenant_request_rate_limit
	original_get_base_domain = saas_api.get_base_domain
	original_get_sites_path = saas_api.get_sites_path

	with tempfile.TemporaryDirectory() as sites_dir:
		try:
			saas_api._enforce_tenant_request_rate_limit = lambda *args, **kwargs: None
			frappe.enqueue = lambda *args, **kwargs: None
			saas_api.get_base_domain = lambda: "localhost"
			saas_api.get_sites_path = lambda: sites_dir

			record = frappe.get_doc(
				{
					"doctype": "SaaS Tenant Request",
					"subdomain": workspace_id,
					"company_name": "Workspace Test Co",
					"company_tax_id": "RFC-DUPLICATE",
					"company_address": "Calle Principal 123",
					"company_phone": "5551234567",
					"company_email": "ops@workspace.test",
					"admin_email": "admin@test.local",
					"admin_password": "SecretPassword123!",
					"status": "Pending",
				}
			)
			record.insert(ignore_permissions=True)
			frappe.db.commit()

			try:
				saas_api.request_tenant(
					workspace_id=workspace_id,
					company_name="Workspace Test Co",
					company_tax_id="RFC-VALID",
					company_address="Calle Principal 123",
					company_phone="5551234567",
					company_email="ops@workspace.test",
					admin_email="admin@test.local",
					admin_password="SecretPassword123!",
				)
				raise AssertionError("Expected ValidationError for duplicate workspace ID")
			except frappe.ValidationError:
				pass
		finally:
			_cleanup_tenant_request(workspace_id)
			frappe.enqueue = original_enqueue
			saas_api._enforce_tenant_request_rate_limit = original_rate_limit
			saas_api.get_base_domain = original_get_base_domain
			saas_api.get_sites_path = original_get_sites_path


def test_request_tenant_requires_user_owned_identity_contract():
	workspace_id = _unique_workspace_id("identity")
	original_enqueue = frappe.enqueue
	original_rate_limit = saas_api._enforce_tenant_request_rate_limit
	original_master_site = saas_api._get_primary_master_site
	original_get_base_domain = saas_api.get_base_domain
	original_get_sites_path = saas_api.get_sites_path

	with tempfile.TemporaryDirectory() as sites_dir:
		try:
			saas_api._enforce_tenant_request_rate_limit = lambda *args, **kwargs: None
			saas_api._get_primary_master_site = lambda: "frontend"
			saas_api.get_base_domain = lambda: "localhost"
			saas_api.get_sites_path = lambda: sites_dir
			frappe.enqueue = lambda *args, **kwargs: None

			with pytest.raises(frappe.ValidationError):
				saas_api.request_tenant(
					workspace_id=workspace_id,
					company_name="Workspace Test Co",
					company_tax_id="",
					company_address="Calle Principal 123",
					company_phone="5551234567",
					company_email="ops@workspace.test",
					admin_email="admin@test.local",
					admin_password="SecretPassword123!",
				)
		finally:
			_cleanup_tenant_request(workspace_id)
			frappe.enqueue = original_enqueue
			saas_api._enforce_tenant_request_rate_limit = original_rate_limit
			saas_api._get_primary_master_site = original_master_site
			saas_api.get_base_domain = original_get_base_domain
			saas_api.get_sites_path = original_get_sites_path
