from importlib import import_module
from types import SimpleNamespace

import frappe


def test_backfill_patch_import_path_matches_frappe_expectations():
	module = import_module("paletixa_saas.patches.backfill_event_cart_reservations")
	assert callable(module.execute)


backfill_module = import_module("paletixa_saas.patches.backfill_event_cart_reservations")
backfill_event_cart_reservations = backfill_module.backfill_event_cart_reservations


class _FakeConfig:
	def __init__(self, values):
		self._values = values

	def get(self, key, default=None):
		return self._values.get(key, default)


class _FakeInvoice:
	def __init__(self, name, update_stock):
		self.name = name
		self.update_stock = update_stock
		self.docstatus = 1

	def get(self, key, default=None):
		return getattr(self, key, default)


class _FakeItem:
	def __init__(self, **values):
		self.__dict__.update(values)

	def get(self, key, default=None):
		return getattr(self, key, default)


class _FakeReservation:
	def __init__(self):
		self.doctype = "Event Cart Reservation"
		self.name = "SO-LEGACY-1"
		self.sales_order = "SO-LEGACY-1"
		self.customer = "Customer A"
		self.event_date = "2026-07-07"
		self.company = "Tenant Co"
		self.reservation_item_code = "Carrito Paletero"
		self.state = "Pending Confirmation"
		self.legacy_consumed = 0
		self.needs_reconciliation = 0
		self.reconciliation_notes = ""
		self.assigned_cart_warehouse = ""
		self.delivery_note = ""
		self.items = []
		self.inserted = False
		self.saved = False

	def append(self, table, row):
		assert table == "items"
		self.items.append(SimpleNamespace(**row))

	def insert(self, ignore_permissions=False):
		self.inserted = True
		return self

	def save(self, ignore_permissions=False):
		self.saved = True
		return self

	def get(self, key, default=None):
		return getattr(self, key, default)


def test_backfill_creates_pending_rows_and_classifies_legacy_invoices(monkeypatch):
	so = SimpleNamespace(
		name="SO-LEGACY-1",
		customer="Customer A",
		delivery_date="2026-07-07",
		company="Tenant Co",
		grand_total=120.0,
		base_grand_total=120.0,
		advance_paid=0.0,
		outstanding_amount=120.0,
		items=[
			_FakeItem(
				name="SOI-1",
				item_code="Carrito Paletero",
				item_name="Carrito Paletero",
				qty=1,
				rate=0.0,
				amount=0.0,
			),
			_FakeItem(name="SOI-2", item_code="ITEM-A", item_name="Item A", qty=2, rate=60.0, amount=120.0),
		],
	)
	new_reservation = _FakeReservation()
	created_docs = []
	conflict_logs = []

	def _fake_sql(query, params=None, **kwargs):
		if "FROM `tabSales Order`" in query:
			return [{"name": "SO-LEGACY-1"}]
		raise AssertionError(f"Unexpected SQL: {query}")

	def _fake_get_all(doctype, filters=None, fields=None, order_by=None, **kwargs):
		if doctype == "Sales Invoice Item" and filters == {"sales_order": "SO-LEGACY-1"}:
			return [{"parent": "SI-LEGACY-1"}]
		if doctype == "Delivery Note Item":
			return []
		raise AssertionError(f"Unexpected get_all lookup: {doctype} / {filters}")

	def _fake_get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig({"reservation_item_code": "Carrito Paletero"})
		if doctype == "Sales Invoice" and name == "SI-LEGACY-1":
			return _FakeInvoice(name, 1)
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	def _fake_new_doc(doctype, *args, **kwargs):
		assert doctype == "Event Cart Reservation"
		created_docs.append(doctype)
		return new_reservation

	monkeypatch.setattr(frappe.db, "sql", _fake_sql)
	monkeypatch.setattr(frappe, "get_all", _fake_get_all)
	monkeypatch.setattr(frappe, "get_cached_doc", _fake_get_cached_doc)
	monkeypatch.setattr(
		frappe.db,
		"exists",
		lambda doctype, name=None, *args, **kwargs: doctype == "Item" and name == "Carrito Paletero",
	)
	monkeypatch.setattr(frappe, "new_doc", _fake_new_doc)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, name=None, *args, **kwargs: so
		if doctype == "Sales Order"
		else (_ for _ in ()).throw(AssertionError(f"Unexpected doc lookup: {doctype} / {name}")),
	)
	monkeypatch.setattr(frappe, "log_error", lambda *args, **kwargs: conflict_logs.append((args, kwargs)))

	report = backfill_event_cart_reservations()

	assert report["created"] == 1
	assert report["legacy_classified"] == 1
	assert report["conflicts"] == []
	assert created_docs == ["Event Cart Reservation"]
	assert new_reservation.inserted is True
	assert new_reservation.state == "Released"
	assert new_reservation.legacy_consumed == 1
	assert new_reservation.needs_reconciliation == 1
	assert new_reservation.items[-1].item_code == "ITEM-A"
	assert conflict_logs == []


