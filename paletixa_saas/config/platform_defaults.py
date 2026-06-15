import frappe


DEMO_COMPANY_NAME = "La Paletixa"
DEMO_COMPANY_ABBR = "LP"
DEMO_DISTRIBUTION_WAREHOUSE = "Distribucion - LP"
DEMO_CASH_ACCOUNT = "Cash - LP"
DEMO_BANK_ACCOUNT = "Bank Accounts - LP"


def _is_development_context():
	return frappe.conf.get("developer_mode") in (1, True, "1") or bool(getattr(frappe.flags, "in_test", False))


def _get_platform_config():
	try:
		return frappe.get_cached_doc("SaaS Feature Config")
	except Exception:
		frappe.throw(
			frappe._("No se pudo leer la configuración SaaS de la plataforma. Ejecutá la configuración inicial."),
			frappe.ValidationError,
		)


def get_platform_company_name(allow_demo_fallback=False):
	config = _get_platform_config()
	company_name = (config.get("company_name") or "").strip()
	if company_name:
		return company_name

	if allow_demo_fallback and _is_development_context():
		company_name = (frappe.defaults.get_global_default("company") or "").strip()
		if company_name:
			return company_name
		return DEMO_COMPANY_NAME

	frappe.throw(
		frappe._("Configurá el nombre de la compañía de la plataforma en SaaS Feature Config."),
		frappe.ValidationError,
	)


def get_platform_company_abbr(company_name=None, allow_demo_fallback=False):
	config = _get_platform_config()
	company_abbr = (config.get("company_abbr") or "").strip()
	if company_abbr:
		return company_abbr

	company_name = company_name or get_platform_company_name(allow_demo_fallback=allow_demo_fallback)
	if company_name:
		try:
			company = frappe.get_cached_doc("Company", company_name)
		except Exception:
			if allow_demo_fallback and _is_development_context():
				return DEMO_COMPANY_ABBR
			frappe.throw(
				frappe._("La compañía {0} no existe.").format(company_name),
				frappe.DoesNotExistError,
			)
		if company and (company.abbr or "").strip():
			return company.abbr.strip()

	if allow_demo_fallback and _is_development_context():
		return DEMO_COMPANY_ABBR

	frappe.throw(
		frappe._("Configurá la abreviatura de la compañía en SaaS Feature Config o en Company."),
		frappe.ValidationError,
	)


def get_platform_distribution_warehouse(allow_demo_fallback=False):
	config = _get_platform_config()
	warehouse = (config.get("default_distribution_warehouse") or "").strip()
	if warehouse:
		return warehouse

	if allow_demo_fallback and _is_development_context():
		return DEMO_DISTRIBUTION_WAREHOUSE

	frappe.throw(
		frappe._("Configurá el almacén de distribución por defecto en SaaS Feature Config."),
		frappe.ValidationError,
	)


def get_platform_payment_account(payment_mode, allow_demo_fallback=False):
	config = _get_platform_config()
	normalized_mode = (payment_mode or "").strip().lower()
	is_cash = normalized_mode in {"cash", "efectivo"}
	account_field = "default_cash_account" if is_cash else "default_bank_account"
	account = (config.get(account_field) or "").strip()
	if account:
		return account

	if allow_demo_fallback and _is_development_context():
		return DEMO_CASH_ACCOUNT if is_cash else DEMO_BANK_ACCOUNT

	payment_label = frappe._("efectivo") if is_cash else frappe._("transferencia o tarjeta")
	frappe.throw(
		frappe._("Configurá la cuenta contable por defecto para {0} en SaaS Feature Config.").format(payment_label),
		frappe.ValidationError,
	)
