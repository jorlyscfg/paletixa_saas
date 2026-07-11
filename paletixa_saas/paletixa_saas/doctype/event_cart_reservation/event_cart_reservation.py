from typing import ClassVar

import frappe
from frappe import _
from frappe.model.document import Document

from paletixa_saas.paletixa_saas.event_reservation_service import (
	assert_active_allocation_available,
	assert_daily_capacity_available,
	build_active_allocation_key,
	validate_confirmed_allocation_warehouse,
)


class EventCartReservation(Document):
	IMMUTABLE_SERVICE_FIELDS: ClassVar[set[str]] = {
		"state",
		"assigned_cart_warehouse",
		"sales_invoice",
		"payment_entry",
		"credit_note",
		"refund_payment_entry",
		"delivery_note",
		"confirmed_at",
		"confirmed_by",
		"cancelled_at",
		"cancelled_by",
		"released_at",
		"released_by",
	}

	def validate(self):
		self.state = (self.get("state") or "Pending Confirmation").strip()
		self._reject_unauthorized_lifecycle_mutation()
		self._validate_state()
		self._validate_capacity_and_allocation()

	def _reject_unauthorized_lifecycle_mutation(self):
		if self.is_new() or self.flags.get("event_reservation_service_operation"):
			return

		before = self.get_doc_before_save()
		if not before:
			return

		changed_fields = [
			fieldname
			for fieldname in self.IMMUTABLE_SERVICE_FIELDS
			if self.get(fieldname) != before.get(fieldname)
		]
		if changed_fields:
			frappe.throw(
				_(
					"Reservation lifecycle and accounting links can only be changed through reservation services."
				),
				frappe.PermissionError,
			)

	def on_trash(self):
		frappe.throw(
			_("Event reservations are audit records and cannot be deleted."),
			frappe.PermissionError,
		)

	def _validate_state(self):
		allowed_states = {"Pending Confirmation", "Confirmed", "Cancelled", "Released"}
		if self.state not in allowed_states:
			frappe.throw(_("Unsupported reservation state {0}.").format(self.state), frappe.ValidationError)

	def _validate_capacity_and_allocation(self):
		exclude_name = self.get("name") or self.get("sales_order")
		capacity_slot = self.get("capacity_slot")

		if self.state == "Pending Confirmation":
			if self.get("assigned_cart_warehouse"):
				frappe.throw(
					_("Pending reservations must not assign a cart warehouse."),
					frappe.ValidationError,
				)
			self.capacity_slot, self.active_capacity_key = assert_daily_capacity_available(
				self.event_date,
				self.company,
				exclude_name=exclude_name,
				preferred_capacity_slot=capacity_slot,
			)
			self.active_allocation_key = None
			return

		if self.state == "Confirmed":
			if not self.get("assigned_cart_warehouse"):
				frappe.throw(
					_("Confirmed reservations require an assigned cart warehouse."),
					frappe.ValidationError,
				)

			self.assigned_cart_warehouse = validate_confirmed_allocation_warehouse(
				self.assigned_cart_warehouse,
				company_name=self.company,
			)
			self.capacity_slot, self.active_capacity_key = assert_daily_capacity_available(
				self.event_date,
				self.company,
				exclude_name=exclude_name,
				preferred_capacity_slot=capacity_slot,
			)
			self.active_allocation_key = build_active_allocation_key(
				self.event_date,
				self.assigned_cart_warehouse,
			)
			assert_active_allocation_available(
				self.event_date,
				self.assigned_cart_warehouse,
				exclude_name=exclude_name,
			)
			return

		self.active_capacity_key = None
		self.active_allocation_key = None
		if self.state in {"Cancelled", "Released"}:
			return

		frappe.throw(_("Unsupported reservation state {0}.").format(self.state), frappe.ValidationError)
