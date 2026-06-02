import frappe

def run():
    doc = frappe.get_single("SaaS Feature Config")
    doc.has_pos = 1
    doc.has_production = 1
    doc.has_logistics = 0
    doc.primary_color = "#3498db"
    doc.save()
    frappe.db.commit()
    print("SaaS Feature Config updated successfully!")

if __name__ == '__main__':
    run()
