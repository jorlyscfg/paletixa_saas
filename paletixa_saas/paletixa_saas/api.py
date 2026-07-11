import json
import secrets
import time
import traceback
from contextlib import contextmanager

import frappe

from paletixa_saas.config.infrastructure import (
	_is_development_context,
)
from paletixa_saas.config.infrastructure import (
	get_bench_path as _get_bench_path,
)
from paletixa_saas.config.infrastructure import (
	get_db_root_credentials as _get_db_root_credentials,
)
from paletixa_saas.config.infrastructure import (
	get_reserved_subdomains as _get_reserved_subdomains,
)
from paletixa_saas.config.infrastructure import (
	get_sites_path as _get_sites_path,
)
from paletixa_saas.config.infrastructure import (
	is_platform_master_site as _is_platform_master_site,
)
from paletixa_saas.config.infrastructure import (
	resolve_platform_master_sites as _resolve_platform_master_sites,
)
from paletixa_saas.config.platform_defaults import (
	_validate_platform_distribution_warehouse,
	ensure_platform_payment_mode,
	get_platform_company_abbr,
	get_platform_company_name,
	get_platform_distribution_warehouse,
	get_platform_payment_account,
)
from paletixa_saas.paletixa_saas.event_reservation_service import (
	get_event_reservation_production_demand as _get_event_reservation_production_demand,
)
from paletixa_saas.paletixa_saas.event_reservation_service import (
	validate_confirmed_allocation_warehouse,
)


def is_tenant_admin_user(user=None):
	user = user or frappe.session.user
	if not user or user == "Guest":
		return False

	normalized_user = user.lower()
	return (
		user == "Administrator"
		or normalized_user.startswith("admin@")
		or normalized_user.startswith("admin.")
	)


def is_service_operator_user(user=None):
	user = user or frappe.session.user
	if not user or user == "Guest":
		return False

	if is_tenant_admin_user(user):
		return True

	normalized_user = user.lower()
	return (
		normalized_user.startswith("cajero.")
		or normalized_user.startswith("produccion@")
		or normalized_user.startswith("logistica@")
	)


def _is_system_manager(user=None):
	user = user or frappe.session.user
	if not user or user == "Guest":
		return False

	return "System Manager" in frappe.get_roles(user)


def _user_has_pos_profile_access(pos_profile, user=None):
	user = user or frappe.session.user
	if not user or user == "Guest":
		return False

	if _is_system_manager(user):
		return True

	return bool(frappe.db.exists("POS Profile User", {"parent": pos_profile, "user": user}))


def _get_primary_master_site():
	master_sites = sorted(_resolve_platform_master_sites())
	preferred_sites = ("frontend.localhost", "frontend", "frontend.local", "erpadmin")
	for preferred_site in preferred_sites:
		if preferred_site in master_sites:
			return preferred_site
	if not master_sites:
		frappe.throw(
			frappe._(
				"Faltan los sitios maestros de la plataforma. Configurá platform_master_sites en site_config.json o PLATFORM_MASTER_SITES."
			),
			frappe.ValidationError,
		)
	return master_sites[0]


def _resolve_workspace_id(subdomain=None, workspace_id=None):
	identifier = workspace_id or subdomain
	if not identifier:
		return None
	return identifier.lower().strip()


def _reservations_are_active(config, has_reservations=None):
	if has_reservations is None:
		return bool(frappe.utils.cint(config.get("has_reservations")))
	return bool(frappe.utils.cint(has_reservations))


def _reservations_disabled_response():
	return {"success": False, "error": frappe._("El módulo de reservas está deshabilitado.")}


def _event_booking_attempt_key(guest_phone, guest_name):
	remote_addr = getattr(getattr(frappe.local, "request", None), "remote_addr", None) or "unknown"
	identity = normalize_phone_number(guest_phone or guest_name or "anonymous")
	return f"event_booking_attempts:{remote_addr}:{identity}"


def _event_booking_is_rate_limited(guest_phone, guest_name):
	now = int(time.time())
	cache_key = _event_booking_attempt_key(guest_phone, guest_name)
	history = frappe.cache().get_value(cache_key) or []
	history = [ts for ts in history if now - int(ts) < 600]
	if len(history) >= 5:
		return True
	history.append(now)
	frappe.cache().set_value(cache_key, history)
	return False


def _services_are_active(config):
	if config is None:
		return True
	has_services = config.get("has_services")
	return bool(frappe.utils.cint(has_services if has_services is not None else 1))


def _wholesale_is_active(config):
	if config is None:
		return True
	has_wholesale = config.get("has_wholesale")
	return bool(frappe.utils.cint(has_wholesale if has_wholesale is not None else 1))


def _wholesale_disabled_response():
	return {
		"success": False,
		"error": frappe._("El módulo de mayoristas está deshabilitado."),
	}


def _reservations_activation_requirements():
	return [
		{
			"fieldname": "reservation_item_code",
			"label": "Artículo Reservable",
			"fieldtype": "Link",
			"options": "Item",
			"required": True,
			"description": "Artículo físico que se reserva para cada evento.",
		},
		{
			"fieldname": "max_reservation_assets",
			"label": "Cantidad Total de Carritos / Recursos",
			"fieldtype": "Int",
			"required": True,
			"description": "Cantidad total disponible para reservar.",
		},
		{
			"fieldname": "default_event_items",
			"label": "Plantilla de Productos Pre-cargados",
			"fieldtype": "Text",
			"required": True,
			"description": "Lista JSON de productos que se precargan en la reserva.",
		},
	]


def _reservations_suggested_dependencies():
	return [
		{
			"feature": "has_products",
			"label": "Productos e Inventario",
			"required": True,
			"reason": "Reservas usa el catálogo para precargar productos y completar la entrega.",
		},
		{
			"feature": "has_wholesale",
			"label": "Venta Mayorista",
			"required": False,
			"reason": "La confirmación de reservas reutiliza el flujo de pedidos y facturación.",
		},
	]


def _reservations_activation_contract(config=None):
	reservations_active = bool(config and _reservations_are_active(config))
	return {
		"module": "reservations",
		"activation": {
			"current_status": {
				"enabled": reservations_active,
				"state": "enabled" if reservations_active else "disabled",
			},
			"required_fields": _reservations_activation_requirements(),
			"suggested_dependencies": _reservations_suggested_dependencies(),
		},
	}


def _saas_feature_config_has_field(fieldname):
	try:
		return bool(frappe.get_meta("SaaS Feature Config").has_field(fieldname))
	except Exception:
		return False


def _safe_is_platform_master_site(site_name=None):
	try:
		if site_name is None:
			return bool(_is_platform_master_site())
		return bool(_is_platform_master_site(site_name))
	except Exception:
		return False


def _saas_debug_context():
	request = getattr(frappe.local, "request", None)
	cookies = getattr(request, "cookies", None)
	form_dict = getattr(frappe.local, "form_dict", None)
	cmd = getattr(form_dict, "cmd", None)
	if cmd is None and hasattr(form_dict, "get"):
		cmd = form_dict.get("cmd")
	return {
		"site": getattr(frappe.local, "site", None),
		"user": getattr(frappe.session, "user", None),
		"cmd": cmd,
		"path": getattr(request, "path", None),
		"tenant_name": cookies.get("tenant_name") if hasattr(cookies, "get") else None,
	}


def _saas_debug_log(event, **context):
	if not _is_development_context():
		return

	payload = _saas_debug_context()
	payload.update(context)
	frappe.logger("paletixa_saas").warning(
		f"[saas-debug] {event} {json.dumps(payload, default=str, ensure_ascii=True, sort_keys=True)}"
	)


def check_tenant_admin_permission():
	_saas_debug_log("check_tenant_admin_permission:start")
	user = frappe.session.user
	if not user or user == "Guest":
		_saas_debug_log("check_tenant_admin_permission:deny", reason="guest")
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if _safe_is_platform_master_site():
		_saas_debug_log("check_tenant_admin_permission:deny", reason="master_site")
		frappe.throw(
			frappe._("Acceso denegado: Este endpoint solo está disponible en sitios de tenant."),
			frappe.PermissionError,
		)

	roles = frappe.get_roles(user)
	is_tenant_admin = is_tenant_admin_user(user)
	is_system_manager = "System Manager" in roles
	_saas_debug_log(
		"check_tenant_admin_permission:resolved",
		roles=roles,
		is_tenant_admin=is_tenant_admin,
		is_system_manager=is_system_manager,
	)

	if is_tenant_admin or is_system_manager:
		_saas_debug_log("check_tenant_admin_permission:allow")
		return

	_saas_debug_log("check_tenant_admin_permission:deny", reason="missing_role")
	frappe.throw(frappe._("No tenés permisos para realizar esta acción"), frappe.PermissionError)


def setup_company_identity_fields():
	# Evitar consultas redundantes usando la caché de Redis por sitio
	cache_key = f"saas_fields_setup_done:v2:{frappe.local.site}"
	if frappe.cache().get_value(cache_key):
		return

	fields = [
		{
			"fieldname": "company_name",
			"label": "Company Name",
			"fieldtype": "Data",
			"insert_after": "client_logo",
			"default": "",
		},
		{
			"fieldname": "company_tax_id",
			"label": "Company Tax ID",
			"fieldtype": "Data",
			"insert_after": "company_name",
			"default": "",
		},
		{
			"fieldname": "company_address",
			"label": "Company Address",
			"fieldtype": "Small Text",
			"insert_after": "company_tax_id",
			"default": "",
		},
		{
			"fieldname": "company_phone",
			"label": "Company Phone",
			"fieldtype": "Data",
			"insert_after": "company_address",
			"default": "",
		},
		{
			"fieldname": "company_email",
			"label": "Company Email",
			"fieldtype": "Data",
			"insert_after": "company_phone",
			"default": "",
		},
		{
			"fieldname": "company_abbr",
			"label": "Company Abbreviation",
			"fieldtype": "Data",
			"insert_after": "company_email",
			"default": "",
		},
		{
			"fieldname": "default_distribution_warehouse",
			"label": "Default Distribution Warehouse",
			"fieldtype": "Link",
			"options": "Warehouse",
			"insert_after": "company_abbr",
			"default": "",
		},
		{
			"fieldname": "default_cash_account",
			"label": "Default Cash Account",
			"fieldtype": "Link",
			"options": "Account",
			"insert_after": "default_distribution_warehouse",
			"default": "",
		},
		{
			"fieldname": "default_bank_account",
			"label": "Default Bank Account",
			"fieldtype": "Link",
			"options": "Account",
			"insert_after": "default_cash_account",
			"default": "",
		},
		{
			"fieldname": "ticket_header",
			"label": "Ticket Header",
			"fieldtype": "Small Text",
			"insert_after": "default_bank_account",
			"default": "",
		},
		{
			"fieldname": "ticket_footer",
			"label": "Ticket Footer",
			"fieldtype": "Small Text",
			"insert_after": "ticket_header",
			"default": "",
		},
		{
			"fieldname": "print_logo",
			"label": "Print Logo on Ticket",
			"fieldtype": "Check",
			"insert_after": "ticket_footer",
			"default": "1",
		},
		{
			"fieldname": "print_tax_id",
			"label": "Print Tax ID on Ticket",
			"fieldtype": "Check",
			"insert_after": "print_logo",
			"default": "1",
		},
		{
			"fieldname": "print_address",
			"label": "Print Address on Ticket",
			"fieldtype": "Check",
			"insert_after": "print_tax_id",
			"default": "1",
		},
		{
			"fieldname": "print_contact",
			"label": "Print Contact on Ticket",
			"fieldtype": "Check",
			"insert_after": "print_address",
			"default": "1",
		},
	]
	created = []
	for f in fields:
		name = f"SaaS Feature Config-{f['fieldname']}"
		if _saas_feature_config_has_field(f["fieldname"]):
			continue
		if not frappe.db.exists("Custom Field", name):
			doc = frappe.get_doc(
				{
					"doctype": "Custom Field",
					"dt": "SaaS Feature Config",
					"fieldname": f["fieldname"],
					"label": f["label"],
					"fieldtype": f["fieldtype"],
					**({"options": f["options"]} if f.get("options") else {}),
					"insert_after": f["insert_after"],
					"default": f["default"],
				}
			)
			doc.insert(ignore_permissions=True)
			created.append(f["fieldname"])

	if created:
		frappe.db.commit()
		frappe.clear_cache(doctype="SaaS Feature Config")

	try:
		config = frappe.get_cached_doc("SaaS Feature Config")
	except Exception:
		config = None

	if _services_are_active(config):
		setup_service_role_permissions()
	frappe.cache().set_value(cache_key, 1)


@frappe.whitelist(allow_guest=True)
def get_features():
	try:
		setup_company_identity_fields()
		config = frappe.get_cached_doc("SaaS Feature Config")
		reservations_active = _reservations_are_active(config)
		can_expose_admin_defaults = is_tenant_admin_user() or _is_system_manager()
		if can_expose_admin_defaults and (config.get("default_distribution_warehouse") or "").strip():
			_validate_platform_distribution_warehouse(config.get("default_distribution_warehouse"))
		features_payload = {
			"client_name": get_platform_company_name(),
			"colors": {
				"primary": config.primary_color or "#1abc9c",
			},
			"features": {
				"pos": bool(config.has_pos),
				"production": bool(config.has_production),
				"logistics": bool(config.has_logistics),
				"reservations": bool(config.get("has_reservations", 0)),
				"wholesale": bool(config.get("has_wholesale", 1)),
				"mexico_taxes": bool(config.get("has_mexico_taxes", 0)),
				"services": bool(config.get("has_services") if config.get("has_services") is not None else 1),
				"products": bool(config.get("has_products") if config.get("has_products") is not None else 1),
				"purchasing": bool(config.get("has_purchasing", 0)),
			},
			"reservation_item_code": (config.get("reservation_item_code") or "Carrito Paletero")
			if reservations_active
			else "",
			"max_reservation_assets": int(config.get("max_reservation_assets") or 0)
			if reservations_active
			else 0,
			"default_event_items": (config.get("default_event_items") or "[]")
			if reservations_active
			else "[]",
			"custom_country": config.get("custom_country") or "Mexico",
			"custom_currency": config.get("custom_currency") or "MXN",
			# Company identity and ticket runtime defaults
			"company_name": config.get("company_name") or get_platform_company_name(),
			"company_logo": config.get("company_logo") or config.get("client_logo") or "",
			"company_tax_id": config.get("company_tax_id") or "",
			"company_address": config.get("company_address") or "",
			"company_phone": config.get("company_phone") or "",
			"company_email": config.get("company_email") or "",
			"ticket_header": config.get("ticket_header") or "",
			"ticket_footer": config.get("ticket_footer") or "",
			"print_logo": bool(config.get("print_logo", 1)),
			"print_tax_id": bool(config.get("print_tax_id", 1)),
			"print_address": bool(config.get("print_address", 1)),
			"print_contact": bool(config.get("print_contact", 1)),
			"is_active": bool(config.get("is_active", 1) if config.get("is_active") is not None else 1),
			"max_branches": int(
				config.get("max_branches", 3) if config.get("max_branches") is not None else 3
			),
		}
		if can_expose_admin_defaults:
			features_payload["default_distribution_warehouse"] = (
				config.get("default_distribution_warehouse") or ""
			)
		return features_payload
	except frappe.ValidationError as e:
		return {
			"error": str(e),
			"setup_required": True,
			"client_name": "",
			"colors": {
				"primary": "#1abc9c",
			},
			"features": {
				"pos": False,
				"production": False,
				"logistics": False,
				"reservations": False,
				"wholesale": False,
				"mexico_taxes": False,
				"services": False,
				"products": False,
				"purchasing": False,
			},
			"reservation_item_code": "",
			"max_reservation_assets": 0,
			"default_event_items": "[]",
			"custom_country": "Mexico",
			"custom_currency": "MXN",
			"company_name": "",
			"company_logo": "",
			"company_tax_id": "",
			"company_address": "",
			"company_phone": "",
			"company_email": "",
			"ticket_header": "",
			"ticket_footer": "",
			"print_logo": False,
			"print_tax_id": False,
			"print_address": False,
			"print_contact": False,
			"is_active": False,
			"max_branches": 0,
		}
	except Exception as e:
		return {
			"error": str(e),
			"setup_required": True,
			"client_name": "",
			"colors": {
				"primary": "#1abc9c",
			},
			"features": {
				"pos": False,
				"production": False,
				"logistics": False,
				"reservations": False,
				"wholesale": False,
				"mexico_taxes": False,
				"services": False,
				"products": False,
				"purchasing": False,
			},
			"reservation_item_code": "",
			"max_reservation_assets": 0,
			"default_event_items": "[]",
			"custom_country": "Mexico",
			"custom_currency": "MXN",
			"company_name": "",
			"company_logo": "",
			"company_tax_id": "",
			"company_address": "",
			"company_phone": "",
			"company_email": "",
			"ticket_header": "",
			"ticket_footer": "",
			"print_logo": False,
			"print_tax_id": False,
			"print_address": False,
			"print_contact": False,
			"is_active": False,
			"max_branches": 0,
		}


@frappe.whitelist()
def get_reservations_activation_contract():
	check_tenant_admin_permission()
	try:
		config = frappe.get_cached_doc("SaaS Feature Config")
	except Exception:
		config = None
	return _reservations_activation_contract(config)


def sync_event_warehouses(company_name, max_assets):
	try:
		company_doc = frappe.get_cached_doc("Company", company_name)
	except Exception:
		frappe.throw(frappe._("La compañía {0} no existe.").format(company_name), frappe.DoesNotExistError)
	company_abbr = (company_doc.abbr or "").strip()
	if not company_abbr:
		frappe.throw(
			frappe._("La compañía {0} no tiene abreviatura configurada.").format(company_name),
			frappe.ValidationError,
		)
	parent_group_name = f"Carritos de Eventos - {company_abbr}"

	# Asegurar que exista el grupo padre de almacenes
	if not frappe.db.exists("Warehouse", parent_group_name):
		parent_doc = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": "Carritos de Eventos",
				"is_group": 1,
				"parent_warehouse": f"All Warehouses - {company_abbr}",
				"company": company_name,
			}
		)
		parent_doc.flags.ignore_permissions = True
		parent_doc.insert(ignore_permissions=True)
		frappe.db.commit()

	# Escalar almacenes según max_assets
	existing_warehouses = frappe.get_all(
		"Warehouse",
		filters={"parent_warehouse": parent_group_name, "company": company_name},
		fields=["name", "warehouse_name", "disabled"],
	)

	existing_map = {}
	for ew in existing_warehouses:
		name_parts = ew.warehouse_name.split()
		if len(name_parts) >= 2 and name_parts[1].isdigit():
			num = int(name_parts[1])
			existing_map[num] = ew

	# Habilitar o crear los almacenes hasta max_assets
	for i in range(1, max_assets + 1):
		w_name = f"Carrito {i}"

		if i in existing_map:
			ew = existing_map[i]
			if ew.disabled:
				doc = frappe.get_doc("Warehouse", ew.name)
				doc.disabled = 0
				doc.flags.ignore_permissions = True
				doc.save(ignore_permissions=True)
		else:
			doc = frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": w_name,
					"is_group": 0,
					"parent_warehouse": parent_group_name,
					"company": company_name,
				}
			)
			doc.flags.ignore_permissions = True
			doc.insert(ignore_permissions=True)

	# Deshabilitar los almacenes mayores a max_assets
	for num, ew in existing_map.items():
		if num > max_assets:
			if not ew.disabled:
				actual_qty = (
					frappe.db.sql(
						"""
                    SELECT SUM(actual_qty)
                    FROM `tabBin`
                    WHERE warehouse = %s
                """,
						(ew.name,),
					)[0][0]
					or 0
				)

				if actual_qty > 0:
					frappe.throw(
						frappe._(
							"No se puede disminuir el límite de carritos porque el '{0}' aún tiene {1} productos registrados físicamente. Realice el traspaso de material correspondiente antes de deshabilitarlo."
						).format(ew.warehouse_name, actual_qty)
					)

				doc = frappe.get_doc("Warehouse", ew.name)
				doc.disabled = 1
				doc.flags.ignore_permissions = True
				doc.save(ignore_permissions=True)

	frappe.db.commit()
	frappe.clear_cache(doctype="Warehouse")


@frappe.whitelist()
def update_saas_config(
	primary_color=None,
	has_pos=None,
	has_production=None,
	has_logistics=None,
	has_reservations=None,
	reservation_item_code=None,
	max_reservation_assets=None,
	default_event_items=None,
	custom_country=None,
	custom_currency=None,
	has_wholesale=None,
	company_name=None,
	company_abbr=None,
	default_distribution_warehouse=None,
	default_cash_account=None,
	default_bank_account=None,
	company_logo=None,
	company_tax_id=None,
	company_address=None,
	company_phone=None,
	company_email=None,
	ticket_header=None,
	ticket_footer=None,
	print_logo=None,
	print_tax_id=None,
	print_address=None,
	print_contact=None,
	has_mexico_taxes=None,
	has_services=None,
	has_products=None,
	has_purchasing=None,
):
	_saas_debug_log(
		"update_saas_config:start",
		incoming_company_name=company_name,
		incoming_company_abbr=company_abbr,
		has_company_logo=company_logo is not None,
		has_company_address=company_address is not None,
	)
	check_tenant_admin_permission()

	# Asegurar que existan los campos de marca
	setup_company_identity_fields()
	config = frappe.get_doc("SaaS Feature Config")
	requested_company_name = (company_name or "").strip()
	if requested_company_name:
		effective_company_name = requested_company_name
		_saas_debug_log(
			"update_saas_config:company_name_from_request",
			requested_company_name=requested_company_name,
		)
	else:
		effective_company_name = get_platform_company_name()
		_saas_debug_log(
			"update_saas_config:company_name_from_platform_default",
			resolved_company_name=effective_company_name,
			reason="fresh_tenant_or_blank_company_name",
		)

	_saas_debug_log(
		"update_saas_config:company_resolved",
		requested_company_name=requested_company_name,
		effective_company_name=effective_company_name,
		current_config_company_name=config.get("company_name"),
	)
	current_reservations_active = _reservations_are_active(config)
	reservations_active_after_update = _reservations_are_active(config, has_reservations)

	if primary_color is not None:
		config.primary_color = primary_color

	if has_pos is not None:
		config.has_pos = int(has_pos)

	if has_production is not None:
		config.has_production = int(has_production)

	if has_logistics is not None:
		config.has_logistics = int(has_logistics)

	if has_reservations is not None:
		config.has_reservations = int(has_reservations)

	if has_wholesale is not None:
		config.has_wholesale = int(has_wholesale)

	if has_services is not None:
		config.has_services = int(has_services)

	if has_products is not None:
		config.has_products = int(has_products)

	if reservations_active_after_update:
		if reservation_item_code is not None:
			config.reservation_item_code = reservation_item_code

		if max_reservation_assets is not None:
			current_max_raw = config.get("max_reservation_assets")
			try:
				current_max = int(current_max_raw or 0)
			except (TypeError, ValueError):
				current_max = 0
			try:
				new_max = int(max_reservation_assets)
			except (TypeError, ValueError):
				new_max = current_max
			else:
				if current_reservations_active or reservations_active_after_update:
					sync_event_warehouses(effective_company_name, new_max)
				config.max_reservation_assets = new_max

		if default_event_items is not None:
			config.default_event_items = default_event_items
	else:
		config.reservation_item_code = ""
		config.max_reservation_assets = 0
		config.default_event_items = "[]"

	if custom_country is not None:
		config.custom_country = custom_country
		if frappe.db.exists("Company", effective_company_name):
			frappe.db.set_value("Company", effective_company_name, "country", custom_country)

	if custom_currency is not None:
		config.custom_currency = custom_currency
		if frappe.db.exists("Company", effective_company_name):
			frappe.db.set_value("Company", effective_company_name, "default_currency", custom_currency)
		for pl in ["Standard Selling", "Standard Wholesale"]:
			if frappe.db.exists("Price List", pl):
				frappe.db.set_value("Price List", pl, "currency", custom_currency)

	# Company Identity & Ticket Printing Custom Fields
	if company_name is not None:
		config.company_name = company_name
		if company_abbr is None and frappe.db.exists("Company", company_name):
			config.company_abbr = frappe.get_cached_doc("Company", company_name).abbr or ""
	if company_abbr is not None:
		config.company_abbr = company_abbr
	if default_distribution_warehouse is not None:
		config.default_distribution_warehouse = _validate_platform_distribution_warehouse(
			default_distribution_warehouse,
			company_name=effective_company_name,
		)
	if default_cash_account is not None:
		config.default_cash_account = default_cash_account
	if default_bank_account is not None:
		config.default_bank_account = default_bank_account
	if company_logo is not None:
		config.company_logo = company_logo
		config.client_logo = company_logo  # Mantener sincronizado por si acaso
	if company_tax_id is not None:
		config.company_tax_id = company_tax_id
	if company_address is not None:
		config.company_address = company_address
	if company_phone is not None:
		config.company_phone = company_phone
	if company_email is not None:
		config.company_email = company_email
	if ticket_header is not None:
		config.ticket_header = ticket_header
	if ticket_footer is not None:
		config.ticket_footer = ticket_footer
	if print_logo is not None:
		config.print_logo = int(print_logo)
	if print_tax_id is not None:
		config.print_tax_id = int(print_tax_id)
	if print_address is not None:
		config.print_address = int(print_address)
	if print_contact is not None:
		config.print_contact = int(print_contact)

	if has_mexico_taxes is not None:
		config.has_mexico_taxes = int(has_mexico_taxes)

	if has_purchasing is not None:
		config.has_purchasing = int(has_purchasing)

	config.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache(doctype="SaaS Feature Config")
	_saas_debug_log(
		"update_saas_config:success",
		effective_company_name=effective_company_name,
		stored_company_name=config.get("company_name"),
	)

	return {"success": True, "config": config.as_dict()}


@frappe.whitelist()
def activate_mexican_taxes(company_name=None):
	check_tenant_admin_permission()

	requested_company_name = (company_name or "").strip()
	effective_company_name = requested_company_name or get_platform_company_name()

	setup_mexican_taxes_and_fields(effective_company_name)

	config = frappe.get_doc("SaaS Feature Config")
	config.has_mexico_taxes = 1
	config.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache(doctype="SaaS Feature Config")

	return {"success": True, "company_name": effective_company_name}


def clean_old_image_file(doc, method=None):
	old_image = frappe.db.get_value("Item", doc.name, "image")
	if old_image and old_image != doc.get("image"):
		file_doc = frappe.db.get_value("File", {"file_url": old_image}, "name")
		if file_doc:
			try:
				frappe.delete_doc("File", file_doc, ignore_permissions=True)
				frappe.db.commit()
			except Exception as e:
				frappe.log_error(message=str(e), title="Error deleting old product image file")


@frappe.whitelist(allow_guest=True)
def get_templates():
	# Obtener todos los items activos que tengan variantes habilitadas
	templates = frappe.get_all(
		"Item", filters={"has_variants": 1, "disabled": 0}, fields=["name", "item_name", "item_group"]
	)
	return templates


@frappe.whitelist(allow_guest=True)
def get_item_barcodes():
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if not (
		is_tenant_admin_user()
		or frappe.session.user.startswith("cajero.")
		or frappe.session.user.startswith("produccion@")
		or frappe.session.user.startswith("logistica@")
	):
		frappe.throw(frappe._("No tenés permisos para acceder a este recurso."), frappe.PermissionError)

	return frappe.get_all("Item Barcode", fields=["parent", "barcode"], limit=1000)


@frappe.whitelist(allow_guest=True)
def get_active_items():
	config = frappe.get_cached_doc("SaaS Feature Config")
	if not _reservations_are_active(config):
		return _reservations_disabled_response()

	item_code = config.get("reservation_item_code") or "Carrito Paletero"
	subgroups = ["Products"] + [
		g.name for g in frappe.get_all("Item Group", filters={"parent_item_group": "Products"})
	]
	return frappe.get_all(
		"Item",
		filters={
			"disabled": 0,
			"item_group": ["in", subgroups],
			"has_variants": 0,
			"name": ["!=", item_code],
		},
		fields=["name", "item_name", "item_group", "standard_rate", "image"],
		limit=100,
	)


@frappe.whitelist(allow_guest=True)
def get_attributes(template_name):
	if not frappe.db.exists("Item", template_name):
		frappe.throw(frappe._("La plantilla especificada no existe"), frappe.DoesNotExistError)

	# Obtener atributos asociados a la plantilla en tabItem Variant Attribute
	attrs = frappe.get_all("Item Variant Attribute", filters={"parent": template_name}, fields=["attribute"])

	result = []
	seen = set()
	for a in attrs:
		attr_name = a.attribute
		if attr_name in seen:
			continue
		seen.add(attr_name)

		# Obtener valores permitidos para este atributo en tabItem Attribute Value
		values = frappe.get_all(
			"Item Attribute Value", filters={"parent": attr_name}, fields=["attribute_value", "abbr"]
		)
		result.append(
			{"attribute": attr_name, "values": [{"value": v.attribute_value, "abbr": v.abbr} for v in values]}
		)
	return result


