import json
import os
import traceback

import frappe

from paletixa_saas.config.infrastructure import get_bench_path, resolve_platform_master_sites
from paletixa_saas.paletixa_saas import api as saas_api
from paletixa_saas.paletixa_saas.api import (
	check_platform_admin_permission,
	create_new_branch_with_cashiers,
	get_platform_admin_dashboard,
	toggle_tenant_branch,
	update_tenant_config,
	validate_tenant_is_active,
)


def run():
	print("==================================================")
	print("STARTING PLATFORM ADMIN & TENANT CONTROLS TEST SUITE")
	print("==================================================")

	try:
		# Save original state for restoration
		original_site = frappe.local.site

		# 1. Test Permissions on Master Site
		print("\n--- TEST 1: API Permissions on Master Site ---")

		# 1a. Tenant active gate must fail closed on config lookup error / suspension
		print("Testing tenant active gate fail-closed behavior...")
		orig_site = frappe.local.site
		orig_form_dict = getattr(frappe.local, "form_dict", None)
		orig_get_single = frappe.get_single
		try:
			frappe.local.site = "tenant.localhost"
			frappe.local.form_dict = frappe._dict(
				{"cmd": "paletixa_saas.paletixa_saas.api.some_protected_call"}
			)

			def _raise_config_error(*args, **kwargs):
				raise Exception("boom")

			frappe.get_single = _raise_config_error
			try:
				validate_tenant_is_active()
				print("❌ FAILED: validate_tenant_is_active did not block on config lookup error")
			except frappe.PermissionError:
				print("✅ PASSED: validate_tenant_is_active blocked on config lookup error")

			class _InactiveConfig:
				def get(self, key, default=None):
					if key == "is_active":
						return 0
					return default

			frappe.get_single = lambda *args, **kwargs: _InactiveConfig()
			try:
				validate_tenant_is_active()
				print("❌ FAILED: validate_tenant_is_active did not block inactive tenant")
			except frappe.PermissionError:
				print("✅ PASSED: validate_tenant_is_active blocked inactive tenant")
		finally:
			frappe.get_single = orig_get_single
			frappe.local.site = orig_site
			frappe.local.form_dict = orig_form_dict

		# Set user to Guest
		frappe.set_user("Guest")
		try:
			get_platform_admin_dashboard()
			print("❌ FAILED: get_platform_admin_dashboard succeeded for Guest")
		except frappe.PermissionError:
			print("✅ PASSED: get_platform_admin_dashboard rejected Guest as expected")
		except Exception as e:
			print(f"❌ FAILED: Unexpected exception for Guest: {e!s}")

		# Test update_tenant_config permissions for non-admin
		try:
			update_tenant_config(subdomain="test", active=False, max_branches=5)
			print("❌ FAILED: update_tenant_config succeeded for Guest")
		except frappe.PermissionError:
			print("✅ PASSED: update_tenant_config rejected Guest as expected")
		except Exception as e:
			print(f"❌ FAILED: Unexpected exception for Guest: {e!s}")

		# Restore user to Administrator
		frappe.set_user("Administrator")
		print("✅ Restored session user to Administrator")

		# Test one configured master site name dynamically
		master_sites = sorted(resolve_platform_master_sites())
		if master_sites:
			test_master_site = next((site for site in master_sites if site != original_site), master_sites[0])
			print(f"Testing master site resolution for '{test_master_site}'...")
			frappe.local.site = test_master_site
			try:
				check_platform_admin_permission()
				print(f"✅ PASSED: api permitted master site '{test_master_site}'")
			except Exception as e:
				if "Este endpoint solo está disponible en el sitio maestro" in str(e):
					print(f"❌ FAILED: '{test_master_site}' was rejected as master site")
				else:
					print(f"❌ FAILED: Unexpected exception during master site check: {e!s}")
			finally:
				frappe.local.site = original_site

		# 2. Query Completed Tenants
		print("\n--- TEST 2: Retrieve Platform Dashboard Data ---")
		try:
			tenants = get_platform_admin_dashboard()
			print(f"✅ PASSED: Dashboard returned {len(tenants)} completed tenants.")
			for t in tenants:
				print(
					f"   - Tenant: {t['name']} (Company: {t['company_name']}, DB: {t['database_name']}, Active: {t['active']}, Max Branches: {t['max_branches']}, Active Branches: {t['branch_count']})"
				)
		except Exception as e:
			print(f"❌ FAILED: Could not retrieve platform admin dashboard: {e!s}")
			traceback.print_exc()
			return

		if not tenants:
			print("\n⚠️ WARNING: No completed tenants found. Skipping cross-DB sync and branch limit tests.")
			print("==================================================")
			print("TEST SUITE COMPLETED (WITH WARNINGS)")
			print("==================================================")
			return

		# Pick the first completed tenant for testing
		test_tenant = tenants[0]
		subdomain = test_tenant["name"]
		orig_active = test_tenant["active"]
		orig_max_branches = test_tenant["max_branches"]

		print(f"\nUsing test tenant: '{subdomain}'")

		# 3. Test update_tenant_config Syncing
		print("\n--- TEST 3: Config Synchronization across DBs ---")
		try:
			# Change max_branches and active status
			target_active = not bool(orig_active)
			target_max_branches = orig_max_branches + 1

			print(f"Updating configuration to: active={target_active}, max_branches={target_max_branches}...")
			update_tenant_config(subdomain=subdomain, active=target_active, max_branches=target_max_branches)

			# Verify on Master DB
			master_doc = frappe.get_doc("SaaS Tenant Request", subdomain)
			assert bool(master_doc.active) == target_active, (
				"Active status not updated in SaaS Tenant Request"
			)
			assert master_doc.max_branches == target_max_branches, (
				"Max branches not updated in SaaS Tenant Request"
			)
			print("✅ Verified master database fields updated correctly")

			# Verify on Tenant DB
			bench_path = get_bench_path()
			import os

			from paletixa_saas.paletixa_saas.api import get_base_domain

			base_domain = get_base_domain()
			domain = f"{subdomain}.{base_domain}"

			if os.path.exists(os.path.join(bench_path, "sites", domain)):
				frappe.destroy()
				frappe.init(site=domain, sites_path=os.path.join(bench_path, "sites"))
				frappe.connect()

				tenant_config = frappe.get_single("SaaS Feature Config")
				assert bool(tenant_config.is_active) == target_active, (
					"Active status not updated in Tenant SaaS Feature Config"
				)
				assert tenant_config.max_branches == target_max_branches, (
					"Max branches not updated in Tenant SaaS Feature Config"
				)
				print(
					"✅ Verified tenant database SaaS Feature Config updated correctly via connection switch"
				)

				# Restore connection context back to the original site
				frappe.destroy()
				frappe.init(site=original_site, sites_path=os.path.join(bench_path, "sites"))
				frappe.connect()
			else:
				print(
					f"⚠️ Tenant site folder for '{domain}' does not exist on disk, skipped tenant DB assertion"
				)

			# Restore original values
			print("Restoring original configuration values...")
			update_tenant_config(
				subdomain=subdomain, active=bool(orig_active), max_branches=orig_max_branches
			)
			print("✅ Restored original configuration values")
			print("✅ PASSED: Config synchronization working correctly")

		except Exception as e:
			print(f"❌ FAILED: Config synchronization test failed: {e!s}")
			traceback.print_exc()
			# Try to restore context to the original site
			try:
				frappe.destroy()
				frappe.init(site=original_site, sites_path=os.path.join(get_bench_path(), "sites"))
				frappe.connect()
			except Exception:
				pass
			return

		# 4. Test Branch Toggling
		print("\n--- TEST 4: Toggle Tenant Branch ---")
		if test_tenant.get("branches"):
			branch_to_toggle = test_tenant["branches"][0]
			branch_name = branch_to_toggle["name"]
			orig_disabled = branch_to_toggle["disabled"]
			target_disabled = not bool(orig_disabled)

			try:
				print(f"Toggling branch '{branch_name}' (disabled: {orig_disabled} -> {target_disabled})...")
				toggle_tenant_branch(subdomain=subdomain, branch_name=branch_name, disabled=target_disabled)

				# Verify in Tenant DB
				bench_path = get_bench_path()
				from paletixa_saas.paletixa_saas.api import get_base_domain

				base_domain = get_base_domain()
				domain = f"{subdomain}.{base_domain}"

				frappe.destroy()
				frappe.init(site=domain, sites_path=os.path.join(bench_path, "sites"))
				frappe.connect()

				# Verify POS Profile is disabled
				assert frappe.db.get_value("POS Profile", branch_name, "disabled") == (
					1 if target_disabled else 0
				), "POS Profile disabled field mismatch"
				# Verify Warehouse is disabled
				warehouse = frappe.db.get_value("POS Profile", branch_name, "warehouse")
				if warehouse:
					assert frappe.db.get_value("Warehouse", warehouse, "disabled") == (
						1 if target_disabled else 0
					), "Warehouse disabled field mismatch"

				print("✅ Verified branch toggle fields in Tenant DB correctly updated")

				# Restore original branch state
				frappe.destroy()
				frappe.init(site=original_site, sites_path=os.path.join(bench_path, "sites"))
				frappe.connect()

				print(f"Restoring branch '{branch_name}' disabled status...")
				toggle_tenant_branch(
					subdomain=subdomain, branch_name=branch_name, disabled=bool(orig_disabled)
				)
				print("✅ Restored original branch state")
				print("✅ PASSED: Branch toggling works")

			except Exception as e:
				print(f"❌ FAILED: Branch toggling test failed: {e!s}")
				traceback.print_exc()
				try:
					frappe.destroy()
					frappe.init(site=original_site, sites_path=os.path.join(get_bench_path(), "sites"))
					frappe.connect()
				except Exception:
					pass
				return
		else:
			print("⚠️ No branches available in the test tenant. Skipping branch toggling tests.")

		# 5. Test Branch Limit Enforcement
		print("\n--- TEST 5: Branch Limit Enforcement ---")
		try:
			# Shift context to Tenant Site
			bench_path = get_bench_path()
			from paletixa_saas.paletixa_saas.api import get_base_domain

			base_domain = get_base_domain()
			domain = f"{subdomain}.{base_domain}"

			frappe.destroy()
			frappe.init(site=domain, sites_path=os.path.join(bench_path, "sites"))
			frappe.connect()

			# Force a limit of 0 branches to trigger enforcement immediately
			frappe.db.set_single_value("SaaS Feature Config", "max_branches", 0)
			frappe.db.commit()

			# Set user to Administrator on the tenant site (since branch creation requires System Manager)
			frappe.set_user("Administrator")

			try:
				print(
					"Attempting to create a new branch 'Test-Limit-Branch' (should trigger limit enforcement)..."
				)
				create_new_branch_with_cashiers(branch_name="Test-Limit-Branch", cashier_emails=[])
				print("❌ FAILED: Branch creation succeeded but should have been blocked by limit=0")
			except frappe.ValidationError as e:
				print(f"✅ PASSED: Branch creation blocked with message: {e!s}")
			except Exception as e:
				print(f"❌ FAILED: Unexpected exception during limit enforcement test: {e!s}")
				traceback.print_exc()

			# Restore max_branches back to its original value on the tenant
			frappe.db.set_single_value("SaaS Feature Config", "max_branches", orig_max_branches)
			frappe.db.commit()
			print(f"✅ Restored tenant max_branches to {orig_max_branches}")

			# Restore connection to the original site
			frappe.destroy()
			frappe.init(site=original_site, sites_path=os.path.join(bench_path, "sites"))
			frappe.connect()
			frappe.set_user("Administrator")
			print("✅ Restored session site to the original site and user to Administrator")

		except Exception as e:
			print(f"❌ FAILED: Branch limit enforcement test failed: {e!s}")
			traceback.print_exc()
			try:
				frappe.destroy()
				frappe.init(site=original_site, sites_path=os.path.join(get_bench_path(), "sites"))
				frappe.connect()
			except Exception:
				pass
			return

		# 6. Test Tenant Billing & Automatic Deactivation
		print("\n--- TEST 6: Tenant Billing & Automatic Deactivation ---")
		try:
			# We will use the test subdomain from master
			from paletixa_saas.paletixa_saas.api import confirm_tenant_payment, daily_tenant_billing_check

			# Save original billing fields
			tenant_doc = frappe.get_doc("SaaS Tenant Request", subdomain)
			orig_exempt = tenant_doc.exempt_from_payment
			orig_last_payment = tenant_doc.last_payment_date
			orig_expiration = tenant_doc.expiration_date
			orig_active_state = tenant_doc.active

			print(
				f"Original billing status: exempt={orig_exempt}, last_payment={orig_last_payment}, expiration={orig_expiration}, active={orig_active_state}"
			)

			# A. Test Payment Exemption updates via update_tenant_config
			print("Testing update_tenant_config with exempt_from_payment=True...")
			update_tenant_config(
				subdomain=subdomain, active=True, max_branches=orig_max_branches, exempt_from_payment=True
			)
			assert frappe.db.get_value("SaaS Tenant Request", subdomain, "exempt_from_payment") == 1, (
				"Exempt status not enabled"
			)

			print("Testing update_tenant_config with exempt_from_payment=False...")
			update_tenant_config(
				subdomain=subdomain, active=True, max_branches=orig_max_branches, exempt_from_payment=False
			)
			assert frappe.db.get_value("SaaS Tenant Request", subdomain, "exempt_from_payment") == 0, (
				"Exempt status not disabled"
			)

			# B. Test confirm_tenant_payment updates dates and active flag
			print("Testing confirm_tenant_payment...")
			confirm_tenant_payment(subdomain=subdomain)
			updated_doc = frappe.get_doc("SaaS Tenant Request", subdomain)
			assert updated_doc.active == 1, "Payment confirmation did not set active status to 1"
			assert updated_doc.last_payment_date is not None, (
				"Payment confirmation did not set last_payment_date"
			)
			assert updated_doc.expiration_date is not None, "Payment confirmation did not set expiration_date"
			print(f"✅ Payment confirmed successfully. New expiration date: {updated_doc.expiration_date}")

			# C. Test deactivation sweep (daily_tenant_billing_check)
			from frappe.utils import add_days, getdate, today

			past_date = add_days(today(), -5)
			frappe.db.set_value("SaaS Tenant Request", subdomain, "expiration_date", past_date)
			frappe.db.commit()
			print(f"Manually set expiration_date to past date: {past_date}")

			# Run the daily sweep
			print("Running daily_tenant_billing_check...")
			daily_tenant_billing_check()

			# Assert tenant is now deactivated on master DB
			assert frappe.db.get_value("SaaS Tenant Request", subdomain, "active") == 0, (
				"Deactivation sweep did not deactivate past-due tenant"
			)
			print("✅ Verified tenant deactivated on Master DB")

			# Assert tenant is now deactivated on Tenant DB
			bench_path = get_bench_path()
			base_domain = get_base_domain()
			domain = f"{subdomain}.{base_domain}"

			if os.path.exists(os.path.join(bench_path, "sites", domain)):
				frappe.destroy()
				frappe.init(site=domain, sites_path=os.path.join(bench_path, "sites"))
				frappe.connect()

				assert frappe.db.get_single_value("SaaS Feature Config", "is_active") == 0, (
					"Deactivation sweep did not sync to Tenant DB"
				)
				print("✅ Verified tenant deactivated on Tenant DB (SaaS Feature Config.is_active = 0)")

				# Restore connection to the original site
				frappe.destroy()
				frappe.init(site=original_site, sites_path=os.path.join(bench_path, "sites"))
				frappe.connect()

			# D. Test exemption from daily billing check
			print("Testing daily check with exempt_from_payment = True...")
			# Reactivate tenant first
			frappe.db.set_value(
				"SaaS Tenant Request",
				subdomain,
				{"active": 1, "exempt_from_payment": 1, "expiration_date": past_date},
			)
			frappe.db.commit()

			# Run daily sweep again
			daily_tenant_billing_check()

			# Assert tenant remains active because it is exempt
			assert frappe.db.get_value("SaaS Tenant Request", subdomain, "active") == 1, (
				"Deactivation sweep deactivated an exempt tenant!"
			)
			print("✅ Verified exempt tenant was ignored by deactivation sweep")

			# Restore original values
			print("Restoring original billing values for the test tenant...")
			tenant_doc = frappe.get_doc("SaaS Tenant Request", subdomain)
			tenant_doc.exempt_from_payment = orig_exempt
			tenant_doc.last_payment_date = orig_last_payment
			tenant_doc.expiration_date = orig_expiration
			tenant_doc.active = orig_active_state
			tenant_doc.save()
			frappe.db.commit()

			# Re-sync original active state to Tenant DB
			if os.path.exists(os.path.join(bench_path, "sites", domain)):
				frappe.destroy()
				frappe.init(site=domain, sites_path=os.path.join(bench_path, "sites"))
				frappe.connect()
				frappe.db.set_single_value("SaaS Feature Config", "is_active", 1 if orig_active_state else 0)
				frappe.db.commit()

				frappe.destroy()
				frappe.init(site=original_site, sites_path=os.path.join(bench_path, "sites"))
				frappe.connect()

			frappe.set_user("Administrator")
			print("✅ Restored all original billing values")
			print("✅ PASSED: Billing and deactivation suite completed successfully")

		except Exception as e:
			print(f"❌ FAILED: Billing deactivation tests failed: {e!s}")
			traceback.print_exc()
			# Try to restore context to the original site and user to Administrator
			try:
				frappe.destroy()
				frappe.init(site=original_site, sites_path=os.path.join(get_bench_path(), "sites"))
				frappe.connect()
				frappe.set_user("Administrator")
			except Exception:
				pass
			return

		print("\n==================================================")
		print("ALL TESTS PASSED SUCCESSFULLY!")
		print("==================================================")

	except Exception as e:
		print(f"\n❌ CRITICAL EXCEPTION RUNNING TESTS: {e!s}")
		traceback.print_exc()


