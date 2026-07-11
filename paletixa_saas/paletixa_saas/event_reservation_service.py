from collections import defaultdict
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, flt

from paletixa_saas.config.platform_defaults import (
	_validate_platform_distribution_warehouse,
	get_platform_company_name,
	get_platform_distribution_warehouse,
)

ACTIVE_RESERVATION_STATES = ("Pending Confirmation", "Confirmed")


def build_daily_capacity_lock_key(event_date, company=None):
	company_key = (company or "all-companies").strip() or "all-companies"
	return f"event_cart_reservation:{company_key}:{event_date}"


def build_active_capacity_key(event_date, capacity_slot, company=None):
	slot = cint(capacity_slot)
	if not event_date or slot <= 0:
		frappe.throw(_("Capacity slot is required for active reservations."), frappe.ValidationError)

	company_key = (company or "all-companies").strip() or "all-companies"
	return f"{company_key}|{str(event_date).strip()}|{slot}"


def build_active_allocation_key(event_date, assigned_cart_warehouse):
	warehouse = str(assigned_cart_warehouse or "").strip()
	if not event_date or not warehouse:
		frappe.throw(
			_("Assigned cart warehouse is required for confirmed reservations."), frappe.ValidationError
		)

	return f"{str(event_date).strip()}|{warehouse}"


@contextmanager
def _reservation_lock(event_date, company=None, timeout_seconds=5):
	lock_key = build_daily_capacity_lock_key(event_date, company)
	result = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_key, timeout_seconds))
	acquired = bool(result and cint(result[0][0]))
	if not acquired:
		frappe.throw(
			_("Could not lock event reservation capacity for the selected date. Please try again."),
			frappe.ValidationError,
		)

	try:
		yield lock_key
	finally:
		try:
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))
		except Exception:
			pass


def _get_max_reservation_assets():
	config = frappe.get_cached_doc("SaaS Feature Config")
	max_assets = cint(config.get("max_reservation_assets") or 10)
	return max_assets if max_assets > 0 else 10


def _get_active_capacity_slots(event_date, company=None, exclude_name=None):
	filters = {"event_date": event_date, "state": ["in", list(ACTIVE_RESERVATION_STATES)]}
	if company:
		filters["company"] = company
	if exclude_name:
		filters["name"] = ["!=", exclude_name]

	reservations = frappe.get_all(
		"Event Cart Reservation",
		filters=filters,
		fields=["name", "capacity_slot"],
		order_by="capacity_slot asc, creation asc, name asc",
	)
	occupied_slots = set()
	for reservation in reservations:
		slot = cint(reservation.get("capacity_slot"))
		if slot > 0:
			occupied_slots.add(slot)
	return occupied_slots


def _first_free_capacity_slot(occupied_slots, max_assets):
	for slot in range(1, max_assets + 1):
		if slot not in occupied_slots:
			return slot
	return None


def assert_daily_capacity_available(
	event_date, company=None, exclude_name=None, required_slots=1, preferred_capacity_slot=None
):
	if not event_date:
		frappe.throw(_("Event date is required."), frappe.ValidationError)

	if cint(required_slots) != 1:
		frappe.throw(_("Reservations currently reserve one capacity slot at a time."), frappe.ValidationError)

	with _reservation_lock(event_date, company):
		occupied_slots = _get_active_capacity_slots(event_date, company=company, exclude_name=exclude_name)
		max_assets = _get_max_reservation_assets()
		preferred_slot = cint(preferred_capacity_slot)
		if preferred_slot > 0:
			if preferred_slot > max_assets:
				frappe.throw(
					_("The selected date has reached its reservation capacity."),
					frappe.ValidationError,
				)
			if preferred_slot in occupied_slots:
				frappe.throw(
					_("The selected date has reached its reservation capacity."),
					frappe.ValidationError,
				)
			slot = preferred_slot
		else:
			slot = _first_free_capacity_slot(occupied_slots, max_assets)

		if not slot:
			frappe.throw(
				_("The selected date has reached its reservation capacity."),
				frappe.ValidationError,
			)

		return slot, build_active_capacity_key(event_date, slot, company=company)


def assert_active_allocation_available(event_date, assigned_cart_warehouse, exclude_name=None):
	allocation_key = build_active_allocation_key(event_date, assigned_cart_warehouse)
	filters = {"active_allocation_key": allocation_key, "state": "Confirmed"}
	if exclude_name:
		filters["name"] = ["!=", exclude_name]

	existing = frappe.get_all("Event Cart Reservation", filters=filters, fields=["name"], limit=1)
	if existing:
		frappe.throw(
			_("The selected event date and cart warehouse are already reserved."),
			frappe.ValidationError,
		)

	return allocation_key


def _get_company_abbr(company_name):
	company_name = (company_name or "").strip()
	if not company_name:
		frappe.throw(
			_("La compañía es obligatoria para reservas de eventos confirmadas."),
			frappe.ValidationError,
		)

	try:
		company = frappe.get_cached_doc("Company", company_name)
	except Exception:
		frappe.throw(_("La compañía {0} no existe.").format(company_name), frappe.ValidationError)

	company_abbr = getattr(company, "abbr", None)
	if not company_abbr and hasattr(company, "get"):
		company_abbr = company.get("abbr")
	company_abbr = (company_abbr or "").strip()
	if company_abbr:
		return company_abbr

	frappe.throw(
		_("Configurá la abreviatura de la compañía {0}.").format(company_name),
		frappe.ValidationError,
	)