@frappe.whitelist()
def create_custom_variant(
	template_name,
	attribute_values,
	retail_price,
	wholesale_price=None,
	image=None,
	barcode=None,
	item_group=None,
):
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	# Validar permisos generales de creación de items
	if not frappe.has_permission("Item", "write"):
		frappe.throw(frappe._("No tenés permisos para crear o modificar productos"), frappe.PermissionError)

	# Parsear los atributos pasados desde el cliente
	if isinstance(attribute_values, str):
		attribute_values = frappe.parse_json(attribute_values)

	if not attribute_values:
		frappe.throw(frappe._("Debés especificar los valores de los atributos para la variante"))

	# Validar que los precios sean correctos
	try:
		retail_price = float(retail_price)
	except (ValueError, TypeError):
		frappe.throw(frappe._("El precio menudeo ingresado es inválido"))

	if wholesale_price not in (None, "", "*No aplica*", "null", "undefined"):
		try:
			wholesale_price = float(wholesale_price)
		except (ValueError, TypeError):
			frappe.throw(frappe._("El precio mayoreo ingresado es inválido"))
	else:
		wholesale_price = None

	from erpnext.controllers.item_variant import create_variant

	frappe.db.begin()
	try:
		# 1. Crear el documento variante de ERPNext
		variant_doc = create_variant(template_name, attribute_values)
		item_code = variant_doc.item_code or variant_doc.name

		# Si el producto ya existe en la base de datos
		if frappe.db.exists("Item", item_code):
			existing_item = frappe.get_doc("Item", item_code)
			if not existing_item.disabled:
				frappe.throw(frappe._("El producto '{0}' ya existe y está activo.").format(item_code))
			else:
				# Si existe pero está inactivo, lo reactivamos
				existing_item.disabled = 0
				if item_group:
					existing_item.item_group = item_group
				if image:
					existing_item.image = image

				if barcode:
					existing_item.set("barcodes", [])
					existing_item.append("barcodes", {"barcode": barcode.strip(), "uom": "Unit"})

				existing_item.save(ignore_permissions=True)

				# Actualizar o asignar precios
				# Precio minorista (Standard Selling)
				retail_price_name = frappe.db.get_value(
					"Item Price", {"item_code": item_code, "price_list": "Standard Selling"}, "name"
				)
				if retail_price_name:
					frappe.db.set_value("Item Price", retail_price_name, "price_list_rate", retail_price)
				else:
					p_retail = frappe.new_doc("Item Price")
					p_retail.price_list = "Standard Selling"
					p_retail.item_code = item_code
					p_retail.price_list_rate = retail_price
					p_retail.insert(ignore_permissions=True)

				# Precio mayorista (Standard Wholesale)
				wholesale_price_name = frappe.db.get_value(
					"Item Price", {"item_code": item_code, "price_list": "Standard Wholesale"}, "name"
				)
				if wholesale_price is not None:
					if wholesale_price_name:
						frappe.db.set_value(
							"Item Price", wholesale_price_name, "price_list_rate", wholesale_price
						)
					else:
						p_wholesale = frappe.new_doc("Item Price")
						p_wholesale.price_list = "Standard Wholesale"
						p_wholesale.item_code = item_code
						p_wholesale.price_list_rate = wholesale_price
						p_wholesale.insert(ignore_permissions=True)
				elif wholesale_price_name:
					frappe.delete_doc("Item Price", wholesale_price_name, ignore_permissions=True)

				frappe.db.commit()
				frappe.clear_cache(doctype="Item")
				frappe.clear_cache(doctype="Item Price")

				return {
					"success": True,
					"item_code": item_code,
					"item_name": existing_item.item_name,
					"retail_price": retail_price,
					"wholesale_price": wholesale_price,
				}

		# Sobrescribir UOM estándar de ERPNext a "Unit"
		variant_doc.stock_uom = "Unit"

		if item_group:
			variant_doc.item_group = item_group

		if image:
			variant_doc.image = image

		if barcode:
			variant_doc.append("barcodes", {"barcode": barcode.strip(), "uom": "Unit"})

		# Insertar en la base de datos
		variant_doc.insert(ignore_permissions=True)

		# 2. Asignar precio minorista (Standard Selling)
		p_retail = frappe.new_doc("Item Price")
		p_retail.price_list = "Standard Selling"
		p_retail.item_code = item_code
		p_retail.price_list_rate = retail_price
		p_retail.insert(ignore_permissions=True)

		# 3. Asignar precio mayorista (Standard Wholesale) si se especificó
		if wholesale_price is not None:
			p_wholesale = frappe.new_doc("Item Price")
			p_wholesale.price_list = "Standard Wholesale"
			p_wholesale.item_code = item_code
			p_wholesale.price_list_rate = wholesale_price
			p_wholesale.insert(ignore_permissions=True)

		frappe.db.commit()
		frappe.clear_cache(doctype="Item")
		frappe.clear_cache(doctype="Item Price")

		return {
			"success": True,
			"item_code": item_code,
			"item_name": variant_doc.item_name,
			"retail_price": retail_price,
			"wholesale_price": wholesale_price,
		}

	except Exception as e:
		frappe.db.rollback()
		frappe.throw(frappe._("Error al crear la variante: {0}").format(str(e)))


@frappe.whitelist(allow_guest=True)
def get_all_attributes():
	# Obtiene todos los atributos definidos en ERPNext
	return frappe.get_all("Item Attribute", fields=["name"])


@frappe.whitelist()
def add_attribute_value(attribute_name, value_name, value_abbr):
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	if not frappe.has_permission("Item Attribute", "write"):
		frappe.throw(frappe._("No tenés permisos para modificar atributos"), frappe.PermissionError)

	if not frappe.db.exists("Item Attribute", attribute_name):
		frappe.throw(frappe._("El atributo especificado no existe"), frappe.DoesNotExistError)

	# Cargar el documento del atributo
	attr_doc = frappe.get_doc("Item Attribute", attribute_name)

	# Validar duplicados de nombre de valor o abreviación
	for val in attr_doc.item_attribute_values:
		if val.attribute_value.lower() == value_name.strip().lower():
			frappe.throw(frappe._("El valor de atributo '{0}' ya existe").format(value_name))
		if val.abbr.lower() == value_abbr.strip().lower():
			frappe.throw(frappe._("La abreviación '{0}' ya está en uso por otro valor").format(value_abbr))

	# Agregar el nuevo valor
	attr_doc.append(
		"item_attribute_values", {"attribute_value": value_name.strip(), "abbr": value_abbr.strip().upper()}
	)

	attr_doc.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache(doctype="Item Attribute")

	return {
		"success": True,
		"attribute": attribute_name,
		"value": value_name.strip(),
		"abbr": value_abbr.strip().upper(),
	}


@frappe.whitelist()
def create_item_template(template_name, attributes_list, item_group="Products"):
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	if not frappe.has_permission("Item", "write"):
		frappe.throw(frappe._("No tenés permisos para crear productos"), frappe.PermissionError)

	if not template_name or not template_name.strip():
		frappe.throw(frappe._("El nombre de la plantilla no puede estar vacío"))

	if isinstance(attributes_list, str):
		attributes_list = frappe.parse_json(attributes_list)

	if not attributes_list:
		frappe.throw(frappe._("Debés seleccionar al menos un atributo para la plantilla"))

	template_name = template_name.strip()

	if frappe.db.exists("Item", template_name):
		frappe.throw(frappe._("Ya existe un producto o plantilla con el nombre '{0}'").format(template_name))

	# Crear el Item plantilla
	item = frappe.new_doc("Item")
	item.item_code = template_name
	item.item_name = template_name
	item.has_variants = 1
	item.item_group = item_group
	item.stock_uom = "Unit"
	item.disabled = 0

	# Agregar atributos asociados
	for attr in attributes_list:
		if not frappe.db.exists("Item Attribute", attr):
			frappe.throw(frappe._("El atributo '{0}' no existe en el sistema").format(attr))
		item.append("attributes", {"attribute": attr})

	item.insert(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache(doctype="Item")

	return {"success": True, "name": item.name, "item_name": item.item_name}


@frappe.whitelist()
def create_custom_field():
	if not _is_system_manager():
		frappe.throw(frappe._("No tenés permisos para configurar el sistema"), frappe.PermissionError)

	if not frappe.db.exists("Custom Field", "SaaS Feature Config-allow_pos_out_of_stock"):
		doc = frappe.get_doc(
			{
				"doctype": "Custom Field",
				"dt": "SaaS Feature Config",
				"fieldname": "allow_pos_out_of_stock",
				"label": "Allow POS Out of Stock",
				"fieldtype": "Check",
				"insert_after": "has_logistics",
				"default": "0",
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache(doctype="SaaS Feature Config")
		return {"success": True, "message": "Custom field created successfully!"}
	return {"success": True, "message": "Custom field already exists!"}


def _ensure_pos_feature_enabled():
	try:
		config = frappe.get_cached_doc("SaaS Feature Config")
	except Exception:
		config = None

	if not config or not bool(getattr(config, "has_pos", 0)):
		frappe.throw(
			frappe._("El módulo de punto de venta está desactivado en la configuración."),
			frappe.PermissionError,
		)


@frappe.whitelist()
def get_pos_profile(selected_profile=None):
	_ensure_pos_feature_enabled()

	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	is_admin = "System Manager" in frappe.get_roles(frappe.session.user)

	# 1. Obtener los perfiles de POS asignados al usuario en la tabla hija POS Profile User
	assigned_profiles = frappe.get_all(
		"POS Profile User", filters={"user": frappe.session.user}, fields=["parent", "default"]
	)
	assigned_names = [p.parent for p in assigned_profiles]

	active_assigned_profiles = []
	if assigned_names:
		active_assigned_profiles = [
			p.name
			for p in frappe.get_all(
				"POS Profile", filters={"name": ["in", assigned_names], "disabled": 0}, fields=["name"]
			)
		]

	# 2. Definir los perfiles disponibles para mostrar en el selector
	if is_admin:
		available_profiles = [
			p.name for p in frappe.get_all("POS Profile", filters={"disabled": 0}, fields=["name"])
		]
	else:
		available_profiles = active_assigned_profiles

	# 3. Comprobar si el usuario posee un turno abierto (POS Opening Entry) activo
	# Si hay un turno abierto, se obliga al usuario a usar el perfil de ese turno.
	open_shift = frappe.get_all(
		"POS Opening Entry",
		filters={"user": frappe.session.user, "status": "Open"},
		fields=["pos_profile"],
		limit=1,
	)

	pos_profile_name = None
	if open_shift:
		pos_profile_name = open_shift[0].pos_profile
	elif selected_profile:
		# Validar que el perfil seleccionado esté dentro de sus perfiles permitidos
		if selected_profile in available_profiles:
			pos_profile_name = selected_profile
		else:
			frappe.throw(
				frappe._("No tenés acceso al perfil de punto de venta seleccionado."), frappe.PermissionError
			)
	else:
		# Seleccionar perfil por defecto o el primero activo disponible
		default_profile = next(
			(p.parent for p in assigned_profiles if p.default and p.parent in active_assigned_profiles), None
		)
		if default_profile:
			pos_profile_name = default_profile
		elif active_assigned_profiles:
			pos_profile_name = active_assigned_profiles[0]
		elif is_admin:
			if available_profiles:
				pos_profile_name = available_profiles[0]

	if not pos_profile_name:
		frappe.throw(frappe._("No tenés un Perfil de Punto de Venta asignado."), frappe.DoesNotExistError)

	profile = frappe.get_doc("POS Profile", pos_profile_name)

	# Obtener métodos de pago
	payment_methods = []
	for pm in profile.payments:
		payment_methods.append({"mode_of_payment": pm.mode_of_payment, "default": pm.default})

	return {
		"pos_profile": profile.name,
		"company": profile.company,
		"warehouse": profile.warehouse,
		"customer": profile.customer,
		"currency": profile.currency or "MXN",
		"selling_price_list": profile.selling_price_list or "Standard Selling",
		"payment_methods": payment_methods,
		"available_profiles": available_profiles,
	}


@frappe.whitelist()
def get_active_pos_opening(pos_profile=None):
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	filters = {"user": frappe.session.user, "status": "Open"}
	if pos_profile:
		filters["pos_profile"] = pos_profile

	openings = frappe.get_all(
		"POS Opening Entry",
		filters=filters,
		fields=["name", "pos_profile", "company", "posting_date", "period_start_date"],
		limit=1,
	)

	if not openings:
		if pos_profile:
			other_openings = frappe.get_all(
				"POS Opening Entry",
				filters={"pos_profile": pos_profile, "status": "Open"},
				fields=["name", "user"],
				limit=1,
				ignore_permissions=True,
			)
			if other_openings:
				return {
					"is_different_user": True,
					"user": other_openings[0].user,
					"name": other_openings[0].name,
				}
		return None

	doc = frappe.get_doc("POS Opening Entry", openings[0].name)
	balance_details = []
	for detail in doc.balance_details:
		balance_details.append(
			{"mode_of_payment": detail.mode_of_payment, "opening_amount": detail.opening_amount}
		)

	return {
		"name": doc.name,
		"pos_profile": doc.pos_profile,
		"company": doc.company,
		"posting_date": doc.posting_date,
		"period_start_date": doc.period_start_date,
		"balance_details": balance_details,
	}


@frappe.whitelist()
def create_pos_opening(pos_profile, company, balance_details):
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if not _user_has_pos_profile_access(pos_profile):
		frappe.throw(frappe._("No tenés permisos para realizar esta acción."), frappe.PermissionError)

	if isinstance(balance_details, str):
		balance_details = frappe.parse_json(balance_details)

	doc = frappe.new_doc("POS Opening Entry")
	doc.pos_profile = pos_profile
	doc.company = company
	doc.user = frappe.session.user
	doc.posting_date = frappe.utils.today()
	doc.period_start_date = frappe.utils.now_datetime()

	for item in balance_details:
		doc.append(
			"balance_details",
			{
				"mode_of_payment": item.get("mode_of_payment"),
				"opening_amount": float(item.get("opening_amount", 0.0)),
			},
		)

	doc.insert(ignore_permissions=True)
	doc.submit()
	frappe.db.commit()

	return {"success": True, "name": doc.name}


@frappe.whitelist()
def close_pos_shift(pos_opening_entry, closing_details):
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	# Bloquear la fila de la apertura de caja para evitar condiciones de carrera por doble click o peticiones concurrentes
	opening_status = frappe.db.sql(
		"select status, pos_closing_entry, user, pos_profile from `tabPOS Opening Entry` where name = %s for update",
		(pos_opening_entry,),
		as_dict=True,
	)

	if not opening_status:
		frappe.throw(frappe._("La apertura de caja especificada no existe."))

	status_info = opening_status[0]
	if not _is_system_manager() and status_info.user != frappe.session.user:
		frappe.throw(frappe._("No tenés permisos para realizar esta acción."), frappe.PermissionError)

	if status_info.status == "Closed":
		return {"success": True, "name": status_info.pos_closing_entry}
	elif status_info.status != "Open":
		frappe.throw(frappe._("La apertura especificada no está abierta o ya fue cerrada."))

	if isinstance(closing_details, str):
		closing_details = frappe.parse_json(closing_details)

	opening_doc = frappe.get_doc("POS Opening Entry", pos_opening_entry)

	closing_doc = frappe.new_doc("POS Closing Entry")
	closing_doc.pos_opening_entry = pos_opening_entry
	closing_doc.pos_profile = opening_doc.pos_profile
	closing_doc.company = opening_doc.company
	closing_doc.user = frappe.session.user
	closing_doc.period_start_date = opening_doc.period_start_date
	closing_doc.posting_date = frappe.utils.today()
	closing_doc.posting_time = frappe.utils.nowtime()

	closing_doc.insert(ignore_permissions=True)

	# Reconciliación de montos declarados por el cajero
	declared_map = {
		item.get("mode_of_payment"): float(item.get("closing_amount", 0.0)) for item in closing_details
	}

	for item in closing_doc.payment_reconciliation:
		mop = item.mode_of_payment
		item.closing_amount = declared_map.get(mop, 0.0)

	closing_doc.save(ignore_permissions=True)
	closing_doc.submit()

	# Actualizar la apertura de caja directamente en la base de datos para evitar conflictos de bloqueo optimista
	frappe.db.set_value(
		"POS Opening Entry",
		pos_opening_entry,
		{"status": "Closed", "pos_closing_entry": closing_doc.name},
		update_modified=True,
	)

	frappe.db.commit()

	return {"success": True, "name": closing_doc.name}


@frappe.whitelist()
def get_closing_reconciliation_details(pos_opening_entry):
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	opening_doc = frappe.get_doc("POS Opening Entry", pos_opening_entry)

	closing_doc = frappe.new_doc("POS Closing Entry")
	closing_doc.pos_opening_entry = pos_opening_entry
	closing_doc.pos_profile = opening_doc.pos_profile
	closing_doc.company = opening_doc.company
	closing_doc.user = frappe.session.user
	closing_doc.period_start_date = opening_doc.period_start_date
	closing_doc.posting_date = frappe.utils.today()
	closing_doc.posting_time = frappe.utils.nowtime()

	closing_doc.run_method("get_payment_reconciliation_details")

	reconciliation = []
	for item in closing_doc.payment_reconciliation:
		reconciliation.append(
			{
				"mode_of_payment": item.mode_of_payment,
				"opening_amount": item.opening_amount,
				"expected_amount": item.expected_amount,
			}
		)

	return reconciliation


@frappe.whitelist()
def search_customers(query):
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if not query or len(query.strip()) < 2:
		return []

	return frappe.get_all(
		"Customer",
		filters={"customer_name": ["like", f"%{query}%"], "disabled": 0},
		fields=["name", "customer_name"],
		limit=20,
	)


@frappe.whitelist()
def create_pos_customer(customer_name, phone=None, rfc=None, tax_regime=None, cfdi_use=None):
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	doc = frappe.new_doc("Customer")
	doc.customer_name = customer_name.strip()
	doc.customer_type = "Individual"
	doc.customer_group = "Individual"
	doc.territory = "All Territories"
	if phone and phone.strip():
		doc.mobile_no = phone.strip()

	if rfc and rfc.strip():
		doc.rfc = rfc.strip().upper()
		doc.tax_id = rfc.strip().upper()

	if tax_regime and tax_regime.strip():
		doc.tax_regime = tax_regime.strip()

	if cfdi_use and cfdi_use.strip():
		doc.cfdi_use = cfdi_use.strip()

	doc.insert(ignore_permissions=True)
	frappe.db.commit()

	return {"success": True, "name": doc.name, "customer_name": doc.customer_name}


@frappe.whitelist()
def find_customer_by_name_or_phone(name=None, phone=None):
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	res = None
	if name and name.strip() and phone and phone.strip():
		res = frappe.db.get_value(
			"Customer",
			{"customer_name": name.strip(), "disabled": 0},
			["name", "customer_name", "mobile_no"],
			as_dict=1,
		)
		if not res:
			res = frappe.db.get_value(
				"Customer",
				{"mobile_no": phone.strip(), "disabled": 0},
				["name", "customer_name", "mobile_no"],
				as_dict=1,
			)
	elif name and name.strip():
		res = frappe.db.get_value(
			"Customer",
			{"customer_name": name.strip(), "disabled": 0},
			["name", "customer_name", "mobile_no"],
			as_dict=1,
		)
		if not res:
			matches = frappe.get_all(
				"Customer",
				filters={"customer_name": ["like", f"%{name.strip()}%"], "disabled": 0},
				fields=["name", "customer_name", "mobile_no"],
				limit=1,
			)
			if matches:
				res = matches[0]
	elif phone and phone.strip():
		res = frappe.db.get_value(
			"Customer",
			{"mobile_no": phone.strip(), "disabled": 0},
			["name", "customer_name", "mobile_no"],
			as_dict=1,
		)

	if res:
		return {"found": True, "name": res.name, "customer_name": res.customer_name, "phone": res.mobile_no}

	return {"found": False}


@frappe.whitelist()
def get_customer_orders_history(customer_name):
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		profile = get_customer_wholesale_profile()
		if not profile or not profile.get("success") or profile.get("customer") != customer_name:
			frappe.throw(frappe._("No tenés permisos para ver este historial."), frappe.PermissionError)

	orders = frappe.get_all(
		"Sales Order",
		filters={"customer": customer_name, "docstatus": ["!=", 2]},
		fields=["name", "transaction_date", "grand_total", "status", "delivery_date"],
		order_by="creation desc",
		limit=15,
	)

	invoices = frappe.get_all(
		"Sales Invoice",
		filters={"customer": customer_name, "docstatus": ["!=", 2]},
		fields=["name", "posting_date", "grand_total", "outstanding_amount", "status"],
		order_by="creation desc",
		limit=15,
	)

	return {"orders": orders, "invoices": invoices}


@frappe.whitelist()
def get_all_customers():
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if not is_tenant_admin_user() and "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para acceder a esta información."), frappe.PermissionError)

	fields = [
		"name",
		"customer_name",
		"mobile_no",
		"email_id",
		"territory",
		"customer_group",
	]
	if frappe.get_meta("Customer").has_field("custom_wholesale_access_pin"):
		fields.append("custom_wholesale_access_pin")

	customers = frappe.get_all(
		"Customer",
		filters={"disabled": 0},
		fields=fields,
		order_by="customer_name asc",
		limit=200,
	)
	for customer in customers:
		customer.setdefault("custom_wholesale_access_pin", None)
	return customers


@frappe.whitelist()
def setup_reservation_fields():
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para configurar el sistema"), frappe.PermissionError)

	fields = [
		{
			"fieldname": "has_reservations",
			"label": "Has Reservations",
			"fieldtype": "Check",
			"insert_after": "allow_pos_out_of_stock",
			"default": "0",
		},
		{
			"fieldname": "has_wholesale",
			"label": "Venta Mayorista",
			"fieldtype": "Check",
			"insert_after": "has_reservations",
			"default": "1",
		},
		{
			"fieldname": "reservation_item_code",
			"label": "Reservation Item Code",
			"fieldtype": "Link",
			"options": "Item",
			"insert_after": "has_reservations",
			"default": "Carrito Paletero",
		},
		{
			"fieldname": "max_reservation_assets",
			"label": "Max Reservation Assets",
			"fieldtype": "Int",
			"insert_after": "reservation_item_code",
			"default": "10",
		},
		{
			"fieldname": "default_event_items",
			"label": "Default Event Items",
			"fieldtype": "Text",
			"insert_after": "max_reservation_assets",
			"default": "[]",
		},
		{
			"fieldname": "custom_country",
			"label": "Country",
			"fieldtype": "Link",
			"options": "Country",
			"insert_after": "default_event_items",
			"default": "Mexico",
		},
		{
			"fieldname": "custom_currency",
			"label": "Currency",
			"fieldtype": "Link",
			"options": "Currency",
			"insert_after": "custom_country",
			"default": "MXN",
		},
	]
	created = []
	for f in fields:
		name = f"SaaS Feature Config-{f['fieldname']}"
		if _saas_feature_config_has_field(f["fieldname"]):
			continue
		if not frappe.db.exists("Custom Field", name):
			doc = frappe.get_doc(
				{
					"doctype": "Custom Field",
					"dt": "SaaS Feature Config",
					"fieldname": f["fieldname"],
					"label": f["label"],
					"fieldtype": f["fieldtype"],
					"options": f.get("options"),
					"insert_after": f["insert_after"],
					"default": f["default"],
				}
			)
			doc.insert(ignore_permissions=True)
			created.append(f["fieldname"])

	if created:
		frappe.db.commit()
		frappe.clear_cache(doctype="SaaS Feature Config")

	return {"success": True, "created": created}


def _require_event_lifecycle_admin_access():
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para acceder a este recurso."), frappe.PermissionError)


def _get_event_reservation(sales_order_name):
	if not frappe.db.exists("Event Cart Reservation", sales_order_name):
		frappe.throw(frappe._("La reserva {0} no existe.").format(sales_order_name))

	return frappe.get_doc("Event Cart Reservation", sales_order_name)


def _get_active_event_reservation_count(event_date, company=None):
	company = (company or get_platform_company_name()).strip()
	filters = {"event_date": event_date, "state": ["in", ["Pending Confirmation", "Confirmed"]]}
	if company:
		filters["company"] = company

	return len(frappe.get_all("Event Cart Reservation", filters=filters, fields=["name"]))


def _build_event_reservation_items(so, reservation_item_code):
	items = []
	for so_item in so.items:
		items.append(
			{
				"item_code": so_item.item_code,
				"item_name": so_item.item_name,
				"qty": so_item.qty,
				"rate": so_item.rate,
				"amount": so_item.amount,
				"sales_order_item": so_item.name,
				"is_reservation_asset": 1 if so_item.item_code == reservation_item_code else 0,
			}
		)

	return items


def _create_event_reservation_from_sales_order(so, reservation_item_code):
	reservation = frappe.new_doc("Event Cart Reservation")
	reservation.sales_order = so.name
	reservation.customer = so.customer
	reservation.event_date = so.delivery_date
	reservation.company = so.company
	reservation.reservation_item_code = reservation_item_code
	reservation.state = "Pending Confirmation"
	reservation.grand_total = float(so.grand_total or 0.0)
	reservation.base_grand_total = float(
		getattr(so, "base_grand_total", so.grand_total) or so.grand_total or 0.0
	)
	reservation.advance_paid = float(getattr(so, "advance_paid", 0) or 0.0)
	reservation.outstanding_amount = float(
		getattr(so, "outstanding_amount", so.grand_total) or so.grand_total or 0.0
	)

	for item in _build_event_reservation_items(so, reservation_item_code):
		reservation.append("items", item)

	reservation.insert(ignore_permissions=True)
	return reservation


@frappe.whitelist(allow_guest=True)
def check_cart_availability(date, company=None):
	if not date:
		frappe.throw(frappe._("Por favor especifique una fecha."))

	# Leer configuración SaaS
	config = frappe.get_cached_doc("SaaS Feature Config")
	has_res = bool(config.get("has_reservations", 0))
	item_code = config.get("reservation_item_code") or "Carrito Paletero"
	max_assets = int(config.get("max_reservation_assets") or 10)
	company = (company or get_platform_company_name()).strip()

	if not has_res:
		return {"enabled": False, "message": "El módulo de reservas está deshabilitado."}

	active_reserved = _get_active_event_reservation_count(date, company=company)
	available_qty = max(0, max_assets - active_reserved)

	return {
		"enabled": True,
		"date": date,
		"item_code": item_code,
		"max_assets": max_assets,
		"active_reserved": active_reserved,
		"already_booked": active_reserved,
		"available_qty": available_qty,
	}


@frappe.whitelist(allow_guest=True)
def create_event_booking(
	customer=None,
	delivery_date=None,
	items=None,
	advance_amount=0,
	payment_mode="Cash",
	guest_name=None,
	guest_phone=None,
):
	if not delivery_date:
		frappe.throw(frappe._("Debe proporcionar una fecha de entrega."))

	if not items:
		frappe.throw(frappe._("Debe agregar al menos un artículo para reservar."))

	is_guest_request = not frappe.session.user or frappe.session.user == "Guest"
	requested_advance_amount = float(advance_amount or 0)
	if is_guest_request:
		requested_advance_amount = 0.0

	# Validar que si no está autenticado, proporcione sus datos
	if is_guest_request and not guest_name:
		frappe.throw(
			frappe._("Debe iniciar sesión o proporcionar su nombre para la reserva."), frappe.PermissionError
		)

	config = frappe.get_cached_doc("SaaS Feature Config")
	if not _reservations_are_active(config):
		return _reservations_disabled_response()

	company_name = get_platform_company_name()

	if is_guest_request and _event_booking_is_rate_limited(guest_phone, guest_name):
		return {"success": False, "error": frappe._("No se pudo registrar la reserva de evento.")}

	lock_name = f"event_booking:{company_name}:{delivery_date}"
	lock_result = frappe.db.sql("SELECT GET_LOCK(%s, 10)", (lock_name,))
	lock_acquired = bool(lock_result and lock_result[0][0] == 1)
	if not lock_acquired:
		frappe.throw(
			frappe._("No se pudo asegurar la fecha de reserva. Intentá nuevamente."), frappe.PermissionError
		)

	try:
		availability = check_cart_availability(delivery_date, company=company_name)
		if not availability.get("enabled", True):
			return _reservations_disabled_response()
		if int(availability.get("available_qty") or 0) <= 0:
			return {"success": False, "error": frappe._("No hay carritos disponibles para esa fecha.")}

		item_code = config.get("reservation_item_code") or "Carrito Paletero"
		allowed_item_codes = {item.get("name") for item in get_active_items()}

		parsed_items = items
		if isinstance(parsed_items, str):
			parsed_items = frappe.parse_json(parsed_items)

		if is_guest_request:
			customer = None
		elif "System Manager" not in frappe.get_roles(frappe.session.user):
			customer = None

		final_customer = customer
		if (
			(not is_guest_request)
			and (not final_customer or final_customer == "Público General")
			and guest_name
		):
			# Buscar si ya existe un cliente con ese nombre o teléfono
			existing = None
			if guest_phone:
				existing = frappe.db.get_value("Customer", {"mobile_no": guest_phone.strip()}, "name")
			if not existing:
				existing = frappe.db.get_value("Customer", {"customer_name": guest_name.strip()}, "name")

			if existing:
				final_customer = existing
			else:
				doc = frappe.new_doc("Customer")
				doc.customer_name = guest_name.strip()
				doc.customer_type = "Individual"
				doc.customer_group = "Individual"
				doc.territory = "All Territories"
				if guest_phone:
					doc.mobile_no = guest_phone.strip()
				doc.flags.ignore_permissions = True
				doc.insert(ignore_permissions=True)
				final_customer = doc.name

		if not final_customer:
			final_customer = "Público General"

		warehouse = get_platform_distribution_warehouse()

		# 1. Crear el Sales Order nativo
		so = frappe.new_doc("Sales Order")
		so.company = company_name
		so.customer = final_customer
		so.delivery_date = delivery_date
		so.selling_price_list = "Standard Selling"

		# Agregar el recurso reservado
		so.append(
			"items",
			{
				"item_code": item_code,
				"qty": 1,
				"rate": 0.0,
				"warehouse": warehouse,
				"delivery_date": delivery_date,
			},
		)

		# Agregar los helados / paletas
		for it in parsed_items:
			item_code_input = it.get("item_code")
			if item_code_input == item_code:
				frappe.throw(
					frappe._("El artículo reservado no puede agregarse dentro de los productos del evento.")
				)
			if item_code_input not in allowed_item_codes:
				frappe.throw(
					frappe._("El producto {0} no está permitido para reservas de evento.").format(
						item_code_input
					)
				)
			item_data = frappe.db.get_value(
				"Item", item_code_input, ["name", "standard_rate", "disabled"], as_dict=True
			)
			if not item_data or item_data.disabled:
				frappe.throw(frappe._("El producto {0} no existe o está inactivo.").format(item_code_input))
			so.append(
				"items",
				{
					"item_code": item_code_input,
					"qty": float(it.get("qty", 1)),
					"rate": float(
						item_data.standard_rate if item_data.standard_rate is not None else it.get("rate", 0)
					),
					"warehouse": warehouse,
					"delivery_date": delivery_date,
				},
			)

		so.insert(ignore_permissions=True)
		so.submit()

		reservation = _create_event_reservation_from_sales_order(so, item_code)

		# 2. Registrar el anticipo cobrado (si es mayor a 0)
		can_register_advance_payment = not is_guest_request and "System Manager" in frappe.get_roles(
			frappe.session.user
		)
		advance_paid = 0.0
		if can_register_advance_payment and requested_advance_amount > 0:
			from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

			try:
				pe = get_payment_entry("Sales Order", so.name, bank_amount=requested_advance_amount)
				pe.mode_of_payment = payment_mode
				pe.reference_no = f"Anticipo Evento {delivery_date}"
				pe.reference_date = frappe.utils.today()

				# Asignar la cuenta contable según modo de pago
				pe.paid_to = get_platform_payment_account(payment_mode)
				pe.paid_amount = requested_advance_amount
				pe.received_amount = requested_advance_amount
				if pe.references:
					pe.references[0].allocated_amount = requested_advance_amount

				pe.insert(ignore_permissions=True)
				pe.submit()
				reservation.db_set("payment_entry", pe.name)
				reservation.db_set("advance_paid", requested_advance_amount)
				reservation.db_set(
					"outstanding_amount",
					max(0.0, float(so.grand_total or 0.0) - float(requested_advance_amount)),
				)
				advance_paid = requested_advance_amount
			except Exception as e:
				# Registrar el error pero no tumbar la Sales Order ya confirmada
				frappe.log_error(message=str(e), title="Error creando anticipo para Sales Order en Reserva")

		frappe.db.commit()
		return {
			"success": True,
			"sales_order": so.name,
			"reservation": reservation.name,
			"reservation_state": reservation.state,
			"advance_paid": advance_paid,
		}
	except Exception:
		frappe.db.rollback()
		raise
	finally:
		if lock_acquired:
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))


