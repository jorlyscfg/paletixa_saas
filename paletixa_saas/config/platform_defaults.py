import unicodedata

import frappe


def _get_platform_config():
	try:
		return frappe.get_cached_doc("SaaS Feature Config")
	except Exception:
		frappe.throw(
			frappe._(
				"No se pudo leer la configuración SaaS de la plataforma. Ejecutá la configuración inicial."
			),
			frappe.ValidationError,
		)


def _get_single_company_name():
	companies = frappe.get_all("Company", fields=["name"], limit=2, order_by="creation asc")
	if len(companies) == 1:
		company_name = (companies[0].get("name") or "").strip()
		if company_name:
			return company_name

	frappe.throw(
		frappe._("Configurá el nombre de la compañía de la plataforma en SaaS Feature Config."),
		frappe.ValidationError,
	)


def get_platform_company_name():
	config = _get_platform_config()
	company_name = (config.get("company_name") or "").strip()
	if company_name:
		return company_name

	return _get_single_company_name()


def get_platform_company_abbr(company_name=None):
	config = _get_platform_config()
	company_abbr = (config.get("company_abbr") or "").strip()
	if company_abbr:
		return company_abbr

	company_name = company_name or get_platform_company_name()
	if company_name:
		try:
			company = frappe.get_cached_doc("Company", company_name)
		except Exception:
			frappe.throw(
				frappe._("La compañía {0} no existe.").format(company_name),
				frappe.DoesNotExistError,
			)
		if company and (company.abbr or "").strip():
			return company.abbr.strip()

	frappe.throw(
		frappe._("Configurá la abreviatura de la compañía en SaaS Feature Config o en Company."),
		frappe.ValidationError,
	)


def get_platform_distribution_warehouse():
	config = _get_platform_config()
	warehouse = (config.get("default_distribution_warehouse") or "").strip()
	if warehouse:
		_validate_platform_distribution_warehouse(warehouse)
		return warehouse

	factory_warehouse = _get_factory_distribution_warehouse()
	if factory_warehouse:
		return factory_warehouse

	frappe.throw(
		frappe._("Configurá el almacén de fábrica por defecto en SaaS Feature Config."),
		frappe.ValidationError,
	)


def _validate_platform_distribution_warehouse(warehouse, company_name=None):
	warehouse = (warehouse or "").strip()
	if not warehouse:
		return ""

	company = (company_name or "").strip() or get_platform_company_name()
	try:
		warehouse_doc = frappe.get_cached_doc("Warehouse", warehouse)
	except Exception:
		frappe.throw(
			frappe._("El almacén operativo por defecto {0} no existe.").format(warehouse),
			frappe.ValidationError,
		)

	if warehouse_doc.company != company:
		frappe.throw(
			frappe._("El almacén operativo por defecto debe pertenecer a la compañía {0}.").format(company),
			frappe.ValidationError,
		)

	if frappe.utils.cint(warehouse_doc.is_group):
		frappe.throw(
			frappe._("El almacén operativo por defecto no puede ser un almacén grupo."),
			frappe.ValidationError,
		)

	if frappe.utils.cint(warehouse_doc.disabled):
		frappe.throw(
			frappe._("El almacén operativo por defecto no puede estar deshabilitado."),
			frappe.ValidationError,
		)

	return warehouse


def _normalize_warehouse_label(value):
	return (
		unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii").lower().strip()
	)


