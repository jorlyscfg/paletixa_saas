import json
from pathlib import Path
from types import SimpleNamespace

import frappe
import pytest

from paletixa_saas.paletixa_saas.doctype.event_cart_reservation.event_cart_reservation import (
	EventCartReservation,
)
from paletixa_saas.paletixa_saas.event_reservation_service import (
	assert_active_allocation_available,
	assert_daily_capacity_available,
	build_active_allocation_key,
	build_active_capacity_key,
	classify_legacy_reservation_row,
	get_event_reservation_production_demand,
	validate_confirmed_allocation_warehouse,
)


class _FakeConfig:
	def __init__(self, values):
		self._values = values

	def get(self, key, default=None):
		return self._values.get(key, default)


class _FakeInvoice:
	def __init__(self, update_stock, docstatus=1):
		self.update_stock = update_stock
		self.docstatus = docstatus

	def get(self, key, default=None):
		return getattr(self, key, default)


def _mock_lock_sql(query, params=None):
	if "GET_LOCK" in query:
		return [(1,)]
	if "RELEASE_LOCK" in query:
		return [(1,)]
	raise AssertionError(f"Unexpected SQL: {query}")


def test_pending_capacity_assigns_distinct_abstract_slots_and_rejects_next(monkeypatch):
	active_reservations = []

	def fake_get_all(doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs):
		assert doctype == "Event Cart Reservation"
		assert filters["event_date"] == "2026-07-07"
		assert filters["company"] == "Tenant Co"
		assert filters["state"] == ["in", ["Pending Confirmation", "Confirmed"]]
		return [
			{"name": reservation["name"], "capacity_slot": reservation["capacity_slot"]}
			for reservation in active_reservations
		]

	monkeypatch.setattr(
		frappe, "get_cached_doc", lambda doctype, name=None: _FakeConfig({"max_reservation_assets": 2})
	)
	monkeypatch.setattr(frappe, "get_all", fake_get_all)
	monkeypatch.setattr(frappe.db, "sql", _mock_lock_sql)

	slot_1, key_1 = assert_daily_capacity_available("2026-07-07", company="Tenant Co")
	active_reservations.append({"name": "RES-1", "capacity_slot": slot_1})
	slot_2, key_2 = assert_daily_capacity_available("2026-07-07", company="Tenant Co")
	active_reservations.append({"name": "RES-2", "capacity_slot": slot_2})

	assert {slot_1, slot_2} == {1, 2}
	assert key_1 == build_active_capacity_key("2026-07-07", slot_1, "Tenant Co")
	assert key_2 == build_active_capacity_key("2026-07-07", slot_2, "Tenant Co")
	with pytest.raises(frappe.ValidationError, match="capacity"):
		assert_daily_capacity_available("2026-07-07", company="Tenant Co")


def test_pending_capacity_scopes_active_key_by_company(monkeypatch):
	def fake_get_all(doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs):
		assert doctype == "Event Cart Reservation"
		assert filters["event_date"] == "2026-07-07"
		assert filters["state"] == ["in", ["Pending Confirmation", "Confirmed"]]
		assert filters["company"] in {"Tenant Co", "Other Co"}
		return []

	monkeypatch.setattr(
		frappe, "get_cached_doc", lambda doctype, name=None: _FakeConfig({"max_reservation_assets": 2})
	)
	monkeypatch.setattr(frappe, "get_all", fake_get_all)
	monkeypatch.setattr(frappe.db, "sql", _mock_lock_sql)

	tenant_slot, tenant_key = assert_daily_capacity_available("2026-07-07", company="Tenant Co")
	other_slot, other_key = assert_daily_capacity_available("2026-07-07", company="Other Co")

	assert tenant_slot == other_slot == 1
	assert tenant_key == build_active_capacity_key("2026-07-07", 1, "Tenant Co")
	assert other_key == build_active_capacity_key("2026-07-07", 1, "Other Co")
	assert tenant_key != other_key


