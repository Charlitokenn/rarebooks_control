"""Data for the desk UI tests of the Books Bulk Import form (spec 0025), made with `bench execute`.

`playwright/tests/bulk_import_form.spec.js` calls `prepare` once and reads the JSON it returns. The control app
turns on two factor auth for every role (`install.enable_two_factor_auth`), so the spec logs in with the
password and a TOTP code from the OTP secret returned here, through the real login. The site's columns are put
in the control site's cache, so the form never calls a customer site; the spec stubs `check` and `refresh`.
"""

import json
import secrets
import uuid

import frappe
from frappe.twofactor import get_otpsecret_for_, set_default
from frappe.utils.password import update_password

from rarebooks_control import bulk_import
from rarebooks_control.tests.data_model import BILLING_MANAGER, make_role_users, make_site
from rarebooks_control.tests.test_bulk_import import TYPES

DOCTYPE = "Books Bulk Import"
ITEM = "Books Item"


def prepare() -> dict:
	"""A Billing Manager who can log in, one eligible site, a Draft import with one wrong cell and an Importing
	import. Refused unless the site allows tests, since it sets a known password."""
	if not frappe.conf.allow_tests:
		frappe.throw("ui_setup.prepare runs only on a site with allow_tests set.")
	frappe.set_user("Administrator")
	make_role_users()
	password = secrets.token_urlsafe(16)
	update_password(BILLING_MANAGER, password)
	# OTP App sends a setup email on a user's first 2FA login; marking it done makes the login ask for TOTP only.
	set_default(f"{BILLING_MANAGER}_otplogin", 1)

	tag = secrets.token_hex(4)
	site = make_site(f"ui-{tag}.example.com", subdomain=f"ui{tag}", company_name=f"UI Traders {tag}").name
	frappe.cache.set_value(
		f"books_bulk_import_columns:{site}",
		{"types": TYPES, "date_format": "dd-mm-yyyy", "number_format": "#,###.##"},
		expires_in_sec=3600,
	)

	good, bad = str(uuid.uuid4()), str(uuid.uuid4())
	draft = _import(
		site,
		[
			{"id": good, "cells": {"name": f"UI Pen {tag}", "rate": "2", "item_usage": "Sales"}},
			{"id": bad, "cells": {"name": f"UI Ink {tag}", "rate": "3", "item_usage": "Bogus"}},
		],
	)
	importing = _import(site, [{"id": str(uuid.uuid4()), "cells": {"name": f"UI Cap {tag}"}}])
	now = frappe.utils.now_datetime()
	frappe.db.set_value(
		DOCTYPE,
		importing,
		{
			"status": "Importing",
			"attempt": 1,
			"site_import": bulk_import.site_import_name(importing, 1),
			"sent_on": now,
			"last_progress_on": now,
		},
	)
	frappe.db.commit()
	return {
		"user": BILLING_MANAGER,
		"password": password,
		"otp_secret": get_otpsecret_for_(BILLING_MANAGER),
		"site": site,
		"subdomain": f"ui{tag}",
		"draft": draft,
		"bad_row": bad,
		"importing": importing,
	}


def _import(site: str, rows: list[dict]) -> str:
	doc = frappe.get_doc(
		{"doctype": DOCTYPE, "hosted_site": site, "import_type": ITEM, "rows": json.dumps(rows)}
	)
	return doc.insert().name