def _get_factory_distribution_warehouse():
	company = get_platform_company_name()
	company_abbr = get_platform_company_abbr(company)
	warehouses = frappe.get_all(
		"Warehouse",
		filters={
			"company": company,
			"is_group": 0,
			"disabled": 0,
		},
		fields=["name", "warehouse_name"],
		order_by="creation asc",
	)

	prefix_matches = []
	contains_matches = []
	for warehouse in warehouses:
		name = warehouse.get("name") or ""
		warehouse_name = warehouse.get("warehouse_name") or ""
		labels = [_normalize_warehouse_label(name), _normalize_warehouse_label(warehouse_name)]
		if any(label.startswith("fabrica") for label in labels):
			prefix_matches.append(warehouse)
		elif any("fabrica" in label for label in labels):
			contains_matches.append(warehouse)

	preferred = prefix_matches or contains_matches
	if not preferred:
		return ""

	if company_abbr:
		suffix = _normalize_warehouse_label(f" - {company_abbr}")
		for warehouse in preferred:
			if _normalize_warehouse_label(warehouse.get("name") or "").endswith(suffix):
				return warehouse.get("name") or ""

	return preferred[0].get("name") or ""


def _validate_platform_payment_account(account, company_name, account_type):
	account = (account or "").strip()
	if not account:
		return ""

	company_name = (company_name or "").strip() or get_platform_company_name()
	account_type = (account_type or "").strip()
	try:
		account_doc = frappe.get_cached_doc("Account", account)
	except Exception:
		frappe.throw(
			frappe._("La cuenta contable {0} configurada en SaaS Feature Config no existe.").format(account),
			frappe.ValidationError,
		)

	if (getattr(account_doc, "company", "") or "").strip() != company_name:
		frappe.throw(
			frappe._(
				"La cuenta contable {0} configurada en SaaS Feature Config debe pertenecer a la compañía {1}."
			).format(
				account,
				company_name,
			),
			frappe.ValidationError,
		)

	if frappe.utils.cint(getattr(account_doc, "is_group", 0)):
		frappe.throw(
			frappe._(
				"La cuenta contable {0} configurada en SaaS Feature Config no puede ser una cuenta grupo."
			).format(
				account,
			),
			frappe.ValidationError,
		)

	if frappe.utils.cint(getattr(account_doc, "disabled", 0)):
		frappe.throw(
			frappe._(
				"La cuenta contable {0} configurada en SaaS Feature Config no puede estar deshabilitada."
			).format(
				account,
			),
			frappe.ValidationError,
		)

	if (getattr(account_doc, "account_type", "") or "").strip() != account_type:
		frappe.throw(
			frappe._("La cuenta contable {0} configurada en SaaS Feature Config debe tener tipo {1}.").format(
				account,
				account_type,
			),
			frappe.ValidationError,
		)

	return account


def get_platform_payment_account(payment_mode):
	config = _get_platform_config()
	normalized_mode = (payment_mode or "").strip().lower()
	is_cash = normalized_mode in {"cash", "efectivo"}
	company_name = get_platform_company_name()
	account_type = "Cash" if is_cash else "Bank"
	account_field = "default_cash_account" if is_cash else "default_bank_account"
	account = (config.get(account_field) or "").strip()
	if account:
		return _validate_platform_payment_account(account, company_name, account_type)

	fallback_account = _get_platform_payment_account_fallback(company_name, account_type)
	if fallback_account:
		return fallback_account

	frappe.throw(
		frappe._(
			"No se encontró una cuenta contable {0} activa de la compañía {1} en ERPNext. Revisá SaaS Feature Config o creá una cuenta {0}."
		).format(
			account_type,
			company_name,
		),
		frappe.ValidationError,
	)


def _normalize_payment_mode_label(value):
	return " ".join(
		unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii").lower().split()
	)