def test_pending_reservations_clear_physical_allocation_key(monkeypatch):
	sequence = iter(
		[
			(1, build_active_capacity_key("2026-07-07", 1, "Tenant Co")),
			(2, build_active_capacity_key("2026-07-07", 2, "Tenant Co")),
		]
	)

	monkeypatch.setattr(
		frappe,
		"get_cached_doc",
		lambda doctype, name=None: _FakeConfig({"max_reservation_assets": 2}),
	)
	monkeypatch.setattr(
		frappe,
		"get_all",
		lambda *args, **kwargs: [],
	)
	monkeypatch.setattr(frappe.db, "sql", _mock_lock_sql)
	monkeypatch.setattr(
		"paletixa_saas.paletixa_saas.doctype.event_cart_reservation.event_cart_reservation.assert_daily_capacity_available",
		lambda *args, **kwargs: next(sequence),
	)

	first = EventCartReservation(
		{
			"doctype": "Event Cart Reservation",
			"sales_order": "SO-1",
			"customer": "Customer A",
			"event_date": "2026-07-07",
			"company": "Tenant Co",
			"reservation_item_code": "ITEM-A",
			"state": "Pending Confirmation",
		}
	)
	second = EventCartReservation(
		{
			"doctype": "Event Cart Reservation",
			"sales_order": "SO-2",
			"customer": "Customer A",
			"event_date": "2026-07-07",
			"company": "Tenant Co",
			"reservation_item_code": "ITEM-A",
			"state": "Pending Confirmation",
		}
	)

	first.validate()
	second.validate()

	assert {first.capacity_slot, second.capacity_slot} == {1, 2}
	assert first.active_capacity_key == build_active_capacity_key(
		"2026-07-07", first.capacity_slot, "Tenant Co"
	)
	assert second.active_capacity_key == build_active_capacity_key(
		"2026-07-07", second.capacity_slot, "Tenant Co"
	)
	assert first.active_allocation_key is None
	assert second.active_allocation_key is None


def test_confirmed_allocation_key_is_unique_per_date_and_warehouse(monkeypatch):
	allocation_key = build_active_allocation_key("2026-07-07", "Cart 1")

	def fake_get_all(doctype, filters=None, fields=None, limit=None, **kwargs):
		assert doctype == "Event Cart Reservation"
		if filters == {
			"active_allocation_key": allocation_key,
			"state": "Confirmed",
			"name": ["!=", "RES-1"],
		}:
			return [{"name": "RES-2"}]
		return []

	monkeypatch.setattr(frappe, "get_all", fake_get_all)

	assert build_active_allocation_key("2026-07-07", "Cart 1") == "2026-07-07|Cart 1"
	with pytest.raises(frappe.ValidationError):
		assert_active_allocation_available("2026-07-07", "Cart 1", exclude_name="RES-1")


def test_confirmed_reservation_sets_capacity_and_allocation_keys(monkeypatch):
	monkeypatch.setattr(
		"paletixa_saas.paletixa_saas.doctype.event_cart_reservation.event_cart_reservation.assert_daily_capacity_available",
		lambda *args, **kwargs: (2, build_active_capacity_key("2026-07-07", 2, "Tenant Co")),
	)
	monkeypatch.setattr(
		"paletixa_saas.paletixa_saas.doctype.event_cart_reservation.event_cart_reservation.assert_active_allocation_available",
		lambda *args, **kwargs: build_active_allocation_key("2026-07-07", "Cart 1"),
	)
	monkeypatch.setattr(
		"paletixa_saas.paletixa_saas.doctype.event_cart_reservation.event_cart_reservation.validate_confirmed_allocation_warehouse",
		lambda warehouse, company_name=None: warehouse,
	)

	doc = EventCartReservation(
		{
			"doctype": "Event Cart Reservation",
			"sales_order": "SO-3",
			"customer": "Customer A",
			"event_date": "2026-07-07",
			"company": "Tenant Co",
			"reservation_item_code": "ITEM-A",
			"state": "Confirmed",
			"assigned_cart_warehouse": "Cart 1",
		}
	)

	doc.validate()

	assert doc.capacity_slot == 2
	assert doc.active_capacity_key == build_active_capacity_key("2026-07-07", 2, "Tenant Co")
	assert doc.active_allocation_key == build_active_allocation_key("2026-07-07", "Cart 1")


