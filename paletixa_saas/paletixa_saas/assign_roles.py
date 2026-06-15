import frappe


def run():
	cashiers = frappe.get_all("User", filters={"name": ["like", "cajero.%"]})
	for c in cashiers:
		user_email = c.name
		# Add Accounts User role
		user_doc = frappe.get_doc("User", user_email)
		has_accounts_user = any(r.role == "Accounts User" for r in user_doc.roles)
		if not has_accounts_user:
			user_doc.append("roles", {"role": "Accounts User"})
			user_doc.save(ignore_permissions=True)
			print(f"Added Accounts User role to {user_email}")

	frappe.db.commit()


if __name__ == "__main__":
	run()
