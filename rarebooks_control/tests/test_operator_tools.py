"""Spec 0008 Milestone 4: the manual edit and Push now (AC-14), roles on the server (AC-20), two factor sign in
(AC-20), and the signature failure alert (AC-22). Secret rotation is in `test_registry.py` (spec 0010 AC-12)."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from rarebooks_control import api, billing, registry
from rarebooks_control.install import enable_two_factor_auth
from rarebooks_control.tests.control import (
	call,
	issued,
	reset_billing,
)
from rarebooks_control.tests.data_model import (
	BILLING_MANAGER,
	REMOVAL_MANAGER,
	SYSTEM_MANAGER,
	make_role_users,
	make_site,
)

SITE = "Books Hosted Site"
OPS = "ops@example.com"


def site_body(site: str) -> dict:
	return {"site": site, "issued_at": issued()}


def ops_emails(sendmail) -> list[dict]:
	return [c.kwargs for c in sendmail.call_args_list if c.kwargs.get("recipients") == [OPS]]


class OperatorToolsTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_role_users()

	def setUp(self):
		reset_billing()
		self.queue_push = self.start(patch("rarebooks_control.push.queue_push"))
		self.sendmail = self.start(patch("frappe.sendmail"))
		self.start(patch.dict(frappe.conf, {"books_ops_email": OPS}))

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.cache.delete_keys("sigfail:")

	def start(self, patcher):
		mocked = patcher.start()
		self.addCleanup(patcher.stop)
		return mocked


class IntegrationTestManualEdit(OperatorToolsTestCase):
	def test_a_billing_manager_edit_is_tracked_and_pushes_the_new_state(self):
		# covers: AC-14, AC-13
		row = make_site("edit.rarebooks.app")
		self.queue_push.reset_mock()
		frappe.set_user(BILLING_MANAGER)
		doc = frappe.get_doc(SITE, row.name)
		doc.update({"paid_until": "2027-06-30", "provider": "ClickPesa", "billing_period": "Yearly"})
		doc.save(ignore_version=False)
		frappe.set_user("Administrator")

		saved = frappe.db.get_value(SITE, row.name, ["state_version", "push_status"], as_dict=True)
		self.assertEqual((saved.state_version, saved.push_status), (1, "Pending"))
		self.queue_push.assert_called_once_with(row.name)
		self.assertTrue(frappe.db.exists("Version", {"ref_doctype": SITE, "docname": row.name}))

	def test_clickpesa_with_a_monthly_period_is_refused(self):
		# covers: AC-14
		row = make_site("monthly.rarebooks.app")
		frappe.set_user(BILLING_MANAGER)
		doc = frappe.get_doc(SITE, row.name)
		doc.update({"provider": "ClickPesa", "billing_period": "Monthly"})
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_provider_reference_changes_only_with_the_provider(self):
		# covers: AC-14
		row = make_site("reference.rarebooks.app", provider="PayPal", billing_period="Monthly")
		frappe.set_user(BILLING_MANAGER)
		doc = frappe.get_doc(SITE, row.name)
		doc.provider_reference = "I-OTHER"
		with self.assertRaises(frappe.ValidationError):
			doc.save()

		doc = frappe.get_doc(SITE, row.name)
		doc.update({"provider": "ClickPesa", "billing_period": "Yearly", "provider_reference": "RB00000001"})
		doc.save()
		self.assertEqual(frappe.db.get_value(SITE, row.name, "provider_reference"), "RB00000001")

	def test_the_control_sites_own_writes_may_set_the_reference(self):
		# AC-9: a payment adopts a new PayPal subscription id with the provider unchanged.
		row = make_site("adopt.rarebooks.app", provider="PayPal", billing_period="Monthly")
		doc = frappe.get_doc(SITE, row.name)
		doc.provider_reference = "I-NEW"
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(SITE, row.name, "provider_reference"), "I-NEW")

	def test_a_system_manager_cant_edit_billing_fields(self):
		# covers: AC-20
		row = make_site("sysman.rarebooks.app")
		frappe.set_user(SYSTEM_MANAGER)
		doc = frappe.get_doc(SITE, row.name)
		doc.paid_until = "2030-01-01"
		with self.assertRaises(frappe.PermissionError):
			doc.save()


class IntegrationTestPushNow(OperatorToolsTestCase):
	def test_a_billing_manager_queues_a_push(self):
		# covers: AC-14
		row = make_site("push.rarebooks.app")
		self.queue_push.reset_mock()
		frappe.set_user(BILLING_MANAGER)
		self.assertEqual(billing.push_now(row.name), {"queued": True})
		self.queue_push.assert_called_once_with(row.name)

	def test_other_roles_get_403(self):
		# covers: AC-20
		row = make_site("push-403.rarebooks.app")
		for user in (REMOVAL_MANAGER, SYSTEM_MANAGER):
			with self.subTest(user=user):
				frappe.set_user(user)
				with self.assertRaises(frappe.PermissionError):
					billing.push_now(row.name)
				with self.assertRaises(frappe.PermissionError):
					registry.rotate_secret(row.name)

	def test_a_removed_site_is_refused(self):
		# covers: AC-13 (a Removed site is never pushed)
		row = make_site("gone.rarebooks.app", status="Removed")
		frappe.set_user(BILLING_MANAGER)
		with self.assertRaises(frappe.ValidationError):
			billing.push_now(row.name)
		with self.assertRaises(frappe.ValidationError):
			registry.rotate_secret(row.name)


class IntegrationTestSignatureFailureAlert(OperatorToolsTestCase):
	def test_21_failures_in_an_hour_send_one_email(self):
		# covers: AC-22
		row = make_site("alert.rarebooks.app")
		for count in range(1, 26):
			self.assertEqual(call(api.get_state, site_body(row.name), "wrong-secret").status_code, 401)
			self.assertEqual(len(ops_emails(self.sendmail)), 0 if count < 21 else 1, count)

	def test_webhook_checksum_failures_count_too(self):
		# covers: AC-22, AC-7
		from rarebooks_control import webhooks

		with patch.dict(frappe.conf, {"clickpesa_checksum_key": "key"}):
			for _ in range(21):
				response = call(webhooks.clickpesa, {"event": "PAYMENT RECEIVED", "checksum": "bad"})
				self.assertEqual(response.status_code, 401)
		self.assertEqual(len(ops_emails(self.sendmail)), 1)


class IntegrationTestTwoFactor(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_role_users()

	def test_install_turns_on_two_factor_before_the_setup_wizard(self):
		# covers: AC-20 (a new Frappe Cloud site installs the app while language and time_zone are still empty)
		before = (
			frappe.db.get_single_value("System Settings", "language"),
			frappe.db.get_single_value("System Settings", "time_zone"),
		)
		two_factor = frappe.db.get_single_value("System Settings", "enable_two_factor_auth")
		all_role = frappe.db.get_value("Role", "All", "two_factor_auth")
		try:
			frappe.db.set_single_value("System Settings", {"language": None, "time_zone": None})
			enable_two_factor_auth()
			self.assertEqual(frappe.db.get_single_value("System Settings", "enable_two_factor_auth"), 1)
			self.assertEqual(frappe.db.get_value("Role", "All", "two_factor_auth"), 1)
		finally:
			frappe.db.set_single_value(
				"System Settings",
				{"language": before[0], "time_zone": before[1], "enable_two_factor_auth": two_factor},
			)
			frappe.db.set_value("Role", "All", "two_factor_auth", all_role)
			from frappe.core.doctype.system_settings.system_settings import clear_system_settings_cache

			clear_system_settings_cache()

	def test_install_turns_on_two_factor_for_every_user(self):
		# covers: AC-20
		settings = frappe.get_single("System Settings")
		before = (settings.enable_two_factor_auth, settings.two_factor_method, settings.otp_issuer_name)
		all_role = frappe.db.get_value("Role", "All", "two_factor_auth")
		try:
			enable_two_factor_auth()
			settings = frappe.get_single("System Settings")
			self.assertEqual((settings.enable_two_factor_auth, settings.two_factor_method), (1, "OTP App"))
			self.assertEqual(frappe.db.get_value("Role", "All", "two_factor_auth"), 1)
			from frappe.twofactor import two_factor_is_enabled

			self.assertTrue(two_factor_is_enabled(user=BILLING_MANAGER))
		finally:
			frappe.db.set_single_value(
				"System Settings",
				{
					"enable_two_factor_auth": before[0],
					"two_factor_method": before[1],
					"otp_issuer_name": before[2],
				},
			)
			frappe.db.set_value("Role", "All", "two_factor_auth", all_role)