def test_confirmed_allocation_warehouse_must_be_allowed_for_event_reservations(monkeypatch):
	def fake_get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig(
				{
					"company_name": "Tenant Co",
					"company_abbr": "TC",
					"default_distribution_warehouse": "Fabrica - TC",
				}
			)
		if doctype == "Warehouse" and name in {"Fabrica - TC", "Cart 1"}:
			return SimpleNamespace(company="Tenant Co", is_group=0, disabled=0)
		if doctype == "Company":
			return SimpleNamespace(abbr="TC")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "get_cached_doc", fake_get_cached_doc)
	monkeypatch.setattr(
		frappe.db,
		"exists",
		lambda doctype, name=None, *args, **kwargs: doctype == "Warehouse"
		and name == "Carritos de Eventos - TC",
	)
	monkeypatch.setattr(
		frappe,
		"get_all",
		lambda doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs: [{"name": "Cart 1"}]
		if doctype == "Warehouse"
		else [],
	)

	assert validate_confirmed_allocation_warehouse("Cart 1", company_name="Tenant Co") == "Cart 1"


def test_confirmed_allocation_warehouse_uses_non_default_company_event_group(monkeypatch):
	def fake_get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig(
				{
					"company_name": "Tenant Co",
					"company_abbr": "TC",
					"default_distribution_warehouse": "Fabrica - TC",
				}
			)
		if doctype == "Warehouse" and name == "Fabrica - TC":
			return SimpleNamespace(company="Tenant Co", is_group=0, disabled=0)
		if doctype == "Warehouse" and name == "Other Co Cart":
			return SimpleNamespace(company="Other Co", is_group=0, disabled=0)
		if doctype == "Company" and name == "Other Co":
			return SimpleNamespace(abbr="OC")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	def fake_get_all(doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs):
		assert doctype == "Warehouse"
		assert filters == {
			"parent_warehouse": "Carritos de Eventos - OC",
			"company": "Other Co",
			"is_group": 0,
			"disabled": 0,
		}
		return [{"name": "Other Co Cart"}]

	monkeypatch.setattr(frappe, "get_cached_doc", fake_get_cached_doc)
	monkeypatch.setattr(
		frappe.db,
		"exists",
		lambda doctype, name=None, *args, **kwargs: doctype == "Warehouse"
		and name == "Carritos de Eventos - OC",
	)
	monkeypatch.setattr(frappe, "get_all", fake_get_all)

	assert (
		validate_confirmed_allocation_warehouse("Other Co Cart", company_name="Other Co") == "Other Co Cart"
	)


@pytest.mark.parametrize(
	"company_name, company_doc, expected_match",
	[
		("Missing Co", None, "no existe"),
		("Other Co", SimpleNamespace(abbr=""), "abreviatura"),
	],
)
def test_confirmed_allocation_warehouse_rejects_missing_company_abbr(
	monkeypatch, company_name, company_doc, expected_match
):
	def fake_get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig(
				{
					"company_name": "Tenant Co",
					"company_abbr": "TC",
					"default_distribution_warehouse": "Fabrica - TC",
				}
			)
		if doctype == "Warehouse" and name == "Cart 1":
			return SimpleNamespace(company=company_name, is_group=0, disabled=0)
		if doctype == "Company" and name == company_name:
			if company_doc is None:
				raise RuntimeError("missing company")
			return company_doc
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "get_cached_doc", fake_get_cached_doc)
	monkeypatch.setattr(frappe.db, "exists", lambda *args, **kwargs: False)
	monkeypatch.setattr(frappe, "get_all", lambda *args, **kwargs: [])

	with pytest.raises(frappe.ValidationError, match=expected_match):
		validate_confirmed_allocation_warehouse("Cart 1", company_name=company_name)


