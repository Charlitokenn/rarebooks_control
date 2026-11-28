"""Spec 0008 data model: who can read and write what (AC-14, AC-18, AC-20), and secrets that never show."""

import secrets

import frappe
from frappe.tests import IntegrationTestCase

from rarebooks_control.tests.data_model import (
	BILLING_MANAGER,
	REMOVAL_MANAGER,
	ROLE_USERS,
	SYSTEM_MANAGER,
	make_role_users,
	make_site,
)

BILLING_DOCTYPES = ("Books Hosted Site", "Books Plan", "Books Checkout", "Books Payment Event")
BILLING_FIELDS = (
	"paid_until",
	"provider",
	"billing_period",
	"auto_renews",
	"provider_reference",
	"exempt",
	"allow_test_plans",
)


class IntegrationTestBooksHostedSite(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_role_users()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_every_role_reads_the_billing_doctypes(self):
		# covers: AC-20
		for user in ROLE_USERS.values():
			for doctype in BILLING_DOCTYPES:
				with self.subTest(user=user, doctype=doctype):
					self.assertTrue(frappe.has_permission(doctype, "read", user=user))

	def test_no_role_can_delete_billing_records(self):
		# covers: AC-18
		for user in ROLE_USERS.values():
			for doctype in BILLING_DOCTYPES:
				with self.subTest(user=user, doctype=doctype):
					self.assertFalse(frappe.has_permission(doctype, "delete", user=user))

	def test_only_a_billing_manager_writes_billing_fields_and_plans(self):
		# covers: AC-14, AC-20
		site = make_site("perm-test.example.com")
		billing = frappe.get_meta("Books Hosted Site")
		for field in BILLING_FIELDS:
			self.assertEqual(billing.get_field(field).permlevel, 1, field)

		for user, can_write in ((BILLING_MANAGER, True), (SYSTEM_MANAGER, False), (REMOVAL_MANAGER, False)):
			with self.subTest(user=user):
				self.assertEqual(
					frappe.has_permission("Books Hosted Site", "write", site, user=user), can_write
				)
				self.assertEqual(frappe.has_permission("Books Plan", "create", user=user), can_write)
				levels = billing.get_permlevel_access("write", user=user)
				self.assertEqual(1 in levels, can_write)

	def test_a_billing_manager_edit_of_paid_until_is_saved_and_tracked(self):
		# covers: AC-14, AC-18 (track changes)
		site = make_site("edit-test.example.com")
		frappe.set_user(BILLING_MANAGER)
		site.reload()
		site.paid_until = "2027-12-31"
		# Frappe skips versions in tests unless asked; a real save always keeps one (track changes).
		site.save(ignore_version=False)
		frappe.set_user("Administrator")
		self.assertEqual(str(frappe.db.get_value("Books Hosted Site", site.name, "paid_until")), "2027-12-31")
		self.assertTrue(
			frappe.db.exists("Version", {"ref_doctype": "Books Hosted Site", "docname": site.name})
		)

	def test_nobody_creates_records_from_desk(self):
		# Registry rows, checkouts and events are made only by the control site's own code (spec 0008).
		for user in ROLE_USERS.values():
			for doctype in ("Books Hosted Site", "Books Checkout", "Books Payment Event"):
				with self.subTest(user=user, doctype=doctype):
					self.assertFalse(frappe.has_permission(doctype, "create", user=user))

	def test_the_secret_is_stored_encrypted_and_never_shows(self):
		# covers: AC-15 (no secret shown in desk), the security model (no Password field in a Version diff)
		secret = secrets.token_hex(32)
		site = make_site("secret-test.example.com", secret=secret)
		frappe.set_user(BILLING_MANAGER)
		site = frappe.get_doc("Books Hosted Site", site.name)
		site.owner_email = "new-owner@example.com"
		# Frappe skips versions in tests unless asked; a real save always keeps one (track changes).
		site.save(ignore_version=False)
		frappe.set_user("Administrator")

		self.assertEqual(site.get_password("control_secret"), secret)
		row = frappe.db.get_value("Books Hosted Site", site.name, "control_secret")
		self.assertNotEqual(row, secret)
		self.assertNotIn(secret, frappe.as_json(frappe.get_doc("Books Hosted Site", site.name).as_dict()))
		versions = frappe.get_all(
			"Version", filters={"ref_doctype": "Books Hosted Site", "docname": site.name}, pluck="data"
		)
		self.assertTrue(versions)
		self.assertTrue(all(secret not in data for data in versions))

	def test_required_values(self):
		self.assertRaisesRegex(
			frappe.ValidationError, "control secret", make_site, "no-secret.example.com", secret=""
		)
		with self.assertRaisesRegex(frappe.ValidationError, "https://"):
			make_site("plain.example.com", site_url="http://plain.example.com")
