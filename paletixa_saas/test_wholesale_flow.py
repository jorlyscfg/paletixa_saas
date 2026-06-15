import json

import frappe

from paletixa_saas.config.platform_defaults import (
	get_platform_company_abbr,
	get_platform_company_name,
	get_platform_distribution_warehouse,
)


def run_tests():
	print(
		"🚀 Iniciando pruebas unitarias para el flujo de Venta Mayorista, Portal de Auto-Servicio y Reservas de Eventos..."
	)

	# Asegurar que estamos corriendo bajo el contexto del sitio correcto
	if not getattr(frappe.local, "site", None):
		frappe.init(site="lapaletixa.localhost")
		frappe.connect()

	# Corregir script de servidor erróneo de reservas en la base de datos
	if frappe.db.exists("Server Script", "Validar Carritos Paleteros LP"):
		doc = frappe.get_doc("Server Script", "Validar Carritos Paleteros LP")
		changed = False
		if "frappe.get_single" in doc.script:
			print("🔧 Corrigiendo script de servidor 'Validar Carritos Paleteros LP' (frappe.get_single)...")
			doc.script = doc.script.replace("frappe.get_single", "frappe.get_doc")
			changed = True
		if "%%s" in doc.script:
			print("🔧 Corrigiendo placeholders '%%s' a '%s' en el script de servidor...")
			doc.script = doc.script.replace("%%s", "%s")
			changed = True

		if changed:
			doc.save(ignore_permissions=True)
			frappe.db.commit()

	# Simular inicio de sesión como Administrador
	frappe.session.user = "Administrator"

	# 1. Probar get_active_items_with_prices
	print("\n🔍 [Prueba 1] Probando API get_active_items_with_prices...")
	from paletixa_saas.paletixa_saas.api import get_active_items_with_prices

	items = get_active_items_with_prices()
	if not items:
		print("❌ Falla: No se encontraron artículos activos en el catálogo.")
		return

	print(f"✅ Éxito: Se encontraron {len(items)} artículos activos con precios.")
	test_item = items[0]
	print(
		f"   Artículo de prueba: '{test_item['name']}' | Precio Menudeo: ${test_item['retail_price']} | Precio Mayoreo: {test_item['wholesale_price']}"
	)

	# Preparar abastecimiento de stock
	test_item_code = test_item["name"]
	wh = get_platform_distribution_warehouse(allow_demo_fallback=True)

	print("   Abasteciendo inventario en bodega para asegurar stock físico...")
	se = frappe.new_doc("Stock Entry")
	se.purpose = "Material Receipt"
	se.stock_entry_type = "Material Receipt"
	se.company = get_platform_company_name(allow_demo_fallback=True)
	se.append("items", {"item_code": test_item_code, "qty": 500.0, "t_warehouse": wh, "basic_rate": 10.0})
	se.insert(ignore_permissions=True)
	se.submit()

	# 2. Probar create_wholesale_sale (Venta Directa)
	print("\n✍️ [Prueba 2] Probando API create_wholesale_sale (Venta Directa)...")
	from paletixa_saas.paletixa_saas.api import create_wholesale_sale

	items_to_buy = [
		{
			"item_code": test_item_code,
			"qty": 10.0,
			"rate": test_item["wholesale_price"]
			if test_item["wholesale_price"]
			else test_item["retail_price"],
		}
	]

	stock_before = (
		frappe.db.get_value("Bin", {"item_code": test_item_code, "warehouse": wh}, "actual_qty") or 0.0
	)

	try:
		frappe.clear_cache()
		res = create_wholesale_sale(
			customer="Público General",
			items=items_to_buy,
			payment_amount=50.0,
			payment_mode="Cash",
			warehouse=wh,
		)

		if res.get("success"):
			print("✅ Éxito: Factura de Venta Mayorista directa creada con éxito.")
			print(
				f"   Factura: {res['sales_invoice']} | Total: ${res['grand_total']} | Pago: ${res['advance_paid']}"
			)

			# Verificar reducción de stock
			stock_after = (
				frappe.db.get_value("Bin", {"item_code": test_item_code, "warehouse": wh}, "actual_qty")
				or 0.0
			)
			if float(stock_after) == float(stock_before) - 10.0:
				print("✅ Éxito: Stock disminuido correctamente en 10 unidades de la sucursal de salida.")
			else:
				print(
					f"⚠️ Alerta: El stock no disminuyó como se esperaba. Antes: {stock_before}, Después: {stock_after}"
				)
		else:
			print("❌ Falla: La API directa retornó success=False.")
	except Exception as e:
		print(f"❌ Error en Prueba 2: {e!s}")

	# 3. Probar Flujo de Auto-Servicio del Cliente (Order -> Complete)
	print("\n📦 [Prueba 3] Probando Flujo de Auto-Servicio del Cliente (Sales Order -> complete)...")
	from paletixa_saas.paletixa_saas.api import (
		complete_wholesale_order,
		create_wholesale_order,
		get_customer_wholesale_profile,
		get_pending_wholesale_orders,
	)

	try:
		frappe.clear_cache()

		# 3a. Obtener perfil del cliente
		profile = get_customer_wholesale_profile()
		if not profile.get("success"):
			print(f"❌ Falla: No se pudo resolver perfil de cliente. Error: {profile.get('error')}")
			return

		customer_name = profile.get("customer")
		print(f"✅ Éxito: Perfil resuelto con cliente '{customer_name}' ({profile.get('customer_name')}).")

		# 3b. Crear pedido (Sales Order)
		print("   Creando pedido mayorista (Sales Order) por el cliente...")
		order_res = create_wholesale_order(
			items=[{"item_code": test_item_code, "qty": 20.0}],
			metodo_pago="Transferencia",
			metodo_entrega="Domicilio",
		)

		if not order_res.get("success"):
			print("❌ Falla: No se pudo crear el Sales Order de mayoreo.")
			return

		sales_order_id = order_res.get("sales_order")
		print(f"✅ Éxito: Sales Order '{sales_order_id}' registrado en estado Submitted.")
		print(f"   Monto del Pedido: ${order_res.get('grand_total')}")

		# 3c. Listar pedidos pendientes
		print("   Listando pedidos pendientes en la cola del administrador...")
		pending_orders = get_pending_wholesale_orders()
		order_names = [o.name for o in pending_orders]
		if sales_order_id in order_names:
			print(
				f"✅ Éxito: El pedido '{sales_order_id}' figura en la lista de pendientes del administrador."
			)
		else:
			print(f"❌ Falla: El pedido '{sales_order_id}' NO figura en la lista de pendientes.")
			return

		# 3d. Completar y facturar el pedido por el administrador
		print("   Completando y facturando el pedido desde el panel del administrador...")
		stock_before_completion = (
			frappe.db.get_value("Bin", {"item_code": test_item_code, "warehouse": wh}, "actual_qty") or 0.0
		)

		comp_res = complete_wholesale_order(
			sales_order_name=sales_order_id, register_payment=True, payment_mode="Cash", warehouse=wh
		)

		if comp_res.get("success"):
			print("✅ Éxito: Pedido completado y facturado correctamente por el admin.")
			print(
				f"   Factura: {comp_res['sales_invoice']} | Saldo Pendiente: ${comp_res['outstanding_amount']}"
			)

			# Verificar reducción de stock
			stock_after_completion = (
				frappe.db.get_value("Bin", {"item_code": test_item_code, "warehouse": wh}, "actual_qty")
				or 0.0
			)
			if float(stock_after_completion) == float(stock_before_completion) - 20.0:
				print(
					"✅ Éxito: El stock de Distribución disminuyó exactamente 20 unidades tras facturar el pedido."
				)
			else:
				print(
					f"❌ Falla: El stock no disminuyó adecuadamente. Antes: {stock_before_completion}, Después: {stock_after_completion}"
				)

			# Verificar outstanding
			if comp_res["outstanding_amount"] == 0.0:
				print("✅ Éxito: Saldo del pedido liquidado a $0.00 con la confirmación de pago.")
			else:
				print(f"❌ Falla: Saldo del pedido incorrecto: ${comp_res['outstanding_amount']}")
		else:
			print("❌ Falla: complete_wholesale_order falló sin lanzar excepción.")

		# 3e. Probar cancelación de pedidos (liberación de stock y preservación del historial)
		print("\n❌ Probando API cancel_wholesale_order...")
		from paletixa_saas.paletixa_saas.api import cancel_wholesale_order

		cancel_so_res = create_wholesale_order(
			items=[{"item_code": test_item_code, "qty": 15.0}],
			metodo_pago="Efectivo",
			metodo_entrega="Recoger",
		)
		cancel_so_id = cancel_so_res.get("sales_order")

		reserved_qty_before = (
			frappe.db.get_value("Bin", {"item_code": test_item_code, "warehouse": wh}, "reserved_qty") or 0.0
		)
		print(f"   Reserved Qty en '{wh}' antes de cancelar: {reserved_qty_before} unidades.")

		cancel_res = cancel_wholesale_order(sales_order_name=cancel_so_id)
		if cancel_res.get("success"):
			print("✅ Éxito: Pedido cancelado correctamente y el historial contable se conservó.")

			# Verificar que el Sales Order sigue existiendo pero quedó cancelado
			cancel_so_status = frappe.db.get_value("Sales Order", cancel_so_id, "docstatus")
			if cancel_so_status == 2:
				print("✅ Éxito: El documento Sales Order quedó cancelado (docstatus=2) y no fue borrado.")
			else:
				print("❌ Falla: El Sales Order no quedó en estado cancelado.")

			# Verificar que el Reserved Qty disminuyó exactamente en 15 unidades
			reserved_qty_after = (
				frappe.db.get_value("Bin", {"item_code": test_item_code, "warehouse": wh}, "reserved_qty")
				or 0.0
			)
			print(f"   Reserved Qty en '{wh}' después de cancelar: {reserved_qty_after} unidades.")
			if float(reserved_qty_after) == float(reserved_qty_before) - 15.0:
				print("✅ Éxito: El Reserved Qty disminuyó exactamente 15 unidades, liberando el stock.")
			else:
				print("❌ Falla: El stock reservado no se liberó correctamente.")
		else:
			print("❌ Falla: La API cancel_wholesale_order retornó success=False.")

	except Exception as e:
		print(f"❌ Error en Prueba 3: {e!s}")
		import traceback

		traceback.print_exc()

	# 4. Probar Módulo de Gestión de Reservas de Eventos (Event Bookings)
	print("\n🎉 [Prueba 4] Probando Módulo de Gestión de Reservas de Eventos...")
	from paletixa_saas.paletixa_saas.api import (
		cancel_event_booking,
		check_cart_availability,
		complete_event_booking,
		create_event_booking,
		get_event_warehouses,
		get_pending_event_bookings,
		update_saas_config,
	)

	try:
		frappe.clear_cache()

		# Limpiar físicamente reservas antiguas en la base de datos para asegurar disponibilidad
		print(
			"   Limpiando físicamente reservas antiguas en la base de datos para asegurar disponibilidad..."
		)

		# Obtener IDs de pedidos de carritos de eventos
		orders_to_delete = [
			o.parent
			for o in frappe.get_all(
				"Sales Order Item", filters={"item_code": "Carrito Paletero"}, fields=["parent"]
			)
		]

		if orders_to_delete:
			# Eliminar cobros de anticipos asociados
			payments = frappe.get_all(
				"Payment Entry Reference",
				filters={"reference_doctype": "Sales Order", "reference_name": ["in", orders_to_delete]},
				fields=["parent"],
			)
			payment_ids = [p.parent for p in payments]
			if payment_ids:
				frappe.db.sql(
					"DELETE FROM `tabPayment Entry Reference` WHERE parent IN (%s)"
					% ", ".join(["'%s'" % p for p in payment_ids])
				)
				frappe.db.sql(
					"DELETE FROM `tabPayment Entry` WHERE name IN (%s)"
					% ", ".join(["'%s'" % p for p in payment_ids])
				)

			# Eliminar entradas de libro de anticipos (Advance Payment Ledger Entry)
			frappe.db.sql(
				"DELETE FROM `tabAdvance Payment Ledger Entry` WHERE against_voucher_no IN (%s)"
				% ", ".join(["'%s'" % o for o in orders_to_delete])
			)

			# Eliminar ítems y cabecera del Sales Order
			frappe.db.sql(
				"DELETE FROM `tabSales Order Item` WHERE parent IN (%s)"
				% ", ".join(["'%s'" % o for o in orders_to_delete])
			)
			frappe.db.sql(
				"DELETE FROM `tabSales Order` WHERE name IN (%s)"
				% ", ".join(["'%s'" % o for o in orders_to_delete])
			)

		frappe.db.commit()

		# 4a. Habilitar módulo de reservas y carritos en SaaS Feature Config
		print("   Sincronizando configuración SaaS para habilitar reservas...")

		# Asegurar que Carrito Paletero exista y esté habilitado para evitar ValidationErrors
		if not frappe.db.exists("Item", "Carrito Paletero"):
			doc_item = frappe.new_doc("Item")
			doc_item.item_code = "Carrito Paletero"
			doc_item.item_name = "Carrito Paletero"
			doc_item.item_group = "Products"
			doc_item.stock_uom = "Unit"
			doc_item.disabled = 0
			doc_item.insert(ignore_permissions=True)
			frappe.db.commit()
		else:
			doc_item = frappe.get_doc("Item", "Carrito Paletero")
			if doc_item.disabled:
				doc_item.disabled = 0
				doc_item.save(ignore_permissions=True)
				frappe.db.commit()

		update_saas_config(
			has_reservations=1, max_reservation_assets=5, reservation_item_code="Carrito Paletero"
		)

		# 4b. Verificar disponibilidad inicial del Carrito
		booking_date = frappe.utils.add_days(frappe.utils.today(), 5)
		avail = check_cart_availability(booking_date)
		initial_available = avail.get("available_qty")
		print(f"✅ Éxito: Disponibilidad inicial para el {booking_date}: {initial_available} carritos.")

		# 4c. Crear una reserva de prueba (Event Booking)
		print("   Creando reserva de evento con anticipo de $500.00...")
		event_items = [{"item_code": test_item_code, "qty": 120.0, "rate": 14.0}]

		booking_res = create_event_booking(
			customer="Público General",
			delivery_date=booking_date,
			items=event_items,
			advance_amount=500.0,
			payment_mode="Cash",
			guest_name="Reserva Pruebas Evento",
			guest_phone="+525512345678",
		)

		if not booking_res.get("success"):
			print("❌ Falla: No se pudo registrar la reserva de evento.")
			return

		booking_id = booking_res.get("sales_order")
		print(f"✅ Éxito: Reserva de Evento '{booking_id}' creada correctamente.")
		print(f"   Anticipo registrado: ${booking_res.get('advance_paid')}")

		# Verificar reducción de disponibilidad del Carrito Paletero
		avail_after = check_cart_availability(booking_date)
		if avail_after.get("available_qty") == initial_available - 1:
			print("✅ Éxito: Disponibilidad de Carritos disminuyó a exactamente 1 unidad libre.")
		else:
			print(
				f"❌ Falla: La disponibilidad de carritos no cambió correctamente. Antes: {initial_available}, Después: {avail_after.get('available_qty')}"
			)

		# 4d. Listar reservas pendientes en el panel
		print("   Listando reservas de eventos pendientes para el administrador...")
		pending_bookings = get_pending_event_bookings()
		pending_names = [pb.name for pb in pending_bookings]
		if booking_id in pending_names:
			print(f"✅ Éxito: La reserva '{booking_id}' figura en la cola del nuevo panel administrativo.")
		else:
			print(f"❌ Falla: La reserva '{booking_id}' NO figura en el listado del panel.")
			return

		# 4e. Obtener almacenes de Carritos disponibles
		print("   Obteniendo almacenes de Carritos sincronizados para imputar el stock...")
		event_whs = get_event_warehouses()
		event_wh_names = [w["name"] for w in event_whs]
		print(f"✅ Éxito: Almacenes de Carritos resueltos: {event_wh_names}")

		company_abbr = get_platform_company_abbr(allow_demo_fallback=True)
		target_carrito_wh = "Carrito 1"
		# Asegurar que Carrito 1 exista y esté habilitado
		if not frappe.db.exists("Warehouse", f"Carrito 1 - {company_abbr}"):
			target_carrito_wh = get_platform_distribution_warehouse(allow_demo_fallback=True)
		else:
			target_carrito_wh = f"Carrito 1 - {company_abbr}"

		print(f"   Almacén seleccionado para el despacho: '{target_carrito_wh}'")

		# Abastecer de inventario el carrito/bodega seleccionado para que tenga stock suficiente
		print("   Abasteciendo de inventario el almacén seleccionado...")
		se_wh = frappe.new_doc("Stock Entry")
		se_wh.purpose = "Material Receipt"
		se_wh.stock_entry_type = "Material Receipt"
		se_wh.company = get_platform_company_name(allow_demo_fallback=True)
		se_wh.append(
			"items",
			{"item_code": test_item_code, "qty": 300.0, "t_warehouse": target_carrito_wh, "basic_rate": 10.0},
		)
		se_wh.insert(ignore_permissions=True)
		se_wh.submit()

		# 4f. Completar y facturar la reserva imputando stock al almacén seleccionado
		print("   Completando y facturando la reserva imputando stock...")
		stock_before_completion = (
			frappe.db.get_value(
				"Bin", {"item_code": test_item_code, "warehouse": target_carrito_wh}, "actual_qty"
			)
			or 0.0
		)

		completion_res = complete_event_booking(
			sales_order_name=booking_id,
			register_payment=True,
			payment_mode="Cash",
			warehouse=target_carrito_wh,
		)

		if completion_res.get("success"):
			print("✅ Éxito: Reserva completada y facturada correctamente por el admin.")
			print(
				f"   Factura: {completion_res['sales_invoice']} | Saldo Cobrado: ${completion_res['advance_paid']} | Outstanding: ${completion_res['outstanding_amount']}"
			)

			# Verificar reducción de stock en el almacén seleccionado
			stock_after_completion = (
				frappe.db.get_value(
					"Bin", {"item_code": test_item_code, "warehouse": target_carrito_wh}, "actual_qty"
				)
				or 0.0
			)
			if float(stock_after_completion) == float(stock_before_completion) - 120.0:
				print(
					f"✅ Éxito: El stock físico en '{target_carrito_wh}' disminuyó exactamente 120 unidades tras facturar el evento."
				)
			else:
				print(
					f"❌ Falla: El stock no disminuyó en el almacén elegido. Antes: {stock_before_completion}, Después: {stock_after_completion}"
				)
		else:
			print("❌ Falla: complete_event_booking falló sin lanzar excepciones.")

		# 4g. Probar la cancelación y liberación de reservas
		print("\n❌ Probando API cancel_event_booking...")
		cancel_booking_res = create_event_booking(
			customer="Público General",
			delivery_date=booking_date,
			items=event_items,
			advance_amount=100.0,
			payment_mode="Cash",
			guest_name="Reserva Cancelar Pruebas",
			guest_phone="+525598765432",
		)
		cancel_booking_id = cancel_booking_res.get("sales_order")

		avail_before_cancel = check_cart_availability(booking_date).get("available_qty")
		print(f"   Disponibilidad de Carritos antes de cancelar la reserva: {avail_before_cancel}")

		cancel_ev_res = cancel_event_booking(sales_order_name=cancel_booking_id)
		if cancel_ev_res.get("success"):
			print("✅ Éxito: Reserva cancelada correctamente y el historial contable se conservó.")

			cancel_booking_status = frappe.db.get_value("Sales Order", cancel_booking_id, "docstatus")
			if cancel_booking_status == 2:
				print(
					"✅ Éxito: El Sales Order de la reserva quedó cancelado (docstatus=2) y no fue borrado."
				)
			else:
				print("❌ Falla: El Sales Order de la reserva no quedó en estado cancelado.")

			avail_after_cancel = check_cart_availability(booking_date).get("available_qty")
			print(f"   Disponibilidad de Carritos después de cancelar: {avail_after_cancel}")
			if avail_after_cancel == avail_before_cancel + 1:
				print(
					"✅ Éxito: La disponibilidad del Carrito Paletero se incrementó correctamente, liberando la fecha."
				)
			else:
				print("❌ Falla: La fecha no liberó la disponibilidad del Carrito Paletero.")
		# 5. Probar Búsqueda Express y Historial de Clientes
		print("\n🔍 [Prueba 5] Probando Búsqueda Express y Historial de Clientes...")
		from paletixa_saas.paletixa_saas.api import (
			create_pos_customer,
			find_customer_by_name_or_phone,
			get_customer_orders_history,
		)

		# Crear un cliente de prueba temporal para buscarlo
		test_customer_name = "Cliente Buscador Express"
		test_customer_phone = "+525544332211"

		if not frappe.db.exists("Customer", test_customer_name):
			create_pos_customer(customer_name=test_customer_name, phone=test_customer_phone)
		else:
			frappe.db.set_value("Customer", test_customer_name, "mobile_no", test_customer_phone)
			frappe.db.commit()

		# 5a. Buscar por nombre exacto
		search_by_name = find_customer_by_name_or_phone(name=test_customer_name)
		if search_by_name.get("found") and search_by_name.get("phone") == test_customer_phone:
			print("✅ Éxito: Cliente encontrado correctamente por Nombre y se cargó su Teléfono.")
		else:
			print("❌ Falla: No se pudo encontrar el cliente por Nombre.")

		# 5b. Buscar por teléfono exacto
		search_by_phone = find_customer_by_name_or_phone(phone=test_customer_phone)
		if search_by_phone.get("found") and search_by_phone.get("customer_name") == test_customer_name:
			print("✅ Éxito: Cliente encontrado correctamente por Teléfono y se cargó su Nombre.")
		else:
			print("❌ Falla: No se pudo encontrar el cliente por Teléfono.")

		# 5c. Consultar historial de órdenes
		history = get_customer_orders_history(customer_name=test_customer_name)
		if "orders" in history and "invoices" in history:
			print("✅ Éxito: Se obtuvo correctamente el historial de pedidos y facturas del cliente.")
		else:
			print("❌ Falla: No se pudo obtener el historial del cliente.")

		# 6. Probar Validación de PIN y Normalización de Celulares
		print("\n🔒 [Prueba 6] Probando Validación de PIN y Normalización de Celulares...")
		from paletixa_saas.paletixa_saas.api import (
			generate_customer_access_pin,
			normalize_phone_number,
			validate_wholesale_access,
		)

		# 6a. Testear normalización de teléfonos
		t1 = normalize_phone_number("5544332211")
		t2 = normalize_phone_number("+52 55 4433 2211")
		t3 = normalize_phone_number("044 55 4433 2211")
		t4 = normalize_phone_number("+52-55-4433-2211")

		if (
			t1 == "+525544332211"
			and t2 == "+525544332211"
			and t3 == "+525544332211"
			and t4 == "+525544332211"
		):
			print("✅ Éxito: Teléfonos normalizados correctamente a formato E.164 (+525544332211).")
		else:
			print(f"❌ Falla: Error en normalización. Res: t1={t1}, t2={t2}, t3={t3}, t4={t4}")

		# 6b. Generar PIN y verificar
		pin_res = generate_customer_access_pin(test_customer_name)
		if pin_res.get("success") and len(pin_res.get("pin")) == 6:
			generated_pin = pin_res.get("pin")
			print(f"✅ Éxito: PIN de 6 dígitos generado correctamente para el cliente: {generated_pin}")
		else:
			print("❌ Falla: No se pudo generar el PIN del cliente.")
			generated_pin = None

		# 6c. Validar acceso exitoso
		if generated_pin:
			access_res = validate_wholesale_access(phone="55 4433 2211", pin=generated_pin)
			if access_res.get("success") and access_res.get("customer") == test_customer_name:
				print("✅ Éxito: Acceso validado correctamente usando celular (sin normalizar) y PIN.")
			else:
				print(f"❌ Falla: Error al validar acceso exitoso. Res: {access_res}")

			# 6d. Validar rechazo por PIN incorrecto
			access_fail = validate_wholesale_access(phone="55 4433 2211", pin="000000")
			if not access_fail.get("success") and "incorrecto" in access_fail.get("error").lower():
				print("✅ Éxito: El validador rechazó correctamente un PIN incorrecto.")
			else:
				print("❌ Falla: El validador aceptó un PIN incorrecto o devolvió un mensaje erróneo.")

		# 7. Probar Módulo de Notificaciones en Tiempo Real (SaaS Notification)
		print("\n🔔 [Prueba 7] Probando Módulo de Notificaciones en Tiempo Real...")
		from paletixa_saas.paletixa_saas.api import get_unread_notifications, mark_notification_as_read

		# Eliminar notificaciones previas de prueba para comenzar limpio
		frappe.db.sql("DELETE FROM `tabSaaS Notification`")
		frappe.db.commit()

		# 7a. Verificar conteo inicial (0)
		init_notif = get_unread_notifications()
		if init_notif.get("unread_count") == 0 and len(init_notif.get("notifications")) == 0:
			print("✅ Éxito: Conteo inicial de notificaciones es 0.")
		else:
			print(f"❌ Falla: Se encontraron notificaciones iniciales. Res: {init_notif}")

		# 7b. Crear una notificación de prueba directamente (simulando inserción de Sales Order)
		print("   Insertando Sales Order de prueba para disparar el hook...")
		so_test = frappe.new_doc("Sales Order")
		so_test.company = get_platform_company_name(allow_demo_fallback=True)
		so_test.customer = test_customer_name
		so_test.delivery_date = frappe.utils.add_days(frappe.utils.today(), 1)
		so_test.selling_price_list = "Standard Selling"
		so_test.custom_metodo_pago = "Transferencia"
		so_test.custom_metodo_entrega = "Domicilio"

		so_test.append(
			"items",
			{
				"item_code": test_item_code,
				"qty": 10.0,
				"rate": 15.0,
				"warehouse": get_platform_distribution_warehouse(allow_demo_fallback=True),
				"delivery_date": so_test.delivery_date,
			},
		)

		so_test.insert(ignore_permissions=True)
		so_test.submit()

		# 7c. Verificar si se disparó el hook y creó la notificación
		unread_after = get_unread_notifications()
		if unread_after.get("unread_count") == 1 and len(unread_after.get("notifications")) == 1:
			notif_item = unread_after.get("notifications")[0]
			print("✅ Éxito: La notificación se creó automáticamente vía hook tras insertar el Sales Order.")
			print(
				f"   Título: {notif_item['title']} | Mensaje: {notif_item['message']} | Referencia: {notif_item['reference_name']}"
			)

			# 7d. Marcar como leída y verificar que el conteo baje a 0
			read_res = mark_notification_as_read(notif_item["name"])
			if read_res.get("success"):
				unread_final = get_unread_notifications()
				if unread_final.get("unread_count") == 0 and len(unread_final.get("notifications")) == 0:
					print("✅ Éxito: La notificación se marcó como leída y el conteo de no leídos bajó a 0.")
				else:
					print(f"❌ Falla: El conteo no bajó a 0 tras marcar como leída. Res: {unread_final}")
			else:
				print("❌ Falla: mark_notification_as_read falló.")
		else:
			print(
				f"❌ Falla: El hook no creó la notificación al confirmar el Sales Order. Res: {unread_after}"
			)

	except Exception as e:
		print(f"❌ Error en Pruebas: {e!s}")
		import traceback

		traceback.print_exc()
	finally:
		frappe.db.rollback()
		frappe.destroy()


if __name__ == "__main__":
	run_tests()