@pytest.mark.parametrize(
	"warehouse_doc, expected_message",
	[
		(SimpleNamespace(company="Other Co", is_group=0, disabled=0), "compa"),
		(SimpleNamespace(company="Tenant Co", is_group=1, disabled=0), "grupo"),
		(SimpleNamespace(company="Tenant Co", is_group=0, disabled=1), "deshabilitado"),
		(SimpleNamespace(company="Tenant Co", is_group=0, disabled=0), "habilitado"),
	],
)
def test_confirmed_allocation_warehouse_rejects_invalid_options(monkeypatch, warehouse_doc, expected_message):
	def fake_get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig(
				{
					"company_name": "Tenant Co",
					"company_abbr": "TC",
					"default_distribution_warehouse": "Fabrica - TC",
				}
			)
		if doctype == "Warehouse" and name == "Cart 1":
			return warehouse_doc
		if doctype == "Company":
			return SimpleNamespace(abbr="TC")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "get_cached_doc", fake_get_cached_doc)
	monkeypatch.setattr(frappe.db, "exists", lambda *args, **kwargs: False)
	monkeypatch.setattr(frappe, "get_all", lambda *args, **kwargs: [])

	with pytest.raises(frappe.ValidationError, match=expected_message):
		validate_confirmed_allocation_warehouse("Cart 1", company_name="Tenant Co")


def test_confirmed_reservation_passes_document_company_to_warehouse_validation(monkeypatch):
	seen = {}

	def fake_get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig(
				{
					"company_name": "Tenant Co",
					"company_abbr": "TC",
					"default_distribution_warehouse": "Fabrica - TC",
				}
			)
		if doctype == "Warehouse" and name in {"Fabrica - TC", "Cart 1"}:
			return SimpleNamespace(company="Tenant Co", is_group=0, disabled=0)
		if doctype == "Company":
			return SimpleNamespace(abbr="TC")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "get_cached_doc", fake_get_cached_doc)
	monkeypatch.setattr(
		frappe.db,
		"exists",
		lambda doctype, name=None, *args, **kwargs: doctype == "Warehouse"
		and name == "Carritos de Eventos - TC",
	)
	monkeypatch.setattr(
		frappe,
		"get_all",
		lambda doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs: [{"name": "Cart 1"}]
		if doctype == "Warehouse"
		else [],
	)
	monkeypatch.setattr(
		"paletixa_saas.paletixa_saas.doctype.event_cart_reservation.event_cart_reservation.assert_daily_capacity_available",
		lambda *args, **kwargs: (1, build_active_capacity_key("2026-07-07", 1, "Tenant Co")),
	)
	monkeypatch.setattr(
		"paletixa_saas.paletixa_saas.doctype.event_cart_reservation.event_cart_reservation.assert_active_allocation_available",
		lambda *args, **kwargs: build_active_allocation_key("2026-07-07", "Cart 1"),
	)
	monkeypatch.setattr(
		"paletixa_saas.paletixa_saas.doctype.event_cart_reservation.event_cart_reservation.validate_confirmed_allocation_warehouse",
		lambda warehouse, company_name=None: seen.update({"company_name": company_name}) or warehouse,
	)

	doc = EventCartReservation(
		{
			"doctype": "Event Cart Reservation",
			"sales_order": "SO-3",
			"customer": "Customer A",
			"event_date": "2026-07-07",
			"company": "Tenant Co",
			"reservation_item_code": "ITEM-A",
			"state": "Confirmed",
			"assigned_cart_warehouse": "Cart 1",
		}
	)

	doc.validate()

	assert seen["company_name"] == "Tenant Co"