def test_get_linked_delivery_note_names_only_returns_submitted_notes(monkeypatch):
	def _fake_get_all(doctype, filters=None, fields=None, order_by=None, **kwargs):
		assert doctype == "Delivery Note Item"
		assert filters == {"against_sales_order": "SO-TEST-1", "docstatus": 1}
		assert fields == ["parent"]
		assert order_by == "creation asc"
		return [{"parent": "DN-SUBMITTED-1"}, {"parent": ""}, {}]

	monkeypatch.setattr(frappe, "get_all", _fake_get_all)

	assert backfill_module._get_linked_delivery_note_names("SO-TEST-1") == ["DN-SUBMITTED-1"]


def test_classify_existing_reservation_uses_only_submitted_delivery_notes(monkeypatch):
	class _FakeDeliveryNote:
		def __init__(self, docstatus):
			self.docstatus = docstatus

		def get(self, key, default=None):
			return getattr(self, key, default)

	def fake_get_cached_doc(doctype, name=None):
		if doctype == "Sales Invoice" and name == "SI-LEGACY-1":
			return _FakeInvoice(name, 1)
		if doctype == "Delivery Note" and name == "DN-SUBMITTED-1":
			return _FakeDeliveryNote(1)
		if doctype == "Delivery Note" and name == "DN-DRAFT-1":
			return _FakeDeliveryNote(0)
		if doctype == "Delivery Note" and name == "DN-CANCELLED-1":
			return _FakeDeliveryNote(2)
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	monkeypatch.setattr(frappe, "get_cached_doc", fake_get_cached_doc)

	submitted = _FakeReservation()
	submitted.sales_invoice = "SI-LEGACY-1"
	submitted.delivery_note = "DN-SUBMITTED-1"
	submitted_classification, submitted_changed = backfill_module._classify_existing_reservation(submitted)

	draft = _FakeReservation()
	draft.sales_invoice = "SI-LEGACY-1"
	draft.delivery_note = "DN-DRAFT-1"
	draft_classification, draft_changed = backfill_module._classify_existing_reservation(draft)

	cancelled = _FakeReservation()
	cancelled.sales_invoice = "SI-LEGACY-1"
	cancelled.delivery_note = "DN-CANCELLED-1"
	cancelled_classification, cancelled_changed = backfill_module._classify_existing_reservation(cancelled)

	missing = _FakeReservation()
	missing.sales_invoice = "SI-LEGACY-1"
	missing.delivery_note = ""
	missing_classification, missing_changed = backfill_module._classify_existing_reservation(missing)

	assert submitted_classification == "legacy_released"
	assert submitted_changed is True
	assert submitted.state == "Released"
	assert draft_classification is None
	assert draft_changed is False
	assert draft.state == "Pending Confirmation"
	assert cancelled_classification is None
	assert cancelled_changed is False
	assert cancelled.state == "Pending Confirmation"
	assert missing_classification == "legacy_consumed"
	assert missing_changed is True
	assert missing.state == "Released"