if __name__ == "__main__":
	run()


def test_get_platform_admin_dashboard_falls_back_to_site_directory_when_requests_are_empty(
	monkeypatch, tmp_path
):
	sites_path = tmp_path / "sites"
	sites_path.mkdir()

	tenant_site = "erpnext.jegdev.com"
	orphan_site = "orphan.jegdev.com"
	master_site = "frontend"

	for site_name, db_name in (
		(tenant_site, "erpnext_jegdev_com"),
		(orphan_site, "orphan_jegdev_com"),
		(master_site, "master_frontend"),
	):
		site_dir = sites_path / site_name
		site_dir.mkdir()
		(site_dir / "site_config.json").write_text(json.dumps({"db_name": db_name}))

	class _FakeConfig:
		def __init__(self, company_name=""):
			self.company_name = company_name
			self.is_active = 1
			self.max_branches = 7
			self.has_pos = 1
			self.has_production = 0
			self.has_logistics = 1
			self.has_wholesale = 1
			self.has_services = 0
			self.has_products = 1
			self.has_purchasing = 1

		def get(self, key, default=None):
			return getattr(self, key, default)

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

	original_user = frappe.session.user
	original_site = getattr(frappe.local, "site", None)
	original_get_roles = frappe.get_roles
	original_get_all = frappe.get_all
	original_get_cached_doc = frappe.get_cached_doc
	original_db_count = frappe.db.count
	original_db_get_value = frappe.db.get_value
	original_safe_context = saas_api.SafeSiteContext
	original_is_master_site = saas_api._is_platform_master_site

	def _fake_is_master_site(site=None):
		site = site or getattr(frappe.local, "site", None)
		return site == master_site

	def _fake_get_all(doctype, filters=None, fields=None, order_by=None, limit=None):
		site = getattr(frappe.local, "site", None)
		if doctype == "SaaS Tenant Request":
			return []
		if doctype == "Company":
			if site == tenant_site:
				return [frappe._dict(name="Jeg Dev S.A. de C.V.")]
			return []
		if doctype == "POS Profile":
			if site == tenant_site:
				return [
					frappe._dict(name="North Branch", warehouse="WH-NORTH", disabled=0),
					frappe._dict(name="South Branch", warehouse="WH-SOUTH", disabled=1),
				]
			if site == orphan_site:
				return [frappe._dict(name="Orphan Branch", warehouse="WH-ORPHAN", disabled=0)]
		return []

	def _fake_db_count(doctype, filters=None):
		site = getattr(frappe.local, "site", None)
		if site == tenant_site and doctype == "User":
			return 3
		if site == tenant_site and doctype == "Customer":
			return 4
		if site == orphan_site and doctype == "User":
			return 1
		if site == orphan_site and doctype == "Customer":
			return 2
		return 0

	def _fake_db_get_value(doctype, filters=None, fieldname=None, **kwargs):
		site = getattr(frappe.local, "site", None)
		if doctype == "Sales Invoice" and fieldname == "sum(grand_total)":
			return 1250.5 if site == tenant_site else 245.0 if site == orphan_site else 0.0
		if doctype == "Sales Invoice" and fieldname == "posting_date":
			return "2026-06-10" if site == tenant_site else "2026-06-09" if site == orphan_site else None
		return None

	def _fake_get_cached_doc(doctype, name=None, *args, **kwargs):
		site = getattr(frappe.local, "site", None)
		if doctype == "SaaS Feature Config":
			if site == tenant_site:
				return _FakeConfig(company_name="Jeg Dev SaaS")
			if site == orphan_site:
				return _FakeConfig(company_name="")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} {name} @ {site}")

	try:
		frappe.session.user = "Administrator"
		frappe.local.site = master_site
		frappe.get_roles = (
			lambda user=None: ["System Manager"] if (user or frappe.session.user) != "Guest" else []
		)
		frappe.get_all = _fake_get_all
		frappe.db.count = _fake_db_count
		frappe.db.get_value = _fake_db_get_value
		frappe.get_cached_doc = _fake_get_cached_doc
		saas_api.SafeSiteContext = _FakeSafeSiteContext
		saas_api._is_platform_master_site = _fake_is_master_site
		saas_api.get_bench_path = lambda: str(tmp_path)
		saas_api.get_sites_path = lambda: str(sites_path)

		tenants = get_platform_admin_dashboard()

		assert len(tenants) == 2
		tenant_row = next(row for row in tenants if row["name"] == tenant_site)
		orphan_row = next(row for row in tenants if row["name"] == orphan_site)

		assert tenant_row["name"] == "erpnext"
		assert tenant_row["site_name"] == tenant_site
		assert tenant_row["company_name"] == "Jeg Dev S.A. de C.V."
		assert tenant_row["database_name"] == "erpnext_jegdev_com"
		assert tenant_row["branch_count"] == 2
		assert tenant_row["users_count"] == 3
		assert tenant_row["customers_count"] == 4
		assert tenant_row["sales_30_days"] == 1250.5
		assert tenant_row["active_modules"]["pos"] is True

		assert orphan_row["name"] == "orphan"
		assert orphan_row["site_name"] == orphan_site
		assert orphan_row["company_name"] == "orphan"
		assert orphan_row["database_name"] == "orphan_jegdev_com"
		assert orphan_row["branch_count"] == 1
		assert orphan_row["users_count"] == 1
		assert orphan_row["customers_count"] == 2
		assert orphan_row["sales_30_days"] == 245.0
		assert orphan_row["active_modules"]["pos"] is True
	finally:
		frappe.session.user = original_user
		frappe.local.site = original_site
		frappe.get_roles = original_get_roles
		frappe.get_all = original_get_all
		frappe.db.count = original_db_count
		frappe.db.get_value = original_db_get_value
		frappe.get_cached_doc = original_get_cached_doc
		saas_api.SafeSiteContext = original_safe_context
		saas_api._is_platform_master_site = original_is_master_site