def test_confirmed_reservation_rejects_wrong_company_warehouse(monkeypatch):
	def fake_get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig(
				{
					"company_name": "Tenant Co",
					"company_abbr": "TC",
					"default_distribution_warehouse": "Fabrica - TC",
				}
			)
		if doctype == "Warehouse" and name == "Wrong Co Cart":
			return SimpleNamespace(company="Other Co", is_group=0, disabled=0)
		if doctype == "Company":
			return SimpleNamespace(abbr="TC")
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "get_cached_doc", fake_get_cached_doc)
	monkeypatch.setattr(frappe.db, "exists", lambda doctype, name=None, *args, **kwargs: False)
	monkeypatch.setattr(
		frappe,
		"get_all",
		lambda doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs: [],
	)
	monkeypatch.setattr(
		"paletixa_saas.paletixa_saas.doctype.event_cart_reservation.event_cart_reservation.assert_daily_capacity_available",
		lambda *args, **kwargs: (1, build_active_capacity_key("2026-07-07", 1, "Tenant Co")),
	)

	doc = EventCartReservation(
		{
			"doctype": "Event Cart Reservation",
			"sales_order": "SO-4",
			"customer": "Customer A",
			"event_date": "2026-07-07",
			"company": "Tenant Co",
			"reservation_item_code": "ITEM-A",
			"state": "Confirmed",
			"assigned_cart_warehouse": "Wrong Co Cart",
		}
	)

	with pytest.raises(frappe.ValidationError, match="compa"):
		doc.validate()


@pytest.mark.parametrize("state", ["Cancelled", "Released"])
def test_inactive_reservations_clear_active_keys(monkeypatch, state):
	doc = EventCartReservation(
		{
			"doctype": "Event Cart Reservation",
			"sales_order": "SO-4",
			"customer": "Customer A",
			"event_date": "2026-07-07",
			"company": "Tenant Co",
			"reservation_item_code": "ITEM-A",
			"state": state,
			"capacity_slot": 2,
			"active_capacity_key": build_active_capacity_key("2026-07-07", 2, "Tenant Co"),
			"active_allocation_key": build_active_allocation_key("2026-07-07", "Cart 1"),
		}
	)

	doc.validate()

	assert doc.active_capacity_key is None
	assert doc.active_allocation_key is None
	assert doc.capacity_slot == 2


def test_existing_reservation_rejects_direct_lifecycle_and_accounting_link_mutation(monkeypatch):
	doc = EventCartReservation(
		{
			"doctype": "Event Cart Reservation",
			"name": "SO-1",
			"sales_order": "SO-1",
			"state": "Confirmed",
			"sales_invoice": "SI-2",
		}
	)
	doc.flags.event_reservation_service_operation = False
	before = frappe._dict(state="Pending Confirmation", sales_invoice=None)
	monkeypatch.setattr(doc, "is_new", lambda: False)
	monkeypatch.setattr(doc, "get_doc_before_save", lambda: before)

	with pytest.raises(frappe.PermissionError, match="lifecycle and accounting links"):
		doc._reject_unauthorized_lifecycle_mutation()


def test_existing_reservation_allows_narrow_authorized_service_mutation(monkeypatch):
	doc = EventCartReservation(
		{
			"doctype": "Event Cart Reservation",
			"name": "SO-1",
			"sales_order": "SO-1",
			"state": "Confirmed",
			"sales_invoice": "SI-1",
		}
	)
	doc.flags.event_reservation_service_operation = True
	monkeypatch.setattr(doc, "is_new", lambda: False)
	monkeypatch.setattr(
		doc,
		"get_doc_before_save",
		lambda: (_ for _ in ()).throw(AssertionError("authorized service should bypass comparison")),
	)

	doc._reject_unauthorized_lifecycle_mutation()


def test_event_reservation_deletion_is_always_rejected():
	doc = EventCartReservation({"doctype": "Event Cart Reservation", "sales_order": "SO-1"})

	with pytest.raises(frappe.PermissionError, match="audit records"):
		doc.on_trash()


def test_event_reservation_doctype_grants_read_only_desk_access():
	metadata_path = (
		Path(__file__).parent
		/ "paletixa_saas"
		/ "doctype"
		/ "event_cart_reservation"
		/ "event_cart_reservation.json"
	)
	permissions = json.loads(metadata_path.read_text())["permissions"]

	assert permissions == [
		{
			"email": 1,
			"export": 1,
			"print": 1,
			"read": 1,
			"report": 1,
			"role": "System Manager",
			"share": 1,
		}
	]


