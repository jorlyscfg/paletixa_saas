import json

import frappe
from frappe import _

from paletixa_saas.config.platform_defaults import get_platform_company_name
from paletixa_saas.paletixa_saas.event_reservation_service import classify_legacy_reservation_row


def _get_reservation_item_code():
	config = frappe.get_cached_doc("SaaS Feature Config")
	item_code = (config.get("reservation_item_code") or "Carrito Paletero").strip()
	if not item_code:
		frappe.throw(_("Reservation item code is required for the backfill."), frappe.ValidationError)

	if not frappe.db.exists("Item", item_code):
		frappe.throw(_("The reservation item {0} does not exist.").format(item_code), frappe.ValidationError)

	return item_code


def _get_submitted_sales_orders_with_reservation_item(reservation_item_code):
	rows = frappe.db.sql(
		"""
		SELECT DISTINCT so.name
		FROM `tabSales Order` so
		INNER JOIN `tabSales Order Item` soi ON soi.parent = so.name
		WHERE so.docstatus = 1
		  AND soi.item_code = %s
		ORDER BY so.creation ASC, so.name ASC
		""",
		(reservation_item_code,),
		as_dict=True,
	)
	return [row.get("name") for row in rows if row.get("name")]


def _get_linked_invoice_names(sales_order_name):
	rows = frappe.get_all(
		"Sales Invoice Item",
		filters={"sales_order": sales_order_name},
		fields=["parent"],
		order_by="creation asc",
	)
	return [row.get("parent") for row in rows if row.get("parent")]


def _get_linked_delivery_note_names(sales_order_name):
	rows = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": sales_order_name, "docstatus": 1},
		fields=["parent"],
		order_by="creation asc",
	)
	return [row.get("parent") for row in rows if row.get("parent")]


def _get_delivery_note_docstatus(delivery_note_name):
	delivery_note_name = (delivery_note_name or "").strip()
	if not delivery_note_name:
		return None

	try:
		delivery_note = frappe.get_cached_doc("Delivery Note", delivery_note_name)
	except Exception:
		return None

	return frappe.utils.cint(delivery_note.get("docstatus") or 0)


def _get_delivery_note_warehouse(delivery_note_name):
	rows = frappe.get_all("Delivery Note Item", filters={"parent": delivery_note_name}, fields=["warehouse"])
	warehouses = {
		((row.get("warehouse") or "").strip()) for row in rows if (row.get("warehouse") or "").strip()
	}
	if len(warehouses) == 1:
		return next(iter(warehouses))
	return ""


def _get_invoice_summaries(invoice_names):
	summaries = []
	for invoice_name in invoice_names:
		try:
			invoice = frappe.get_cached_doc("Sales Invoice", invoice_name)
		except Exception:
			summaries.append({"name": invoice_name, "update_stock": None, "docstatus": None})
			continue

		summaries.append(
			{
				"name": invoice.name,
				"update_stock": frappe.utils.cint(invoice.get("update_stock")),
				"docstatus": frappe.utils.cint(invoice.get("docstatus") or 0),
			}
		)
	return summaries


def _build_items_snapshot(so, reservation_item_code):
	items = []
	for so_item in getattr(so, "items", []) or []:
		item_code = (so_item.get("item_code") or "").strip()
		if not item_code:
			continue

		qty = float(so_item.get("qty") or 0.0)
		rate = float(so_item.get("rate") or 0.0)
		amount = so_item.get("amount")
		if amount is None:
			amount = qty * rate

		items.append(
			{
				"item_code": item_code,
				"item_name": so_item.get("item_name") or item_code,
				"qty": qty,
				"rate": rate,
				"amount": float(amount or 0.0),
				"sales_order_item": so_item.get("name"),
				"is_reservation_asset": 1 if item_code == reservation_item_code else 0,
			}
		)

	return items


def _append_conflict(report, sales_order_name, reason, message, details=None):
	entry = {
		"sales_order": sales_order_name,
		"reason": reason,
		"message": message,
	}
	if details:
		entry["details"] = details
	report["conflicts"].append(entry)
	return entry