def _get_event_cart_parent_group_name(company_name):
	return f"Carritos de Eventos - {_get_company_abbr(company_name)}"


def validate_confirmed_allocation_warehouse(warehouse, company_name=None):
	company = (company_name or "").strip() or get_platform_company_name()
	warehouse = _validate_platform_distribution_warehouse(warehouse, company_name=company)
	allowed_names = {get_platform_distribution_warehouse()}
	parent_group_name = _get_event_cart_parent_group_name(company)

	if frappe.db.exists("Warehouse", parent_group_name):
		event_warehouses = frappe.get_all(
			"Warehouse",
			filters={"parent_warehouse": parent_group_name, "company": company, "is_group": 0, "disabled": 0},
			fields=["name"],
		)
		for row in event_warehouses:
			if row.get("name"):
				allowed_names.add(row.get("name"))

	if warehouse not in allowed_names:
		frappe.throw(
			_("El almacén seleccionado no está habilitado para reservas de eventos."),
			frappe.ValidationError,
		)

	return warehouse


def classify_legacy_reservation_row(reservation, delivery_note_docstatus=None):
	if cint(reservation.get("legacy_consumed")):
		return "legacy_consumed"

	if cint(reservation.get("needs_reconciliation")):
		return "legacy_reconciliation"

	sales_invoice = reservation.get("sales_invoice")
	if not sales_invoice:
		return None

	try:
		invoice = frappe.get_cached_doc("Sales Invoice", sales_invoice)
	except Exception:
		return "legacy_reconciliation"

	if cint(invoice.get("docstatus") or 0) != 1:
		return None

	if cint(invoice.get("update_stock")) != 1:
		return None

	delivery_note_docstatus = (
		reservation.get("delivery_note_docstatus")
		if delivery_note_docstatus is None
		else delivery_note_docstatus
	)
	if reservation.get("delivery_note"):
		if cint(delivery_note_docstatus or 0) != 1:
			return None
		return "legacy_released"

	if reservation.get("credit_note") or reservation.get("refund_payment_entry"):
		return "legacy_reconciliation"

	return "legacy_consumed"


def get_event_reservation_production_demand(event_date, company=None):
	if not event_date:
		frappe.throw(_("Event date is required."), frappe.ValidationError)

	company = (company or "").strip() or get_platform_company_name()

	reservations = frappe.get_all(
		"Event Cart Reservation",
		filters={"event_date": event_date, "company": company},
		fields=[
			"name",
			"state",
			"sales_invoice",
			"delivery_note",
			"credit_note",
			"refund_payment_entry",
			"legacy_consumed",
			"needs_reconciliation",
		],
		order_by="creation asc",
	)

	reservation_names = [reservation.get("name") for reservation in reservations if reservation.get("name")]
	delivery_note_names = []
	for reservation in reservations:
		delivery_note_name = (reservation.get("delivery_note") or "").strip()
		if delivery_note_name and delivery_note_name not in delivery_note_names:
			delivery_note_names.append(delivery_note_name)
	delivery_note_docstatus_by_name = {}
	if delivery_note_names:
		delivery_notes = frappe.get_all(
			"Delivery Note",
			filters={"name": ["in", delivery_note_names]},
			fields=["name", "docstatus"],
		)
		delivery_note_docstatus_by_name = {
			(delivery_note.get("name") or "").strip(): cint(delivery_note.get("docstatus") or 0)
			for delivery_note in delivery_notes
			if (delivery_note.get("name") or "").strip()
		}
	items_by_reservation = defaultdict(list)
	if reservation_names:
		reservation_items = frappe.get_all(
			"Event Cart Reservation Item",
			filters={"parent": ["in", reservation_names]},
			fields=["parent", "item_code", "item_name", "qty", "is_reservation_asset"],
			order_by="idx asc",
		)
		for item in reservation_items:
			items_by_reservation[item.get("parent")].append(item)

	grouped = defaultdict(lambda: {"item_code": "", "item_name": "", "qty": 0.0})
	for reservation in reservations:
		if reservation.get("state") not in {"Confirmed"}:
			continue

		if classify_legacy_reservation_row(
			reservation,
			delivery_note_docstatus=delivery_note_docstatus_by_name.get(
				(reservation.get("delivery_note") or "").strip()
			),
		) in {
			"legacy_consumed",
			"legacy_released",
			"legacy_reconciliation",
		}:
			continue

		for item in items_by_reservation.get(reservation.get("name"), []):
			if cint(item.get("is_reservation_asset")):
				continue

			item_code = (item.get("item_code") or "").strip()
			if not item_code:
				continue

			bucket = grouped[item_code]
			bucket["item_code"] = item_code
			bucket["item_name"] = item.get("item_name") or bucket["item_name"] or item_code
			bucket["qty"] += flt(item.get("qty"))

	items = sorted(grouped.values(), key=lambda row: row["item_code"])
	return {"date": event_date, "items": items}