def test_legacy_reservation_classification_requires_submitted_delivery_notes(monkeypatch):
	def fake_get_cached_doc(doctype, name=None):
		if doctype == "Sales Invoice" and name == "SI-LEGACY":
			return _FakeInvoice(1)
		if doctype == "Sales Invoice" and name == "SI-DRAFT":
			return _FakeInvoice(1, docstatus=0)
		if doctype == "Sales Invoice" and name == "SI-CANCELLED":
			return _FakeInvoice(1, docstatus=2)
		if doctype == "Sales Invoice" and name == "SI-NORMAL":
			return _FakeInvoice(0)
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "get_cached_doc", fake_get_cached_doc)

	assert classify_legacy_reservation_row({"legacy_consumed": 1}) == "legacy_consumed"
	assert classify_legacy_reservation_row({"needs_reconciliation": 1}) == "legacy_reconciliation"
	assert classify_legacy_reservation_row({"sales_invoice": "SI-LEGACY"}) == "legacy_consumed"
	assert (
		classify_legacy_reservation_row(
			{"sales_invoice": "SI-LEGACY", "delivery_note": "DN-1"}, delivery_note_docstatus=1
		)
		== "legacy_released"
	)
	assert (
		classify_legacy_reservation_row(
			{"sales_invoice": "SI-LEGACY", "delivery_note": "DN-DRAFT"}, delivery_note_docstatus=0
		)
		is None
	)
	assert (
		classify_legacy_reservation_row(
			{"sales_invoice": "SI-LEGACY", "delivery_note": "DN-CANCELLED"}, delivery_note_docstatus=2
		)
		is None
	)
	assert (
		classify_legacy_reservation_row(
			{"sales_invoice": "SI-LEGACY", "delivery_note": "DN-MISSING"}, delivery_note_docstatus=None
		)
		is None
	)
	assert classify_legacy_reservation_row({"sales_invoice": "SI-DRAFT"}) is None
	assert classify_legacy_reservation_row({"sales_invoice": "SI-CANCELLED"}) is None
	assert classify_legacy_reservation_row({"sales_invoice": "SI-NORMAL"}) is None


def test_get_event_reservation_production_demand_uses_delivery_note_docstatus(monkeypatch):
	reservations = [
		{
			"name": "RES-SUBMITTED",
			"state": "Confirmed",
			"sales_invoice": "SI-LEGACY",
			"delivery_note": "DN-SUBMITTED",
			"credit_note": "",
			"refund_payment_entry": "",
			"legacy_consumed": 0,
			"needs_reconciliation": 0,
		},
		{
			"name": "RES-DRAFT",
			"state": "Confirmed",
			"sales_invoice": "SI-LEGACY",
			"delivery_note": "DN-DRAFT",
			"credit_note": "",
			"refund_payment_entry": "",
			"legacy_consumed": 0,
			"needs_reconciliation": 0,
		},
		{
			"name": "RES-CANCELLED",
			"state": "Confirmed",
			"sales_invoice": "SI-LEGACY",
			"delivery_note": "DN-CANCELLED",
			"credit_note": "",
			"refund_payment_entry": "",
			"legacy_consumed": 0,
			"needs_reconciliation": 0,
		},
		{
			"name": "RES-MISSING",
			"state": "Confirmed",
			"sales_invoice": "SI-LEGACY",
			"delivery_note": "DN-MISSING",
			"credit_note": "",
			"refund_payment_entry": "",
			"legacy_consumed": 0,
			"needs_reconciliation": 0,
		},
	]
	items = [
		{
			"parent": "RES-SUBMITTED",
			"item_code": "ITEM-SUBMITTED",
			"item_name": "Submitted",
			"qty": 1,
			"is_reservation_asset": 0,
		},
		{
			"parent": "RES-DRAFT",
			"item_code": "ITEM-DRAFT",
			"item_name": "Draft",
			"qty": 2,
			"is_reservation_asset": 0,
		},
		{
			"parent": "RES-CANCELLED",
			"item_code": "ITEM-CANCELLED",
			"item_name": "Cancelled",
			"qty": 3,
			"is_reservation_asset": 0,
		},
		{
			"parent": "RES-MISSING",
			"item_code": "ITEM-MISSING",
			"item_name": "Missing",
			"qty": 4,
			"is_reservation_asset": 0,
		},
	]

	def fake_get_all(doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs):
		if doctype == "Event Cart Reservation":
			assert filters == {"event_date": "2026-07-07", "company": "Tenant Co"}
			return reservations
		if doctype == "Event Cart Reservation Item":
			assert filters == {"parent": ["RES-SUBMITTED", "RES-DRAFT", "RES-CANCELLED", "RES-MISSING"]}
			return items
		if doctype == "Delivery Note":
			assert filters == {"name": ["in", ["DN-SUBMITTED", "DN-DRAFT", "DN-CANCELLED", "DN-MISSING"]]}
			assert fields == ["name", "docstatus"]
			return [
				{"name": "DN-SUBMITTED", "docstatus": 1},
				{"name": "DN-DRAFT", "docstatus": 0},
				{"name": "DN-CANCELLED", "docstatus": 2},
			]
		raise AssertionError(f"Unexpected get_all lookup: {doctype} / {filters}")

	def fake_get_cached_doc(doctype, name=None):
		if doctype == "Sales Invoice" and name == "SI-LEGACY":
			return _FakeInvoice(1)
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(
		"paletixa_saas.paletixa_saas.event_reservation_service.get_platform_company_name",
		lambda: "Tenant Co",
	)
	monkeypatch.setattr(frappe, "get_all", fake_get_all)
	monkeypatch.setattr(frappe, "get_cached_doc", fake_get_cached_doc)

	result = get_event_reservation_production_demand("2026-07-07")

	assert result == {
		"date": "2026-07-07",
		"items": [
			{"item_code": "ITEM-CANCELLED", "item_name": "Cancelled", "qty": 3.0},
			{"item_code": "ITEM-DRAFT", "item_name": "Draft", "qty": 2.0},
			{"item_code": "ITEM-MISSING", "item_name": "Missing", "qty": 4.0},
		],
	}