def test_backfill_ignores_draft_and_cancelled_delivery_notes_when_releasing(monkeypatch):
	so = SimpleNamespace(
		name="SO-DN-DOCSTATUS-1",
		customer="Customer A",
		delivery_date="2026-07-10",
		company="Tenant Co",
		grand_total=90.0,
		base_grand_total=90.0,
		advance_paid=0.0,
		outstanding_amount=90.0,
		items=[
			_FakeItem(
				name="SOI-1",
				item_code="Carrito Paletero",
				item_name="Carrito Paletero",
				qty=1,
				rate=0.0,
				amount=0.0,
			),
			_FakeItem(name="SOI-2", item_code="ITEM-D", item_name="Item D", qty=1, rate=90.0, amount=90.0),
		],
	)
	new_reservation = _FakeReservation()
	conflict_logs = []
	get_all_calls = []

	def _fake_sql(query, params=None, **kwargs):
		if "FROM `tabSales Order`" in query:
			return [{"name": "SO-DN-DOCSTATUS-1"}]
		raise AssertionError(f"Unexpected SQL: {query}")

	def _fake_get_all(doctype, filters=None, fields=None, order_by=None, **kwargs):
		get_all_calls.append((doctype, filters, fields, order_by))
		if doctype == "Sales Invoice Item" and filters == {"sales_order": "SO-DN-DOCSTATUS-1"}:
			return []
		if doctype == "Delivery Note Item" and filters == {
			"against_sales_order": "SO-DN-DOCSTATUS-1",
			"docstatus": 1,
		}:
			return []
		if doctype == "Delivery Note Item" and filters == {"against_sales_order": "SO-DN-DOCSTATUS-1"}:
			return [{"parent": "DN-DRAFT-1"}, {"parent": "DN-CANCELLED-1"}]
		if doctype == "Delivery Note Item" and filters == {"parent": "DN-DRAFT-1"}:
			return [{"warehouse": "Fabrica - TC"}]
		if doctype == "Delivery Note Item" and filters == {"parent": "DN-CANCELLED-1"}:
			return [{"warehouse": "Fabrica - TC"}]
		raise AssertionError(f"Unexpected get_all lookup: {doctype} / {filters}")

	def _fake_get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig({"reservation_item_code": "Carrito Paletero"})
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	def _fake_new_doc(doctype, *args, **kwargs):
		assert doctype == "Event Cart Reservation"
		return new_reservation

	monkeypatch.setattr(frappe.db, "sql", _fake_sql)
	monkeypatch.setattr(frappe, "get_all", _fake_get_all)
	monkeypatch.setattr(frappe, "get_cached_doc", _fake_get_cached_doc)
	monkeypatch.setattr(
		frappe.db,
		"exists",
		lambda doctype, name=None, *args, **kwargs: doctype == "Item" and name == "Carrito Paletero",
	)
	monkeypatch.setattr(frappe, "get_doc", lambda doctype, name=None, *args, **kwargs: so)
	monkeypatch.setattr(frappe, "new_doc", _fake_new_doc)
	monkeypatch.setattr(frappe, "log_error", lambda *args, **kwargs: conflict_logs.append((args, kwargs)))

	report = backfill_event_cart_reservations()

	assert report["created"] == 1
	assert report["conflicts"] == []
	assert new_reservation.state == "Pending Confirmation"
	assert new_reservation.assigned_cart_warehouse == ""
	assert new_reservation.delivery_note == ""
	assert get_all_calls[1][1] == {"against_sales_order": "SO-DN-DOCSTATUS-1", "docstatus": 1}
	assert conflict_logs == []


def test_backfill_reports_missing_company_and_over_capacity_conflicts(monkeypatch):
	valid_so = SimpleNamespace(
		name="SO-CONFLICT-1",
		customer="Customer A",
		delivery_date="2026-07-08",
		company="Tenant Co",
		grand_total=80.0,
		base_grand_total=80.0,
		advance_paid=0.0,
		outstanding_amount=80.0,
		items=[
			_FakeItem(
				name="SOI-1",
				item_code="Carrito Paletero",
				item_name="Carrito Paletero",
				qty=1,
				rate=0.0,
				amount=0.0,
			),
			_FakeItem(name="SOI-2", item_code="ITEM-B", item_name="Item B", qty=1, rate=80.0, amount=80.0),
		],
	)
	missing_company_so = SimpleNamespace(
		name="SO-MISSING-COMPANY",
		customer="Customer B",
		delivery_date="2026-07-08",
		company="",
		grand_total=40.0,
		base_grand_total=40.0,
		advance_paid=0.0,
		outstanding_amount=40.0,
		items=[
			_FakeItem(
				name="SOI-3",
				item_code="Carrito Paletero",
				item_name="Carrito Paletero",
				qty=1,
				rate=0.0,
				amount=0.0,
			)
		],
	)
	conflict_logs = []

	class _OverCapacityReservation(_FakeReservation):
		def insert(self, ignore_permissions=False):
			raise frappe.ValidationError("The selected date has reached its reservation capacity.")

	def _fake_sql(query, params=None, **kwargs):
		if "FROM `tabSales Order`" in query:
			return [{"name": "SO-CONFLICT-1"}, {"name": "SO-MISSING-COMPANY"}]
		raise AssertionError(f"Unexpected SQL: {query}")

	def _fake_get_all(doctype, filters=None, fields=None, order_by=None, **kwargs):
		if doctype in {"Sales Invoice Item", "Delivery Note Item"}:
			return []
		raise AssertionError(f"Unexpected get_all lookup: {doctype} / {filters}")

	def _fake_get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig({"reservation_item_code": "Carrito Paletero"})
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	def _fake_new_doc(doctype, *args, **kwargs):
		assert doctype == "Event Cart Reservation"
		return _OverCapacityReservation()

	monkeypatch.setattr(frappe.db, "sql", _fake_sql)
	monkeypatch.setattr(frappe, "get_all", _fake_get_all)
	monkeypatch.setattr(frappe, "get_cached_doc", _fake_get_cached_doc)
	monkeypatch.setattr(
		frappe,
		"get_doc",
		lambda doctype, name=None, *args, **kwargs: valid_so
		if doctype == "Sales Order" and name == "SO-CONFLICT-1"
		else missing_company_so,
	)
	monkeypatch.setattr(frappe, "new_doc", _fake_new_doc)
	monkeypatch.setattr(
		frappe.db,
		"exists",
		lambda doctype, name=None, *args, **kwargs: doctype == "Item" and name == "Carrito Paletero",
	)
	monkeypatch.setattr(frappe, "log_error", lambda *args, **kwargs: conflict_logs.append((args, kwargs)))

	report = backfill_event_cart_reservations()

	assert report["created"] == 0
	assert report["updated"] == 0
	assert {conflict["reason"] for conflict in report["conflicts"]} == {"over_capacity", "missing_company"}
	assert conflict_logs
	assert conflict_logs[0][1]["title"] == "Event Cart Reservation Backfill Conflicts"