@frappe.whitelist(allow_guest=True)
def get_active_items_with_prices(warehouse=None):
	config = frappe.get_cached_doc("SaaS Feature Config")
	if not _wholesale_is_active(config):
		return _wholesale_disabled_response()

	item_code = config.get("reservation_item_code") or "Carrito Paletero"

	subgroups = ["Products"] + [
		g.name for g in frappe.get_all("Item Group", filters={"parent_item_group": "Products"})
	]

	# Obtener todas las variantes de artículos de productos activas
	items = frappe.get_all(
		"Item",
		filters={
			"disabled": 0,
			"item_group": ["in", subgroups],
			"has_variants": 0,
			"name": ["!=", item_code],
		},
		fields=["name", "item_name", "item_group", "standard_rate", "image"],
		limit=150,
	)

	if not items:
		return []

	# Obtener precios para Standard Selling y Standard Wholesale
	prices = frappe.get_all(
		"Item Price",
		filters={
			"price_list": ["in", ["Standard Selling", "Standard Wholesale"]],
			"item_code": ["in", [i.name for i in items]],
		},
		fields=["item_code", "price_list", "price_list_rate"],
	)

	# Mapear precios
	price_map = {}
	for p in prices:
		if p.item_code not in price_map:
			price_map[p.item_code] = {"retail_price": 0.0, "wholesale_price": None}
		if p.price_list == "Standard Selling":
			price_map[p.item_code]["retail_price"] = float(p.price_list_rate)
		elif p.price_list == "Standard Wholesale":
			price_map[p.item_code]["wholesale_price"] = float(p.price_list_rate)

	# Obtener stock por almacén si corresponde
	stock_map = {}
	if warehouse and (is_tenant_admin_user() or "System Manager" in frappe.get_roles(frappe.session.user)):
		bins = frappe.get_all(
			"Bin",
			filters={"warehouse": warehouse, "item_code": ["in", [i.name for i in items]]},
			fields=["item_code", "actual_qty"],
		)
		for b in bins:
			stock_map[b.item_code] = float(b.actual_qty or 0.0)

	for item in items:
		rates = price_map.get(
			item.name, {"retail_price": float(item.standard_rate or 0.0), "wholesale_price": None}
		)
		item["retail_price"] = rates["retail_price"]
		item["wholesale_price"] = rates["wholesale_price"]
		item["actual_qty"] = stock_map.get(item.name, 0.0) if stock_map else None

	return items


@frappe.whitelist()
def get_active_warehouses_with_stock():
	config = frappe.get_cached_doc("SaaS Feature Config")
	if not _wholesale_is_active(config):
		return _wholesale_disabled_response()

	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if not (is_tenant_admin_user() or "System Manager" in frappe.get_roles(frappe.session.user)):
		frappe.throw(frappe._("No tenés permisos para acceder a este recurso."), frappe.PermissionError)

	company = get_platform_company_name()

	query = """
        SELECT DISTINCT w.name, w.warehouse_name
        FROM `tabWarehouse` w
        INNER JOIN `tabBin` b ON b.warehouse = w.name
        WHERE w.company = %s
          AND w.is_group = 0
          AND w.disabled = 0
          AND b.actual_qty > 0
    """
	warehouses = frappe.db.sql(query, (company,), as_dict=True)
	return warehouses


@frappe.whitelist()
def create_wholesale_sale(customer=None, items=None, payment_amount=0, payment_mode="Cash", warehouse=None):
	config = frappe.get_cached_doc("SaaS Feature Config")
	if not _wholesale_is_active(config):
		return _wholesale_disabled_response()

	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if _safe_is_platform_master_site():
		frappe.throw(
			frappe._("Acceso denegado: Este endpoint solo está disponible en sitios de tenant."),
			frappe.PermissionError,
		)

	if not (is_tenant_admin_user() or "System Manager" in frappe.get_roles(frappe.session.user)):
		frappe.throw(frappe._("No tenés permisos para acceder a este recurso."), frappe.PermissionError)

	if not customer:
		frappe.throw(frappe._("Debe proporcionar un cliente."))

	if not items:
		frappe.throw(frappe._("Debe agregar al menos un artículo para facturar."))

	if isinstance(items, str):
		items = frappe.parse_json(items)

	company_name = get_platform_company_name()
	warehouse = warehouse or get_platform_distribution_warehouse()

	# 1. Crear el Sales Invoice nativo con update_stock=1
	si = frappe.new_doc("Sales Invoice")
	si.company = company_name
	si.customer = customer
	si.update_stock = 1
	si.posting_date = frappe.utils.today()
	si.selling_price_list = "Standard Selling"
	si.currency = frappe.db.get_value("Company", company_name, "default_currency") or "MXN"
	si.set_posting_time = 1

	# Agregar los artículos
	for it in items:
		item_code = it.get("item_code")
		qty = float(it.get("qty", 1))
		rate = float(it.get("rate", 0))

		si.append(
			"items",
			{
				"item_code": item_code,
				"qty": qty,
				"price_list_rate": rate,
				"rate": rate,
				"ignore_pricing_rule": 1,
				"warehouse": warehouse,
			},
		)

	si.insert(ignore_permissions=True)
	si.submit()

	# 2. Registrar el pago inmediato (si payment_amount > 0)
	advance_paid = 0.0
	if float(payment_amount) > 0:
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		try:
			pe = get_payment_entry("Sales Invoice", si.name, bank_amount=float(payment_amount))
			pe.mode_of_payment = payment_mode
			pe.reference_no = f"Pago Venta Mayorista {si.name}"
			pe.reference_date = frappe.utils.today()

			# Asignar la cuenta contable según modo de pago
			pe.paid_to = get_platform_payment_account(payment_mode)

			# Forzar el monto pagado parcial explícitamente en el Payment Entry
			pe.paid_amount = float(payment_amount)
			pe.received_amount = float(payment_amount)
			if pe.references:
				pe.references[0].allocated_amount = float(payment_amount)

			pe.insert(ignore_permissions=True)
			pe.submit()
			advance_paid = float(payment_amount)
		except Exception as e:
			frappe.log_error(message=str(e), title="Error creando pago para Sales Invoice en Venta Mayorista")
			# No lanzamos error para no tumbar la factura ya emitida

	frappe.db.commit()

	# Obtener el saldo pendiente real actualizado de la base de datos
	updated_outstanding = float(
		frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount") or si.grand_total
	)

	return {
		"success": True,
		"sales_invoice": si.name,
		"advance_paid": advance_paid,
		"grand_total": float(si.grand_total),
		"outstanding_amount": updated_outstanding,
	}


def setup_wholesale_custom_fields():
	fields = [
		{
			"dt": "Sales Order",
			"fieldname": "custom_metodo_pago",
			"label": "Metodo de Pago Mayorista",
			"fieldtype": "Select",
			"options": "\nTransferencia\nEfectivo",
			"insert_after": "payment_terms_template",
		},
		{
			"dt": "Sales Order",
			"fieldname": "custom_metodo_entrega",
			"label": "Metodo de Entrega Mayorista",
			"fieldtype": "Select",
			"options": "\nDomicilio\nRecoger",
			"insert_after": "custom_metodo_pago",
		},
		{
			"dt": "Sales Invoice",
			"fieldname": "custom_metodo_pago",
			"label": "Metodo de Pago Mayorista",
			"fieldtype": "Select",
			"options": "\nTransferencia\nEfectivo",
			"insert_after": "payment_terms_template",
		},
		{
			"dt": "Sales Invoice",
			"fieldname": "custom_metodo_entrega",
			"label": "Metodo de Entrega Mayorista",
			"fieldtype": "Select",
			"options": "\nDomicilio\nRecoger",
			"insert_after": "custom_metodo_pago",
		},
		{
			"dt": "Customer",
			"fieldname": "custom_wholesale_access_pin",
			"label": "PIN de Acceso Mayorista",
			"fieldtype": "Data",
			"options": "",
			"insert_after": "mobile_no",
		},
	]

	for f in fields:
		name = f"{f['dt']}-{f['fieldname']}"
		if not frappe.db.exists("Custom Field", name):
			doc = frappe.get_doc(
				{
					"doctype": "Custom Field",
					"dt": f["dt"],
					"fieldname": f["fieldname"],
					"label": f["label"],
					"fieldtype": f["fieldtype"],
					"options": f["options"],
					"insert_after": f["insert_after"],
				}
			)
			doc.insert(ignore_permissions=True)
			frappe.db.commit()
			frappe.clear_cache(doctype=f["dt"])


def ensure_saas_notification_doctype():
	# Generic notification storage used by wholesale and event alerts.
	if frappe.db.exists("DocType", "SaaS Notification"):
		return

	doc = frappe.get_doc(
		{
			"doctype": "DocType",
			"name": "SaaS Notification",
			"module": "Paletixa SaaS",
			"custom": 1,
			"autoname": "hash",
			"fields": [
				{
					"fieldname": "title",
					"label": "Title",
					"fieldtype": "Data",
					"reqd": 1,
					"in_list_view": 1,
				},
				{
					"fieldname": "message",
					"label": "Message",
					"fieldtype": "Small Text",
					"in_list_view": 1,
				},
				{
					"fieldname": "module",
					"label": "Module",
					"fieldtype": "Select",
					"options": "Wholesale\nEvent",
					"in_list_view": 1,
				},
				{
					"fieldname": "reference_doctype",
					"label": "Reference DocType",
					"fieldtype": "Link",
					"options": "DocType",
				},
				{
					"fieldname": "reference_name",
					"label": "Reference Name",
					"fieldtype": "Data",
					"in_list_view": 1,
				},
				{
					"fieldname": "read",
					"label": "Read",
					"fieldtype": "Check",
					"default": "0",
					"in_list_view": 1,
				},
			],
			"permissions": [
				{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "select": 1}
			],
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()


@frappe.whitelist()
def get_customer_wholesale_profile():
	config = frappe.get_cached_doc("SaaS Feature Config")
	if not _wholesale_is_active(config):
		return _wholesale_disabled_response()

	setup_wholesale_custom_fields()
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	# 1. Buscar en Contactos vinculados a un Customer que tengan este email
	contacts = frappe.db.sql(
		"""
        SELECT dl.link_name
        FROM `tabDynamic Link` dl
        JOIN `tabContact` c ON c.name = dl.parent
        WHERE dl.link_doctype = 'Customer'
          AND c.email_id = %s
        LIMIT 1
    """,
		(user,),
	)

	customer_name = None
	if contacts:
		customer_name = contacts[0][0]

	# 2. Si no se encuentra, buscar por el campo email_id del Customer directamente
	if not customer_name:
		customer_name = frappe.db.get_value("Customer", {"email_id": user}, "name")

	# 3. Si no se encuentra y el usuario es System Manager / Administrator, usar un cliente por defecto para pruebas
	if not customer_name and "System Manager" in frappe.get_roles(user):
		customers = frappe.get_all("Customer", filters={"disabled": 0}, fields=["name"], limit=1)
		if customers:
			customer_name = customers[0].name

	if not customer_name:
		return {
			"success": False,
			"error": frappe._(
				"No se encontró ningún Cliente asociado a tu correo electrónico. Por favor, contactá al administrador."
			),
		}

	customer_doc = frappe.get_cached_doc("Customer", customer_name)
	return {
		"success": True,
		"customer": customer_name,
		"customer_name": customer_doc.customer_name,
		"email": user,
	}


def normalize_phone_number(phone):
	if not phone:
		return ""
	import re

	cleaned = re.sub(r"[^\d+]", "", phone.strip())

	# Strip old Mexican prefixes if they are before a 10-digit number
	if cleaned.startswith("044") and len(cleaned) == 13:
		cleaned = cleaned[3:]
	elif cleaned.startswith("045") and len(cleaned) == 13:
		cleaned = cleaned[3:]
	elif cleaned.startswith("044") and len(cleaned) == 10:
		cleaned = cleaned[3:]

	# If the clean number has 10 digits and does not start with +, prepend +52
	if len(cleaned) == 10 and not cleaned.startswith("+"):
		cleaned = "+52" + cleaned

	return cleaned


def _wholesale_session_cache_key(customer):
	return f"wholesale_session:{customer}"


def _wholesale_session_token_is_valid(customer, token):
	if not customer or not token:
		return False

	cache_value = frappe.cache().get_value(_wholesale_session_cache_key(customer))
	if not cache_value:
		return False

	try:
		stored_token, created_at = str(cache_value).split(":", 1)
	except ValueError:
		return False

	if stored_token != token:
		return False

	try:
		issued_at = int(created_at)
	except (TypeError, ValueError):
		return False

	return (int(time.time()) - issued_at) <= 86400


def _wholesale_access_attempt_key(normalized_phone):
	remote_addr = getattr(getattr(frappe.local, "request", None), "remote_addr", None) or "unknown"
	return f"wholesale_access_attempts:{remote_addr}:{normalized_phone}"


def _wholesale_access_is_rate_limited(normalized_phone):
	now = int(time.time())
	cache_key = _wholesale_access_attempt_key(normalized_phone)
	history = frappe.cache().get_value(cache_key) or []
	history = [ts for ts in history if now - int(ts) < 600]
	if len(history) >= 5:
		return True
	history.append(now)
	frappe.cache().set_value(cache_key, history)
	return False


@frappe.whitelist()
def generate_customer_access_pin(customer_name):
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(user):
		frappe.throw(frappe._("No tenés permisos para generar un PIN de acceso."), frappe.PermissionError)

	pin = "".join([str(secrets.randbelow(10)) for _ in range(6)])

	frappe.db.set_value("Customer", customer_name, "custom_wholesale_access_pin", pin)
	frappe.db.commit()

	return {"success": True, "pin": pin}


@frappe.whitelist(allow_guest=True)
def validate_wholesale_access(phone, pin):
	config = frappe.get_cached_doc("SaaS Feature Config")
	if not _wholesale_is_active(config):
		return _wholesale_disabled_response()

	generic_error = {"success": False, "error": frappe._("No se pudo validar el acceso mayorista.")}

	setup_wholesale_custom_fields()

	if not phone or not pin:
		return generic_error

	normalized = normalize_phone_number(phone)
	if _wholesale_access_is_rate_limited(normalized):
		return generic_error

	customers = frappe.get_all(
		"Customer",
		filters={"disabled": 0},
		fields=["name", "customer_name", "mobile_no", "custom_wholesale_access_pin"],
	)

	matching_customer = None
	for c in customers:
		if c.mobile_no:
			if normalize_phone_number(c.mobile_no) == normalized:
				matching_customer = c
				break

	if not matching_customer:
		return generic_error

	stored_pin = matching_customer.custom_wholesale_access_pin
	if not stored_pin or stored_pin.strip() != pin.strip():
		return generic_error

	token = secrets.token_urlsafe(24)
	frappe.cache().set_value(
		_wholesale_session_cache_key(matching_customer.name), f"{token}:{int(time.time())}"
	)

	return {
		"success": True,
		"customer": matching_customer.name,
		"customer_name": matching_customer.customer_name,
		"phone": matching_customer.mobile_no,
		"token": token,
	}


@frappe.whitelist(allow_guest=True)
def create_wholesale_order(
	items=None, metodo_pago=None, metodo_entrega=None, customer=None, wholesale_token=None
):
	config = frappe.get_cached_doc("SaaS Feature Config")
	if not _wholesale_is_active(config):
		return _wholesale_disabled_response()

	setup_wholesale_custom_fields()
	user = frappe.session.user

	if user and user != "Guest":
		profile = get_customer_wholesale_profile()
		if not profile.get("success"):
			frappe.throw(profile.get("error"))

		profile_customer = profile.get("customer")
		if customer and customer != profile_customer:
			frappe.throw(
				frappe._("No tenés permisos para crear pedidos para otro cliente."), frappe.PermissionError
			)
		customer = profile_customer

	if not user or user == "Guest":
		if not _wholesale_session_token_is_valid(customer, wholesale_token):
			frappe.throw(frappe._("Sesión mayorista inválida."), frappe.PermissionError)

	if not customer or not frappe.db.exists("Customer", {"name": customer, "disabled": 0}):
		frappe.throw(frappe._("Cliente inválido o inactivo."))

	if isinstance(items, str):
		items = frappe.parse_json(items)

	if not items:
		frappe.throw(frappe._("Debe agregar al menos un artículo para el pedido."))

	if not metodo_pago or metodo_pago not in ["Transferencia", "Efectivo"]:
		frappe.throw(frappe._("Método de pago inválido."))

	if not metodo_entrega or metodo_entrega not in ["Domicilio", "Recoger"]:
		frappe.throw(frappe._("Método de entrega inválido."))

	company_name = get_platform_company_name()
	warehouse = get_platform_distribution_warehouse()

	# 1. Crear el Sales Order nativo en ERPNext
	so = frappe.new_doc("Sales Order")
	so.company = company_name
	so.customer = customer
	so.delivery_date = frappe.utils.add_days(frappe.utils.today(), 1)
	so.selling_price_list = "Standard Selling"
	so.currency = frappe.db.get_value("Company", company_name, "default_currency") or "MXN"

	# Campos personalizados
	so.custom_metodo_pago = metodo_pago
	so.custom_metodo_entrega = metodo_entrega

	# Obtener precios para validar
	active_items = get_active_items_with_prices()
	item_price_map = {i.name: i for i in active_items}

	for it in items:
		item_code = it.get("item_code")
		qty = float(it.get("qty", 1))

		ref_item = item_price_map.get(item_code)
		if not ref_item:
			frappe.throw(frappe._("El producto {0} no existe o no está activo.").format(item_code))

		retail = float(ref_item.get("retail_price") or 0.0)
		wholesale = ref_item.get("wholesale_price")

		if wholesale is not None and qty >= 10:
			rate = float(wholesale)
		else:
			rate = retail

		so.append(
			"items",
			{
				"item_code": item_code,
				"qty": qty,
				"price_list_rate": rate,
				"rate": rate,
				"ignore_pricing_rule": 1,
				"warehouse": warehouse,
				"delivery_date": so.delivery_date,
			},
		)

	so.insert(ignore_permissions=True)
	so.submit()
	frappe.db.commit()

	return {"success": True, "sales_order": so.name, "grand_total": float(so.grand_total)}


@frappe.whitelist()
def get_pending_wholesale_orders():
	config = frappe.get_cached_doc("SaaS Feature Config")
	if not _wholesale_is_active(config):
		return []

	setup_wholesale_custom_fields()
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para acceder a este recurso."), frappe.PermissionError)

	orders = frappe.get_all(
		"Sales Order",
		filters={
			"docstatus": 1,
			"status": ["not in", ["Completed", "Closed", "Cancelled"]],
			"custom_metodo_pago": ["in", ["Transferencia", "Efectivo"]],
			"per_billed": ["<", 100],
			"per_delivered": ["<", 100],
		},
		fields=[
			"name",
			"customer",
			"customer_name",
			"transaction_date",
			"delivery_date",
			"grand_total",
			"custom_metodo_pago",
			"custom_metodo_entrega",
			"status",
		],
		order_by="creation desc",
	)

	result = []
	for o in orders:
		items = frappe.get_all(
			"Sales Order Item",
			filters={"parent": o.name},
			fields=["item_code", "item_name", "qty", "rate", "amount"],
		)
		mobile_no = frappe.db.get_value("Customer", o.customer, "mobile_no") or ""
		order_dict = o.copy()
		order_dict["items"] = items
		order_dict["contact_phone"] = mobile_no
		result.append(order_dict)

	return result


def _require_wholesale_admin_access():
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para acceder a este recurso."), frappe.PermissionError)


def _get_wholesale_completed_order_tracking(order_name):
	sales_invoice_name = ""
	payment_entry_name = ""
	invoice_status = ""
	completed_on = ""
	outstanding_amount = 0.0

	sales_invoices = frappe.get_all(
		"Sales Invoice Item",
		filters={"sales_order": order_name},
		pluck="parent",
		limit=1,
	)
	if sales_invoices:
		sales_invoice_name = sales_invoices[0]
		invoice_status = frappe.db.get_value("Sales Invoice", sales_invoice_name, "status") or ""
		completed_on = frappe.db.get_value("Sales Invoice", sales_invoice_name, "modified") or ""
		outstanding_amount = float(
			frappe.db.get_value("Sales Invoice", sales_invoice_name, "outstanding_amount") or 0.0
		)

		payment_entries = frappe.get_all(
			"Payment Entry Reference",
			filters={
				"reference_doctype": "Sales Invoice",
				"reference_name": sales_invoice_name,
				"docstatus": ["!=", 2],
			},
			pluck="parent",
			limit=1,
		)
		if payment_entries:
			payment_entry_name = payment_entries[0]

	return {
		"sales_invoice": sales_invoice_name,
		"payment_entry": payment_entry_name,
		"invoice_status": invoice_status,
		"completed_on": completed_on,
		"outstanding_amount": outstanding_amount,
	}


def _get_wholesale_order_rows(orders, include_tracking=False):
	result = []
	for o in orders:
		items = frappe.get_all(
			"Sales Order Item",
			filters={"parent": o.name},
			fields=["item_code", "item_name", "qty", "rate", "amount"],
			order_by="idx asc",
		)
		mobile_no = frappe.db.get_value("Customer", o.customer, "mobile_no") or ""
		order_dict = o.copy()
		order_dict["items"] = items
		order_dict["contact_phone"] = mobile_no

		if include_tracking:
			order_dict.update(_get_wholesale_completed_order_tracking(o.name))

		result.append(order_dict)

	return result


@frappe.whitelist()
def get_completed_wholesale_orders(limit=50):
	config = frappe.get_cached_doc("SaaS Feature Config")
	if not _wholesale_is_active(config):
		return []

	setup_wholesale_custom_fields()
	_require_wholesale_admin_access()

	try:
		limit = max(1, min(int(limit or 50), 100))
	except Exception:
		limit = 50

	orders = frappe.get_all(
		"Sales Order",
		filters={
			"docstatus": 1,
			"custom_metodo_pago": ["in", ["Transferencia", "Efectivo"]],
			"status": ["!=", "Cancelled"],
		},
		or_filters=[
			{"status": ["in", ["Completed", "Closed"]]},
			{"per_billed": [">=", 100]},
			{"per_delivered": [">=", 100]},
		],
		fields=[
			"name",
			"customer",
			"customer_name",
			"transaction_date",
			"delivery_date",
			"grand_total",
			"custom_metodo_pago",
			"custom_metodo_entrega",
			"status",
			"modified",
			"per_billed",
			"per_delivered",
		],
		order_by="modified desc",
		limit=limit,
	)

	return _get_wholesale_order_rows(orders, include_tracking=True)


@contextmanager
def _wholesale_completion_lock(sales_order_name, timeout_seconds=10):
	lock_name = f"wholesale_order_completion:{sales_order_name}"
	lock_result = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, timeout_seconds))
	lock_acquired = bool(lock_result and lock_result[0][0] == 1)
	if not lock_acquired:
		frappe.throw(
			frappe._("No se pudo asegurar el pedido mayorista. Intentá nuevamente."),
			frappe.ValidationError,
		)

	try:
		yield lock_name
	finally:
		try:
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
		except Exception:
			pass


