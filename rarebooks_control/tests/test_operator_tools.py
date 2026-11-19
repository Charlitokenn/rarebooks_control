"""Spec 0008 Milestone 4: the manual edit and Push now (AC-14), secret rotation (AC-15), roles on the server
(AC-20), two factor sign in (AC-20), and the signature failure alert and runner rate limit (AC-22)."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from rarebooks_control import api, billing, push, removal
from rarebooks_control.install import enable_two_factor_auth
from rarebooks_control.tests.control import (
	RUNNER_SECRET,
	CustomerStub,
	answer,
	call,
	issued,
	reset_billing,
	runner_config,
)
from rarebooks_control.tests.data_model import (
	BENCH,
	BILLING_MANAGER,
	REMOVAL_MANAGER,
	SYSTEM_MANAGER,
	make_bench,
	make_role_users,
	make_site,
)

SITE = "Books Hosted Site"
OPS = "ops@example.com"


def site_body(site: str) -> dict:
	return {"site": site, "issued_at": issued()}


def rotated_body(site: str, bench: str = BENCH) -> dict:
	return {"bench": bench, "issued_at": issued(), "site": site}


def ops_emails(sendmail) -> list[dict]:
	return [c.kwargs for c in sendmail.call_args_list if c.kwargs.get("recipients") == [OPS]]


class OperatorToolsTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_role_users()

	def setUp(self):
		reset_billing()
		make_bench()
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
					billing.rotate_secret(row.name)

	def test_a_removed_site_is_refused(self):
		# covers: AC-13 (a Removed site is never pushed)
		row = make_site("gone.rarebooks.app", status="Removed")
		frappe.set_user(BILLING_MANAGER)
		with self.assertRaises(frappe.ValidationError):
			billing.push_now(row.name)
		with self.assertRaises(frappe.ValidationError):
			billing.rotate_secret(row.name)


class IntegrationTestRotation(OperatorToolsTestCase):
	def rotate(self, site: str) -> tuple[str, str]:
		"""Start a rotation as a Billing Manager; the old and the pending secret."""
		old = frappe.get_doc(SITE, site).get_password("control_secret")
		frappe.set_user(BILLING_MANAGER)
		self.assertEqual(billing.rotate_secret(site), {"pending": True})
		frappe.set_user("Administrator")
		return old, frappe.get_doc(SITE, site).get_password("pending_secret")

	def test_rotate_secret_stores_a_new_pending_secret_once(self):
		# covers: AC-15
		row = make_site("rotate.rarebooks.app")
		old, pending = self.rotate(row.name)
		self.assertEqual(len(pending), 64)
		self.assertNotEqual(pending, old)
		frappe.set_user(BILLING_MANAGER)
		with self.assertRaises(frappe.ValidationError):
			billing.rotate_secret(row.name)

	def test_the_bench_gets_its_own_sites_rotations_only(self):
		# covers: AC-15
		row = make_site("mine.rarebooks.app")
		other = make_site("theirs.rarebooks.app", hosting_bench=make_bench("bench-test-2"))
		make_site("quiet.rarebooks.app")
		_old, pending = self.rotate(row.name)
		self.rotate(other.name)
		self.assertEqual(
			removal.rotation_instructions(BENCH),
			[{"site": row.name, "action": "rotate_secret", "secret": pending}],
		)

	def test_while_pending_either_secret_works_and_is_answered_with_the_same(self):
		# covers: AC-15
		row = make_site("either.rarebooks.app")
		old, pending = self.rotate(row.name)
		for secret in (old, pending):
			with self.subTest(secret=secret[:6]):
				response = call(api.get_state, site_body(row.name), secret)
				self.assertEqual(response.status_code, 200)
				answer(response, secret)

	def test_secret_rotated_swaps_pushes_and_the_old_secret_stops_working(self):
		# covers: AC-15, AC-13
		row = make_site("swap.rarebooks.app")
		old, pending = self.rotate(row.name)
		self.queue_push.reset_mock()
		with runner_config(BENCH):
			response = call(removal.secret_rotated, rotated_body(row.name), RUNNER_SECRET)
		data = answer(response, RUNNER_SECRET)
		self.assertEqual((response.status_code, data["status"]), (200, "rotated"))

		doc = frappe.get_doc(SITE, row.name)
		self.assertEqual(doc.get_password("control_secret"), pending)
		self.assertFalse(doc.pending_secret)
		self.assertEqual((doc.state_version, doc.push_status), (1, "Pending"))
		self.queue_push.assert_called_once_with(row.name)

		self.assertEqual(call(api.get_state, site_body(row.name), old).status_code, 401)
		self.assertEqual(call(api.get_state, site_body(row.name), pending).status_code, 200)

		stub = CustomerStub(pending)
		with patch("rarebooks_control.push.post", stub):
			push.push_site(row.name)
		self.assertEqual(len(stub.pushes), 1, "the push is signed with the new secret")

	def test_secret_rotated_with_no_rotation_or_for_another_bench_gets_409(self):
		# covers: AC-15
		row = make_site("none.rarebooks.app")
		other = make_site("elsewhere.rarebooks.app", hosting_bench=make_bench("bench-test-2"))
		self.rotate(other.name)
		with runner_config(BENCH):
			for site in (row.name, other.name, "unknown.rarebooks.app"):
				with self.subTest(site=site):
					response = call(removal.secret_rotated, rotated_body(site), RUNNER_SECRET)
					self.assertEqual(response.status_code, 409)
					self.assertEqual(answer(response, RUNNER_SECRET)["status"], "no_rotation")
		self.assertTrue(frappe.get_doc(SITE, other.name).pending_secret, "the other bench's rotation waits")

	def test_secret_rotated_with_a_bad_signature_gets_401(self):
		# covers: AC-15
		row = make_site("badsig.rarebooks.app")
		self.rotate(row.name)
		with runner_config(BENCH):
			response = call(removal.secret_rotated, rotated_body(row.name), "not-the-runner-secret")
		self.assertEqual(response.status_code, 401)
		self.assertTrue(frappe.get_doc(SITE, row.name).pending_secret)

	def test_no_secret_reaches_a_version_or_an_answer(self):
		# covers: AC-15 (no secret shown in desk or written to any log), the security model
		row = make_site("private.rarebooks.app")
		old, pending = self.rotate(row.name)
		frappe.set_user(BILLING_MANAGER)
		doc = frappe.get_doc(SITE, row.name)
		doc.owner_email = "new@example.com"
		doc.save(ignore_version=False)
		shown = frappe.as_json(frappe.get_doc(SITE, row.name).as_dict())
		frappe.set_user("Administrator")
		versions = frappe.get_all("Version", filters={"ref_doctype": SITE, "docname": row.name}, pluck="data")
		for secret in (old, pending):
			self.assertNotIn(secret, shown)
			self.assertTrue(all(secret not in data for data in versions))

	def test_the_31st_runner_call_in_a_minute_is_refused(self):
		# covers: AC-22 (per bench limit, 120 a minute for runner calls)
		row = make_site("limit.rarebooks.app")
		with runner_config(BENCH), patch.object(removal, "RUNNER_LIMIT", 2):
			codes = [
				call(removal.secret_rotated, rotated_body(row.name), RUNNER_SECRET).status_code
				for _ in range(3)
			]
		self.assertEqual(codes, [409, 409, 429])


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