def test_backfill_reports_missing_warehouse_conflict_and_stays_idempotent_on_rerun(monkeypatch):
	so = SimpleNamespace(
		name="SO-CONFIRMED-1",
		customer="Customer A",
		delivery_date="2026-07-09",
		company="Tenant Co",
		grand_total=150.0,
		base_grand_total=150.0,
		advance_paid=0.0,
		outstanding_amount=150.0,
		items=[
			_FakeItem(
				name="SOI-1",
				item_code="Carrito Paletero",
				item_name="Carrito Paletero",
				qty=1,
				rate=0.0,
				amount=0.0,
			),
			_FakeItem(name="SOI-2", item_code="ITEM-C", item_name="Item C", qty=3, rate=50.0, amount=150.0),
		],
	)
	conflict_logs = []
	new_doc_calls = []

	def _fake_sql(query, params=None, **kwargs):
		if "FROM `tabSales Order`" in query:
			return [{"name": "SO-CONFIRMED-1"}]
		raise AssertionError(f"Unexpected SQL: {query}")

	def _fake_get_all(doctype, filters=None, fields=None, order_by=None, **kwargs):
		if doctype == "Sales Invoice Item" and filters == {"sales_order": "SO-CONFIRMED-1"}:
			return [{"parent": "SI-CONFIRMED-1"}]
		if doctype == "Delivery Note Item":
			return []
		raise AssertionError(f"Unexpected get_all lookup: {doctype} / {filters}")

	def _fake_get_cached_doc(doctype, name=None):
		if doctype == "SaaS Feature Config":
			return _FakeConfig({"reservation_item_code": "Carrito Paletero"})
		if doctype == "Sales Invoice" and name == "SI-CONFIRMED-1":
			return _FakeInvoice(name, 0)
		raise AssertionError(f"Unexpected cached doc lookup: {doctype} / {name}")

	def _fake_new_doc(doctype, *args, **kwargs):
		assert doctype == "Event Cart Reservation"
		new_doc_calls.append(doctype)
		raise AssertionError("Reservation should not be inserted when confirmation warehouse is missing.")

	monkeypatch.setattr(frappe.db, "sql", _fake_sql)
	monkeypatch.setattr(frappe, "get_all", _fake_get_all)
	monkeypatch.setattr(frappe, "get_cached_doc", _fake_get_cached_doc)
	monkeypatch.setattr(
		frappe.db,
		"exists",
		lambda doctype, name=None, *args, **kwargs: doctype == "Item" and name == "Carrito Paletero",
	)
	monkeypatch.setattr(frappe, "get_doc", lambda doctype, name=None, *args, **kwargs: so)
	monkeypatch.setattr(frappe, "new_doc", _fake_new_doc)
	monkeypatch.setattr(frappe, "log_error", lambda *args, **kwargs: conflict_logs.append((args, kwargs)))

	first_report = backfill_event_cart_reservations()
	second_report = backfill_event_cart_reservations()

	assert first_report["created"] == 0
	assert second_report["created"] == 0
	assert first_report["conflicts"] == second_report["conflicts"]
	assert first_report["conflicts"][0]["reason"] == "missing_assigned_warehouse"
	assert new_doc_calls == []
	assert len(conflict_logs) == 2
