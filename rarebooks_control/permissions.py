import frappe

MODULE = "Rarebooks Control"


def has_app_permission() -> bool:
	"""Show and open the control app for users who can read one of its doctypes: the Billing, Removal and
	System Managers. Anyone else never sees its apps screen tile or its rail."""
	readable = frappe.get_user().get_can_read()
	return bool(frappe.db.exists("DocType", {"module": MODULE, "name": ("in", readable)}))