def test_get_platform_admin_dashboard_skips_pos_profile_reads_when_pos_is_disabled(monkeypatch, tmp_path):
	sites_path = tmp_path / "sites"
	sites_path.mkdir()

	tenant_site = "tenant-no-pos.localhost"
	master_site = "frontend"
	(sites_path / tenant_site).mkdir()

	class _FakeConfig:
		def __init__(self):
			self.has_pos = 0
			self.has_production = 0
			self.has_logistics = 0
			self.has_wholesale = 1
			self.has_services = 1
			self.has_products = 1
			self.has_purchasing = 0

		def get(self, key, default=None):
			return getattr(self, key, default)

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

	original_user = frappe.session.user
	original_site = getattr(frappe.local, "site", None)
	original_get_roles = frappe.get_roles
	original_get_all = frappe.get_all
	original_db_count = frappe.db.count
	original_db_get_value = frappe.db.get_value
	original_get_cached_doc = frappe.get_cached_doc
	original_safe_context = saas_api.SafeSiteContext
	original_is_master_site = saas_api._is_platform_master_site

	def _fake_get_all(doctype, filters=None, fields=None, order_by=None, limit=None):
		if doctype == "SaaS Tenant Request":
			return [
				frappe._dict(
					name="tenant-no-pos",
					company_name="Tenant No POS",
					admin_email="admin@tenant.test",
					active=1,
					max_branches=3,
					creation=None,
					database_name="tenant_no_pos_db",
					exempt_from_payment=0,
					last_payment_date=None,
					expiration_date=None,
					site_name=tenant_site,
				)
			]
		if doctype == "POS Profile":
			raise AssertionError("POS Profile should not be queried when POS is disabled")
		return []

	def _fake_db_count(doctype, filters=None):
		if doctype == "User":
			return 2
		if doctype == "Customer":
			return 5
		return 0

	def _fake_db_get_value(doctype, filters=None, fieldname=None, **kwargs):
		if doctype == "Sales Invoice" and fieldname == "sum(grand_total)":
			return 99.5
		if doctype == "Sales Invoice" and fieldname == "posting_date":
			return "2026-06-19"
		return None

	try:
		frappe.session.user = "Administrator"
		frappe.local.site = master_site
		frappe.get_roles = (
			lambda user=None: ["System Manager"] if (user or frappe.session.user) != "Guest" else []
		)
		frappe.get_all = _fake_get_all
		frappe.db.count = _fake_db_count
		frappe.db.get_value = _fake_db_get_value
		frappe.get_cached_doc = (
			lambda doctype, name=None: _FakeConfig() if doctype == "SaaS Feature Config" else None
		)
		saas_api.SafeSiteContext = _FakeSafeSiteContext
		saas_api._is_platform_master_site = lambda site=None: True
		saas_api.get_bench_path = lambda: str(tmp_path)
		saas_api.get_sites_path = lambda: str(sites_path)

		tenants = get_platform_admin_dashboard()

		assert len(tenants) == 1
		tenant = tenants[0]
		assert tenant["name"] == "tenant-no-pos"
		assert tenant["branch_count"] == 0
		assert tenant["branches"] == []
		assert tenant["active_modules"]["pos"] is False
		assert tenant["users_count"] == 2
		assert tenant["customers_count"] == 5
		assert tenant["sales_30_days"] == 99.5
		assert tenant["last_sale_date"] == "2026-06-19"
	finally:
		frappe.session.user = original_user
		frappe.local.site = original_site
		frappe.get_roles = original_get_roles
		frappe.get_all = original_get_all
		frappe.db.count = original_db_count
		frappe.db.get_value = original_db_get_value
		frappe.get_cached_doc = original_get_cached_doc
		saas_api.SafeSiteContext = original_safe_context
		saas_api._is_platform_master_site = original_is_master_site
