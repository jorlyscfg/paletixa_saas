import os
import re

import frappe


DEFAULT_DEV_MASTER_SITES = {"frontend", "erpadmin"}
DEFAULT_INFRA_RESERVED_SUBDOMAINS = {"master", "api", "files", "www", "static", "admin"}


def _is_development_context():
    return frappe.conf.get("developer_mode") in (1, True, "1") or bool(getattr(frappe.flags, "in_test", False))


def _normalize_items(raw_value):
    if not raw_value:
        return []

    if isinstance(raw_value, (list, tuple, set)):
        values = raw_value
    else:
        values = re.split(r"[\n,]+", str(raw_value))

    return [str(item).strip() for item in values if str(item).strip()]


def get_bench_path():
    configured_path = frappe.conf.get("bench_path") or os.environ.get("BENCH_PATH")
    if configured_path:
        return configured_path

    site_path = getattr(frappe.local, "site_path", None)
    if site_path:
        return os.path.abspath(os.path.join(site_path, os.pardir, os.pardir))

    get_site_path = getattr(frappe, "get_site_path", None)
    if callable(get_site_path):
        try:
            return os.path.abspath(os.path.join(get_site_path(), os.pardir, os.pardir))
        except Exception:
            pass

    frappe.throw(
        frappe._("No se pudo resolver la ruta del bench. Configurá bench_path en site_config.json o BENCH_PATH."),
        frappe.ValidationError,
    )


def get_sites_path():
    return os.path.join(get_bench_path(), "sites")


def get_db_root_credentials():
    username = frappe.conf.get("db_root_username") or os.environ.get("DB_ROOT_USERNAME")
    password = frappe.conf.get("db_root_password") or os.environ.get("DB_ROOT_PASSWORD")

    if username and password:
        return username, password

    if _is_development_context():
        return "root", "admin"

    frappe.throw(
        frappe._(
            "Faltan las credenciales de la base de datos para crear sitios. Configurá db_root_username y db_root_password en site_config.json o las variables de entorno DB_ROOT_USERNAME/DB_ROOT_PASSWORD."
        ),
        frappe.ValidationError,
    )


def resolve_platform_master_sites():
    configured_sites = _normalize_items(frappe.conf.get("platform_master_sites") or os.environ.get("PLATFORM_MASTER_SITES"))
    if configured_sites:
        return set(configured_sites)

    if _is_development_context():
        return set(DEFAULT_DEV_MASTER_SITES)

    frappe.throw(
        frappe._(
            "Faltan los sitios maestros de la plataforma. Configurá platform_master_sites en site_config.json o PLATFORM_MASTER_SITES."
        ),
        frappe.ValidationError,
    )


def is_platform_master_site(site=None):
	site = site or getattr(frappe.local, "site", None)
	if not site:
		return False

	master_sites = resolve_platform_master_sites()

	return any(site == master_site or site.startswith(master_site) for master_site in master_sites)


def get_reserved_subdomains():
    reserved_subdomains = set(DEFAULT_INFRA_RESERVED_SUBDOMAINS)
    reserved_subdomains.update(resolve_platform_master_sites())
    reserved_subdomains.update(_normalize_items(frappe.conf.get("platform_reserved_subdomains") or os.environ.get("PLATFORM_RESERVED_SUBDOMAINS")))
    return reserved_subdomains