def _classify_insert_error(exc):
	message = str(exc).lower()
	if "active_allocation_key" in message or "already reserved" in message:
		return "duplicate_active_cart_date"
	if "active_capacity_key" in message or "reservation capacity" in message or "capacity" in message:
		return "over_capacity"
	if "assigned cart warehouse" in message:
		return "missing_assigned_warehouse"
	if "reservation item" in message or ("item" in message and "required" in message):
		return "missing_item"
	return "validation_error"


def _classify_existing_reservation(reservation):
	delivery_note_docstatus = _get_delivery_note_docstatus(reservation.get("delivery_note"))
	classification = classify_legacy_reservation_row(
		reservation, delivery_note_docstatus=delivery_note_docstatus
	)
	changed = False

	if classification == "legacy_consumed":
		if not frappe.utils.cint(reservation.get("legacy_consumed")):
			reservation.legacy_consumed = 1
			changed = True
		if reservation.state != "Released":
			reservation.state = "Released"
			changed = True
		if not str(reservation.get("needs_reconciliation") or "").strip():
			reservation.needs_reconciliation = 1
			changed = True
			if not (reservation.get("reconciliation_notes") or "").strip():
				reservation.reconciliation_notes = _(
					"Legacy Sales Invoice with update_stock=1 was backfilled as a released reservation for manual review."
				)

	elif classification == "legacy_reconciliation":
		if not frappe.utils.cint(reservation.get("needs_reconciliation")):
			reservation.needs_reconciliation = 1
			changed = True
		if reservation.state != "Cancelled":
			reservation.state = "Cancelled"
			changed = True
		if not (reservation.get("reconciliation_notes") or "").strip():
			reservation.reconciliation_notes = _(
				"Legacy Sales Invoice with update_stock=1 needs manual reconciliation."
			)
			changed = True

	elif classification == "legacy_released":
		if reservation.state != "Released":
			reservation.state = "Released"
			changed = True

	return classification, changed


def _backfill_new_reservation(so, reservation_item_code, report):
	reservation = frappe.new_doc("Event Cart Reservation")
	reservation.sales_order = so.name
	reservation.customer = so.customer
	reservation.event_date = so.delivery_date
	reservation.company = so.company
	reservation.reservation_item_code = reservation_item_code
	reservation.state = "Pending Confirmation"
	reservation.grand_total = float(getattr(so, "grand_total", 0.0) or 0.0)
	reservation.base_grand_total = float(
		getattr(so, "base_grand_total", reservation.grand_total) or reservation.grand_total
	)
	reservation.advance_paid = float(getattr(so, "advance_paid", 0.0) or 0.0)
	reservation.outstanding_amount = float(
		getattr(so, "outstanding_amount", reservation.grand_total) or reservation.grand_total
	)

	for item in _build_items_snapshot(so, reservation_item_code):
		reservation.append("items", item)

	invoice_names = _get_linked_invoice_names(so.name)
	delivery_note_names = _get_linked_delivery_note_names(so.name)
	invoices = _get_invoice_summaries(invoice_names)
	submitted_invoices = [invoice for invoice in invoices if invoice.get("docstatus") == 1]
	legacy_invoice = next(
		(invoice for invoice in submitted_invoices if invoice.get("update_stock") == 1), None
	)
	confirmed_invoice = next(
		(invoice for invoice in submitted_invoices if invoice.get("update_stock") == 0), None
	)
	release_warehouse = ""
	if delivery_note_names:
		release_warehouse = _get_delivery_note_warehouse(delivery_note_names[0])

	if legacy_invoice:
		reservation.state = "Released"
		reservation.legacy_consumed = 1
		reservation.needs_reconciliation = 1 if not release_warehouse else 0
		reservation.reconciliation_notes = reservation.reconciliation_notes or _(
			"Backfilled from legacy Sales Invoice {0} with update_stock=1; manual reconciliation may be required."
		).format(legacy_invoice.get("name"))
		if release_warehouse:
			reservation.assigned_cart_warehouse = release_warehouse
			reservation.delivery_note = delivery_note_names[0]
		elif confirmed_invoice:
			_append_conflict(
				report,
				so.name,
				"missing_assigned_warehouse",
				_(
					"Submitted reservation has a confirmation invoice but no cart warehouse could be resolved."
				),
				{"sales_invoice": confirmed_invoice.get("name")},
			)
			return None, False, None

	elif release_warehouse:
		reservation.state = "Released"
		reservation.assigned_cart_warehouse = release_warehouse
		reservation.delivery_note = delivery_note_names[0]

	elif confirmed_invoice:
		_append_conflict(
			report,
			so.name,
			"missing_assigned_warehouse",
			_("Submitted reservation has a confirmation invoice but no cart warehouse could be resolved."),
			{"sales_invoice": confirmed_invoice.get("name")},
		)
		return None, False, None

	try:
		reservation.insert(ignore_permissions=True)
		classification = "legacy_consumed" if legacy_invoice else None
		return reservation, True, classification
	except Exception as exc:
		_append_conflict(
			report,
			so.name,
			_classify_insert_error(exc),
			_("Backfill insert failed for Sales Order {0}.").format(so.name),
			{
				"sales_invoices": [invoice.get("name") for invoice in invoices if invoice.get("name")],
				"delivery_notes": delivery_note_names,
			},
		)
		return None, False, None