@frappe.whitelist()
def complete_wholesale_order(sales_order_name, register_payment=True, payment_mode="Cash", warehouse=None):
	config = frappe.get_cached_doc("SaaS Feature Config")
	if not _wholesale_is_active(config):
		return _wholesale_disabled_response()

	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para acceder a este recurso."), frappe.PermissionError)

	if not frappe.db.exists("Sales Order", sales_order_name):
		frappe.throw(frappe._("El pedido {0} no existe.").format(sales_order_name))

	allowed_wholesale_payment_modes = {"Transferencia", "Efectivo"}
	so = frappe.get_doc("Sales Order", sales_order_name)
	if so.docstatus != 1:
		frappe.throw(
			frappe._("El pedido {0} debe estar confirmado antes de completarse.").format(sales_order_name)
		)

	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	frappe.db.commit()
	frappe.db.begin()
	try:
		with _wholesale_completion_lock(sales_order_name):
			so = frappe.get_doc("Sales Order", sales_order_name)
			if so.docstatus != 1:
				frappe.throw(
					frappe._("El pedido {0} debe estar confirmado antes de completarse.").format(
						sales_order_name
					)
				)

			order_payment_mode = (getattr(so, "custom_metodo_pago", "") or "").strip()
			order_status = (getattr(so, "status", "") or "").strip()
			per_billed = float(getattr(so, "per_billed", 0) or 0)
			per_delivered = float(getattr(so, "per_delivered", 0) or 0)

			if order_payment_mode not in allowed_wholesale_payment_modes:
				frappe.throw(
					frappe._("El pedido {0} no tiene un método de pago mayorista válido.").format(
						sales_order_name
					)
				)

			if (
				order_status in {"Completed", "Closed", "Cancelled"}
				or per_billed >= 100
				or per_delivered >= 100
			):
				frappe.throw(
					frappe._("El pedido {0} ya fue procesado o no está pendiente de completarse.").format(
						sales_order_name
					)
				)

			warehouse_name = warehouse or get_platform_distribution_warehouse()
			_validate_platform_distribution_warehouse(warehouse_name, company_name=so.company)
			should_register_payment = bool(frappe.utils.cint(register_payment))

			resolved_payment_mode = ""
			paid_to_account = ""
			if should_register_payment:
				resolved_payment_mode, paid_to_account = ensure_platform_payment_mode(
					payment_mode, company_name=so.company
				)

			# 1. Crear Sales Invoice a partir del Sales Order
			si = make_sales_invoice(sales_order_name)
			si.update_stock = 1
			si.posting_date = frappe.utils.today()
			si.set_posting_time = 1
			si.currency = so.currency

			# Asegurar que el almacén sea el correcto para todos los items
			for item in si.items:
				item.warehouse = warehouse_name

			si.insert(ignore_permissions=True)
			si.submit()

			# 2. Registrar el pago si se solicita
			advance_paid = 0.0
			if should_register_payment:
				from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

				grand_total = float(si.grand_total)
				pe = get_payment_entry("Sales Invoice", si.name, bank_amount=grand_total)
				pe.mode_of_payment = resolved_payment_mode
				pe.reference_no = f"Confirmacion Pedido Mayorista {sales_order_name}"
				pe.reference_date = frappe.utils.today()

				pe.paid_to = paid_to_account

				pe.paid_amount = grand_total
				pe.received_amount = grand_total
				if pe.references:
					pe.references[0].allocated_amount = grand_total

				pe.insert(ignore_permissions=True)
				pe.submit()
				advance_paid = grand_total

			frappe.db.commit()

			outstanding = float(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount") or 0.0)

			return {
				"success": True,
				"sales_invoice": si.name,
				"advance_paid": advance_paid,
				"grand_total": float(si.grand_total),
				"outstanding_amount": outstanding,
			}
	except Exception as e:
		frappe.db.rollback()
		frappe.throw(frappe._("Error al completar el pedido: {0}").format(str(e)))


def _cancel_or_delete_draft_doc(doctype, name):
	if not frappe.db.exists(doctype, name):
		return

	doc = frappe.get_doc(doctype, name)
	if doc.docstatus == 1:
		doc.cancel()
	elif doc.docstatus == 0:
		frappe.delete_doc(doctype, name, ignore_permissions=True)


def _cancel_sales_order_transaction_chain(sales_order_name):
	invoice_names = frappe.get_all(
		"Sales Invoice Item",
		filters={"sales_order": sales_order_name},
		pluck="parent",
	)
	relevant_names = [sales_order_name, *set(invoice_names)]

	payment_names = frappe.get_all(
		"Payment Entry Reference",
		filters={
			"reference_doctype": ["in", ["Sales Order", "Sales Invoice"]],
			"reference_name": ["in", relevant_names],
			"docstatus": ["!=", 2],
		},
		pluck="parent",
	)

	for payment_name in list(set(payment_names)):
		_cancel_or_delete_draft_doc("Payment Entry", payment_name)

	for invoice_name in list(set(invoice_names)):
		_cancel_or_delete_draft_doc("Sales Invoice", invoice_name)

	_cancel_or_delete_draft_doc("Sales Order", sales_order_name)


@frappe.whitelist()
def cancel_wholesale_order(sales_order_name):
	config = frappe.get_cached_doc("SaaS Feature Config")
	if not _wholesale_is_active(config):
		return _wholesale_disabled_response()

	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para acceder a este recurso."), frappe.PermissionError)

	if not frappe.db.exists("Sales Order", sales_order_name):
		frappe.throw(frappe._("El pedido {0} no existe.").format(sales_order_name))

	frappe.db.commit()
	frappe.db.begin()
	try:
		_cancel_sales_order_transaction_chain(sales_order_name)
		frappe.db.commit()
		return {
			"success": True,
			"message": frappe._("Pedido cancelado correctamente y el historial contable se conservó."),
		}
	except Exception as e:
		frappe.db.rollback()
		frappe.throw(frappe._("Error al cancelar el pedido: {0}").format(str(e)))


def _get_event_reservation_rows(states):
	normalized_states = [str(state).strip() for state in (states or []) if str(state).strip()]
	if not normalized_states:
		normalized_states = ["Pending Confirmation", "Confirmed"]

	reservations = frappe.get_all(
		"Event Cart Reservation",
		filters={"state": ["in", normalized_states]},
		fields=[
			"name",
			"sales_order",
			"customer",
			"event_date",
			"company",
			"grand_total",
			"advance_paid",
			"outstanding_amount",
			"assigned_cart_warehouse",
			"reservation_item_code",
			"state",
		],
		order_by="creation desc",
	)

	result = []
	for reservation in reservations:
		reservation_doc = frappe.get_doc("Event Cart Reservation", reservation.name)
		reservation_dict = frappe._dict(
			{
				"name": reservation_doc.name,
				"sales_order": reservation_doc.sales_order,
				"customer": reservation_doc.customer,
				"customer_name": frappe.db.get_value("Customer", reservation_doc.customer, "customer_name")
				or "",
				"contact_phone": frappe.db.get_value("Customer", reservation_doc.customer, "mobile_no") or "",
				"transaction_date": frappe.db.get_value(
					"Sales Order", reservation_doc.sales_order, "transaction_date"
				),
				"delivery_date": reservation_doc.event_date,
				"event_date": reservation_doc.event_date,
				"company": reservation_doc.company,
				"grand_total": float(reservation_doc.grand_total or 0.0),
				"advance_paid": float(reservation_doc.advance_paid or 0.0),
				"outstanding_amount": float(reservation_doc.outstanding_amount or 0.0),
				"status": reservation_doc.state,
				"state": reservation_doc.state,
				"assigned_cart_warehouse": reservation_doc.assigned_cart_warehouse,
				"reservation_item_code": reservation_doc.reservation_item_code,
				"sales_invoice": getattr(reservation_doc, "sales_invoice", ""),
				"payment_entry": getattr(reservation_doc, "payment_entry", ""),
				"delivery_note": getattr(reservation_doc, "delivery_note", ""),
				"cancel_reason": getattr(reservation_doc, "cancel_reason", ""),
				"release_notes": getattr(reservation_doc, "release_notes", ""),
			},
		)
		reservation_dict["items"] = [frappe._dict(item.as_dict()) for item in reservation_doc.items]
		result.append(reservation_dict)

	return result


@frappe.whitelist()
def get_event_reservations(states=None):
	_require_event_lifecycle_admin_access()

	if isinstance(states, str):
		try:
			states = frappe.parse_json(states)
		except Exception:
			states = [states]
	elif states is None:
		states = ["Pending Confirmation", "Confirmed"]
	elif not isinstance(states, (list, tuple, set)):
		states = [states]

	return _get_event_reservation_rows(states)


@frappe.whitelist()
def get_pending_event_bookings():
	_require_event_lifecycle_admin_access()
	return _get_event_reservation_rows(["Pending Confirmation"])


@frappe.whitelist()
def get_event_warehouses(company=None):
	_require_event_lifecycle_admin_access()
	warehouses = _get_allowed_event_warehouses(company_name=company)

	return warehouses


@frappe.whitelist()
def get_event_reservation_production_demand(event_date, company=None):
	_require_event_lifecycle_admin_access()
	return _get_event_reservation_production_demand(event_date, company=company)


def _get_allowed_event_warehouses(company_name=None):
	company = (company_name or get_platform_company_name()).strip()
	company_abbr = get_platform_company_abbr(company)
	parent_group_name = f"Carritos de Eventos - {company_abbr}"
	default_warehouse = get_platform_distribution_warehouse()

	warehouses = [{"name": default_warehouse, "warehouse_name": default_warehouse}]

	if frappe.db.exists("Warehouse", parent_group_name):
		event_warehouses = frappe.get_all(
			"Warehouse",
			filters={"parent_warehouse": parent_group_name, "company": company, "is_group": 0, "disabled": 0},
			fields=["name", "warehouse_name"],
		)
		for w in event_warehouses:
			warehouses.append({"name": w.name, "warehouse_name": w.warehouse_name})

	return warehouses


def _validate_event_booking_warehouse(warehouse, company_name=None):
	return validate_confirmed_allocation_warehouse(warehouse, company_name=company_name)


def _resolve_event_lifecycle_company(reservation_company, sales_order_company, sales_order_name):
	reservation_company = (reservation_company or "").strip()
	sales_order_company = (sales_order_company or "").strip()
	if reservation_company and sales_order_company and reservation_company != sales_order_company:
		frappe.throw(
			frappe._(
				"La reserva {0} pertenece a la compañía {1}, pero el pedido {0} pertenece a la compañía {2}."
			).format(sales_order_name, reservation_company, sales_order_company),
			frappe.ValidationError,
		)

	return reservation_company or sales_order_company or get_platform_company_name()


@contextmanager
def _event_reservation_named_lock(lock_key, timeout_seconds=5):
	lock_result = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_key, timeout_seconds))
	acquired = bool(lock_result and frappe.utils.cint(lock_result[0][0]))
	if not acquired:
		frappe.throw(
			frappe._("No se pudo bloquear la reserva de evento. Volvé a intentar."),
			frappe.ValidationError,
		)

	try:
		yield
	finally:
		try:
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))
		except Exception:
			pass


def _event_reservation_lock_key(operation, sales_order_name):
	import hashlib

	site = str(getattr(frappe.local, "site", "unknown-site") or "unknown-site")
	identity = f"{site}:{sales_order_name}"
	digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
	return f"event_reservation:{operation}:{digest}"


def _save_event_reservation(reservation):
	reservation.flags.event_reservation_service_operation = True
	return reservation.save(ignore_permissions=True)


@frappe.whitelist()
def confirm_event_reservation(sales_order_name, register_payment=True, payment_mode="Cash", warehouse=None):
	_require_event_lifecycle_admin_access()
	lock_key = _event_reservation_lock_key("lifecycle", sales_order_name)
	with _event_reservation_named_lock(lock_key, timeout_seconds=10):
		return _confirm_event_reservation_locked(
			sales_order_name,
			register_payment=register_payment,
			payment_mode=payment_mode,
			warehouse=warehouse,
		)


def _confirm_event_reservation_locked(
	sales_order_name, register_payment=True, payment_mode="Cash", warehouse=None
):
	reservation = _get_event_reservation(sales_order_name)
	if reservation.state == "Released":
		frappe.throw(
			frappe._("La reserva {0} ya fue liberada y no puede confirmarse otra vez.").format(
				sales_order_name
			)
		)

	if reservation.state == "Cancelled":
		frappe.throw(frappe._("La reserva {0} ya fue cancelada.").format(sales_order_name))

	so = frappe.get_doc("Sales Order", sales_order_name)
	if so.docstatus != 1:
		frappe.throw(
			frappe._("La reserva {0} debe estar confirmada antes de completarse.").format(sales_order_name)
		)

	company_name = _resolve_event_lifecycle_company(reservation.company, so.company, sales_order_name)
	should_register_payment = bool(frappe.utils.cint(register_payment))
	if not should_register_payment:
		frappe.throw(
			frappe._(
				"La confirmación de reservas requiere registrar el pago; no se admite register_payment=0."
			),
			frappe.ValidationError,
		)

	if reservation.state == "Confirmed" and reservation.sales_invoice:
		if reservation.assigned_cart_warehouse:
			_validate_event_booking_warehouse(reservation.assigned_cart_warehouse, company_name=company_name)
		return {
			"success": True,
			"sales_order": reservation.sales_order,
			"reservation": reservation.name,
			"reservation_state": reservation.state,
			"sales_invoice": reservation.sales_invoice,
			"payment_entry": reservation.payment_entry,
			"grand_total": float(reservation.grand_total or 0.0),
			"advance_paid": float(reservation.advance_paid or 0.0),
			"outstanding_amount": float(reservation.outstanding_amount or 0.0),
			"assigned_cart_warehouse": reservation.assigned_cart_warehouse,
		}

	payment_account = None

	warehouse = warehouse or get_platform_distribution_warehouse()
	warehouse = _validate_event_booking_warehouse(warehouse, company_name=company_name)

	expected_payment_amount = max(
		float(getattr(so, "grand_total", 0.0) or 0.0),
		float(getattr(so, "outstanding_amount", 0.0) or 0.0),
	)
	if expected_payment_amount > 0:
		payment_account = get_platform_payment_account(payment_mode)

	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	frappe.db.commit()
	frappe.db.begin()
	try:
		# 1. Crear Sales Invoice a partir del Sales Order
		si = make_sales_invoice(sales_order_name)
		si.update_stock = 0
		si.posting_date = frappe.utils.today()
		si.set_posting_time = 1
		si.currency = so.currency
		si.insert(ignore_permissions=True)
		si.submit()

		# 2. Registrar el pago si se solicita, considerando la diferencia restante
		advance_paid = 0.0
		outstanding = float(si.outstanding_amount)
		pe = None
		if outstanding > 0:
			from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

			pe = get_payment_entry("Sales Invoice", si.name, bank_amount=outstanding)
			pe.mode_of_payment = payment_mode
			pe.reference_no = f"Confirmacion Reserva Evento {sales_order_name}"
			pe.reference_date = frappe.utils.today()

			pe.paid_to = payment_account

			pe.paid_amount = outstanding
			pe.received_amount = outstanding
			if pe.references:
				pe.references[0].allocated_amount = outstanding

			pe.insert(ignore_permissions=True)
			pe.submit()
			advance_paid = outstanding

		reservation.assigned_cart_warehouse = warehouse
		reservation.sales_invoice = si.name
		reservation.payment_entry = pe.name if pe else reservation.payment_entry
		reservation.grand_total = float(si.grand_total or 0.0)
		reservation.base_grand_total = float(
			getattr(si, "base_grand_total", si.grand_total) or si.grand_total or 0.0
		)
		reservation.advance_paid = advance_paid
		reservation.outstanding_amount = float(
			frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount") or 0.0
		)
		reservation.state = "Confirmed"
		reservation.confirmed_at = frappe.utils.now_datetime()
		reservation.confirmed_by = frappe.session.user
		_save_event_reservation(reservation)
		frappe.db.commit()

		updated_outstanding = float(
			frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount") or 0.0
		)

		return {
			"success": True,
			"sales_order": so.name,
			"reservation": reservation.name,
			"reservation_state": reservation.state,
			"sales_invoice": si.name,
			"payment_entry": pe.name if pe else reservation.payment_entry,
			"advance_paid": advance_paid,
			"grand_total": float(si.grand_total),
			"outstanding_amount": updated_outstanding,
			"assigned_cart_warehouse": reservation.assigned_cart_warehouse,
		}
	except Exception as e:
		frappe.db.rollback()
		frappe.throw(frappe._("Error al completar la reserva: {0}").format(str(e)))


@frappe.whitelist()
def complete_event_booking(sales_order_name, register_payment=True, payment_mode="Cash", warehouse=None):
	return confirm_event_reservation(
		sales_order_name,
		register_payment=register_payment,
		payment_mode=payment_mode,
		warehouse=warehouse,
	)


def _get_event_payment_entry_names(reference_doctypes, reference_names):
	rows = frappe.get_all(
		"Payment Entry Reference",
		filters={
			"reference_doctype": ["in", list(reference_doctypes)],
			"reference_name": ["in", list(reference_names)],
			"docstatus": 1,
		},
		fields=["parent"],
	)
	return sorted({row.get("parent") for row in rows if row.get("parent")})


def _payment_amount(payment, payment_type):
	fieldname = "received_amount" if payment_type == "Receive" else "paid_amount"
	return abs(frappe.utils.flt(payment.get(fieldname) or 0, 2))


def _validate_event_payment_identity(payment, original_invoice, payment_type):
	currency_field = "paid_to_account_currency" if payment_type == "Receive" else "paid_from_account_currency"
	if (
		payment.docstatus != 1
		or payment.get("payment_type") != payment_type
		or payment.get("party_type") != "Customer"
		or payment.get("party") != original_invoice.customer
		or payment.get("company") != original_invoice.company
		or payment.get(currency_field) != original_invoice.currency
	):
		frappe.throw(
			frappe._(
				"Un Payment Entry vinculado no coincide con el cliente, compañía o moneda de la reserva."
			),
			frappe.ValidationError,
		)


def _validate_payment_reference_amount(payment, allowed_references, expected_amount):
	matching_references = [
		reference
		for reference in (payment.get("references") or [])
		if (reference.reference_doctype, reference.reference_name) in allowed_references
	]
	if len(matching_references) != 1:
		frappe.throw(
			frappe._("Cada Payment Entry debe tener una única referencia contable a esta reserva."),
			frappe.ValidationError,
		)

	allocated_amount = abs(frappe.utils.flt(matching_references[0].allocated_amount or 0, 2))
	if allocated_amount != expected_amount:
		frappe.throw(
			frappe._("El importe asignado del Payment Entry no coincide con su importe pagado."),
			frappe.ValidationError,
		)


def _validate_submitted_event_reversals(reservation):
	if not reservation.sales_invoice:
		frappe.throw(
			frappe._("La reserva confirmada no tiene una factura original vinculada."),
			frappe.ValidationError,
		)

	original_invoice = frappe.get_doc("Sales Invoice", reservation.sales_invoice)
	if original_invoice.docstatus != 1:
		frappe.throw(
			frappe._("La factura original de la reserva debe estar enviada."),
			frappe.ValidationError,
		)
	credit_note_name = str(reservation.get("credit_note") or "").strip()
	if not credit_note_name:
		frappe.throw(
			frappe._("Vinculá una nota de crédito enviada antes de cancelar la reserva."),
			frappe.ValidationError,
		)

	credit_note = frappe.get_doc("Sales Invoice", credit_note_name)
	credit_note_amount = abs(frappe.utils.flt(credit_note.grand_total or 0, 2))
	original_amount = abs(frappe.utils.flt(original_invoice.grand_total or 0, 2))
	if (
		credit_note.docstatus != 1
		or not credit_note.get("is_return")
		or credit_note.get("return_against") != original_invoice.name
		or credit_note.company != original_invoice.company
		or credit_note.customer != original_invoice.customer
		or credit_note.currency != original_invoice.currency
		or credit_note_amount != original_amount
	):
		frappe.throw(
			frappe._("La nota de crédito vinculada no revierte íntegramente la factura de la reserva."),
			frappe.ValidationError,
		)

	original_references = {
		("Sales Order", reservation.sales_order),
		("Sales Invoice", original_invoice.name),
	}
	original_payment_names = _get_event_payment_entry_names(
		("Sales Order", "Sales Invoice"),
		(reservation.sales_order, original_invoice.name),
	)
	stored_original_payment = str(reservation.get("payment_entry") or "").strip()
	if stored_original_payment and stored_original_payment not in original_payment_names:
		frappe.throw(
			frappe._("El Payment Entry guardado no tiene una referencia contable válida a la reserva."),
			frappe.ValidationError,
		)

	original_payments = []
	total_paid = 0.0
	for payment_name in original_payment_names:
		payment = frappe.get_doc("Payment Entry", payment_name)
		_validate_event_payment_identity(payment, original_invoice, "Receive")
		amount = _payment_amount(payment, "Receive")
		_validate_payment_reference_amount(payment, original_references, amount)
		original_payments.append(payment)
		total_paid += amount

	if not original_payments:
		if original_amount:
			frappe.throw(
				frappe._("No se encontraron pagos enviados vinculados a la reserva confirmada."),
				frappe.ValidationError,
			)
		return credit_note, [], [], 0.0

	refund_payment_names = _get_event_payment_entry_names(("Sales Invoice",), (credit_note.name,))
	stored_refund = str(reservation.get("refund_payment_entry") or "").strip()
	if stored_refund and stored_refund not in refund_payment_names:
		frappe.throw(
			frappe._("El Payment Entry de reembolso guardado no referencia la nota de crédito."),
			frappe.ValidationError,
		)

	refund_payments = []
	total_refunded = 0.0
	refund_reference = {("Sales Invoice", credit_note.name)}
	for payment_name in refund_payment_names:
		refund = frappe.get_doc("Payment Entry", payment_name)
		_validate_event_payment_identity(refund, original_invoice, "Pay")
		amount = _payment_amount(refund, "Pay")
		_validate_payment_reference_amount(refund, refund_reference, amount)
		refund_payments.append(refund)
		total_refunded += amount

	total_paid = frappe.utils.flt(total_paid, 2)
	total_refunded = frappe.utils.flt(total_refunded, 2)
	if total_refunded != total_paid:
		frappe.throw(
			frappe._("Los reembolsos enviados deben cubrir todos los pagos cobrados para la reserva."),
			frappe.ValidationError,
		)

	return credit_note, original_payments, refund_payments, total_refunded


@frappe.whitelist()
def cancel_event_reservation(
	sales_order_name,
	refund_evidence=None,
	reversal_evidence=None,
	cancel_reason=None,
	credit_note=None,
	refund_payment_entry=None,
):
	_require_event_lifecycle_admin_access()
	if any(str(value or "").strip() for value in (refund_evidence, reversal_evidence)):
		frappe.throw(
			frappe._(
				"El texto libre no constituye evidencia contable. Vinculá documentos de reversión enviados."
			),
			frappe.ValidationError,
		)

	lock_key = _event_reservation_lock_key("lifecycle", sales_order_name)
	with _event_reservation_named_lock(lock_key, timeout_seconds=10):
		reservation = _get_event_reservation(sales_order_name)
		if reservation.state == "Released":
			frappe.throw(frappe._("Las reservas liberadas no se pueden cancelar."))

		if reservation.state == "Cancelled":
			return {
				"success": True,
				"reservation": reservation.name,
				"reservation_state": reservation.state,
				"message": frappe._("La reserva ya estaba cancelada."),
			}

		if reservation.state == "Confirmed":
			reservation.credit_note = credit_note or reservation.credit_note
			reservation.refund_payment_entry = refund_payment_entry or reservation.refund_payment_entry
			credit_note, _original_payments, refunds, total_refunded = _validate_submitted_event_reversals(
				reservation
			)
			reservation.credit_note_amount = abs(float(credit_note.grand_total or 0))
			reservation.refund_amount = total_refunded
			if refunds and not reservation.refund_payment_entry:
				reservation.refund_payment_entry = refunds[0].name
		else:
			_cancel_sales_order_transaction_chain(sales_order_name)

		reservation.state = "Cancelled"
		reservation.cancel_reason = cancel_reason or reservation.cancel_reason
		reservation.cancelled_at = frappe.utils.now_datetime()
		reservation.cancelled_by = frappe.session.user
		_save_event_reservation(reservation)
		return {
			"success": True,
			"reservation": reservation.name,
			"reservation_state": reservation.state,
			"message": frappe._("Reserva cancelada correctamente y el historial contable se conservó."),
		}


@frappe.whitelist()
def cancel_event_booking(
	sales_order_name,
	refund_evidence=None,
	reversal_evidence=None,
	cancel_reason=None,
	credit_note=None,
	refund_payment_entry=None,
):
	return cancel_event_reservation(
		sales_order_name,
		refund_evidence=refund_evidence,
		reversal_evidence=reversal_evidence,
		cancel_reason=cancel_reason,
		credit_note=credit_note,
		refund_payment_entry=refund_payment_entry,
	)


@frappe.whitelist()
def release_event_reservation(sales_order_name, release_notes=None):
	_require_event_lifecycle_admin_access()

	lock_key = f"event_cart_reservation_release:{sales_order_name}"
	with _event_reservation_named_lock(lock_key):
		reservation = _get_event_reservation(sales_order_name)
		if reservation.state == "Cancelled":
			frappe.throw(frappe._("Las reservas canceladas no se pueden liberar."))

		so = frappe.get_doc("Sales Order", sales_order_name)
		if so.docstatus != 1:
			frappe.throw(
				frappe._("La reserva {0} debe estar confirmada antes de liberarse.").format(sales_order_name)
			)

		company_name = _resolve_event_lifecycle_company(reservation.company, so.company, sales_order_name)
		if not reservation.assigned_cart_warehouse:
			frappe.throw(
				frappe._("Las reservas confirmadas requieren un almacén asignado antes de liberarse.")
			)
		_validate_event_booking_warehouse(reservation.assigned_cart_warehouse, company_name=company_name)

		if reservation.delivery_note and reservation.state == "Released":
			return {
				"success": True,
				"reservation": reservation.name,
				"reservation_state": reservation.state,
				"delivery_note": reservation.delivery_note,
				"message": frappe._("La reserva ya estaba liberada."),
			}

		if reservation.delivery_note and reservation.state != "Released":
			reservation.state = "Released"
			reservation.release_notes = release_notes or reservation.release_notes
			reservation.released_at = frappe.utils.now_datetime()
			reservation.released_by = frappe.session.user
			_save_event_reservation(reservation)
			frappe.db.commit()
			return {
				"success": True,
				"reservation": reservation.name,
				"reservation_state": reservation.state,
				"delivery_note": reservation.delivery_note,
				"message": frappe._("La reserva ya tenía un Delivery Note asociado."),
			}

		if reservation.state != "Confirmed":
			frappe.throw(frappe._("Solo las reservas confirmadas pueden liberarse."))

		config = frappe.get_cached_doc("SaaS Feature Config")
		reservation_item_code = (
			reservation.reservation_item_code or config.get("reservation_item_code") or "Carrito Paletero"
		)

		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		frappe.db.commit()
		frappe.db.begin()
		try:
			dn = make_delivery_note(sales_order_name)
			dn.items = [item for item in dn.items if item.item_code != reservation_item_code]
			if not dn.items:
				frappe.throw(frappe._("No hay artículos facturables para liberar la reserva."))

			for item in dn.items:
				item.warehouse = reservation.assigned_cart_warehouse

			dn.insert(ignore_permissions=True)
			dn.submit()

			reservation.delivery_note = dn.name
			reservation.state = "Released"
			reservation.release_notes = release_notes or reservation.release_notes
			reservation.released_at = frappe.utils.now_datetime()
			reservation.released_by = frappe.session.user
			_save_event_reservation(reservation)
			frappe.db.commit()

			return {
				"success": True,
				"reservation": reservation.name,
				"reservation_state": reservation.state,
				"delivery_note": dn.name,
				"assigned_cart_warehouse": reservation.assigned_cart_warehouse,
				"message": frappe._("Reserva liberada correctamente."),
			}
		except Exception as e:
			frappe.db.rollback()
			frappe.throw(frappe._("Error al liberar la reserva: {0}").format(str(e)))


@frappe.whitelist()
def release_event_booking(sales_order_name, release_notes=None):
	return release_event_reservation(sales_order_name, release_notes=release_notes)


@frappe.whitelist(allow_guest=True)
def custom_logout():
	"""
	Bypasses standard POST-only CSRF deadlock on decoupled SaaS setups.
	Clears the active user session on the server via whitelisted GET access.
	"""
	frappe.local.login_manager.logout()
	frappe.db.commit()
	return {"success": True}


def create_notification_on_order(doc, method=None):
	"""
	Hook to create an unread SaaS Notification on a Sales Order insertion.
	Identifies if it's a wholesale order or an event booking, and creates the alert.
	"""
	try:
		# 1. Detect if it's a Wholesale Order
		is_wholesale = False
		if doc.get("custom_metodo_pago"):
			is_wholesale = True

		# 2. Detect if it's an Event Booking
		# We know a Sales Order is an event booking if it contains the reservation asset (e.g. "Carrito Paletero")
		is_event = False
		config = frappe.get_cached_doc("SaaS Feature Config")
		item_code = config.get("reservation_item_code") or "Carrito Paletero"

		for item in doc.items:
			if item.item_code == item_code:
				is_event = True
				break

		# 3. Create the SaaS Notification if applicable
		if is_wholesale or is_event:
			ensure_saas_notification_doctype()
			title = "Nuevo Pedido Mayorista" if is_wholesale else "Nueva Reserva de Evento"
			module = "Wholesale" if is_wholesale else "Event"
			message = (
				f"{doc.customer_name} ha registrado el pedido {doc.name}."
				if is_wholesale
				else f"{doc.customer_name} ha reservado un carrito en {doc.name}."
			)

			notif = frappe.new_doc("SaaS Notification")
			notif.title = title
			notif.message = message
			notif.module = module
			notif.reference_doctype = "Sales Order"
			notif.reference_name = doc.name
			notif.read = 0
			notif.insert(ignore_permissions=True)
			frappe.db.commit()
	except Exception as e:
		frappe.log_error(message=str(e), title="Error in create_notification_on_order hook")


@frappe.whitelist()
def get_unread_notifications():
	ensure_saas_notification_doctype()

	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para acceder a esta información."), frappe.PermissionError)

	# 1. Count unread notifications
	unread_count = frappe.db.count("SaaS Notification", filters={"read": 0})

	# 2. Fetch 5 most recent unread notifications
	notifications = frappe.get_all(
		"SaaS Notification",
		filters={"read": 0},
		fields=["name", "title", "message", "module", "reference_name", "creation"],
		order_by="creation desc",
		limit=5,
	)

	# Convert datetime objects to string format for JSON serialization
	for n in notifications:
		if n.get("creation"):
			n["creation"] = str(n["creation"])

	return {"unread_count": unread_count, "notifications": notifications}


