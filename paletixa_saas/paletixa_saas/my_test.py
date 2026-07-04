import json
import os
import re

import frappe

from paletixa_saas.config.infrastructure import get_reserved_subdomains


def run():
	print("Registering DocType 'SaaS Tenant Request' in database...")

	# Load JSON file using module path
	module_dir = os.path.dirname(__file__)
	json_path = os.path.join(module_dir, "doctype", "saas_tenant_request", "saas_tenant_request.json")
	print(f"Loading JSON from absolute path: {json_path}")

	with open(json_path) as f:
		doc_data = json.load(f)

	if not frappe.db.exists("DocType", "SaaS Tenant Request"):
		doc = frappe.get_doc(doc_data)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		print("✓ DocType registered successfully.")
	else:
		print("✓ DocType already registered.")

	print("Starting automated tests for SaaS Tenant Request...")

	# Verify DocType exists
	if not frappe.db.exists("DocType", "SaaS Tenant Request"):
		raise Exception("DocType 'SaaS Tenant Request' not found!")
	print("✓ DocType exists in DB.")

	# Test API request_tenant Workspace ID validations
	from paletixa_saas.paletixa_saas.api import get_tenant_status, request_tenant

	invalid_workspace_ids = [
		"my_company",
		"test!",
		"sub.domain",
		"",
		*sorted(get_reserved_subdomains()),
		"this-workspace-id-is-way-too-long-for-the-system-to-accept",
	]
	for workspace_id in invalid_workspace_ids:
		try:
			request_tenant(
				workspace_id=workspace_id,
				company_name="Test Company",
				company_tax_id="RFC-TEST",
				company_address="Calle Principal 123",
				company_phone="5551234567",
				company_email="ops@test.com",
				admin_email="admin@test.com",
				admin_password="admin123",
			)
			raise Exception(f"Failed to raise validation error for invalid Workspace ID: {workspace_id}")
		except frappe.ValidationError:
			pass  # Expected behavior
	print("✓ Workspace ID format validation passes.")

	# Test request creation
	workspace_id = "test-tenant-temp"
	company_name = "Temp Company S.A."
	admin_email = "admin@tempcompany.com"
	admin_password = "SecretPassword123!"

	# Delete any existing request safely
	existing = frappe.db.get_value("SaaS Tenant Request", {"subdomain": workspace_id}, "name")
	if existing:
		frappe.delete_doc("SaaS Tenant Request", existing, ignore_permissions=True)
		frappe.db.commit()

	res = request_tenant(
		workspace_id=workspace_id,
		company_name=company_name,
		company_tax_id="RFC-TEMP",
		company_address="Calle Principal 123",
		company_phone="5551234567",
		company_email="ops@tempcompany.com",
		admin_email=admin_email,
		admin_password=admin_password,
	)
	if not res.get("success") or res.get("request_id") != workspace_id or not res.get("request_token"):
		raise Exception("Failed to register tenant request!")
	request_token = res.get("request_token")

	doc = frappe.get_doc("SaaS Tenant Request", workspace_id)
	if doc.company_name != company_name or doc.admin_email != admin_email or doc.status != "Pending":
		raise Exception("Created request fields do not match request parameters!")
	print("✓ Tenant Request document created successfully in DB.")

	# Verify get_tenant_status endpoint
	status_res = get_tenant_status(workspace_id=workspace_id, token=request_token)
	if status_res.get("status") != "Pending":
		raise Exception("Status polling did not return Pending!")
	print("✓ Status polling returns Pending.")

	unauth_status_res = get_tenant_status(workspace_id=workspace_id)
	if unauth_status_res.get("status") != "NotFound":
		raise Exception("Token-gated status polling did not reject missing token!")
	print("✓ Status polling is token-gated.")

	# Verify failed status is sanitized for guests
	doc.status = "Failed"
	doc.error_log = "Traceback: secret infra details"
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	failed_status_res = get_tenant_status(workspace_id=workspace_id, token=request_token)
	if failed_status_res.get("status") != "Failed":
		raise Exception("Status polling did not return Failed!")
	if "Traceback" in (failed_status_res.get("error_log") or ""):
		raise Exception("Failed status exposed raw error logs!")
	print("✓ Failed status is sanitized for guest polling.")

	# Clean up request
	frappe.delete_doc("SaaS Tenant Request", workspace_id, ignore_permissions=True)
	frappe.db.commit()

	print("All tests completed successfully! 🎉")


if __name__ == "__main__":
	run()