def _backfill_existing_reservation(reservation, so, report):
	classification, changed = _classify_existing_reservation(reservation)
	if changed:
		try:
			reservation.save(ignore_permissions=True)
		except Exception as exc:
			_append_conflict(
				report,
				so.name,
				_classify_insert_error(exc),
				_("Backfill update failed for Sales Order {0}.").format(so.name),
			)
			return False, classification

	return changed, classification


def backfill_event_cart_reservations():
	report = {"created": 0, "updated": 0, "legacy_classified": 0, "skipped": 0, "conflicts": []}
	reservation_item_code = _get_reservation_item_code()
	company = get_platform_company_name()
	if not (company or "").strip():
		_append_conflict(
			report,
			"__global__",
			"missing_company",
			_("Platform company is required for backfilling event reservations."),
		)
		return report

	sales_order_names = _get_submitted_sales_orders_with_reservation_item(reservation_item_code)
	for sales_order_name in sales_order_names:
		so = frappe.get_doc("Sales Order", sales_order_name)
		if not (getattr(so, "company", "") or "").strip():
			_append_conflict(
				report, sales_order_name, "missing_company", _("Sales Order is missing a company.")
			)
			continue
		if not getattr(so, "delivery_date", None):
			_append_conflict(
				report, sales_order_name, "missing_event_date", _("Sales Order is missing an event date.")
			)
			continue

		reservation_item_rows = [
			item for item in getattr(so, "items", []) or [] if item.get("item_code") == reservation_item_code
		]
		if len(reservation_item_rows) != 1:
			_append_conflict(
				report,
				sales_order_name,
				"missing_item",
				_("Sales Order must contain exactly one reservation item to backfill."),
				{"reservation_item_code": reservation_item_code, "count": len(reservation_item_rows)},
			)
			continue

		if frappe.db.exists("Event Cart Reservation", sales_order_name):
			reservation = frappe.get_doc("Event Cart Reservation", sales_order_name)
			changed, classification = _backfill_existing_reservation(reservation, so, report)
			if changed:
				report["updated"] += 1
			if classification in {"legacy_consumed", "legacy_released", "legacy_reconciliation"}:
				report["legacy_classified"] += 1
			continue

		reservation, inserted, classification = _backfill_new_reservation(so, reservation_item_code, report)
		if inserted:
			report["created"] += 1
		if classification in {"legacy_consumed", "legacy_released", "legacy_reconciliation"}:
			report["legacy_classified"] += 1
		elif reservation is None:
			report["skipped"] += 1

	if report["conflicts"]:
		frappe.log_error(
			message=json.dumps(report["conflicts"], indent=2, sort_keys=True, default=str),
			title="Event Cart Reservation Backfill Conflicts",
		)

	return report


def execute():
	return backfill_event_cart_reservations()