def _get_payment_mode_rule(payment_mode):
	normalized_mode = _normalize_payment_mode_label(payment_mode)

	rules = (
		{
			"aliases": {"cash", "efectivo"},
			"canonical_name": "Cash",
			"candidate_names": ["Cash"],
			"payment_account_mode": "Cash",
			"type": "Cash",
			"allow_create": True,
		},
		{
			"aliases": {
				"bank draft",
				"bank",
				"transferencia",
				"wire transfer",
				"transfer",
				"bank transfer",
				"wire",
			},
			"canonical_name": "Transferencia",
			"candidate_names": ["Bank Draft", "Bank", "Transferencia", "Wire Transfer"],
			"payment_account_mode": "Transferencia",
			"type": "Bank",
			"allow_create": True,
		},
		{
			"aliases": {"credit card", "tarjeta", "tarjeta de credito", "tarjeta de debito"},
			"canonical_name": "Credit Card",
			"candidate_names": ["Credit Card", "Tarjeta"],
			"payment_account_mode": "Credit Card",
			"type": "Bank",
			"allow_create": False,
		},
	)

	for rule in rules:
		if normalized_mode in rule["aliases"]:
			return rule

	frappe.throw(
		frappe._("Método de pago inválido: {0}. Usá Cash, Efectivo, Transferencia o Tarjeta.").format(
			payment_mode
		),
		frappe.ValidationError,
	)


def _get_existing_mode_of_payment(candidate_names):
	for candidate in candidate_names:
		candidate = (candidate or "").strip()
		if not candidate:
			continue

		if not frappe.db.exists("Mode of Payment", candidate):
			continue

		try:
			mode_doc = frappe.get_cached_doc("Mode of Payment", candidate)
		except Exception:
			continue

		if frappe.utils.cint(getattr(mode_doc, "enabled", 1)):
			return candidate

	return ""


def _get_disabled_mode_of_payment(candidate_names):
	for candidate in candidate_names:
		candidate = (candidate or "").strip()
		if not candidate:
			continue

		if not frappe.db.exists("Mode of Payment", candidate):
			continue

		try:
			mode_doc = frappe.get_cached_doc("Mode of Payment", candidate)
		except Exception:
			continue

		if not frappe.utils.cint(getattr(mode_doc, "enabled", 1)):
			return candidate

	return ""


def _ensure_mode_of_payment_account(mode_of_payment_name, company_name, default_account):
	company_name = (company_name or "").strip() or get_platform_company_name()
	default_account = (default_account or "").strip()
	mode_doc = frappe.get_doc("Mode of Payment", mode_of_payment_name)
	if not frappe.utils.cint(getattr(mode_doc, "enabled", 1)):
		frappe.throw(
			frappe._(
				"El Modo de pago {0} está deshabilitado en ERPNext. Habilitalo o creá una alternativa activa antes de completar el pedido."
			).format(mode_of_payment_name),
			frappe.ValidationError,
		)

	account_row = None
	changed = False

	for row in mode_doc.get("accounts") or []:
		if (getattr(row, "company", "") or "").strip() == company_name:
			account_row = row
			break

	if account_row:
		if (getattr(account_row, "default_account", "") or "").strip() != default_account:
			account_row.default_account = default_account
			changed = True
	else:
		mode_doc.append("accounts", {"company": company_name, "default_account": default_account})
		changed = True

	if changed:
		mode_doc.save(ignore_permissions=True)

	return mode_doc.mode_of_payment or mode_of_payment_name


def _create_mode_of_payment_account(mode_of_payment_name, company_name, default_account, mode_type):
	company_name = (company_name or "").strip() or get_platform_company_name()
	default_account = (default_account or "").strip()
	mode_doc = frappe.new_doc("Mode of Payment")
	mode_doc.mode_of_payment = mode_of_payment_name
	mode_doc.type = mode_type
	mode_doc.enabled = 1
	mode_doc.append("accounts", {"company": company_name, "default_account": default_account})
	mode_doc.insert(ignore_permissions=True)

	return mode_doc.mode_of_payment or mode_of_payment_name