@frappe.whitelist()
def mark_notification_as_read(notification_name):
	if not notification_name:
		frappe.throw(frappe._("Falta el nombre de la notificación."))

	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para realizar esta acción."), frappe.PermissionError)

	frappe.db.set_value("SaaS Notification", notification_name, "read", 1)
	frappe.db.commit()
	return {"success": True}


@frappe.whitelist()
def get_admin_dashboard_metrics():
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(
			frappe._("No tenés permisos para acceder a esta información de reportes."), frappe.PermissionError
		)

	today = frappe.utils.today()
	company = get_platform_company_name()
	company_abbr = get_platform_company_abbr(company)
	# 1. Ventas del día (POS + Mayoristas)
	pos_sales = (
		frappe.db.sql(
			"""
        SELECT SUM(grand_total)
        FROM `tabSales Invoice`
        WHERE docstatus = 1 AND posting_date = %s AND is_pos = 1 AND company = %s
    """,
			(today, company),
		)[0][0]
		or 0.0
	)

	wholesale_sales = (
		frappe.db.sql(
			"""
        SELECT SUM(grand_total)
        FROM `tabSales Invoice`
        WHERE docstatus = 1 AND posting_date = %s AND is_pos = 0 AND company = %s
    """,
			(today, company),
		)[0][0]
		or 0.0
	)

	total_sales_today = float(pos_sales + wholesale_sales)

	# 2. Órdenes mayoristas pendientes y total valorizado
	pending_wholesale = frappe.db.sql(
		"""
        SELECT COUNT(*), SUM(grand_total)
        FROM `tabSales Order`
        WHERE docstatus = 1 AND status NOT IN ('Completed', 'Closed', 'Cancelled') AND company = %s
    """,
		(company,),
	)[0]
	pending_wholesale_count = int(pending_wholesale[0] or 0)
	pending_wholesale_total = float(pending_wholesale[1] or 0.0)

	# 3. Reservas de eventos pendientes y total valorizado (usando item_code configurable)
	config = frappe.get_cached_doc("SaaS Feature Config")
	item_code = config.get("reservation_item_code") or "Carrito Paletero"

	pending_events = frappe.db.sql(
		"""
        SELECT COUNT(DISTINCT so.name), SUM(so.grand_total)
        FROM `tabSales Order` so
        JOIN `tabSales Order Item` soi ON so.name = soi.parent
        WHERE so.docstatus = 1 AND so.status NOT IN ('Completed', 'Closed', 'Cancelled') AND soi.item_code = %s AND so.company = %s
    """,
		(item_code, company),
	)[0]
	pending_events_count = int(pending_events[0] or 0)
	pending_events_total = float(pending_events[1] or 0.0)

	# 4. Alerta de stock crítico (menos de 100 unidades en Fábrica del tenant activo)
	low_stock_items = frappe.db.sql(
		"""
        SELECT b.item_code, i.item_name, b.warehouse, b.actual_qty
        FROM `tabBin` b
        JOIN `tabItem` i ON b.item_code = i.name
        WHERE b.warehouse = %s AND b.actual_qty < 100 AND i.disabled = 0
        ORDER BY b.actual_qty ASC
        LIMIT 5
    """,
		(f"Fabrica - {company_abbr}",),
		as_dict=1,
	)

	for item in low_stock_items:
		item["actual_qty"] = float(item["actual_qty"])

	# 5. Desglose de Métodos de Pago hoy
	payment_methods = frappe.db.sql(
		"""
        SELECT sip.mode_of_payment, SUM(sip.amount) as total
        FROM `tabSales Invoice Payment` sip
        JOIN `tabSales Invoice` si ON si.name = sip.parent
        WHERE si.docstatus = 1 AND si.posting_date = %s AND si.company = %s
        GROUP BY sip.mode_of_payment
    """,
		(today, company),
		as_dict=1,
	)

	for pm in payment_methods:
		pm["total"] = float(pm["total"])

	return {
		"success": True,
		"metrics": {
			"total_sales_today": total_sales_today,
			"pos_sales_today": float(pos_sales),
			"wholesale_sales_today": float(wholesale_sales),
			"pending_wholesale_count": pending_wholesale_count,
			"pending_wholesale_total": pending_wholesale_total,
			"pending_events_count": pending_events_count,
			"pending_events_total": pending_events_total,
			"low_stock_alerts": low_stock_items,
			"payment_methods_breakdown": payment_methods,
		},
	}


@frappe.whitelist()
def get_sales_report_data(start_date=None, end_date=None):
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(
			frappe._("No tenés permisos para acceder a esta información de reportes."), frappe.PermissionError
		)

	if not start_date:
		start_date = frappe.utils.add_months(frappe.utils.today(), -1)
	if not end_date:
		end_date = frappe.utils.today()

	company = get_platform_company_name()
	company_abbr = get_platform_company_abbr(company)
	suffix = f" - {company_abbr}"

	# 1. Tendencia de ventas diarias
	sales_trend = frappe.db.sql(
		"""
        SELECT posting_date as date, SUM(grand_total) as total
        FROM `tabSales Invoice`
        WHERE docstatus = 1 AND posting_date BETWEEN %s AND %s AND company = %s
        GROUP BY posting_date
        ORDER BY posting_date ASC
    """,
		(start_date, end_date, company),
		as_dict=1,
	)

	for s in sales_trend:
		s["date"] = str(s["date"])
		s["total"] = float(s["total"])

	# 2. Desglose de ventas por Sucursal/Almacén
	sales_by_branch = frappe.db.sql(
		"""
        SELECT sii.warehouse, SUM(sii.amount) as total
        FROM `tabSales Invoice Item` sii
        JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE si.docstatus = 1 AND si.posting_date BETWEEN %s AND %s AND si.company = %s
        GROUP BY sii.warehouse
        ORDER BY total DESC
    """,
		(start_date, end_date, company),
		as_dict=1,
	)

	for sb in sales_by_branch:
		sb["total"] = float(sb["total"])
		if sb.get("warehouse"):
			sb["branch"] = sb["warehouse"].replace(suffix, "")
		else:
			sb["branch"] = "Público General"

	# 3. Top 5 Productos más vendidos
	top_products = frappe.db.sql(
		"""
        SELECT sii.item_code, sii.item_name, SUM(sii.qty) as total_qty, SUM(sii.amount) as total_amount
        FROM `tabSales Invoice Item` sii
        JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE si.docstatus = 1 AND si.posting_date BETWEEN %s AND %s AND si.company = %s
        GROUP BY sii.item_code
        ORDER BY total_qty DESC
        LIMIT 5
    """,
		(start_date, end_date, company),
		as_dict=1,
	)

	for tp in top_products:
		tp["total_qty"] = float(tp["total_qty"])
		tp["total_amount"] = float(tp["total_amount"])

	# 4. Detalle de facturas del período
	detailed_sales = frappe.db.sql(
		"""
        SELECT si.name, si.posting_date as date, si.customer, si.customer_name, si.grand_total as total, si.is_pos,
               (SELECT GROUP_CONCAT(sip.mode_of_payment SEPARATOR ', ') FROM `tabSales Invoice Payment` sip WHERE sip.parent = si.name) as payment_mode
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1 AND si.posting_date BETWEEN %s AND %s AND si.company = %s
        ORDER BY si.posting_date DESC, si.creation DESC
        LIMIT 100
    """,
		(start_date, end_date, company),
		as_dict=1,
	)

	for ds in detailed_sales:
		ds["date"] = str(ds["date"])
		ds["total"] = float(ds["total"])
		if not ds.get("payment_mode"):
			ds["payment_mode"] = "Transferencia" if not ds["is_pos"] else "Efectivo"

	return {
		"success": True,
		"sales_trend": sales_trend,
		"sales_by_branch": sales_by_branch,
		"top_products": top_products,
		"detailed_sales": detailed_sales,
	}


@frappe.whitelist()
def get_stock_report_data():
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(
			frappe._("No tenés permisos para acceder a esta información de reportes."), frappe.PermissionError
		)

	company = get_platform_company_name()
	company_abbr = get_platform_company_abbr(company)
	suffix = f" - {company_abbr}"

	stock_data = frappe.db.sql(
		"""
        SELECT b.item_code, i.item_name, b.warehouse, b.actual_qty
        FROM `tabBin` b
        JOIN `tabItem` i ON b.item_code = i.name
        WHERE i.disabled = 0
          AND (i.item_group = 'Products' OR i.item_group IN (SELECT name FROM `tabItem Group` WHERE parent_item_group = 'Products'))
          AND b.actual_qty > 0
        ORDER BY b.warehouse ASC, b.actual_qty DESC
    """,
		as_dict=1,
	)

	for row in stock_data:
		row["actual_qty"] = float(row["actual_qty"])
		if row.get("warehouse"):
			row["branch"] = row["warehouse"].replace(suffix, "")
		else:
			row["branch"] = "Desconocido"

	return {"success": True, "stock_data": stock_data}


@frappe.whitelist()
def get_audit_report_data(start_date=None, end_date=None, limit=100):
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(
			frappe._("No tenés permisos para acceder a esta información de reportes."), frappe.PermissionError
		)

	if not start_date:
		start_date = frappe.utils.add_months(frappe.utils.today(), -1)
	if not end_date:
		end_date = frappe.utils.today()

	company = get_platform_company_name()
	company_abbr = get_platform_company_abbr(company)
	suffix = f" - {company_abbr}"

	# 1. Obtener movimientos de stock (Stock Ledger Entry)
	stock_moves = frappe.db.sql(
		"""
        SELECT
            sle.name,
            sle.creation as timestamp,
            sle.item_code,
            (SELECT item_name FROM `tabItem` WHERE name = sle.item_code) as item_name,
            sle.warehouse,
            sle.actual_qty,
            sle.voucher_type,
            sle.voucher_no,
            (SELECT owner FROM `tabStock Ledger Entry` WHERE name = sle.name) as user
        FROM `tabStock Ledger Entry` sle
        WHERE DATE(sle.posting_date) BETWEEN %s AND %s AND sle.company = %s
        ORDER BY sle.creation DESC
        LIMIT %s
    """,
		(start_date, end_date, company, frappe.utils.cint(limit)),
		as_dict=1,
	)

	for move in stock_moves:
		move["timestamp"] = str(move["timestamp"])
		move["actual_qty"] = float(move["actual_qty"])
		if move.get("warehouse"):
			move["branch"] = move["warehouse"].replace(suffix, "")
		else:
			move["branch"] = "Desconocido"

	# 2. Obtener historial de facturación de ventas (Sales Invoices)
	sales_moves = frappe.db.sql(
		"""
        SELECT
            name,
            creation as timestamp,
            customer_name,
            grand_total as amount,
            docstatus,
            owner as user,
            'Sales Invoice' as voucher_type
        FROM `tabSales Invoice`
        WHERE DATE(posting_date) BETWEEN %s AND %s AND company = %s
        ORDER BY creation DESC
        LIMIT %s
    """,
		(start_date, end_date, company, frappe.utils.cint(limit)),
		as_dict=1,
	)

	for sale in sales_moves:
		sale["timestamp"] = str(sale["timestamp"])
		sale["amount"] = float(sale["amount"])

	# 3. Obtener historial de modificaciones críticas (tabVersion)
	version_logs = frappe.db.sql(
		"""
        SELECT
            name,
            creation as timestamp,
            ref_doctype as voucher_type,
            docname as voucher_no,
            owner as user,
            data
        FROM `tabVersion`
        WHERE ref_doctype IN ('Sales Invoice', 'Stock Entry', 'Price List', 'Item Price', 'Warehouse')
          AND DATE(creation) BETWEEN %s AND %s
        ORDER BY creation DESC
        LIMIT %s
    """,
		(start_date, end_date, frappe.utils.cint(limit)),
		as_dict=1,
	)

	for log in version_logs:
		log["timestamp"] = str(log["timestamp"])
		try:
			import json

			log["data_diff"] = json.loads(log["data"]) if log.get("data") else None
		except Exception:
			log["data_diff"] = None
		log.pop("data", None)

	return {
		"success": True,
		"stock_moves": stock_moves,
		"sales_moves": sales_moves,
		"version_logs": version_logs,
	}


def _get_first_existing_tax_parent(company_abbr, parent_prefixes):
	for parent_prefix in parent_prefixes:
		parent_name = f"{parent_prefix} - {company_abbr}"
		if frappe.db.exists("Account", parent_name):
			return parent_name
	return None


def _ensure_mexican_tax_account(account_label, company_name, company_abbr, parent_prefixes):
	account_name = f"{account_label} - {company_abbr}"
	if frappe.db.exists("Account", account_name):
		return account_name

	parent_account = _get_first_existing_tax_parent(company_abbr, parent_prefixes)
	if not parent_account:
		available_parents = ", ".join(f"{prefix} - {company_abbr}" for prefix in parent_prefixes)
		frappe.logger("paletixa_saas").warning(
			frappe._(
				"Se omitió la cuenta {0} porque no se encontró ningún padre disponible entre: {1}"
			).format(account_name, available_parents)
		)
		return None

	doc = frappe.new_doc("Account")
	doc.account_name = account_label
	doc.parent_account = parent_account
	doc.company = company_name
	doc.account_type = "Tax"
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_mexican_tax_template(
	doctype, template_name, company_name, company_abbr, account_head, description
):
	if not account_head:
		frappe.logger("paletixa_saas").warning(
			frappe._("Se omitió la plantilla {0} porque no existe una cuenta contable válida.").format(
				template_name
			)
		)
		return

	template_candidates = [template_name]
	if company_abbr:
		template_candidates.insert(0, f"{template_name} - {company_abbr}")
	for candidate in template_candidates:
		if frappe.db.exists(doctype, candidate):
			return candidate

	doc = frappe.new_doc(doctype)
	doc.title = template_name
	doc.company = company_name
	doc.is_default = 1
	doc.append(
		"taxes",
		{
			"charge_type": "On Net Total",
			"account_head": account_head,
			"description": description,
			"rate": 16.0,
		},
	)
	doc.insert(ignore_permissions=True)


def setup_mexican_taxes_and_fields(company_name):
	# 1. Asegurar cuentas contables para IVA
	try:
		company_doc = frappe.get_cached_doc("Company", company_name)
	except Exception:
		frappe.throw(frappe._("La compañía {0} no existe.").format(company_name), frappe.DoesNotExistError)
	company_abbr = (company_doc.abbr or "").strip()
	if not company_abbr:
		frappe.throw(
			frappe._("La compañía {0} no tiene abreviatura configurada.").format(company_name),
			frappe.ValidationError,
		)

	# Cuenta de IVA Cobrado (Pasivo Directo)
	iva_cobrado_name = _ensure_mexican_tax_account(
		"IVA 16% Cobrado",
		company_name,
		company_abbr,
		("Current Liabilities", "Direct Liabilities"),
	)

	# Cuenta de IVA Pagado (Activo Circulante)
	iva_pagado_name = _ensure_mexican_tax_account(
		"IVA 16% Pagado",
		company_name,
		company_abbr,
		("Current Assets", "Direct Assets"),
	)

	frappe.db.commit()

	# 2. Crear Plantilla de Impuestos de Venta (IVA 16%)
	template_name = "IVA 16% México"
	_ensure_mexican_tax_template(
		"Sales Taxes and Charges Template",
		template_name,
		company_name,
		company_abbr,
		iva_cobrado_name,
		"IVA 16%",
	)

	# 3. Crear Plantilla de Impuestos de Compra (IVA 16% Compras)
	purchase_template_name = "IVA 16% México Compras"
	_ensure_mexican_tax_template(
		"Purchase Taxes and Charges Template",
		purchase_template_name,
		company_name,
		company_abbr,
		iva_pagado_name,
		"IVA 16% Compras",
	)

	frappe.db.commit()

	# 4. Crear Campos Personalizados para SAT
	custom_fields = [
		# Para Customer
		{"dt": "Customer", "fieldname": "rfc", "label": "RFC", "fieldtype": "Data", "insert_after": "tax_id"},
		{
			"dt": "Customer",
			"fieldname": "tax_regime",
			"label": "Régimen Fiscal",
			"fieldtype": "Select",
			"options": "\n601 | General de Ley Personas Morales\n603 | Personas Morales con Fines no Lucrativos\n605 | Sueldos y Salarios e Ingresos Asimilados a Salarios\n606 | Arrendamiento\n608 | Demás ingresos\n612 | Personas Físicas con Actividades Empresariales y Profesionales\n621 | Incorporación Fiscal\n625 | Régimen de Actividades Agrícolas, Ganaderas, Silvícolas y Pesqueras\n626 | Régimen Simplificado de Confianza (RESICO)",
			"insert_after": "rfc",
		},
		{
			"dt": "Customer",
			"fieldname": "cfdi_use",
			"label": "Uso de CFDI",
			"fieldtype": "Select",
			"options": "\nG01 | Adquisición de mercancías\nG02 | Devoluciones, descuentos o bonificaciones\nG03 | Gastos en general\nI01 | Construcciones\nI02 | Mobiliario y equipo de oficina por inversiones\nI03 | Equipo de transporte\nS01 | Sin efectos fiscales\nCP01 | Pagos",
			"insert_after": "tax_regime",
		},
		# Para Item
		{
			"dt": "Item",
			"fieldname": "sat_product_code",
			"label": "Código de Producto SAT",
			"fieldtype": "Data",
			"insert_after": "item_group",
		},
		{
			"dt": "Item",
			"fieldname": "sat_uom_code",
			"label": "Código de Unidad SAT",
			"fieldtype": "Data",
			"insert_after": "sat_product_code",
		},
		# Para Sales Invoice
		{
			"dt": "Sales Invoice",
			"fieldname": "sat_payment_method",
			"label": "Método de Pago SAT",
			"fieldtype": "Select",
			"options": "PUE | Pago en una sola exhibición\nPPD | Pago en parcialidades o diferido",
			"insert_after": "company",
		},
		{
			"dt": "Sales Invoice",
			"fieldname": "sat_payment_option",
			"label": "Forma de Pago SAT",
			"fieldtype": "Select",
			"options": "01 | Efectivo\n02 | Cheque nominativo\n03 | Transferencia electrónica de fondos\n04 | Tarjeta de crédito\n28 | Tarjeta de débito\n99 | Por definir",
			"insert_after": "sat_payment_method",
		},
	]

	for f in custom_fields:
		name = f"{f['dt']}-{f['fieldname']}"
		if not frappe.db.exists("Custom Field", name):
			doc = frappe.new_doc("Custom Field")
			doc.dt = f["dt"]
			doc.fieldname = f["fieldname"]
			doc.label = f["label"]
			doc.fieldtype = f["fieldtype"]
			doc.insert_after = f["insert_after"]
			if "options" in f:
				doc.options = f["options"]
			doc.insert(ignore_permissions=True)

	frappe.db.commit()
	frappe.clear_cache(doctype="Customer")
	frappe.clear_cache(doctype="Item")
	frappe.clear_cache(doctype="Sales Invoice")


@frappe.whitelist()
def seed_demo_data():
	"""Genera datos históricos transaccionales completos para La Paletixa en lapaletixa.localhost"""
	if not (is_tenant_admin_user() or _is_system_manager()):
		frappe.throw(frappe._("No tenés permisos para realizar esta acción."), frappe.PermissionError)

	# 0. Asegurar contexto
	company = "La Paletixa"
	if not frappe.db.exists("Company", company):
		frappe.throw(frappe._("La compañía 'La Paletixa' no existe. Por favor, corra el setup primero."))

	# Guardar over_billing_allowance actual y establecerlo a 1000% para evitar OverAllowanceError
	old_allowance = frappe.db.get_single_value("Accounts Settings", "over_billing_allowance") or 0.0
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 1000.0)

	# Asegurar año fiscal 2026
	if not frappe.db.exists("Fiscal Year", "2026"):
		frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": "2026",
				"year_start_date": "2026-01-01",
				"year_end_date": "2026-12-31",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	# Asegurar cuenta de Banco (Ledger hoja) para transacciones de tarjeta/transferencia
	bank_group = "Bank Accounts - LP"
	bank_account = "Banco - LP"
	if frappe.db.exists("Account", bank_group) and not frappe.db.exists("Account", bank_account):
		doc = frappe.new_doc("Account")
		doc.account_name = "Banco"
		doc.parent_account = bank_group
		doc.company = company
		doc.account_type = "Bank"
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		print("  - Cuenta de banco hoja 'Banco - LP' creada exitosamente.")

		# Actualizar Credit Card Mode of Payment
		if frappe.db.exists("Mode of Payment", "Credit Card"):
			cc_mop = frappe.get_doc("Mode of Payment", "Credit Card")
			for acc in cc_mop.accounts:
				if acc.company == company:
					acc.default_account = bank_account
			cc_mop.save(ignore_permissions=True)
			frappe.db.commit()
			print("  - Modo de pago 'Credit Card' actualizado a 'Banco - LP'.")

	# Habilitar configuraciones SaaS necesarias para reservas y mayoreo
	update_saas_config(
		has_pos=1,
		has_production=1,
		has_logistics=1,
		has_reservations=1,
		has_wholesale=1,
		reservation_item_code="Carrito Paletero",
		max_reservation_assets=10,
	)

	# 1. Limpiar transacciones piloto previas para evitar duplicidad de stock e historial
	print("🧹 Limpiando transacciones antiguas en La Paletixa...")

	# Obtener todas las Sales Invoices de la compañía y borrarlas
	invoices = frappe.get_all("Sales Invoice", filters={"company": company}, pluck="name")
	for name in invoices:
		frappe.db.set_value("Sales Invoice", name, "docstatus", 0)  # cancelar/poner en borrador
		frappe.delete_doc("Sales Invoice", name, force=True, ignore_permissions=True)

	# Obtener todas las POS Invoices
	pos_invoices = frappe.get_all("POS Invoice", filters={"company": company}, pluck="name")
	for name in pos_invoices:
		frappe.db.set_value("POS Invoice", name, "docstatus", 0)
		frappe.delete_doc("POS Invoice", name, force=True, ignore_permissions=True)

	# Obtener todas las Sales Orders
	orders = frappe.get_all("Sales Order", filters={"company": company}, pluck="name")
	for name in orders:
		frappe.db.set_value("Sales Order", name, "docstatus", 0)
		frappe.delete_doc("Sales Order", name, force=True, ignore_permissions=True)

	# Obtener Payment Entries
	payments = frappe.get_all("Payment Entry", filters={"company": company}, pluck="name")
	for name in payments:
		frappe.db.set_value("Payment Entry", name, "docstatus", 0)
		frappe.delete_doc("Payment Entry", name, force=True, ignore_permissions=True)

	# Obtener Stock Entries
	stock_entries = frappe.get_all("Stock Entry", filters={"company": company}, pluck="name")
	for name in stock_entries:
		frappe.db.set_value("Stock Entry", name, "docstatus", 0)
		frappe.delete_doc("Stock Entry", name, force=True, ignore_permissions=True)

	# Obtener notificaciones
	frappe.db.sql("DELETE FROM `tabSaaS Notification`")

	frappe.db.commit()
	print("🧹 Limpieza completada.")

	# 2. Cargar catálogo de ítems y variantes
	items = frappe.get_all(
		"Item",
		filters={
			"disabled": 0,
			"is_stock_item": 1,
			"has_variants": 0,
			"name": ["not in", ["Carrito Paletero"]],
		},
	)
	if not items:
		frappe.throw(frappe._("No se encontraron artículos en el catálogo. Corra setup_paletixa primero."))

	variant_names = [it.name for it in items]

	# 3. Ingresar stock en Fábrica (hace 32 días)
	print("🏭 Generando stock inicial en fábrica...")
	se_receipt = frappe.new_doc("Stock Entry")
	se_receipt.purpose = "Material Receipt"
	se_receipt.stock_entry_type = "Material Receipt"
	se_receipt.company = company
	se_receipt.posting_date = frappe.utils.add_days(frappe.utils.today(), -32)
	se_receipt.posting_time = "08:00:00"

	for item_code in variant_names:
		se_receipt.append(
			"items",
			{
				"item_code": item_code,
				"qty": 3000.0,
				"t_warehouse": "Fabrica - LP",
				"basic_rate": 5.0,
				"uom": "Unit",
			},
		)
	se_receipt.insert(ignore_permissions=True)
	se_receipt.submit()

	# Ingresar 10 carritos en Distribución
	if frappe.db.exists("Item", "Carrito Paletero"):
		se_carritos = frappe.new_doc("Stock Entry")
		se_carritos.purpose = "Material Receipt"
		se_carritos.stock_entry_type = "Material Receipt"
		se_carritos.company = company
		se_carritos.posting_date = frappe.utils.add_days(frappe.utils.today(), -32)
		se_carritos.posting_time = "09:00:00"
		se_carritos.append(
			"items",
			{
				"item_code": "Carrito Paletero",
				"qty": 10.0,
				"t_warehouse": "Distribucion - LP",
				"basic_rate": 2000.0,
				"uom": "Unit",
			},
		)
		se_carritos.insert(ignore_permissions=True)
		se_carritos.submit()

	print("✅ Stock inicial creado.")

	# 4. Transferir stock de Fábrica a Bodega Distribución (hace 30 días)
	print("📦 Distribuyendo stock a Bodega central...")
	se_transfer_dist = frappe.new_doc("Stock Entry")
	se_transfer_dist.purpose = "Material Transfer"
	se_transfer_dist.stock_entry_type = "Material Transfer"
	se_transfer_dist.company = company
	se_transfer_dist.posting_date = frappe.utils.add_days(frappe.utils.today(), -30)
	se_transfer_dist.posting_time = "10:00:00"

	for item_code in variant_names:
		se_transfer_dist.append(
			"items",
			{
				"item_code": item_code,
				"qty": 2400.0,
				"s_warehouse": "Fabrica - LP",
				"t_warehouse": "Distribucion - LP",
				"uom": "Unit",
				"basic_rate": 5.0,
			},
		)
	se_transfer_dist.insert(ignore_permissions=True)
	se_transfer_dist.submit()

	# 5. Trasladar stock a Sucursales (hace 28 días)
	print("🏪 Abasteciendo sucursales...")
	for s in range(1, 5):
		se_branch = frappe.new_doc("Stock Entry")
		se_branch.purpose = "Material Transfer"
		se_branch.stock_entry_type = "Material Transfer"
		se_branch.company = company
		se_branch.posting_date = frappe.utils.add_days(frappe.utils.today(), -28)
		se_branch.posting_time = "12:00:00"

		for item_code in variant_names:
			se_branch.append(
				"items",
				{
					"item_code": item_code,
					"qty": 300.0,
					"s_warehouse": "Distribucion - LP",
					"t_warehouse": f"Sucursal {s} - LP",
					"uom": "Unit",
					"basic_rate": 5.0,
				},
			)
		se_branch.insert(ignore_permissions=True)
		se_branch.submit()

	print("✅ Distribución a sucursales completada.")

	# 6. Simular ventas de mostrador (POS Invoices) del último mes
	import random

	print("💰 Generando facturas del POS históricas...")
	sales_created = 0

	for day_offset in range(1, 31):
		posting_date = frappe.utils.add_days(frappe.utils.today(), -day_offset)

		# 3 a 5 ventas por día
		num_sales = random.randint(3, 5)
		for _ in range(num_sales):
			sucursal = random.randint(1, 4)
			payment_mode = "Cash" if random.random() < 0.70 else "Credit Card"

			# 1 a 4 artículos aleatorios
			num_items = random.randint(1, 4)
			selected_items = random.sample(variant_names, min(num_items, len(variant_names)))

			si = frappe.new_doc("Sales Invoice")
			si.company = company
			si.posting_date = posting_date
			si.posting_time = f"{random.randint(9, 21):02d}:{random.randint(0, 59):02d}:00"
			si.customer = "Público General"
			si.is_pos = 1
			si.pos_profile = f"Punto de Venta - Sucursal {sucursal}"
			si.update_stock = 1
			si.selling_price_list = "Standard Selling"
			si.currency = "MXN"

			grand_total = 0.0
			for item_code in selected_items:
				price = (
					frappe.db.get_value(
						"Item Price",
						{"item_code": item_code, "price_list": "Standard Selling"},
						"price_list_rate",
					)
					or 15.0
				)
				qty = float(random.randint(1, 6))
				amount = price * qty
				si.append(
					"items",
					{
						"item_code": item_code,
						"qty": qty,
						"rate": price,
						"warehouse": f"Sucursal {sucursal} - LP",
						"uom": "Unit",
					},
				)
				grand_total += amount

			# Set payments
			si.append("payments", {"mode_of_payment": payment_mode, "amount": grand_total})

			si.insert(ignore_permissions=True)
			si.submit()
			sales_created += 1

	print(f"✅ {sales_created} ventas del POS registradas correctamente.")

	# 7. Simular ventas mayoristas (Wholesale)
	# Crearemos 8 pedidos completados e históricos y 2 pendientes de completar
	print("🤝 Generando ventas mayoristas históricas...")

	# Crear cliente mayorista si no existe
	wholesale_customer = "Distribuidora del Norte"
	if not frappe.db.exists("Customer", wholesale_customer):
		c_doc = frappe.new_doc("Customer")
		c_doc.customer_name = wholesale_customer
		c_doc.customer_type = "Company"
		c_doc.customer_group = "Individual"
		c_doc.territory = "Mexico"
		c_doc.insert(ignore_permissions=True)

	for i in range(1, 11):
		# 1 a 15 días atrás
		day_offset = random.randint(2, 15)
		posting_date = frappe.utils.add_days(frappe.utils.today(), -day_offset)

		# Seleccionar 3 variantes aleatorias
		selected_items = random.sample(variant_names, 3)

		# Crear Sales Order
		so = frappe.new_doc("Sales Order")
		so.company = company
		so.customer = wholesale_customer
		so.transaction_date = posting_date
		so.delivery_date = frappe.utils.add_days(posting_date, 1)
		so.selling_price_list = "Standard Selling"
		so.currency = "MXN"
		so.custom_metodo_pago = "Transferencia"
		so.custom_metodo_entrega = "Domicilio"

		grand_total = 0.0
		for item_code in selected_items:
			# Wholesale rule gives discount if quantity >= 10, let's buy 50 units of each to get wholesale price
			retail_price = (
				frappe.db.get_value(
					"Item Price",
					{"item_code": item_code, "price_list": "Standard Selling"},
					"price_list_rate",
				)
				or 15.0
			)
			# Mayoreo es típicamente un 30% menos
			price = retail_price * 0.7
			qty = 50.0
			amount = price * qty
			so.append(
				"items",
				{
					"item_code": item_code,
					"qty": qty,
					"rate": price,
					"amount": amount,
					"warehouse": "Distribucion - LP",
					"uom": "Unit",
					"delivery_date": so.delivery_date,
				},
			)
			grand_total += amount

		so.run_method("calculate_taxes_and_totals")
		so.insert(ignore_permissions=True)
		so.submit()

		# Las primeras 8 se completan y facturan
		if i <= 8:
			from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

			si = make_sales_invoice(so.name)
			si.update_stock = 1
			si.posting_date = posting_date
			si.posting_time = "16:00:00"
			si.insert(ignore_permissions=True)
			si.submit()

			# Registrar el pago
			pe = frappe.new_doc("Payment Entry")
			pe.payment_type = "Receive"
			pe.posting_date = posting_date
			pe.company = company
			pe.party_type = "Customer"
			pe.party = wholesale_customer
			pe.paid_from = "Debtors - LP"
			pe.paid_to = "Banco - LP"
			pe.paid_amount = grand_total
			pe.received_amount = grand_total
			pe.target_exchange_rate = 1.0
			pe.reference_no = f"TRSF-WH-{so.name}"
			pe.reference_date = posting_date
			pe.append(
				"references",
				{
					"reference_doctype": "Sales Invoice",
					"reference_name": si.name,
					"allocated_amount": grand_total,
				},
			)
			pe.insert(ignore_permissions=True)
			pe.submit()

	print("✅ 8 facturas mayoristas históricas y 2 pedidos pendientes de completar agregados.")

	# 8. Simular Reservas de Eventos (Event Bookings) con carritos
	# Crearemos 3 reservas en fechas futuras (ej. en los próximos 3, 5, 8 días)
	print("🎉 Generando reservas de eventos para los próximos días...")

	event_guest_names = [
		{"name": "Boda de Mariana y Diego", "offset": 3, "qty": 1},
		{"name": "Fiesta Infantil Santiago", "offset": 5, "qty": 1},
		{"name": "Graduación Colegio Tepeyac", "offset": 8, "qty": 2},
	]

	for event in event_guest_names:
		booking_date = frappe.utils.add_days(frappe.utils.today(), event["offset"])

		# Seleccionar un par de sabores para el evento
		flavors = random.sample(variant_names, 2)
		items_list = []
		for _idx, fl in enumerate(flavors):
			items_list.append({"item_code": fl, "qty": 150.0, "rate": 10.0})  # 150 piezas de cada sabor

		create_event_booking(
			customer="Público General",
			delivery_date=booking_date,
			items=items_list,
			advance_amount=1000.0,
			payment_mode="Cash",
			guest_name=event["name"],
			guest_phone="+525566778899",
		)

	print("✅ Reservas de eventos del calendario generadas correctamente.")

	# 9. Limpieza final de cachés
	# Restaurar over_billing_allowance
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", old_allowance)
	frappe.db.commit()

	frappe.clear_cache()
	print("🚀 ¡SEDEER COMPLETADO CON ÉXITO! LA BASE DE DATOS DE LA PALETIXA ESTÁ VIVA.")

	return {
		"success": True,
		"message": "Base de datos de La Paletixa poblada con éxito con datos transaccionales realistas.",
	}


