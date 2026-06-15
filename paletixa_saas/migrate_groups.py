import frappe

def run():
    print("Iniciando migración de grupos de artículos...")
    groups = ["Bolis", "Paletas", "Trompitos", "Eskimales", "Nieves"]
    for g in groups:
        if not frappe.db.exists("Item Group", g):
            doc = frappe.new_doc("Item Group")
            doc.item_group_name = g
            doc.parent_item_group = "Products"
            doc.is_group = 0
            doc.insert(ignore_permissions=True)
            print(f"Grupo creado: {g}")
        else:
            print(f"El grupo {g} ya existe.")

    # Reasignar ítems
    items = frappe.get_all("Item", filters={"item_group": "Products"})
    print(f"Total de ítems en Products a clasificar: {len(items)}")
    
    for item_doc in items:
        doc = frappe.get_doc("Item", item_doc.name)
        name_lower = (doc.item_name or "").lower()
        
        target_group = None
        if name_lower.startswith("bolis") or "saborines" in name_lower:
            target_group = "Bolis"
        elif name_lower.startswith("paleta"):
            target_group = "Paletas"
        elif name_lower.startswith("trompito"):
            target_group = "Trompitos"
        elif "eskimo" in name_lower:
            target_group = "Eskimales"
        elif name_lower.startswith("nieve"):
            target_group = "Nieves"
            
        if target_group:
            doc.item_group = target_group
            doc.save(ignore_permissions=True)
            print(f"Reasignado {doc.name} -> {target_group}")
            
    frappe.db.commit()
    print("Migración de base de datos finalizada.")
