"""Test helpers for the data model: users with one role each, and registry rows (specs 0008, 0010)."""

import secrets

import frappe

BILLING_MANAGER = "billing-manager@example.com"
REMOVAL_MANAGER = "removal-manager@example.com"
SYSTEM_MANAGER = "system-manager@example.com"
ROLE_USERS = {
	"Billing Manager": BILLING_MANAGER,
	"Removal Manager": REMOVAL_MANAGER,
	"System Manager": SYSTEM_MANAGER,
}


def make_role_users():
	"""One desk user per role, with no other role."""
	for role, email in ROLE_USERS.items():
		if frappe.db.exists("User", email):
			continue
		user = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": role, "send_welcome_email": 0}
		)
		user.append("roles", {"role": role})
		user.insert(ignore_permissions=True)


def make_site(site: str, secret: str | None = None, **values):
	"""A registry row whose config a verified ping has made Active (spec 0010 AC-11), unless `values` say
	otherwise."""
	doc = frappe.get_doc(
		{
			"doctype": "Books Hosted Site",
			"site": site,
			"site_url": f"https://{site}",
			"config_status": "Active",
			"owner_email": "owner@example.com",
			"control_secret": secrets.token_hex(32) if secret is None else secret,
			**values,
		}
	)
	return doc.insert(ignore_permissions=True)


def make_outgoing_email_account():
	"""A default outgoing Email Account, so `frappe.sendmail` queues for real. Tests never send the queue."""
	name = "Removal Test Outgoing"
	if frappe.db.exists("Email Account", name):
		return
	doc = frappe.get_doc(
		{
			"doctype": "Email Account",
			"email_account_name": name,
			"email_id": "control@example.com",
			"enable_outgoing": 1,
			"default_outgoing": 1,
			"smtp_server": "localhost",
			"no_smtp_authentication": 1,
		}
	)
	# Its validate would try to reach the SMTP server.
	doc.flags.ignore_validate = True
	doc.insert(ignore_permissions=True)