def test_production_demand_includes_confirmed_rows_excludes_pending_cancelled_released_legacy_and_other_company_rows(
	monkeypatch,
):
	reservations = [
		{
			"name": "RES-1",
			"company": "Tenant Co",
			"state": "Confirmed",
			"sales_invoice": "SI-NORMAL",
			"delivery_note": "",
			"credit_note": "",
			"refund_payment_entry": "",
			"legacy_consumed": 0,
			"needs_reconciliation": 0,
		},
		{
			"name": "RES-2",
			"company": "Tenant Co",
			"state": "Pending Confirmation",
			"sales_invoice": "",
			"delivery_note": "",
			"credit_note": "",
			"refund_payment_entry": "",
			"legacy_consumed": 0,
			"needs_reconciliation": 0,
		},
		{
			"name": "RES-3",
			"company": "Tenant Co",
			"state": "Cancelled",
			"sales_invoice": "",
			"delivery_note": "",
			"credit_note": "",
			"refund_payment_entry": "",
			"legacy_consumed": 0,
			"needs_reconciliation": 0,
		},
		{
			"name": "RES-4",
			"company": "Tenant Co",
			"state": "Released",
			"sales_invoice": "",
			"delivery_note": "",
			"credit_note": "",
			"refund_payment_entry": "",
			"legacy_consumed": 0,
			"needs_reconciliation": 0,
		},
		{
			"name": "RES-5",
			"company": "Tenant Co",
			"state": "Confirmed",
			"sales_invoice": "SI-LEGACY",
			"delivery_note": "",
			"credit_note": "",
			"refund_payment_entry": "",
			"legacy_consumed": 0,
			"needs_reconciliation": 0,
		},
		{
			"name": "RES-6",
			"company": "Tenant Co",
			"state": "Confirmed",
			"sales_invoice": "",
			"delivery_note": "",
			"credit_note": "",
			"refund_payment_entry": "",
			"legacy_consumed": 0,
			"needs_reconciliation": 1,
		},
		{
			"name": "RES-7",
			"company": "Tenant Co",
			"state": "Confirmed",
			"sales_invoice": "SI-LEGACY-RELEASED",
			"delivery_note": "DN-1",
			"credit_note": "",
			"refund_payment_entry": "",
			"legacy_consumed": 0,
			"needs_reconciliation": 0,
		},
		{
			"name": "RES-8",
			"company": "Other Co",
			"state": "Confirmed",
			"sales_invoice": "",
			"delivery_note": "",
			"credit_note": "",
			"refund_payment_entry": "",
			"legacy_consumed": 0,
			"needs_reconciliation": 0,
		},
	]
	items = [
		{
			"parent": "RES-1",
			"item_code": "ITEM-A",
			"item_name": "Item A",
			"qty": 2,
			"is_reservation_asset": 0,
		},
		{
			"parent": "RES-1",
			"item_code": "ITEM-ASSET",
			"item_name": "Cart Asset",
			"qty": 1,
			"is_reservation_asset": 1,
		},
		{
			"parent": "RES-2",
			"item_code": "ITEM-B",
			"item_name": "Item B",
			"qty": 5,
			"is_reservation_asset": 0,
		},
		{
			"parent": "RES-3",
			"item_code": "ITEM-C",
			"item_name": "Item C",
			"qty": 6,
			"is_reservation_asset": 0,
		},
		{
			"parent": "RES-4",
			"item_code": "ITEM-D",
			"item_name": "Item D",
			"qty": 7,
			"is_reservation_asset": 0,
		},
		{
			"parent": "RES-5",
			"item_code": "ITEM-A",
			"item_name": "Item A",
			"qty": 4,
			"is_reservation_asset": 0,
		},
		{
			"parent": "RES-6",
			"item_code": "ITEM-E",
			"item_name": "Item E",
			"qty": 8,
			"is_reservation_asset": 0,
		},
		{
			"parent": "RES-7",
			"item_code": "ITEM-F",
			"item_name": "Item F",
			"qty": 9,
			"is_reservation_asset": 0,
		},
		{
			"parent": "RES-8",
			"item_code": "ITEM-A",
			"item_name": "Item A",
			"qty": 11,
			"is_reservation_asset": 0,
		},
	]

	def fake_get_all(doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs):
		if doctype == "Event Cart Reservation":
			assert filters in (
				{"event_date": "2026-07-07", "company": "Tenant Co"},
				{"event_date": "2026-07-07", "company": "Other Co"},
			)
			company = filters["company"]
			return [reservation for reservation in reservations if reservation["company"] == company]
		if doctype == "Event Cart Reservation Item":
			parents = filters.get("parent", [])
			assert parents in (
				["RES-1", "RES-2", "RES-3", "RES-4", "RES-5", "RES-6", "RES-7"],
				["RES-8"],
			)
			return [item for item in items if item["parent"] in parents]
		if doctype == "Delivery Note":
			assert filters == {"name": ["in", ["DN-1"]]}
			assert fields == ["name", "docstatus"]
			return [{"name": "DN-1", "docstatus": 1}]
		raise AssertionError(f"Unexpected get_all lookup: {doctype}")

	def fake_get_cached_doc(doctype, name=None):
		if doctype == "Sales Invoice" and name == "SI-NORMAL":
			return _FakeInvoice(0)
		if doctype == "Sales Invoice" and name == "SI-LEGACY":
			return _FakeInvoice(1)
		if doctype == "Sales Invoice" and name == "SI-LEGACY-RELEASED":
			return _FakeInvoice(1)
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(
		"paletixa_saas.paletixa_saas.event_reservation_service.get_platform_company_name",
		lambda: "Tenant Co",
	)
	monkeypatch.setattr(frappe, "get_all", fake_get_all)
	monkeypatch.setattr(frappe, "get_cached_doc", fake_get_cached_doc)

	default_result = get_event_reservation_production_demand("2026-07-07")
	other_company_result = get_event_reservation_production_demand("2026-07-07", company="Other Co")

	assert default_result == {
		"date": "2026-07-07",
		"items": [{"item_code": "ITEM-A", "item_name": "Item A", "qty": 2.0}],
	}
	assert other_company_result == {
		"date": "2026-07-07",
		"items": [{"item_code": "ITEM-A", "item_name": "Item A", "qty": 11.0}],
	}