def ensure_platform_payment_mode(payment_mode, company_name=None):
	rule = _get_payment_mode_rule(payment_mode)
	company_name = (company_name or "").strip() or get_platform_company_name()
	existing_mode_of_payment = _get_existing_mode_of_payment(rule["candidate_names"])

	if existing_mode_of_payment:
		payment_account = get_platform_payment_account(rule["payment_account_mode"])
		resolved_mode = _ensure_mode_of_payment_account(
			existing_mode_of_payment, company_name, payment_account
		)
		return resolved_mode, payment_account

	disabled_mode_of_payment = _get_disabled_mode_of_payment(rule["candidate_names"])
	if disabled_mode_of_payment:
		if not rule["allow_create"]:
			frappe.throw(
				frappe._(
					"El Modo de pago {0} está deshabilitado en ERPNext. Habilitalo o creá una alternativa activa antes de completar el pedido."
				).format(rule["canonical_name"]),
				frappe.ValidationError,
			)

		payment_account = get_platform_payment_account(rule["payment_account_mode"])
		active_canonical_mode = _get_existing_mode_of_payment([rule["canonical_name"]])
		if active_canonical_mode:
			resolved_mode = _ensure_mode_of_payment_account(
				active_canonical_mode, company_name, payment_account
			)
			return resolved_mode, payment_account

		canonical_disabled_mode = _get_disabled_mode_of_payment([rule["canonical_name"]])
		if canonical_disabled_mode:
			frappe.throw(
				frappe._(
					"El Modo de pago {0} está deshabilitado en ERPNext y no hay una alternativa activa. Habilitalo o creá una alternativa antes de completar el pedido."
				).format(rule["canonical_name"]),
				frappe.ValidationError,
			)

		resolved_mode = _create_mode_of_payment_account(
			rule["canonical_name"], company_name, payment_account, rule["type"]
		)
		return resolved_mode, payment_account

	if not rule["allow_create"]:
		frappe.throw(
			frappe._(
				"No se encontró el Modo de pago {0} en ERPNext. Crealo antes de completar el pedido."
			).format(
				rule["canonical_name"],
			),
			frappe.ValidationError,
		)

	payment_account = get_platform_payment_account(rule["payment_account_mode"])
	resolved_mode = _create_mode_of_payment_account(
		rule["canonical_name"], company_name, payment_account, rule["type"]
	)
	return resolved_mode, payment_account


def _get_existing_platform_payment_account(company_name, account_type):
	company_name = (company_name or "").strip()
	account_type = (account_type or "").strip()
	if not company_name or not account_type:
		return ""

	accounts = frappe.get_all(
		"Account",
		filters={
			"company": company_name,
			"account_type": account_type,
			"disabled": 0,
			"is_group": 0,
		},
		fields=["name"],
		order_by="creation asc",
	)

	for account_row in accounts:
		account_name = (account_row.get("name") or "").strip()
		if not account_name:
			continue

		try:
			account_doc = frappe.get_cached_doc("Account", account_name)
		except Exception:
			continue

		if (getattr(account_doc, "company", "") or "").strip() != company_name:
			continue
		if frappe.utils.cint(getattr(account_doc, "disabled", 0)):
			continue
		if frappe.utils.cint(getattr(account_doc, "is_group", 0)):
			continue
		if (getattr(account_doc, "account_type", "") or "").strip() != account_type:
			continue

		return account_name

	return ""


def _get_platform_payment_account_fallback(company_name, account_type):
	company_name = (company_name or "").strip()
	account_type = (account_type or "").strip()
	if not company_name or not account_type:
		return ""

	existing_account = _get_existing_platform_payment_account(company_name, account_type)
	if existing_account:
		return existing_account

	if account_type != "Bank":
		return ""

	bank_group = _get_platform_bank_group(company_name)
	if not bank_group:
		frappe.throw(
			frappe._(
				"No se encontró un grupo de cuentas bancarias activo y seguro para la compañía {0}. Creá o habilitá Bank Accounts antes de completar el pedido."
			).format(company_name),
			frappe.ValidationError,
		)

	return _create_platform_bank_account(company_name, bank_group)


