import frappe
import traceback
from paletixa_saas.paletixa_saas.api import close_pos_shift, create_pos_opening

def run():
    try:
        frappe.set_user("admin@lapaletixa.com")
        
        # 1. Create a new POS Opening Entry
        pos_profile = "Punto de Venta - Sucursal 4"
        company = "La Paletixa"
        balance_details = [
            {"mode_of_payment": "Cash", "opening_amount": 1000.0},
            {"mode_of_payment": "Credit Card", "opening_amount": 1000.0}
        ]
        
        print("Creating POS Opening Entry...")
        res_open = create_pos_opening(pos_profile, company, balance_details)
        opening_entry = res_open["name"]
        print(f"Created and opened: {opening_entry}")
        
        # Get details
        ope_doc = frappe.get_doc("POS Opening Entry", opening_entry)
        closing_details = [
            {"mode_of_payment": "Cash", "closing_amount": 1000.0},
            {"mode_of_payment": "Credit Card", "closing_amount": 1000.0}
        ]
        
        # 2. Try closing it
        print("Closing POS Opening Entry...")
        res_close = close_pos_shift(opening_entry, closing_details)
        print(f"SUCCESS! Closed successfully: {res_close}")
        
    except Exception as e:
        print("ERROR OCCURRED:")
        traceback.print_exc()

if __name__ == "__main__":
    run()