@frappe.whitelist()
def get_branches_and_cashiers():
	check_tenant_admin_permission()

	# 1. Obtener todas las sucursales (POS Profiles)
	profiles = frappe.get_all(
		"POS Profile", filters={"disabled": 0}, fields=["name", "warehouse", "company", "customer"]
	)
	branches = []
	for p in profiles:
		doc = frappe.get_doc("POS Profile", p.name)
		cashiers = []
		for u in doc.applicable_for_users:
			cashiers.append({"user": u.user, "default": u.default})
		branches.append({"name": p.name, "warehouse": p.warehouse, "cashiers": cashiers})

	# 2. Obtener todos los usuarios activos
	users = frappe.get_all(
		"User",
		filters={"enabled": 1, "name": ["not in", ["Administrator", "Guest"]]},
		fields=["name", "first_name", "last_name", "email"],
	)

	return {"branches": branches, "users": users}


@frappe.whitelist()
def create_new_branch_with_cashiers(branch_name, cashier_emails=None):
	check_tenant_admin_permission()

	if not branch_name:
		frappe.throw(frappe._("El nombre de la sucursal es obligatorio"))

	# Validar limite de sucursales desde SaaS Feature Config
	config = frappe.get_cached_doc("SaaS Feature Config")
	max_branches = int(config.get("max_branches", 3) if config.get("max_branches") is not None else 3)
	pos_profile_name = f"Punto de Venta - {branch_name}"

	if not frappe.db.exists("POS Profile", pos_profile_name):
		current_active_branches = frappe.db.count("POS Profile", filters={"disabled": 0})
		if current_active_branches >= max_branches:
			frappe.throw(
				frappe._(
					"Límite de sucursales alcanzado ({0} permitidas). Contactá al administrador de la plataforma para mejorar tu plan."
				).format(max_branches)
			)

	company = get_platform_company_name()
	company_abbr = get_platform_company_abbr(company)

	# Parsear emails de cajeros si vienen como string JSON
	if isinstance(cashier_emails, str):
		cashier_emails = frappe.parse_json(cashier_emails)

	# 1. Crear el Almacén si no existe
	warehouse_name = f"{branch_name} - {company_abbr}"
	if not frappe.db.exists("Warehouse", warehouse_name):
		w_doc = frappe.new_doc("Warehouse")
		w_doc.warehouse_name = branch_name
		w_doc.parent_warehouse = f"All Warehouses - {company_abbr}"
		w_doc.company = company
		w_doc.insert(ignore_permissions=True)

	# 2. Crear o actualizar el POS Profile
	pos_profile_name = f"Punto de Venta - {branch_name}"
	if not frappe.db.exists("POS Profile", pos_profile_name):
		# Intentamos obtener un perfil base de sucursal
		base_profile_name = "Punto de Venta - Sucursal 1"
		if not frappe.db.exists("POS Profile", base_profile_name):
			profiles = frappe.get_all("POS Profile", limit=1)
			if profiles:
				base_profile_name = profiles[0].name
			else:
				base_profile_name = None

		if base_profile_name:
			base_profile = frappe.get_doc("POS Profile", base_profile_name)
			new_profile = frappe.copy_doc(base_profile)
			new_profile.name = pos_profile_name
			new_profile.warehouse = warehouse_name
			new_profile.applicable_for_users = []
		else:
			new_profile = frappe.new_doc("POS Profile")
			new_profile.name = pos_profile_name
			new_profile.company = company
			new_profile.warehouse = warehouse_name
			new_profile.customer = "Público General"
			new_profile.currency = "MXN"
			new_profile.selling_price_list = "Standard Selling"
			new_profile.applicable_for_users = []

		# Asignar los cajeros
		if cashier_emails:
			for email in cashier_emails:
				already_default = frappe.db.exists("POS Profile User", {"user": email, "default": 1})
				new_profile.append(
					"applicable_for_users", {"user": email, "default": 0 if already_default else 1}
				)
		new_profile.insert(ignore_permissions=True)
	else:
		# Si ya existe, actualizamos los cajeros
		profile = frappe.get_doc("POS Profile", pos_profile_name)
		profile.applicable_for_users = []
		if cashier_emails:
			for email in cashier_emails:
				# Comprobar si ya tiene un perfil por defecto que no sea el actual
				already_default = frappe.db.exists(
					"POS Profile User", {"user": email, "default": 1, "parent": ["!=", pos_profile_name]}
				)
				profile.append(
					"applicable_for_users", {"user": email, "default": 0 if already_default else 1}
				)
		profile.save(ignore_permissions=True)

	frappe.db.commit()
	return {"success": True, "message": f"Sucursal '{branch_name}' configurada exitosamente."}


@frappe.whitelist()
def delete_branch(branch_name):
	check_tenant_admin_permission()

	if not branch_name:
		frappe.throw(frappe._("El nombre de la sucursal es obligatorio"))

	pos_profile_name = f"Punto de Venta - {branch_name}"
	company_abbr = "LP"
	warehouse_name = f"{branch_name} - {company_abbr}"

	try:
		# Intentar borrado físico del POS Profile si existe
		if frappe.db.exists("POS Profile", pos_profile_name):
			frappe.delete_doc("POS Profile", pos_profile_name, ignore_permissions=True)

		# Intentar borrado físico de la bodega si existe
		if frappe.db.exists("Warehouse", warehouse_name):
			frappe.delete_doc("Warehouse", warehouse_name, ignore_permissions=True)

		frappe.db.commit()
		return {
			"success": True,
			"message": f"Sucursal '{branch_name}' eliminada físicamente de la base de datos por completo.",
		}
	except Exception:
		# Si falla por integridad (transacciones existentes), caemos elegantemente a desactivación lógica
		frappe.db.rollback()

		# Desactivación Lógica del POS Profile
		if frappe.db.exists("POS Profile", pos_profile_name):
			frappe.db.set_value("POS Profile", pos_profile_name, "disabled", 1)

		# Desactivación Lógica de la bodega
		if frappe.db.exists("Warehouse", warehouse_name):
			frappe.db.set_value("Warehouse", warehouse_name, "disabled", 1)

		frappe.db.commit()

		return {
			"success": True,
			"message": f"La sucursal '{branch_name}' no se pudo eliminar físicamente por tener registros históricos, pero ha sido desactivada y archivada de forma segura para preservar los datos de auditoría.",
		}


@frappe.whitelist()
def get_users_with_roles():
	check_tenant_admin_permission()

	# 1. Obtener todos los usuarios del sistema excluyendo Administrador y Guest
	users = frappe.get_all(
		"User",
		filters={"name": ["not in", ["Administrator", "Guest"]]},
		fields=["name", "first_name", "last_name", "email", "enabled"],
	)

	# 2. Evitar consultas N+1: Obtener todos los roles asociados a usuarios en una única consulta de base de datos
	user_roles_list = frappe.get_all("Has Role", filters={"parenttype": "User"}, fields=["parent", "role"])

	# Agrupar roles por usuario en memoria
	roles_by_user = {}
	for ur in user_roles_list:
		parent = ur.get("parent")
		role = ur.get("role")
		if parent not in roles_by_user:
			roles_by_user[parent] = []
		roles_by_user[parent].append(role)

	# Asignar roles a cada objeto de usuario de forma directa y ultrarrápida
	for u in users:
		u["roles"] = roles_by_user.get(u.name, [])

	# Roles relevantes disponibles para la app
	available_roles = [
		"Sales User",
		"Accounts User",
		"Stock User",
		"Stock Manager",
		"Manufacturing User",
		"System Manager",
		"Accounts Manager",
		"Sales Manager",
		"Item Manager",
	]

	return {"users": users, "available_roles": available_roles}


@frappe.whitelist()
def create_or_update_user(email, first_name, last_name, roles, password=None, enabled=1, is_new=1):
	check_tenant_admin_permission()

	if not email:
		frappe.throw(frappe._("El correo electrónico es obligatorio"))

	if isinstance(roles, str):
		roles = frappe.parse_json(roles)

	is_new = int(is_new)
	enabled = int(enabled)

	if is_new:
		if frappe.db.exists("User", email):
			frappe.throw(frappe._("El usuario con este correo ya existe"))
		if not password:
			frappe.throw(frappe._("La contraseña es obligatoria para nuevos usuarios"))

		user_doc = frappe.new_doc("User")
		user_doc.email = email
		user_doc.first_name = first_name
		user_doc.last_name = last_name
		user_doc.new_password = password
		user_doc.send_welcome_email = 0
		user_doc.enabled = enabled

		# Asignar roles
		if roles:
			for r in roles:
				user_doc.append("roles", {"role": r})

		user_doc.insert(ignore_permissions=True)
	else:
		if not frappe.db.exists("User", email):
			frappe.throw(frappe._("El usuario no existe"))

		user_doc = frappe.get_doc("User", email)
		user_doc.first_name = first_name
		user_doc.last_name = last_name
		user_doc.enabled = enabled

		if password:
			user_doc.new_password = password

		# Re-asignar roles
		user_doc.set("roles", [])
		if roles:
			for r in roles:
				user_doc.append("roles", {"role": r})

		user_doc.save(ignore_permissions=True)

	frappe.db.commit()
	return {"success": True, "message": f"Usuario '{email}' guardado exitosamente."}


def ensure_platform_admin_user(email, password):
	if not email:
		frappe.throw(frappe._("El correo electrónico es obligatorio"))

	if not password:
		frappe.throw(frappe._("La contraseña es obligatoria"))

	if not frappe.db.exists("User", email):
		user_doc = frappe.new_doc("User")
		user_doc.email = email
		user_doc.first_name = "Platform"
		user_doc.last_name = "Admin"
		user_doc.send_welcome_email = 0
		user_doc.insert(ignore_permissions=True)
	else:
		user_doc = frappe.get_doc("User", email)

	from frappe.utils.password import update_password

	update_password(email, password)

	if not any(r.role == "System Manager" for r in user_doc.roles):
		user_doc.add_roles("System Manager")

	if not user_doc.enabled:
		user_doc.enabled = 1
		user_doc.save(ignore_permissions=True)

	frappe.db.commit()
	return {"success": True, "message": f"User '{email}' ensured as platform admin."}


@frappe.whitelist()
def seed_test_stock():
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	# Verificar que el usuario tenga rol de System Manager
	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para ejecutar esta acción"), frappe.PermissionError)

	# 1. Definir items y almacenes
	# Buscamos variantes activas de productos
	subgroups = ["Products"] + [
		g.name for g in frappe.get_all("Item Group", filters={"parent_item_group": "Products"})
	]
	items = frappe.get_all(
		"Item", filters={"disabled": 0, "item_group": ["in", subgroups], "has_variants": 0}, pluck="name"
	)

	if not items:
		return {
			"success": False,
			"message": "No se encontraron productos activos (variantes) para inyectar stock.",
		}

	from frappe.utils import getdate

	# Calculate how much we need to transfer for each item to sucursal 1-4
	transfer_needs = {item_code: 0.0 for item_code in items}
	sucursales_transfers = {s: [] for s in range(1, 5)}
	for s in range(1, 5):
		target_wh = f"Sucursal {s} - LP"
		for item_code in items:
			current_target_qty = (
				frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": target_wh}, "actual_qty")
				or 0.0
			)
			if current_target_qty < 10.0:
				qty_to_transfer = 100.0
				sucursales_transfers[s].append(
					{
						"item_code": item_code,
						"s_warehouse": "Fabrica - LP",
						"t_warehouse": target_wh,
						"qty": qty_to_transfer,
						"uom": "Unit",
					}
				)
				transfer_needs[item_code] += qty_to_transfer

	# Cargar stock en Fabrica - LP si no es suficiente para cubrir las transferencias
	items_to_receipt = []
	for item_code in items:
		current_qty = (
			frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": "Fabrica - LP"}, "actual_qty")
			or 0.0
		)
		needed = transfer_needs[item_code]
		if current_qty < needed + 50.0:
			price = (
				frappe.db.get_value(
					"Item Price",
					{"item_code": item_code, "price_list": "Standard Selling"},
					"price_list_rate",
				)
				or 5.0
			)
			to_add = max(needed - current_qty + 50.0, 500.0)
			items_to_receipt.append(
				{
					"item_code": item_code,
					"t_warehouse": "Fabrica - LP",
					"qty": to_add,
					"uom": "Unit",
					"basic_rate": price,
				}
			)

	receipt_name = None
	if items_to_receipt:
		receipt = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"purpose": "Material Receipt",
				"stock_entry_type": "Material Receipt",
				"company": "La Paletixa",
				"posting_date": getdate(),
				"items": items_to_receipt,
			}
		)
		receipt.insert(ignore_permissions=True)
		receipt.submit()
		receipt_name = receipt.name

	# Realizar las transferencias a las sucursales
	transfers_created = []
	for s in range(1, 5):
		items_to_transfer = sucursales_transfers[s]
		if items_to_transfer:
			transfer = frappe.get_doc(
				{
					"doctype": "Stock Entry",
					"purpose": "Material Transfer",
					"stock_entry_type": "Material Transfer",
					"company": "La Paletixa",
					"posting_date": getdate(),
					"items": items_to_transfer,
				}
			)
			transfer.insert(ignore_permissions=True)
			transfer.submit()
			transfers_created.append(transfer.name)

	frappe.db.commit()

	return {
		"success": True,
		"message": f"¡Stock cargado con éxito! Entrada: {receipt_name or 'Ninguna (ya había stock)'}. Traspasos: {', '.join(transfers_created) or 'Ninguno'}",
	}


@frappe.whitelist()
def fix_item_price_permissions():
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para realizar esta acción"), frappe.PermissionError)

	from frappe.core.doctype.doctype.doctype import validate_permissions_for_doctype
	from frappe.permissions import setup_custom_perms

	item_price_perms = {
		"Sales User": ["read"],
		"Stock User": ["read"],
		"Manufacturing User": ["read"],
		"Stock Manager": ["read", "write", "create", "delete"],
		"Sales Manager": ["read", "write", "create", "delete"],
		"System Manager": ["read", "write", "create", "delete"],
	}

	setup_custom_perms("Item Price")
	for r_name, ptypes in item_price_perms.items():
		perm_name = frappe.db.get_value(
			"Custom DocPerm", dict(parent="Item Price", role=r_name, permlevel=0, if_owner=0)
		)
		if perm_name:
			custom_docperm = frappe.get_doc("Custom DocPerm", perm_name)
		else:
			custom_docperm = frappe.get_doc(
				{
					"doctype": "Custom DocPerm",
					"__islocal": 1,
					"parent": "Item Price",
					"parenttype": "DocType",
					"parentfield": "permissions",
					"role": r_name,
					"permlevel": 0,
				}
			)
		for p in ["read", "write", "create", "delete", "submit", "cancel", "amend"]:
			custom_docperm.set(p, 0)
		for pt in ptypes:
			custom_docperm.set(pt, 1)
		custom_docperm.save(ignore_permissions=True)

	validate_permissions_for_doctype("Item Price")
	frappe.db.commit()
	return {"success": True, "message": "Permisos de Item Price configurados con éxito."}


@frappe.whitelist()
def get_pos_shifts(start_date=None, end_date=None):
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para acceder a esta información"), frappe.PermissionError)

	# Query POS Opening Entries (each opening entry represents a shift)
	filters = {}
	if start_date:
		filters["posting_date"] = [">=", start_date]
	if end_date:
		if "posting_date" in filters:
			filters["posting_date"] = ["between", [start_date, end_date]]
		else:
			filters["posting_date"] = ["<=", end_date]

	openings = frappe.get_all(
		"POS Opening Entry",
		filters=filters,
		fields=[
			"name",
			"user",
			"pos_profile",
			"posting_date",
			"period_start_date",
			"status",
			"pos_closing_entry",
		],
		order_by="period_start_date desc",
		limit=100,
	)

	shifts = []
	for ope in openings:
		start = ope.period_start_date
		end = None
		closing_details = []
		grand_total = 0.0

		if ope.status == "Closed" and ope.pos_closing_entry:
			closing_doc = frappe.db.get_value(
				"POS Closing Entry", ope.pos_closing_entry, ["period_end_date", "grand_total"], as_dict=True
			)
			if closing_doc:
				end = closing_doc.period_end_date
				grand_total = closing_doc.grand_total

			# Fetch payment reconciliation details
			reconciliation = frappe.get_all(
				"POS Closing Entry Detail",
				filters={"parent": ope.pos_closing_entry},
				fields=[
					"mode_of_payment",
					"opening_amount",
					"expected_amount",
					"closing_amount",
					"difference",
				],
			)
			closing_details = reconciliation
		else:
			end = frappe.utils.now_datetime()
			try:
				closing_details = get_closing_reconciliation_details(ope.name)
			except Exception:
				closing_details = []

		# Find all Sales Invoices created during this shift
		invoices = frappe.get_all(
			"Sales Invoice",
			filters={
				"owner": ope.user,
				"pos_profile": ope.pos_profile,
				"creation": ["between", [start, end]],
				"docstatus": ["!=", 2],
			},
			fields=["name", "creation", "customer_name", "grand_total", "remarks", "docstatus"],
		)

		invoice_items_map = {}
		if invoices:
			invoice_names = [inv.name for inv in invoices]
			items = frappe.get_all(
				"Sales Invoice Item",
				filters={"parent": ["in", invoice_names]},
				fields=["parent", "item_code", "item_name", "qty", "rate", "amount"],
			)
			for item in items:
				if item.parent not in invoice_items_map:
					invoice_items_map[item.parent] = []
				invoice_items_map[item.parent].append(
					{
						"item_code": item.item_code,
						"item_name": item.item_name,
						"qty": item.qty,
						"rate": item.rate,
						"amount": item.amount,
					}
				)

		sales_count = len(invoices)
		sales_total = sum(inv.grand_total for inv in invoices)

		usd_sales_count = 0
		usd_amount_collected = 0.0
		usd_invoices = []

		import re

		for inv in invoices:
			if inv.remarks and "[Pago USD]" in inv.remarks:
				usd_amt = 0.0
				tc = 0.0
				cambio = 0.0
				try:
					match_recibido = re.search(r"Recibido:\s*\$(\d+(\.\d+)?)\s*USD", inv.remarks)
					match_tc = re.search(r"TC:\s*(\d+(\.\d+)?)\s*MXN", inv.remarks)
					match_cambio = re.search(r"Cambio:\s*\$(\d+(\.\d+)?)\s*MXN", inv.remarks)

					if match_recibido:
						usd_amt = float(match_recibido.group(1))
					if match_tc:
						tc = float(match_tc.group(1))
					if match_cambio:
						cambio = float(match_cambio.group(1))
				except Exception:
					pass

				usd_sales_count += 1
				usd_amount_collected += usd_amt
				usd_invoices.append(
					{
						"name": inv.name,
						"creation": inv.creation,
						"customer_name": inv.customer_name,
						"grand_total": inv.grand_total,
						"usd_amount": usd_amt,
						"exchange_rate": tc,
						"change_due": cambio,
						"remarks": inv.remarks,
					}
				)

		shifts.append(
			{
				"opening_entry": ope.name,
				"closing_entry": ope.pos_closing_entry,
				"user": ope.user,
				"pos_profile": ope.pos_profile,
				"period_start_date": start,
				"period_end_date": end,
				"status": ope.status,
				"grand_total": grand_total or sales_total,
				"sales_count": sales_count,
				"sales_total": sales_total,
				"usd_sales_count": usd_sales_count,
				"usd_amount_collected": usd_amount_collected,
				"usd_invoices": usd_invoices,
				"closing_details": closing_details,
				"invoices": [
					{
						"name": i.name,
						"creation": i.creation,
						"customer_name": i.customer_name,
						"grand_total": i.grand_total,
						"remarks": i.remarks,
						"docstatus": i.docstatus,
						"items": invoice_items_map.get(i.name, []),
					}
					for i in invoices
				],
			}
		)

	return {"success": True, "shifts": shifts}


@frappe.whitelist()
def get_assistant_keys():
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	# Verificar que el usuario tenga el asistente habilitado
	assistant_enabled = frappe.db.get_value("User", frappe.session.user, "assistant_enabled")
	if not assistant_enabled or int(assistant_enabled) != 1:
		frappe.throw(
			frappe._("El acceso del asistente de IA está desactivado para tu usuario"), frappe.PermissionError
		)

	user_doc = frappe.get_doc("User", frappe.session.user)

	# Si no tiene llaves de API, las generamos al vuelo automáticamente
	if not user_doc.api_key:
		user_doc.generate_keys()
		user_doc.save(ignore_permissions=True)
		frappe.db.commit()

	from frappe.utils.password import get_decrypted_password

	try:
		api_secret = get_decrypted_password("User", frappe.session.user, "api_secret")
	except Exception:
		# Si la desencriptación falla o la llave secreta se corrompió, la regeneramos
		user_doc.generate_keys()
		user_doc.save(ignore_permissions=True)
		frappe.db.commit()
		api_secret = get_decrypted_password("User", frappe.session.user, "api_secret")

	# Obtener información de la compañía activa de forma dinámica
	company = get_platform_company_name()
	company_abbr = get_platform_company_abbr(company)

	return {
		"success": True,
		"api_key": user_doc.api_key,
		"api_secret": api_secret,
		"company": company,
		"company_abbr": company_abbr,
	}


@frappe.whitelist()
def rename_item_group(old_name, new_name):
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para continuar"), frappe.PermissionError)

	if not frappe.has_permission("Item Group", "write"):
		frappe.throw(frappe._("No tenés permisos para modificar categorías"), frappe.PermissionError)

	old_name = old_name.strip()
	new_name = new_name.strip()

	if not old_name or not new_name:
		frappe.throw(frappe._("Los nombres no pueden estar vacíos"))

	if frappe.db.exists("Item Group", new_name):
		frappe.throw(frappe._("Ya existe una categoría con el nombre '{0}'").format(new_name))

	frappe.rename_doc("Item Group", old_name, new_name)
	frappe.db.commit()
	return {"success": True, "name": new_name}


def validate_item_type(doc, method=None):
	config = frappe.get_cached_doc("SaaS Feature Config")
	has_services = bool(config.get("has_services") if config.get("has_services") is not None else 1)
	has_products = bool(config.get("has_products") if config.get("has_products") is not None else 1)

	if not has_products and doc.is_stock_item:
		frappe.throw(
			frappe._("La gestión de productos está desactivada. No podés crear artículos inventariables.")
		)

	if not has_services and not doc.is_stock_item:
		frappe.throw(
			frappe._(
				"La gestión de servicios está desactivada. Todos los artículos deben ser inventariables."
			)
		)


def validate_product_features_enabled(doc, method=None):
	config = frappe.get_cached_doc("SaaS Feature Config")
	has_products = bool(config.get("has_products") if config.get("has_products") is not None else 1)
	if not has_products:
		frappe.throw(
			frappe._("La gestión de productos físicos/inventariables está desactivada en la configuración.")
		)


def validate_service_features_enabled(doc, method=None):
	config = frappe.get_cached_doc("SaaS Feature Config")
	has_services = bool(config.get("has_services") if config.get("has_services") is not None else 1)
	if not has_services:
		frappe.throw(
			frappe._(
				"La gestión de servicios (Hojas de horas, Mantenimientos, Soporte) está desactivada en la configuración."
			)
		)


def has_services_permission(doc, ptype=None, user=None):
	config = frappe.get_cached_doc("SaaS Feature Config")
	return bool(config.get("has_services") if config.get("has_services") is not None else 1)


def has_products_permission(doc, ptype=None, user=None):
	config = frappe.get_cached_doc("SaaS Feature Config")
	return bool(config.get("has_products") if config.get("has_products") is not None else 1)


def setup_service_role_permissions():
	doctypes = ["Timesheet", "Maintenance Visit", "Issue"]
	for dt in doctypes:
		if not frappe.db.exists("Custom DocPerm", {"parent": dt, "role": "System Manager", "permlevel": 0}):
			try:
				doc = frappe.get_doc(
					{
						"doctype": "Custom DocPerm",
						"parent": dt,
						"parenttype": "DocType",
						"parentfield": "permissions",
						"role": "System Manager",
						"permlevel": 0,
						"read": 1,
						"write": 1,
						"create": 1,
						"delete": 1,
						"submit": 1,
						"cancel": 1,
						"amend": 1,
						"report": 1,
						"export": 1,
						"share": 1,
						"print": 1,
						"email": 1,
					}
				)
				doc.insert(ignore_permissions=True)
			except Exception as e:
				frappe.log_error(message=str(e), title=f"Error setting up Custom DocPerm for {dt}")

	frappe.clear_cache()