def _get_platform_bank_group(company_name):
	company_name = (company_name or "").strip() or get_platform_company_name()
	company_abbr = ""
	try:
		company_abbr = (get_platform_company_abbr(company_name) or "").strip()
	except Exception:
		company_abbr = ""

	candidate_names = []
	if company_abbr:
		candidate_names.extend([f"Bank Accounts - {company_abbr}", f"Bank - {company_abbr}"])
	candidate_names.extend(["Bank Accounts", "Bank"])

	for candidate in candidate_names:
		candidate = (candidate or "").strip()
		if not candidate:
			continue
		try:
			group_doc = frappe.get_cached_doc("Account", candidate)
		except Exception:
			continue
		if _is_safe_platform_bank_group(group_doc, company_name):
			return candidate

	bank_groups = frappe.get_all(
		"Account",
		filters={
			"company": company_name,
			"account_type": "Bank",
			"disabled": 0,
			"is_group": 1,
		},
		fields=["name", "account_name", "root_type", "report_type", "parent_account"],
		order_by="creation asc",
	)

	preferred_bank_groups = []
	fallback_bank_groups = []
	for bank_group in bank_groups:
		group_name = (bank_group.get("name") or "").strip()
		if not group_name:
			continue

		labels = {
			_normalize_payment_mode_label(bank_group.get("account_name") or ""),
			_normalize_payment_mode_label(group_name),
		}
		if any(
			label == "bank" or label.startswith("bank account") or label.startswith("bank accounts")
			for label in labels
			if label
		):
			preferred_bank_groups.append(group_name)
		else:
			fallback_bank_groups.append(group_name)

	for group_name in preferred_bank_groups + fallback_bank_groups:
		try:
			group_doc = frappe.get_cached_doc("Account", group_name)
		except Exception:
			continue

		if _is_safe_platform_bank_group(group_doc, company_name):
			return group_name

	return ""


def _is_safe_platform_bank_group(account_doc, company_name):
	if not account_doc:
		return False

	if (getattr(account_doc, "company", "") or "").strip() != company_name:
		return False

	if not frappe.utils.cint(getattr(account_doc, "is_group", 0)):
		return False

	if frappe.utils.cint(getattr(account_doc, "disabled", 0)):
		return False

	if (getattr(account_doc, "account_type", "") or "").strip() != "Bank":
		return False

	if (getattr(account_doc, "root_type", "") or "").strip() != "Asset":
		return False

	return True


def _create_platform_bank_account(company_name, parent_account):
	company_name = (company_name or "").strip() or get_platform_company_name()
	parent_account = (parent_account or "").strip()
	if not parent_account:
		frappe.throw(
			frappe._(
				"No se encontró un grupo bancario válido para crear la cuenta operativa de la compañía {0}."
			).format(company_name),
			frappe.ValidationError,
		)

	parent_doc = frappe.get_cached_doc("Account", parent_account)
	if not _is_safe_platform_bank_group(parent_doc, company_name):
		frappe.throw(
			frappe._(
				"No se encontró un grupo bancario válido para crear la cuenta operativa de la compañía {0}."
			).format(company_name),
			frappe.ValidationError,
		)

	doc = frappe.new_doc("Account")
	doc.account_name = "Bank"
	doc.parent_account = parent_account
	doc.company = company_name
	doc.account_type = "Bank"
	doc.root_type = (getattr(parent_doc, "root_type", "") or "Asset").strip()
	doc.report_type = (getattr(parent_doc, "report_type", "") or "Balance Sheet").strip()
	doc.is_group = 0
	doc.disabled = 0
	try:
		doc.insert(ignore_permissions=True)
	except Exception as exc:
		frappe.log_error(
			message=str(exc),
			title="Error creating platform bank account",
		)
		fallback_account = _get_existing_platform_payment_account(company_name, "Bank")
		if fallback_account:
			return fallback_account
		frappe.throw(
			frappe._("No se pudo crear la cuenta bancaria operativa para la compañía {0}.").format(
				company_name
			),
			frappe.ValidationError,
		)

	return doc.name
