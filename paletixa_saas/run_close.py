import frappe
import traceback
from paletixa_saas.paletixa_saas.api import close_pos_shift

def run():
    try:
        # Set session user to cajero.s1.t1@lapaletixa.com
        frappe.set_user("admin@lapaletixa.com")
        
        # Let's inspect POS-OPE-2026-00003
        opening_entry = "POS-OPE-2026-00003"
        
        # We need to simulate the arguments
        ope = frappe.get_doc("POS Opening Entry", opening_entry)
        closing_details = []
        for d in ope.balance_details:
            closing_details.append({
                "mode_of_payment": d.mode_of_payment,
                "closing_amount": d.opening_amount
            })
            
        print(f"Closing details: {closing_details}")
        
        # Now run close_pos_shift
        frappe.db.begin()
        res = close_pos_shift(opening_entry, closing_details)
        frappe.db.commit()
        print(f"Success! Result: {res}")
    except Exception as e:
        frappe.db.rollback()
        print("ERROR OCCURRED:")
        traceback.print_exc()