def setup_inventory_role_permissions():
	doctypes = ["Stock Entry", "Delivery Note", "Purchase Receipt"]
	for dt in doctypes:
		if not frappe.db.exists("Custom DocPerm", {"parent": dt, "role": "System Manager", "permlevel": 0}):
			try:
				doc = frappe.get_doc(
					{
						"doctype": "Custom DocPerm",
						"parent": dt,
						"parenttype": "DocType",
						"parentfield": "permissions",
						"role": "System Manager",
						"permlevel": 0,
						"read": 1,
						"write": 1,
						"create": 1,
						"delete": 1,
						"submit": 1,
						"cancel": 1,
						"amend": 1,
						"report": 1,
						"export": 1,
						"share": 1,
						"print": 1,
						"email": 1,
					}
				)
				doc.insert(ignore_permissions=True)
			except Exception as e:
				frappe.log_error(message=str(e), title=f"Error setting up Custom DocPerm for {dt}")

	frappe.clear_cache()


def setup_saas_role_permissions():
	setup_service_role_permissions()
	setup_inventory_role_permissions()


def get_services_permission_query_conditions(user):
	config = frappe.get_cached_doc("SaaS Feature Config")
	has_services = bool(config.get("has_services") if config.get("has_services") is not None else 1)
	if not has_services:
		return "1=0"
	return ""


def get_products_permission_query_conditions(user):
	config = frappe.get_cached_doc("SaaS Feature Config")
	has_products = bool(config.get("has_products") if config.get("has_products") is not None else 1)
	if not has_products:
		return "1=0"
	return ""


def validate_purchasing_features_enabled(doc, method=None):
	config = frappe.get_cached_doc("SaaS Feature Config")
	if not bool(config.get("has_purchasing", 0)):
		frappe.throw(frappe._("El módulo de compras está desactivado en la configuración."))


def has_purchasing_permission(doc, ptype=None, user=None):
	config = frappe.get_cached_doc("SaaS Feature Config")
	return bool(config.get("has_purchasing", 0))


def get_purchasing_permission_query_conditions(user):
	config = frappe.get_cached_doc("SaaS Feature Config")
	if not bool(config.get("has_purchasing", 0)):
		return "1=0"
	return ""


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                        MÓDULO DE COMPRAS (PURCHASING)                       ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


@frappe.whitelist()
def get_suppliers():
	"""List all suppliers with purchase statistics."""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para acceder al módulo de compras"), frappe.PermissionError)

	suppliers = frappe.get_all(
		"Supplier",
		filters={"disabled": 0},
		fields=["name", "supplier_name", "supplier_group", "mobile_no", "email_id"],
		order_by="supplier_name asc",
		limit=500,
	)

	for s in suppliers:
		# Purchase stats from Purchase Order
		stats = frappe.db.sql(
			"""
            SELECT COUNT(name) as total_orders, COALESCE(SUM(grand_total), 0) as total_amount
            FROM `tabPurchase Order`
            WHERE supplier = %s AND docstatus = 1
        """,
			(s.name,),
			as_dict=True,
		)

		s["total_orders"] = stats[0].total_orders if stats else 0
		s["total_amount"] = float(stats[0].total_amount) if stats else 0.0

		# Last purchase date
		last_po = frappe.db.get_value(
			"Purchase Order",
			filters={"supplier": s.name, "docstatus": 1},
			fieldname="transaction_date",
			order_by="transaction_date desc",
		)
		s["last_purchase_date"] = str(last_po) if last_po else None

	return suppliers


@frappe.whitelist()
def create_supplier(supplier_name, phone=None, email=None, supplier_group=None):
	"""Create a new Supplier in ERPNext."""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para crear proveedores"), frappe.PermissionError)

	if not supplier_name or not supplier_name.strip():
		frappe.throw(frappe._("El nombre del proveedor no puede estar vacío"))

	supplier_name = supplier_name.strip()

	# Check for duplicates
	if frappe.db.exists("Supplier", {"supplier_name": supplier_name}):
		frappe.throw(frappe._("Ya existe un proveedor con el nombre '{0}'").format(supplier_name))

	doc = frappe.new_doc("Supplier")
	doc.supplier_name = supplier_name
	doc.supplier_group = supplier_group or "All Supplier Groups"
	doc.supplier_type = "Company"

	if phone:
		doc.mobile_no = phone.strip()
	if email:
		doc.email_id = email.strip()

	doc.insert(ignore_permissions=True)
	frappe.db.commit()

	return {"success": True, "name": doc.name, "supplier_name": doc.supplier_name}


@frappe.whitelist()
def update_supplier(supplier_name, phone=None, email=None, supplier_group=None):
	"""Update an existing Supplier."""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para modificar proveedores"), frappe.PermissionError)

	if not frappe.db.exists("Supplier", supplier_name):
		frappe.throw(frappe._("El proveedor '{0}' no existe").format(supplier_name), frappe.DoesNotExistError)

	doc = frappe.get_doc("Supplier", supplier_name)

	if phone is not None:
		doc.mobile_no = phone.strip() if phone else ""
	if email is not None:
		doc.email_id = email.strip() if email else ""
	if supplier_group is not None:
		doc.supplier_group = supplier_group

	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {"success": True, "name": doc.name, "supplier_name": doc.supplier_name}


@frappe.whitelist()
def get_items_for_purchase():
	"""List all active items available for purchase (raw materials and products)."""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	items = frappe.get_all(
		"Item",
		filters={"disabled": 0, "has_variants": 0, "is_stock_item": 1},
		fields=["name", "item_name", "item_group", "stock_uom", "image", "standard_rate"],
		order_by="item_name asc",
		limit=500,
	)

	return items


@frappe.whitelist()
def get_warehouses_for_purchase():
	"""List available warehouses for receiving purchased goods."""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	company = get_platform_company_name()
	warehouses = frappe.get_all(
		"Warehouse",
		filters={"company": company, "is_group": 0, "disabled": 0},
		fields=["name", "warehouse_name"],
		order_by="warehouse_name asc",
	)

	return warehouses


@frappe.whitelist()
def create_purchase_and_receive(supplier, items, warehouse, notes=None):
	"""
	Create a Purchase Order and immediately receive it (Purchase Receipt).
	This is a single-step purchase flow: PO + PR in one transaction.
	"""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para registrar compras"), frappe.PermissionError)

	if isinstance(items, str):
		items = frappe.parse_json(items)

	if not items or len(items) == 0:
		frappe.throw(frappe._("Debés agregar al menos un producto a la compra"))

	if not frappe.db.exists("Supplier", supplier):
		frappe.throw(frappe._("El proveedor '{0}' no existe").format(supplier), frappe.DoesNotExistError)

	if not frappe.db.exists("Warehouse", warehouse):
		frappe.throw(frappe._("El almacén '{0}' no existe").format(warehouse), frappe.DoesNotExistError)

	company = get_platform_company_name()

	# Check if Mexico taxes are enabled for purchase tax template
	config = frappe.get_cached_doc("SaaS Feature Config")
	has_mexico_taxes = bool(config.get("has_mexico_taxes", 0))

	frappe.db.begin()
	try:
		# 1. Create Purchase Order
		po = frappe.new_doc("Purchase Order")
		po.supplier = supplier
		po.company = company
		po.transaction_date = frappe.utils.today()
		po.schedule_date = frappe.utils.today()
		po.set_warehouse = warehouse

		if notes:
			po.remarks = notes

		for item in items:
			if not item.get("item_code"):
				frappe.throw(frappe._("Cada producto debe tener un código de artículo"))

			qty = float(item.get("qty", 1))
			rate = float(item.get("rate", 0))

			if qty <= 0:
				frappe.throw(frappe._("La cantidad debe ser mayor a 0 para '{0}'").format(item["item_code"]))
			if rate <= 0:
				frappe.throw(
					frappe._("El costo unitario debe ser mayor a 0 para '{0}'").format(item["item_code"])
				)

			po.append(
				"items",
				{
					"item_code": item["item_code"],
					"qty": qty,
					"rate": rate,
					"warehouse": warehouse,
					"schedule_date": frappe.utils.today(),
				},
			)

		# Apply Mexico purchase taxes if enabled
		if has_mexico_taxes:
			tax_template = "IVA 16% México Compras"
			if frappe.db.exists("Purchase Taxes and Charges Template", tax_template):
				po.taxes_and_charges = tax_template
				from erpnext.controllers.accounts_controller import get_taxes_and_charges

				taxes = get_taxes_and_charges("Purchase Taxes and Charges Template", tax_template)
				for tax in taxes:
					po.append("taxes", tax)

		po.insert(ignore_permissions=True)
		po.submit()

		# 2. Create Purchase Receipt from the PO
		pr = frappe.new_doc("Purchase Receipt")
		pr.supplier = supplier
		pr.company = company
		pr.posting_date = frappe.utils.today()
		pr.set_warehouse = warehouse

		for po_item in po.items:
			pr.append(
				"items",
				{
					"item_code": po_item.item_code,
					"item_name": po_item.item_name,
					"qty": po_item.qty,
					"rate": po_item.rate,
					"warehouse": warehouse,
					"purchase_order": po.name,
					"purchase_order_item": po_item.name,
					"uom": po_item.uom or po_item.stock_uom,
					"stock_uom": po_item.stock_uom,
				},
			)

		pr.insert(ignore_permissions=True)
		pr.submit()

		frappe.db.commit()
		frappe.clear_cache(doctype="Purchase Order")
		frappe.clear_cache(doctype="Purchase Receipt")

		return {
			"success": True,
			"purchase_order": po.name,
			"purchase_receipt": pr.name,
			"grand_total": float(po.grand_total),
			"items_count": len(po.items),
		}

	except Exception as e:
		frappe.db.rollback()
		frappe.throw(frappe._("Error al registrar la compra: {0}").format(str(e)))


@frappe.whitelist()
def get_purchase_history(supplier=None, from_date=None, to_date=None, status=None, limit=50, start=0):
	"""Get purchase order history with optional filters."""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para ver el historial de compras"), frappe.PermissionError)

	filters = {}

	if supplier:
		filters["supplier"] = supplier

	if from_date:
		filters["transaction_date"] = [">=", from_date]

	if to_date:
		if "transaction_date" in filters:
			filters["transaction_date"] = ["between", [from_date, to_date]]
		else:
			filters["transaction_date"] = ["<=", to_date]

	# Status mapping: docstatus 0=Draft, 1=Submitted, 2=Cancelled
	if status == "received":
		filters["docstatus"] = 1
		filters["per_received"] = 100
	elif status == "cancelled":
		filters["docstatus"] = 2
	elif status == "draft":
		filters["docstatus"] = 0
	else:
		# Show all submitted and cancelled by default
		filters["docstatus"] = ["in", [1, 2]]

	orders = frappe.get_all(
		"Purchase Order",
		filters=filters,
		fields=[
			"name",
			"supplier",
			"supplier_name",
			"transaction_date",
			"grand_total",
			"net_total",
			"total_taxes_and_charges",
			"docstatus",
			"per_received",
			"status",
		],
		order_by="transaction_date desc, creation desc",
		limit_page_length=int(limit),
		start=int(start),
	)

	# Add item count per order
	for order in orders:
		order["items_count"] = frappe.db.count("Purchase Order Item", filters={"parent": order["name"]})
		order["grand_total"] = float(order["grand_total"] or 0)
		order["net_total"] = float(order["net_total"] or 0)

		# Determine display status
		if order["docstatus"] == 2:
			order["display_status"] = "Cancelada"
		elif order.get("per_received", 0) >= 100:
			order["display_status"] = "Recibida"
		elif order["docstatus"] == 1:
			order["display_status"] = "Confirmada"
		else:
			order["display_status"] = "Borrador"

	# Get total count for pagination
	total_count = frappe.db.count("Purchase Order", filters=filters)

	return {"orders": orders, "total": total_count}


@frappe.whitelist()
def get_purchase_detail(po_name):
	"""Get detailed information about a specific purchase order."""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para ver el detalle de compras"), frappe.PermissionError)

	if not frappe.db.exists("Purchase Order", po_name):
		frappe.throw(frappe._("La orden de compra '{0}' no existe").format(po_name), frappe.DoesNotExistError)

	po = frappe.get_doc("Purchase Order", po_name)

	items = []
	for item in po.items:
		items.append(
			{
				"item_code": item.item_code,
				"item_name": item.item_name,
				"qty": float(item.qty),
				"rate": float(item.rate),
				"amount": float(item.amount),
				"warehouse": item.warehouse,
				"uom": item.uom or item.stock_uom,
			}
		)

	# Get related Purchase Receipt(s)
	receipts = frappe.get_all(
		"Purchase Receipt",
		filters={"supplier": po.supplier, "docstatus": 1},
		fields=["name", "posting_date"],
		limit=10,
	)
	# Filter to only receipts linked to this PO
	linked_receipts = []
	for r in receipts:
		pr_items = frappe.get_all(
			"Purchase Receipt Item",
			filters={"parent": r.name, "purchase_order": po.name},
			fields=["name"],
			limit=1,
		)
		if pr_items:
			linked_receipts.append(r)

	return {
		"name": po.name,
		"supplier": po.supplier,
		"supplier_name": po.supplier_name,
		"transaction_date": str(po.transaction_date),
		"grand_total": float(po.grand_total),
		"net_total": float(po.net_total),
		"total_taxes_and_charges": float(po.total_taxes_and_charges or 0),
		"docstatus": po.docstatus,
		"per_received": float(po.per_received or 0),
		"status": po.status,
		"remarks": po.remarks or "",
		"items": items,
		"receipts": [{"name": r.name, "date": str(r.posting_date)} for r in linked_receipts],
		"display_status": "Cancelada"
		if po.docstatus == 2
		else ("Recibida" if (po.per_received or 0) >= 100 else "Confirmada"),
	}


@frappe.whitelist()
def cancel_purchase_order(po_name):
	"""Cancel a Purchase Order and its linked Purchase Receipts."""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para cancelar compras"), frappe.PermissionError)

	if not frappe.db.exists("Purchase Order", po_name):
		frappe.throw(frappe._("La orden de compra '{0}' no existe").format(po_name), frappe.DoesNotExistError)

	po = frappe.get_doc("Purchase Order", po_name)

	if po.docstatus != 1:
		frappe.throw(frappe._("Solo se pueden cancelar órdenes de compra confirmadas"))

	frappe.db.begin()
	try:
		# First cancel linked Purchase Receipts
		linked_pr_items = frappe.get_all(
			"Purchase Receipt Item",
			filters={"purchase_order": po_name, "docstatus": 1},
			fields=["parent"],
			group_by="parent",
		)

		for pr_item in linked_pr_items:
			pr = frappe.get_doc("Purchase Receipt", pr_item.parent)
			if pr.docstatus == 1:
				pr.cancel()

		# Then cancel the PO
		po.cancel()

		frappe.db.commit()
		frappe.clear_cache(doctype="Purchase Order")
		frappe.clear_cache(doctype="Purchase Receipt")

		return {
			"success": True,
			"message": frappe._("Compra cancelada exitosamente. El stock fue revertido."),
		}

	except Exception as e:
		frappe.db.rollback()
		frappe.throw(frappe._("Error al cancelar la compra: {0}").format(str(e)))


@frappe.whitelist()
def get_supplier_groups():
	"""Get all supplier groups for the dropdown."""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	groups = frappe.get_all("Supplier Group", fields=["name"], order_by="name asc")
	return [g.name for g in groups]


@frappe.whitelist()
def get_active_services():
	"""List all active services (non-stock items in the Services group or is_stock_item=0)."""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	services = frappe.get_all(
		"Item",
		filters={"disabled": 0, "has_variants": 0, "is_stock_item": 0},
		fields=["name", "item_code", "item_name", "item_group", "standard_rate", "description", "image"],
		order_by="item_name asc",
		limit=500,
	)
	return services


@frappe.whitelist()
def create_service_invoice(
	customer, items, payment_amount, payment_mode, before_image=None, after_image=None
):
	"""
	Create a Sales Invoice for service items, submit it, and register the payment.
	Also attach before and after images if provided.
	"""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	if not is_service_operator_user():
		frappe.throw(frappe._("No tenés permisos para registrar servicios"), frappe.PermissionError)

	if isinstance(items, str):
		items = frappe.parse_json(items)

	if not items:
		frappe.throw(frappe._("Debe agregar al menos un servicio."))

	if not frappe.db.exists("Customer", customer):
		frappe.throw(frappe._("El cliente '{0}' no existe").format(customer), frappe.DoesNotExistError)

	company = get_platform_company_name()

	frappe.db.begin()
	try:
		# 1. Crear el Sales Invoice (update_stock = 0 para servicios)
		si = frappe.new_doc("Sales Invoice")
		si.company = company
		si.customer = customer
		si.update_stock = 0
		si.posting_date = frappe.utils.today()
		si.selling_price_list = "Standard Selling"
		si.currency = frappe.db.get_value("Company", company, "default_currency") or "MXN"
		si.set_posting_time = 1

		for it in items:
			item_code = it.get("item_code")
			qty = float(it.get("qty", 1))
			rate = float(it.get("rate", 0))
			description = it.get("description") or ""

			si.append(
				"items",
				{
					"item_code": item_code,
					"qty": qty,
					"price_list_rate": rate,
					"rate": rate,
					"description": description,
					"ignore_pricing_rule": 1,
				},
			)

		si.insert(ignore_permissions=True)
		si.submit()

		# 2. Registrar el pago inmediato
		payment_amount = float(payment_amount or 0)
		if payment_amount > 0:
			from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

			try:
				pe = get_payment_entry("Sales Invoice", si.name, bank_amount=payment_amount)
				pe.mode_of_payment = payment_mode
				pe.reference_no = f"Pago Servicio {si.name}"
				pe.reference_date = frappe.utils.today()
				pe.insert(ignore_permissions=True)
				pe.submit()
			except Exception as pay_err:
				frappe.log_error(message=str(pay_err), title="Error creando pago para Factura de Servicio")

		# 3. Vincular imágenes si se proveen
		if before_image:
			file_doc = frappe.get_doc(
				{
					"doctype": "File",
					"file_url": before_image,
					"attached_to_doctype": "Sales Invoice",
					"attached_to_name": si.name,
					"is_private": 0,
				}
			)
			file_doc.insert(ignore_permissions=True)

		if after_image:
			file_doc = frappe.get_doc(
				{
					"doctype": "File",
					"file_url": after_image,
					"attached_to_doctype": "Sales Invoice",
					"attached_to_name": si.name,
					"is_private": 0,
				}
			)
			file_doc.insert(ignore_permissions=True)

		frappe.db.commit()
		frappe.clear_cache(doctype="Sales Invoice")

		return {"success": True, "invoice_name": si.name, "grand_total": float(si.grand_total)}

	except Exception as e:
		frappe.db.rollback()
		frappe.throw(frappe._("Error al registrar el servicio: {0}").format(str(e)))


@frappe.whitelist()
def create_service_item(item_name, standard_rate):
	"""Create a new service Item (is_stock_item=0) in the Services group."""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para crear servicios"), frappe.PermissionError)

	if not item_name or not item_name.strip():
		frappe.throw(frappe._("El nombre del servicio no puede estar vacío"))

	item_name = item_name.strip()
	rate = float(standard_rate or 0)

	if frappe.db.exists("Item", {"item_name": item_name}):
		frappe.throw(frappe._("Ya existe un artículo/servicio con el nombre '{0}'").format(item_name))

	if not frappe.db.exists("Item Group", "Services"):
		ig = frappe.new_doc("Item Group")
		ig.item_group_name = "Services"
		ig.parent_item_group = "All Item Groups"
		ig.is_group = 0
		ig.insert(ignore_permissions=True)

	item = frappe.new_doc("Item")
	item.item_code = item_name
	item.item_name = item_name
	item.item_group = "Services"
	item.is_stock_item = 0
	item.stock_uom = "Nos"
	item.standard_rate = rate
	item.insert(ignore_permissions=True)

	price_list = "Standard Selling"
	if frappe.db.exists("Price List", price_list):
		ip = frappe.new_doc("Item Price")
		ip.item_code = item.name
		ip.price_list = price_list
		ip.price_list_rate = rate
		ip.insert(ignore_permissions=True)

	frappe.db.commit()
	frappe.clear_cache(doctype="Item")

	return {"success": True, "item_code": item.name, "item_name": item.item_name}


@frappe.whitelist()
def update_service_item(item_code, item_name, standard_rate):
	"""Update standard rate and name of a service Item."""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(frappe._("Iniciá sesión para realizar esta acción"), frappe.PermissionError)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para editar servicios"), frappe.PermissionError)

	if not frappe.db.exists("Item", item_code):
		frappe.throw(frappe._("El servicio '{0}' no existe").format(item_code), frappe.DoesNotExistError)

	rate = float(standard_rate or 0)
	item = frappe.get_doc("Item", item_code)
	item.standard_rate = rate
	if item_name and item_name.strip() and item.item_name != item_name.strip():
		item.item_name = item_name.strip()
	item.save(ignore_permissions=True)

	price_list = "Standard Selling"
	if frappe.db.exists("Price List", price_list):
		ip_name = frappe.db.get_value("Item Price", {"item_code": item_code, "price_list": price_list})
		if ip_name:
			ip = frappe.get_doc("Item Price", ip_name)
			ip.price_list_rate = rate
			ip.save(ignore_permissions=True)
		else:
			ip = frappe.new_doc("Item Price")
			ip.item_code = item_code
			ip.price_list = price_list
			ip.price_list_rate = rate
			ip.insert(ignore_permissions=True)

	frappe.db.commit()
	frappe.clear_cache(doctype="Item")

	return {"success": True, "item_code": item.name, "item_name": item.item_name}


def get_base_domain():
	base_domain = "localhost"
	if hasattr(frappe.local, "request") and frappe.local.request:
		host = frappe.local.request.host.split(":")[0]
		if "localhost" in host:
			base_domain = "localhost"
		elif "local" in host:
			base_domain = "local"
		elif "." in host:
			parts = host.split(".")
			if len(parts) > 2:
				base_domain = ".".join(parts[1:])
			else:
				base_domain = host
	return base_domain


def get_bench_path():
	return _get_bench_path()


def get_sites_path():
	return _get_sites_path()


def get_db_root_credentials():
	return _get_db_root_credentials()


def _normalize_required_setup_field(value, label):
	normalized = (value or "").strip()
	if not normalized:
		frappe.throw(frappe._("{0} es obligatorio.").format(label), frappe.ValidationError)
	return normalized


def _normalize_required_email_field(value, label):
	normalized = _normalize_required_setup_field(value, label)
	local_part, separator, domain = normalized.partition("@")
	if not separator or not local_part or "." not in domain:
		frappe.throw(frappe._("{0} es inválido.").format(label), frappe.ValidationError)
	return normalized


def _validate_tenant_request_payload(
	workspace_id,
	company_name,
	admin_email,
	admin_password,
	company_tax_id,
	company_address,
	company_phone,
	company_email,
):
	reserved_subdomains = _get_reserved_subdomains()

	if not workspace_id or len(workspace_id) > 30:
		frappe.throw(frappe._("El Workspace ID debe tener entre 1 y 30 caracteres."))

	if workspace_id in reserved_subdomains:
		frappe.throw(frappe._("El Workspace ID ingresado no está permitido."))

	if not company_name or len(company_name.strip()) < 3:
		frappe.throw(frappe._("El nombre de la empresa es obligatorio."))

	if not admin_email or "@" not in admin_email:
		frappe.throw(frappe._("El email del administrador es inválido."))

	if not admin_password or len(admin_password) < 8:
		frappe.throw(frappe._("La contraseña del administrador debe tener al menos 8 caracteres."))

	workspace_id = workspace_id.lower().strip()
	company_name = _normalize_required_setup_field(company_name, frappe._("El nombre de la empresa"))
	admin_email = _normalize_required_email_field(admin_email, frappe._("El email del administrador"))
	company_tax_id = _normalize_required_setup_field(company_tax_id, frappe._("El tax ID de la empresa"))
	company_address = _normalize_required_setup_field(company_address, frappe._("La dirección de la empresa"))
	company_phone = _normalize_required_setup_field(company_phone, frappe._("El teléfono de la empresa"))
	if not any(ch.isdigit() for ch in company_phone):
		frappe.throw(frappe._("El teléfono de la empresa es inválido."), frappe.ValidationError)
	company_email = _normalize_required_email_field(company_email, frappe._("El email de la empresa"))

	return {
		"workspace_id": workspace_id,
		"company_name": company_name,
		"admin_email": admin_email,
		"admin_password": admin_password,
		"company_tax_id": company_tax_id,
		"company_address": company_address,
		"company_phone": company_phone,
		"company_email": company_email,
	}


def _tenant_workspace_availability(workspace_id):
	import os
	import re

	workspace_id = _resolve_workspace_id(workspace_id=workspace_id)
	if not workspace_id or len(workspace_id) > 30:
		return {
			"available": False,
			"reason": "invalid",
			"message": frappe._("El Workspace ID debe tener entre 1 y 30 caracteres."),
			"workspace_id": workspace_id,
		}

	if not re.match(r"^[a-zA-Z0-9\-]+$", workspace_id):
		return {
			"available": False,
			"reason": "invalid",
			"message": frappe._(
				"El Workspace ID ingresado es inválido. Solo se admiten letras, números y guiones."
			),
			"workspace_id": workspace_id,
		}

	if workspace_id in _get_reserved_subdomains():
		return {
			"available": False,
			"reason": "reserved",
			"message": frappe._("El Workspace ID ingresado no está permitido."),
			"workspace_id": workspace_id,
		}

	if frappe.db.exists("SaaS Tenant Request", {"subdomain": workspace_id}):
		existing_status = frappe.db.get_value("SaaS Tenant Request", {"subdomain": workspace_id}, "status")
		if existing_status != "Failed":
			return {
				"available": False,
				"reason": "duplicate",
				"message": frappe._("El Workspace ID ya está registrado o en proceso de creación."),
				"workspace_id": workspace_id,
			}

	base_domain = get_base_domain()
	domain = f"{workspace_id}.{base_domain}"
	if os.path.exists(os.path.join(get_sites_path(), domain)):
		return {
			"available": False,
			"reason": "site_exists",
			"message": frappe._("El sitio ya existe en el servidor."),
			"workspace_id": workspace_id,
		}

	return {
		"available": True,
		"reason": "available",
		"message": frappe._("El Workspace ID está disponible."),
		"workspace_id": workspace_id,
	}


def _enforce_tenant_request_rate_limit(max_requests=3, window_seconds=900):
	import time

	request = getattr(frappe.local, "request", None)
	remote_addr = "unknown"
	if request:
		remote_addr = (
			request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
			or getattr(request, "remote_addr", None)
			or "unknown"
		)

	cache_key = f"tenant_request_rate_limit:{remote_addr}"
	now = int(time.time())
	history = frappe.cache().get_value(cache_key) or []
	history = [ts for ts in history if now - int(ts) < window_seconds]

	if len(history) >= max_requests:
		frappe.throw(
			frappe._("Estás realizando demasiadas solicitudes. Esperá unos minutos e intentá de nuevo."),
			frappe.PermissionError,
		)

	history.append(now)
	frappe.cache().set_value(cache_key, history)


@frappe.whitelist(allow_guest=True)
def check_tenant_availability(subdomain=None, workspace_id=None):
	master_site = _get_primary_master_site()
	with SafeSiteContext(master_site):
		return _tenant_workspace_availability(_resolve_workspace_id(subdomain, workspace_id))


@frappe.whitelist(allow_guest=True)
def request_tenant(
	subdomain=None,
	company_name=None,
	admin_email=None,
	admin_password=None,
	workspace_id=None,
	company_tax_id=None,
	company_address=None,
	company_phone=None,
	company_email=None,
):
	master_site = _get_primary_master_site()
	with SafeSiteContext(master_site):
		return _request_tenant_impl(
			subdomain,
			company_name,
			admin_email,
			admin_password,
			workspace_id=workspace_id,
			company_tax_id=company_tax_id,
			company_address=company_address,
			company_phone=company_phone,
			company_email=company_email,
		)


def _request_tenant_impl(
	subdomain,
	company_name,
	admin_email,
	admin_password,
	workspace_id=None,
	company_tax_id=None,
	company_address=None,
	company_phone=None,
	company_email=None,
):
	import re
	import secrets

	_enforce_tenant_request_rate_limit()
	workspace_id = _resolve_workspace_id(subdomain, workspace_id)
	if not workspace_id or not re.match(r"^[a-zA-Z0-9\-]+$", workspace_id):
		frappe.throw(
			frappe._("El Workspace ID ingresado es inválido. Solo se admiten letras, números y guiones.")
		)

	validated_payload = _validate_tenant_request_payload(
		workspace_id,
		company_name,
		admin_email,
		admin_password,
		company_tax_id,
		company_address,
		company_phone,
		company_email,
	)
	availability = _tenant_workspace_availability(workspace_id)
	if not availability["available"]:
		frappe.throw(availability["message"])
	base_domain = get_base_domain()
	status_token = secrets.token_urlsafe(24)

	# Create request
	if frappe.db.exists("SaaS Tenant Request", {"subdomain": workspace_id}):
		doc = frappe.get_doc("SaaS Tenant Request", {"subdomain": workspace_id})
		doc.company_name = validated_payload["company_name"]
		doc.admin_email = validated_payload["admin_email"]
		doc.admin_password = validated_payload["admin_password"]
		doc.company_tax_id = validated_payload["company_tax_id"]
		doc.company_address = validated_payload["company_address"]
		doc.company_phone = validated_payload["company_phone"]
		doc.company_email = validated_payload["company_email"]
		doc.status_token = status_token
		doc.status = "Pending"
		doc.error_log = ""
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{
				"doctype": "SaaS Tenant Request",
				"subdomain": workspace_id,
				"company_name": validated_payload["company_name"],
				"admin_email": validated_payload["admin_email"],
				"admin_password": validated_payload["admin_password"],
				"company_tax_id": validated_payload["company_tax_id"],
				"company_address": validated_payload["company_address"],
				"company_phone": validated_payload["company_phone"],
				"company_email": validated_payload["company_email"],
				"status_token": status_token,
				"status": "Pending",
			}
		)
		doc.insert(ignore_permissions=True)

	frappe.db.commit()

	# Enqueue task
	frappe.enqueue(
		"paletixa_saas.paletixa_saas.api.provision_tenant_task",
		queue="long",
		timeout=1800,
		request_id=doc.name,
		base_domain=base_domain,
	)

	return {
		"success": True,
		"message": "Solicitud de creación registrada. Iniciando aprovisionamiento.",
		"request_id": doc.name,
		"request_token": status_token,
	}


@frappe.whitelist(allow_guest=True)
def get_tenant_status(subdomain=None, token=None, workspace_id=None):
	master_site = _get_primary_master_site()
	with SafeSiteContext(master_site):
		return _get_tenant_status_impl(subdomain, token=token, workspace_id=workspace_id)


def _get_tenant_status_impl(subdomain, token=None, workspace_id=None):
	import os

	workspace_id = _resolve_workspace_id(subdomain, workspace_id)
	if not workspace_id:
		return {"status": "NotFound"}

	if not frappe.db.exists("SaaS Tenant Request", {"subdomain": workspace_id}):
		return {"status": "NotFound"}

	status, _error_log, status_token = frappe.db.get_value(
		"SaaS Tenant Request", {"subdomain": workspace_id}, ["status", "error_log", "status_token"]
	)
	if status_token and token != status_token:
		return {"status": "NotFound"}

	safe_error_log = ""
	if status == "Failed":
		safe_error_log = frappe._("El aprovisionamiento falló. Contactá al administrador.")
	response = {"status": status, "error_log": safe_error_log}

	if status == "Pending":
		response.update({"phase": "pending", "progress": 10, "message": frappe._("Validando solicitud...")})
	elif status == "In Progress":
		base_domain = get_base_domain()
		site_name = f"{workspace_id}.{base_domain}"
		site_path = os.path.join(get_sites_path(), site_name)
		if not os.path.exists(site_path):
			response.update(
				{
					"phase": "creating_site",
					"progress": 25,
					"message": frappe._("Creando la base de datos y preparando el sitio..."),
				}
			)
		else:
			try:
				with SafeSiteContext(site_name):
					company_exists = bool(frappe.db.count("Company") > 0)
					admin_email = frappe.db.get_value(
						"SaaS Tenant Request", {"subdomain": workspace_id}, "admin_email"
					)
					if not company_exists:
						response.update(
							{
								"phase": "installing_apps",
								"progress": 55,
								"message": frappe._("Instalando módulos y apps de ERPNext..."),
							}
						)
					elif admin_email and not frappe.db.exists("User", admin_email):
						response.update(
							{
								"phase": "configuring_identity",
								"progress": 80,
								"message": frappe._(
									"Configurando identidad y datos requeridos del tenant..."
								),
							}
						)
					else:
						try:
							validate_tenant_runtime_config()
						except frappe.ValidationError as ex:
							response.update(
								{
									"phase": "configuring_runtime",
									"progress": 90,
									"message": str(ex),
								}
							)
						else:
							response.update(
								{
									"phase": "configuring_runtime",
									"progress": 95,
									"message": frappe._("Validando la configuración requerida del tenant..."),
								}
							)
			except Exception:
				response.update(
					{
						"phase": "installing_apps",
						"progress": 55,
						"message": frappe._("Instalando módulos y apps de ERPNext..."),
					}
				)
	elif status == "Completed":
		response.update({"phase": "completed", "progress": 100, "message": frappe._("¡Despliegue exitoso!")})

	return response


def _first_record_name(records):
	if not records:
		return ""

	first_record = records[0]
	if isinstance(first_record, dict):
		return (first_record.get("name") or "").strip()
	return (getattr(first_record, "name", "") or "").strip()


def _discover_tenant_runtime_defaults(company_name):
	company_name = (company_name or "").strip()
	if not company_name:
		frappe.throw(frappe._("La compañía del tenant no está definida."), frappe.ValidationError)

	try:
		company = frappe.get_cached_doc("Company", company_name)
	except Exception:
		frappe.throw(
			frappe._("La compañía {0} no existe o no puede leerse.").format(company_name),
			frappe.ValidationError,
		)

	company_abbr = (company.abbr or "").strip()
	if not company_abbr:
		frappe.throw(
			frappe._("La compañía {0} no tiene abreviatura configurada.").format(company_name),
			frappe.ValidationError,
		)

	runtime_defaults = {
		"company_name": company_name,
		"company_abbr": company_abbr,
		"custom_country": (company.get("country") or "Mexico").strip() or "Mexico",
		"custom_currency": (company.get("default_currency") or "MXN").strip() or "MXN",
	}

	warehouse_name = _first_record_name(
		frappe.get_all(
			"Warehouse",
			filters={"company": company_name, "is_group": 0, "disabled": 0},
			fields=["name"],
			order_by="creation asc",
			limit=1,
		)
	)
	cash_account_name = _first_record_name(
		frappe.get_all(
			"Account",
			filters={"company": company_name, "is_group": 0, "disabled": 0, "account_type": "Cash"},
			fields=["name"],
			order_by="creation asc",
			limit=1,
		)
	)
	bank_account_name = _first_record_name(
		frappe.get_all(
			"Account",
			filters={"company": company_name, "is_group": 0, "disabled": 0, "account_type": "Bank"},
			fields=["name"],
			order_by="creation asc",
			limit=1,
		)
	)

	missing_links = []
	if not warehouse_name:
		missing_links.append(frappe._("Warehouse"))
	if not cash_account_name:
		missing_links.append(frappe._("Cash Account"))
	if not bank_account_name:
		missing_links.append(frappe._("Bank Account"))

	if missing_links:
		frappe.throw(
			frappe._("No se pudo completar el tenant porque faltan enlaces runtime válidos: {0}.").format(
				", ".join(missing_links)
			),
			frappe.ValidationError,
		)

	runtime_defaults.update(
		{
			"default_distribution_warehouse": warehouse_name,
			"default_cash_account": cash_account_name,
			"default_bank_account": bank_account_name,
		}
	)
	return runtime_defaults


def validate_tenant_runtime_config(config=None):
	try:
		config = config or frappe.get_cached_doc("SaaS Feature Config")
	except Exception:
		frappe.throw(
			frappe._("No se pudo leer la configuración runtime del tenant. Reintentá el aprovisionamiento."),
			frappe.ValidationError,
		)

	company_name = (config.get("company_name") or "").strip()
	if not company_name:
		frappe.throw(
			frappe._("La configuración runtime todavía no tiene una compañía definida."),
			frappe.ValidationError,
		)

	company = _discover_tenant_runtime_defaults(company_name)
	current_links = {
		"default_distribution_warehouse": (config.get("default_distribution_warehouse") or "").strip(),
		"default_cash_account": (config.get("default_cash_account") or "").strip(),
		"default_bank_account": (config.get("default_bank_account") or "").strip(),
	}

	missing_links = []
	if current_links["default_distribution_warehouse"]:
		if not frappe.get_all(
			"Warehouse",
			filters={
				"name": current_links["default_distribution_warehouse"],
				"company": company_name,
				"disabled": 0,
			},
			fields=["name"],
			limit=1,
		):
			missing_links.append(frappe._("Warehouse"))
	else:
		missing_links.append(frappe._("Warehouse"))

	for account_field, label in [
		("default_cash_account", frappe._("Cash Account")),
		("default_bank_account", frappe._("Bank Account")),
	]:
		link_name = current_links[account_field]
		if not link_name:
			missing_links.append(label)
			continue
		if not frappe.get_all(
			"Account",
			filters={"name": link_name, "company": company_name, "disabled": 0},
			fields=["name"],
			limit=1,
		):
			missing_links.append(label)

	if missing_links:
		frappe.throw(
			frappe._("La configuración runtime todavía no está completa: {0}.").format(
				", ".join(missing_links)
			),
			frappe.ValidationError,
		)

	return {
		"company_name": company_name,
		"company_abbr": company["company_abbr"],
		"default_distribution_warehouse": current_links["default_distribution_warehouse"],
		"default_cash_account": current_links["default_cash_account"],
		"default_bank_account": current_links["default_bank_account"],
		"custom_country": (config.get("custom_country") or "").strip() or company["custom_country"],
		"custom_currency": (config.get("custom_currency") or "").strip() or company["custom_currency"],
	}


def provision_tenant_task(request_id, base_domain="localhost"):
	import json
	import os
	import subprocess

	doc = frappe.get_doc("SaaS Tenant Request", request_id)
	doc.status = "In Progress"
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	raw_password = doc.get_password("admin_password")
	subdomain = doc.subdomain
	company_name = doc.company_name
	admin_email = doc.admin_email

	domain = f"{subdomain}.{base_domain}"
	bench_path = get_bench_path()

	try:
		# Run bench new-site command
		cmd = [
			"bench",
			"new-site",
			domain,
			"--admin-password",
			raw_password,
			"--mariadb-user-host-login-scope",
			"%",
			"--db-root-username",
			get_db_root_credentials()[0],
			"--db-root-password",
			get_db_root_credentials()[1],
			"--install-app",
			"erpnext",
			"--install-app",
			"frappe_assistant_core",
			"--install-app",
			"paletixa_saas",
		]

		# We need to preserve path environment for bench execution
		env = os.environ.copy()
		env["PATH"] = f"/home/frappe/.local/bin:{env.get('PATH', '')}"

		result = subprocess.run(cmd, capture_output=True, text=True, cwd=bench_path, env=env)

		if result.returncode != 0:
			raise Exception(
				f"bench new-site failed with exit status {result.returncode}.\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
			)

		# Extract db name from site_config.json
		site_config_path = os.path.join(bench_path, "sites", domain, "site_config.json")
		db_name = ""
		if os.path.exists(site_config_path):
			with open(site_config_path) as f:
				config_data = json.load(f)
				db_name = config_data.get("db_name", "")

		with SafeSiteContext(domain):
			frappe.set_user("Administrator")
			# 1. Bypass setup wizard
			frappe.db.set_default("desktop:home_page", "Workspace")
			frappe.db.set_single_value(
				"System Settings",
				{"setup_complete": 1, "country": "Mexico", "time_zone": "America/Mexico_City"},
			)

			# Install erpnext fixtures (like Warehouse Types, UOMs, address templates)
			from erpnext.setup.setup_wizard.operations.install_fixtures import (
				install as install_erpnext_fixtures,
			)

			install_erpnext_fixtures(country="Mexico")
			setup_saas_role_permissions()

			# 2. Create Company
			if not frappe.db.exists("Company", company_name):
				company = frappe.new_doc("Company")
				company.company_name = company_name
				company.abbr = "".join([w[0].upper() for w in company_name.split() if w])[:5] or "COMP"
				company.default_currency = "MXN"
				company.country = "Mexico"
				company.create_chart_of_accounts_based_on = "Standard Template"
				company.chart_of_accounts = "Mexico - Plan de Cuentas"
				company.insert(ignore_permissions=True)
				frappe.db.set_default("company", company_name)

			# 3. Create / update Admin User
			if not frappe.db.exists("User", admin_email):
				user = frappe.new_doc("User")
				user.email = admin_email
				user.first_name = "Administrador"
				user.send_welcome_email = 0
				user.insert(ignore_permissions=True)
				user.add_roles("System Manager")

			# Set/update password bypassing the password strength test
			from frappe.utils.password import update_password

			update_password(admin_email, raw_password)

			# 4. Generate keys
			from frappe.core.doctype.user.user import generate_keys

			generate_keys(admin_email)

			runtime_defaults = _discover_tenant_runtime_defaults(company_name)
			config = frappe.get_doc("SaaS Feature Config")
			config.company_name = runtime_defaults["company_name"]
			config.company_abbr = runtime_defaults["company_abbr"]
			config.company_tax_id = doc.company_tax_id
			config.company_address = doc.company_address
			config.company_phone = doc.company_phone
			config.company_email = doc.company_email
			config.default_distribution_warehouse = runtime_defaults["default_distribution_warehouse"]
			config.default_cash_account = runtime_defaults["default_cash_account"]
			config.default_bank_account = runtime_defaults["default_bank_account"]
			config.custom_country = runtime_defaults["custom_country"]
			config.custom_currency = runtime_defaults["custom_currency"]
			config.is_active = 1
			config.max_branches = int(getattr(doc, "max_branches", 0) or config.get("max_branches") or 3)
			config.print_logo = 1
			config.print_tax_id = 1
			config.print_address = 1
			config.print_contact = 1
			config.ticket_header = config.get("ticket_header") or ""
			config.ticket_footer = config.get("ticket_footer") or ""
			config.save(ignore_permissions=True)
			validate_tenant_runtime_config(config)

			frappe.db.commit()

		doc = frappe.get_doc("SaaS Tenant Request", request_id)
		doc.status = "Completed"
		doc.database_name = db_name
		doc.save(ignore_permissions=True)
		frappe.db.commit()

	except Exception as e:
		err_msg = f"Error: {e!s}\n\nTraceback:\n{traceback.format_exc()}"

		try:
			doc = frappe.get_doc("SaaS Tenant Request", request_id)
			doc.status = "Failed"
			doc.error_log = err_msg
			doc.save(ignore_permissions=True)
			frappe.db.commit()
		except Exception as save_err:
			frappe.log_error(
				message=f"Failed to save tenant failure state: {save_err!s}\nOriginal error: {err_msg}",
				title="Tenant Provisioning Failed",
			)


class SafeSiteContext:
	def __init__(self, target_site):
		self.target_site = target_site
		self.bench_path = get_bench_path()
		self.old_site = frappe.local.site

		# Guardar copia del diccionario de contexto completo
		from frappe.utils.local import _contextvar

		ctx = _contextvar.get(None)
		self.saved_ctx = dict(ctx) if ctx is not None else None

	def __enter__(self):
		try:
			frappe.destroy()
			import os

			frappe.init(site=self.target_site, sites_path=os.path.join(self.bench_path, "sites"))
			frappe.connect()
		except Exception as e:
			# Si falla la inicialización o conexión, restauramos el contexto original inmediatamente
			self.restore_original_context()
			raise e
		return frappe

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.restore_original_context()

	def restore_original_context(self):
		try:
			frappe.destroy()
			import os

			frappe.init(site=self.old_site, sites_path=os.path.join(self.bench_path, "sites"))
			frappe.connect()

			# Restaurar claves del contexto anterior excepto las específicas de BD/Sitio
			from frappe.utils.local import _contextvar

			new_ctx = _contextvar.get(None)
			if new_ctx is not None and self.saved_ctx is not None:
				site_keys = {
					"site",
					"site_name",
					"site_path",
					"sites_path",
					"conf",
					"db",
					"qb",
					"cache",
					"jenv",
					"jloader",
					"initialised",
					"all_apps",
					"module_app",
					"app_modules",
				}
				for k, v in self.saved_ctx.items():
					if k not in site_keys:
						new_ctx[k] = v
		except Exception:
			pass


def _populate_platform_dashboard_metrics(tenant, site_name):
	tenant.setdefault("branches", [])
	tenant.setdefault("branch_count", 0)
	tenant.setdefault("users_count", 0)
	tenant.setdefault("customers_count", 0)
	tenant.setdefault("sales_30_days", 0.0)
	tenant.setdefault("last_sale_date", None)
	tenant.setdefault("active_modules", {})

	try:
		config = frappe.get_cached_doc("SaaS Feature Config")
		pos_enabled = bool(config.has_pos)
		tenant["active_modules"] = {
			"pos": pos_enabled,
			"production": bool(config.has_production),
			"logistics": bool(config.has_logistics),
			"wholesale": bool(config.get("has_wholesale", 1)),
			"services": bool(config.get("has_services") if config.get("has_services") is not None else 1),
			"products": bool(config.get("has_products") if config.get("has_products") is not None else 1),
			"purchasing": bool(config.get("has_purchasing", 0)),
		}

		if pos_enabled:
			profiles = frappe.get_all("POS Profile", fields=["name", "warehouse", "disabled"])
			tenant["branch_count"] = len([p for p in profiles if not p.disabled])
			tenant["branches"] = [
				{"name": p.name, "warehouse": p.warehouse, "disabled": p.disabled} for p in profiles
			]

		try:
			tenant["users_count"] = frappe.db.count(
				"User", {"enabled": 1, "name": ["not in", ["Administrator", "Guest"]]}
			)
		except Exception:
			pass

		try:
			tenant["customers_count"] = frappe.db.count("Customer")
		except Exception:
			pass

		try:
			from frappe.utils import add_days, today

			thirty_days_ago = add_days(today(), -30)
			tenant["sales_30_days"] = (
				frappe.db.get_value(
					"Sales Invoice",
					{"docstatus": 1, "posting_date": [">=", thirty_days_ago]},
					"sum(grand_total)",
				)
				or 0.0
			)
		except Exception:
			pass

		try:
			tenant["last_sale_date"] = frappe.db.get_value(
				"Sales Invoice", {"docstatus": 1}, "posting_date", order_by="posting_date desc"
			)
		except Exception:
			pass
	except Exception as ex:
		tenant["error"] = str(ex)

	return tenant


def _derive_dashboard_tenant_name(site_name):
	if not site_name:
		return site_name
	parts = str(site_name).split(".")
	return parts[0].strip() or site_name


def _build_platform_dashboard_row_from_site(site_name, database_name=""):
	tenant_name = _derive_dashboard_tenant_name(site_name)
	row = {
		"name": tenant_name,
		"site_name": site_name,
		"company_name": tenant_name,
		"admin_email": "",
		"active": 1,
		"max_branches": 0,
		"creation": None,
		"database_name": database_name or "",
		"exempt_from_payment": 0,
		"last_payment_date": None,
		"expiration_date": None,
	}

	try:
		with SafeSiteContext(site_name):
			company_rows = []
			try:
				company_rows = frappe.get_all("Company", fields=["name"], limit=1)
			except Exception:
				pass

			company_name = company_rows[0].name if company_rows else ""
			config = None
			try:
				config = frappe.get_cached_doc("SaaS Feature Config")
				is_active = config.get("is_active")
				row["active"] = 1 if is_active is None else 1 if is_active else 0
				row["max_branches"] = int(config.get("max_branches") or 0)
			except Exception:
				config = None

			if company_name:
				row["company_name"] = company_name
			elif config:
				row["company_name"] = config.get("company_name") or tenant_name

			row = _populate_platform_dashboard_metrics(row, site_name)
	except Exception as ex:
		row["error"] = str(ex)

	return row


def _discover_platform_dashboard_tenants():
	import os

	sites_path = get_sites_path()
	tenant_rows = []

	for site_name in sorted(os.listdir(sites_path)):
		site_path = os.path.join(sites_path, site_name)
		if not os.path.isdir(site_path):
			continue

		if _is_platform_master_site(site_name):
			continue

		site_config_path = os.path.join(site_path, "site_config.json")
		if not os.path.exists(site_config_path):
			continue

		database_name = ""
		try:
			with open(site_config_path) as f:
				database_name = json.load(f).get("db_name", "")
		except Exception:
			pass

		tenant_rows.append(_build_platform_dashboard_row_from_site(site_name, database_name=database_name))

	return tenant_rows


def check_platform_admin_permission():
	if not _is_platform_master_site():
		frappe.throw(
			frappe._("Acceso denegado: Este endpoint solo está disponible en el sitio maestro."),
			frappe.PermissionError,
		)

	if "System Manager" not in frappe.get_roles(frappe.session.user):
		frappe.throw(frappe._("No tenés permisos para realizar esta acción."), frappe.PermissionError)


@frappe.whitelist()
def get_platform_admin_dashboard():
	check_platform_admin_permission()

	tenants = frappe.get_all(
		"SaaS Tenant Request",
		filters={"status": "Completed"},
		fields=[
			"name",
			"company_name",
			"admin_email",
			"active",
			"max_branches",
			"creation",
			"database_name",
			"exempt_from_payment",
			"last_payment_date",
			"expiration_date",
		],
	)
	if not tenants:
		tenants = _discover_platform_dashboard_tenants()

	bench_path = get_bench_path()
	import os

	for t in tenants:
		if "branches" not in t:
			t["branches"] = []
		if "branch_count" not in t:
			t["branch_count"] = 0
		if "users_count" not in t:
			t["users_count"] = 0
		if "customers_count" not in t:
			t["customers_count"] = 0
		if "sales_30_days" not in t:
			t["sales_30_days"] = 0.0
		if "last_sale_date" not in t:
			t["last_sale_date"] = None
		if "active_modules" not in t:
			t["active_modules"] = {}

		domain = t.get("site_name")
		if not domain:
			from paletixa_saas.paletixa_saas.api import get_base_domain

			base_domain = get_base_domain()
			domain = f"{t.name}.{base_domain}"

		if os.path.exists(os.path.join(bench_path, "sites", domain)):
			try:
				with SafeSiteContext(domain):
					_populate_platform_dashboard_metrics(t, domain)
			except Exception as ex:
				t["error"] = str(ex)

	return tenants


@frappe.whitelist()
def update_tenant_config(subdomain, active, max_branches, exempt_from_payment=None):
	check_platform_admin_permission()

	active = frappe.parse_json(active)
	max_branches = frappe.parse_json(max_branches)

	update_dict = {"active": 1 if active else 0, "max_branches": int(max_branches)}
	if exempt_from_payment is not None:
		exempt_from_payment = frappe.parse_json(exempt_from_payment)
		update_dict["exempt_from_payment"] = 1 if exempt_from_payment else 0

	frappe.db.set_value("SaaS Tenant Request", subdomain, update_dict)
	frappe.db.commit()

	bench_path = get_bench_path()
	import os

	from paletixa_saas.paletixa_saas.api import get_base_domain

	base_domain = get_base_domain()
	domain = f"{subdomain}.{base_domain}"

	if os.path.exists(os.path.join(bench_path, "sites", domain)):
		try:
			with SafeSiteContext(domain):
				frappe.db.set_single_value(
					"SaaS Feature Config",
					{"is_active": 1 if active else 0, "max_branches": int(max_branches)},
				)
				frappe.db.commit()
		except Exception as ex:
			frappe.log_error(
				message=f"Error updating tenant config for {domain}: {ex!s}",
				title="Tenant Config Sync Failed",
			)

	return {"success": True, "message": "Configuración del inquilino sincronizada correctamente."}


@frappe.whitelist()
def confirm_tenant_payment(subdomain):
	check_platform_admin_permission()

	if not frappe.db.exists("SaaS Tenant Request", subdomain):
		frappe.throw("Inquilino no encontrado")

	doc = frappe.get_doc("SaaS Tenant Request", subdomain)

	from frappe.utils import add_days, getdate, today

	current_today = today()

	current_expiry = doc.expiration_date
	if current_expiry and getdate(current_expiry) > getdate(current_today):
		new_expiry = add_days(current_expiry, 30)
	else:
		new_expiry = add_days(current_today, 30)

	doc.last_payment_date = current_today
	doc.expiration_date = new_expiry
	doc.active = 1
	doc.save()
	frappe.db.commit()

	# Sync active configuration to the tenant database
	bench_path = get_bench_path()
	import os

	from paletixa_saas.paletixa_saas.api import get_base_domain

	base_domain = get_base_domain()
	domain = f"{subdomain}.{base_domain}"

	if os.path.exists(os.path.join(bench_path, "sites", domain)):
		try:
			with SafeSiteContext(domain):
				frappe.db.set_single_value(
					"SaaS Feature Config", {"is_active": 1, "max_branches": int(doc.max_branches)}
				)
				frappe.db.commit()
		except Exception as ex:
			frappe.log_error(
				message=f"Error syncing tenant config on payment for {domain}: {ex!s}",
				title="Tenant Payment Config Sync Failed",
			)

	return {"success": True, "message": f"Pago confirmado. Vigencia extendida hasta el {new_expiry}."}


def daily_tenant_billing_check():
	from frappe.utils import add_days, getdate, today

	current_today = getdate(today())

	tenants = frappe.get_all(
		"SaaS Tenant Request",
		filters={"status": "Completed", "active": 1, "exempt_from_payment": 0},
		fields=["name", "max_branches", "expiration_date", "creation"],
	)

	bench_path = get_bench_path()
	import os

	from paletixa_saas.paletixa_saas.api import get_base_domain

	base_domain = get_base_domain()

	for t in tenants:
		expiry = t.get("expiration_date")

		# Initialize expiration date defensively if not present
		if not expiry:
			creation_date = getdate(t.get("creation"))
			expiry_calculated = add_days(creation_date, 30)
			expiry = max(current_today, expiry_calculated)
			frappe.db.set_value("SaaS Tenant Request", t.name, "expiration_date", expiry)
			frappe.db.commit()

		if current_today > getdate(expiry):
			# Deactivate due to expiry
			frappe.db.set_value("SaaS Tenant Request", t.name, "active", 0)
			frappe.db.commit()

			# Sync to tenant site config
			domain = f"{t.name}.{base_domain}"
			if os.path.exists(os.path.join(bench_path, "sites", domain)):
				try:
					with SafeSiteContext(domain):
						frappe.db.set_single_value("SaaS Feature Config", "is_active", 0)
						frappe.db.commit()
				except Exception as ex:
					frappe.log_error(
						message=f"Error suspending tenant {domain} on billing check: {ex!s}",
						title="Tenant Billing Suspension Failed",
					)


@frappe.whitelist()
def toggle_tenant_branch(subdomain, branch_name, disabled):
	check_platform_admin_permission()

	disabled = frappe.parse_json(disabled)

	bench_path = get_bench_path()
	import os

	from paletixa_saas.paletixa_saas.api import get_base_domain

	base_domain = get_base_domain()
	domain = f"{subdomain}.{base_domain}"

	if os.path.exists(os.path.join(bench_path, "sites", domain)):
		try:
			with SafeSiteContext(domain):
				if frappe.db.exists("POS Profile", branch_name):
					frappe.db.set_value("POS Profile", branch_name, "disabled", 1 if disabled else 0)

					warehouse_name = frappe.db.get_value("POS Profile", branch_name, "warehouse")
					if warehouse_name and frappe.db.exists("Warehouse", warehouse_name):
						frappe.db.set_value("Warehouse", warehouse_name, "disabled", 1 if disabled else 0)

					frappe.db.commit()
		except Exception as ex:
			frappe.log_error(
				message=f"Error toggling branch status for {domain}: {ex!s}",
				title="Tenant Branch Toggle Failed",
			)

	return {"success": True, "message": f"Estado de sucursal '{branch_name}' actualizado."}


def validate_tenant_is_active():
	form_dict = getattr(frappe.local, "form_dict", None) or {}
	cmd = form_dict.get("cmd")
	if not cmd:
		request = getattr(frappe.local, "request", None)
		path = getattr(request, "path", "") or ""
		if path.startswith("/api/method/"):
			cmd = path.removeprefix("/api/method/")
	if cmd in [
		"paletixa_saas.paletixa_saas.api.get_features",
		"paletixa_saas.paletixa_saas.api.check_tenant_availability",
		"paletixa_saas.paletixa_saas.api.request_tenant",
		"paletixa_saas.paletixa_saas.api.get_tenant_status",
		"logout",
		"paletixa_saas.paletixa_saas.api.custom_logout",
	]:
		return

	if _safe_is_platform_master_site():
		return

	try:
		config = frappe.get_cached_doc("SaaS Feature Config")
	except Exception as ex:
		frappe.log_error(
			message=f"Tenant active check failed for site '{frappe.local.site}': {ex!s}",
			title="Tenant Active Check Failed",
		)
		frappe.throw(
			frappe._(
				"No se pudo verificar el estado de la cuenta. Intentá nuevamente o contactá al administrador."
			),
			frappe.PermissionError,
		)

	is_active = bool(config.get("is_active", 1) if config.get("is_active") is not None else 1)
	if not is_active:
		frappe.throw(
			frappe._(
				"Esta cuenta ha sido suspendida. Por favor, ponete en contacto con el administrador para regularizar tu pago."
			),
			frappe.PermissionError,
		)
