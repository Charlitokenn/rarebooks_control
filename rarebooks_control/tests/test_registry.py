"""Registration and secret rotation through the Frappe Cloud API (spec 0010 AC-10 to AC-12, AC-16; spec 0011
AC-4 to AC-8), and the Frappe Cloud client itself (spec 0010 AC-13)."""

from datetime import timedelta
from unittest.mock import Mock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase, UnitTestCase
from urllib3.exceptions import NewConnectionError

from rarebooks_control import api, push, registry
from rarebooks_control.providers import frappe_cloud
from rarebooks_control.providers.frappe_cloud import FrappeCloudError, OutcomeUnknown
from rarebooks_control.signing import utc_now
from rarebooks_control.tests.control import (
	FakeFrappeCloud,
	SiteStub,
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
SETTINGS = "Books Removal Settings"
OPS = "ops@example.com"
CONTROL_URL = "https://control.rarebooks.app"
NEW = "new.rarebooks.app"


class RegistryTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_role_users()

	def setUp(self):
		reset_billing()
		frappe.db.set_single_value(SETTINGS, "books_bench_group", FakeFrappeCloud.GROUP)
		# Removal itself stays off here, so the stale run alert of the ping job keeps quiet.
		frappe.db.set_single_value(SETTINGS, "mode", "off")
		frappe.cache.delete_keys("removal-alert:")
		self.start(patch.dict(frappe.conf, {"books_ops_email": OPS, "books_control_public_url": CONTROL_URL}))
		self.cloud = FakeFrappeCloud()
		self.start(self.cloud.patch())
		self.sites = SiteStub()
		self.start(patch("rarebooks_control.site_calls.post", side_effect=self.sites))
		self.queue_push = self.start(patch("rarebooks_control.push.queue_push"))
		self.send_ops = self.start(patch("rarebooks_control.removal_mail.send_ops"))

	def tearDown(self):
		frappe.set_user("Administrator")

	def start(self, patcher):
		mocked = patcher.start()
		self.addCleanup(patcher.stop)
		return mocked

	def register(self, site: str = NEW, **values):
		frappe.set_user(BILLING_MANAGER)
		try:
			return registry.register_site(
				**{"site": site, "owner_email": "owner@example.com", "site_url": f"https://{site}", **values}
			)
		finally:
			frappe.set_user("Administrator")

	def as_billing_manager(self, method, *args):
		frappe.set_user(BILLING_MANAGER)
		try:
			return method(*args)
		finally:
			frappe.set_user("Administrator")

	def row(self, site: str = NEW):
		return frappe.get_doc(SITE, site)

	def take_config(self, site: str = NEW):
		"""The site now holds whatever secret Frappe Cloud last wrote to its config."""
		self.sites.secrets[site] = self.cloud.configs[site]["books_control_secret"]


class TestRegister(RegistryTestCase):
	def test_a_good_site_goes_pending_with_its_config_sent_then_active_after_a_ping(self):
		# covers: 0010 AC-10, AC-11
		self.cloud.add(NEW)
		self.assertEqual(self.register(), {"site": NEW, "config_status": "Pending"})
		row = self.row()
		self.assertEqual((row.config_status, row.site_url), ("Pending", f"https://{NEW}"))
		self.assertTrue(row.config_sent_on)
		secret = row.get_password("control_secret")
		self.assertEqual(len(secret), 64)
		self.assertEqual(
			self.cloud.configs[NEW], {"books_control_url": CONTROL_URL, "books_control_secret": secret}
		)
		registry.run_pings()
		self.assertEqual(self.row().config_status, "Pending", "the site doesn't hold the secret yet")
		self.take_config()
		registry.run_pings()
		self.assertEqual(self.row().config_status, "Active")

	def test_refusals_write_nothing(self):
		# covers: 0010 AC-10
		self.cloud.add(NEW)
		self.cloud.add("elsewhere.rarebooks.app", group="other-group")
		self.cloud.add("ungrouped.rarebooks.app", group=None)
		make_site("taken.rarebooks.app")
		self.cloud.add("taken.rarebooks.app")
		cases = {
			"http": {"site_url": f"http://{NEW}"},
			"not on Frappe Cloud": {"site": "nowhere.rarebooks.app"},
			"other bench group": {"site": "elsewhere.rarebooks.app"},
			"empty bench group": {"site": "ungrouped.rarebooks.app"},
			"the control site": {"site": frappe.local.site},
			"already registered": {"site": "taken.rarebooks.app"},
			"bad email": {"owner_email": "nope"},
		}
		for name, values in cases.items():
			with self.subTest(name), self.assertRaises(frappe.ValidationError):
				self.register(**values)
		self.assertEqual(frappe.db.count(SITE), 1)
		self.assertNotIn("update_config", [c[0] for c in self.cloud.calls])

	def test_only_a_billing_manager_registers(self):
		# covers: 0010 AC-10
		self.cloud.add(NEW)
		for user in (REMOVAL_MANAGER, SYSTEM_MANAGER):
			frappe.set_user(user)
			with self.subTest(user), self.assertRaises(frappe.PermissionError):
				registry.register_site(NEW, "owner@example.com", f"https://{NEW}")
			for method in (registry.retry_config, registry.rotate_secret):
				with self.subTest(user, method=method.__name__), self.assertRaises(frappe.PermissionError):
					method(NEW)
		frappe.set_user("Administrator")

	def test_a_failed_config_write_commits_a_failed_row_then_raises(self):
		# covers: 0011 AC-7, 0010 AC-13
		self.cloud.add(NEW)
		self.cloud.errors["update_config"] = OutcomeUnknown("press.api.site.update_config sent, no answer")
		with self.assertRaises(frappe.ValidationError) as raised:
			self.register()
		frappe.db.rollback()
		row = self.row()
		self.assertEqual((row.config_status, row.config_sent_on), ("Failed", None))
		self.assertNotIn(row.get_password("control_secret"), str(raised.exception))
		self.assertIn("Retry config", str(raised.exception))
		self.send_ops.assert_not_called()
		registry.run_pings()
		self.assertEqual(self.sites.calls, [], "a Failed site isn't pinged")

		del self.cloud.errors["update_config"]
		self.assertEqual(self.as_billing_manager(registry.retry_config, NEW)["config_status"], "Pending")
		self.assertTrue(self.row().config_sent_on)
		self.take_config()
		registry.run_pings()
		self.assertEqual(self.row().config_status, "Active")

	def test_no_ping_for_an_hour_fails_with_one_email(self):
		# covers: 0010 AC-11
		self.cloud.add(NEW)
		self.register()
		registry.run_pings()
		self.send_ops.assert_not_called()
		frappe.db.set_value(SITE, NEW, "config_sent_on", utc_now() - timedelta(hours=1, minutes=1))
		registry.run_pings()
		registry.run_pings()
		self.assertEqual(self.row().config_status, "Failed")
		self.send_ops.assert_called_once()

	def test_a_site_whose_config_isnt_active_gets_no_push(self):
		# covers: 0010 AC-11
		row = make_site("waiting.rarebooks.app", config_status="Pending", push_status="Pending")
		with patch("rarebooks_control.push.send") as send:
			push.push_site(row.name)
			push.sweep()
		send.assert_not_called()
		self.queue_push.assert_not_called()


class TestRetryConfig(RegistryTestCase):
	def test_on_an_active_site_it_sends_both_keys_and_goes_pending_until_the_next_ping(self):
		# covers: 0011 AC-4
		row = make_site("healthy.rarebooks.app")
		self.sites.hold(row)
		with patch.dict(frappe.conf, {"books_control_public_url": "https://new-control.rarebooks.app"}):
			self.as_billing_manager(registry.retry_config, row.name)
		self.assertEqual(
			self.cloud.configs[row.name],
			{
				"books_control_url": "https://new-control.rarebooks.app",
				"books_control_secret": row.get_password("control_secret"),
			},
		)
		self.assertEqual(self.row(row.name).config_status, "Pending")
		self.assertEqual(self.cloud.calls, [("update_config", row.name)], "no registration checks")
		registry.run_pings()
		self.assertEqual(self.row(row.name).config_status, "Active")

	def test_mid_rotation_it_sends_the_pending_secret_restarts_the_clock_and_stays_active(self):
		# covers: 0011 AC-4, 0010 AC-12
		row = make_site("rotating.rarebooks.app")
		self.as_billing_manager(registry.rotate_secret, row.name)
		frappe.db.set_value(SITE, row.name, "rotation_started_on", utc_now() - timedelta(hours=2))
		sent_on = self.row(row.name).config_sent_on
		self.as_billing_manager(registry.retry_config, row.name)
		row = self.row(row.name)
		self.assertEqual(
			self.cloud.configs[row.name]["books_control_secret"], row.get_password("pending_secret")
		)
		self.assertEqual((row.config_status, row.config_sent_on), ("Active", sent_on))
		self.assertLess(utc_now() - row.rotation_started_on, timedelta(minutes=1))
		for secret in (row.get_password("control_secret"), row.get_password("pending_secret")):
			body = {"site": row.name, "issued_at": issued()}
			self.assertEqual(call(api.get_state, body, secret).status_code, 200)

	def test_a_removed_site_is_refused(self):
		# covers: 0011 AC-4
		row = make_site("gone.rarebooks.app", status="Removed")
		for method in (registry.retry_config, registry.rotate_secret):
			with self.subTest(method.__name__), self.assertRaises(frappe.ValidationError):
				self.as_billing_manager(method, row.name)


class TestRotation(RegistryTestCase):
	def test_both_secrets_work_until_the_first_ping_with_the_new_one_swaps_them(self):
		# covers: 0010 AC-12
		row = make_site("rotate.rarebooks.app")
		old = row.get_password("control_secret")
		self.sites.hold(row)
		self.assertEqual(self.as_billing_manager(registry.rotate_secret, row.name), {"pending": True})
		pending = self.row(row.name).get_password("pending_secret")
		self.assertNotEqual(old, pending)
		self.assertEqual(self.cloud.configs[row.name]["books_control_secret"], pending)
		self.assertTrue(self.row(row.name).rotation_started_on)
		with self.assertRaises(frappe.ValidationError):
			self.as_billing_manager(registry.rotate_secret, row.name)

		registry.run_pings()
		self.assertTrue(self.row(row.name).pending_secret, "the site still holds the old secret")
		self.take_config(row.name)
		self.queue_push.reset_mock()
		registry.run_pings()
		row = self.row(row.name)
		self.assertEqual(row.get_password("control_secret"), pending)
		self.assertFalse(row.pending_secret or row.rotation_started_on)
		self.queue_push.assert_called_once_with(row.name)
		body = {"site": row.name, "issued_at": issued()}
		self.assertEqual(call(api.get_state, body, old).status_code, 401)
		self.assertEqual(call(api.get_state, body, pending).status_code, 200)

	def test_the_swap_runs_once_and_only_for_the_secret_that_verified(self):
		# covers: 0010 AC-12
		row = make_site("twice.rarebooks.app")
		self.as_billing_manager(registry.rotate_secret, row.name)
		old = row.get_password("control_secret")

		def replaced_in_flight(row, secret):
			# Another pending secret lands while this ping is out: the ping proves the older one only.
			frappe.db.set_value(SITE, row.name, "pending_secret", None)
			doc = frappe.get_doc(SITE, row.name)
			doc.pending_secret = "another-pending-secret-0123456789abcdef0123456789"
			doc.save(ignore_permissions=True)
			return True

		with patch.object(registry, "_pinged", side_effect=replaced_in_flight):
			registry.ping_rotation(row.name)
		self.assertEqual(self.row(row.name).get_password("control_secret"), old, "no swap")

		self.take_config(row.name)
		self.cloud.configs[row.name]["books_control_secret"] = self.row(row.name).get_password(
			"pending_secret"
		)
		self.take_config(row.name)
		self.queue_push.reset_mock()
		registry.ping_rotation(row.name)
		registry.ping_rotation(row.name)
		self.assertEqual(
			self.row(row.name).get_password("control_secret"),
			"another-pending-secret-0123456789abcdef0123456789",
		)
		self.assertEqual(self.queue_push.call_count, 1, "swapped once")

	def test_refused_while_the_config_isnt_active(self):
		# covers: 0011 AC-6
		for config_status in ("Pending", "Failed"):
			row = make_site(f"{config_status.lower()}-rot.rarebooks.app", config_status=config_status)
			with self.subTest(config_status), self.assertRaisesRegex(frappe.ValidationError, "Retry config"):
				self.as_billing_manager(registry.rotate_secret, row.name)
			self.assertFalse(self.row(row.name).pending_secret)

	def test_a_failed_config_write_keeps_the_pending_secret_and_retry_sends_it(self):
		# covers: 0011 AC-8
		row = make_site("keep.rarebooks.app")
		self.cloud.errors["update_config"] = FrappeCloudError(
			"press.api.site.update_config answered HTTP 500"
		)
		with self.assertRaises(frappe.ValidationError):
			self.as_billing_manager(registry.rotate_secret, row.name)
		frappe.db.rollback()
		row = self.row(row.name)
		pending = row.get_password("pending_secret")
		self.assertTrue(pending and row.rotation_started_on)
		del self.cloud.errors["update_config"]
		self.as_billing_manager(registry.retry_config, row.name)
		self.assertEqual(self.cloud.configs[row.name]["books_control_secret"], pending)

	def test_a_rotation_pending_over_an_hour_alerts_once(self):
		# covers: 0010 AC-12
		row = make_site("slow.rarebooks.app")
		self.sites.hold(row)
		self.as_billing_manager(registry.rotate_secret, row.name)
		frappe.db.set_value(SITE, row.name, "rotation_started_on", utc_now() - timedelta(hours=1, minutes=1))
		registry.run_pings()
		registry.run_pings()
		subjects = [c.args[0] for c in self.send_ops.call_args_list]
		self.assertEqual(len([s for s in subjects if "rotation" in s]), 1, subjects)
		self.assertTrue(self.row(row.name).pending_secret, "both secrets stay valid")

	def test_no_secret_reaches_a_version_or_desk(self):
		# covers: 0010 AC-13, the security model
		row = make_site("private.rarebooks.app")
		old = row.get_password("control_secret")
		self.as_billing_manager(registry.rotate_secret, row.name)
		pending = self.row(row.name).get_password("pending_secret")
		frappe.set_user(BILLING_MANAGER)
		doc = frappe.get_doc(SITE, row.name)
		doc.owner_email = "new@example.com"
		doc.save(ignore_version=False)
		shown = frappe.as_json(frappe.get_doc(SITE, row.name).as_dict())
		frappe.set_user("Administrator")
		versions = frappe.get_all("Version", filters={"ref_doctype": SITE, "docname": row.name}, pluck="data")
		logs = frappe.get_all("Error Log", fields=["method", "error"])
		for secret in (old, pending):
			self.assertNotIn(secret, shown)
			self.assertTrue(all(secret not in data for data in versions))
			self.assertTrue(all(secret not in (log.method or "") + (log.error or "") for log in logs))


CLOUD_CONF = {
	"frappe_cloud_api_key": "key-123",
	"frappe_cloud_api_secret": "secret-456",
	"frappe_cloud_team": "team@example.com",
}


class UnitTestFrappeCloudClient(UnitTestCase):
	def response(self, status=200, body=None):
		response = Mock(status_code=status)
		response.json.return_value = {"message": body} if body is not None else {}
		return response

	def test_calls_the_three_methods_with_token_team_and_timeout(self):
		# covers: 0010 AC-13, 0011 AC-3
		with patch.dict(frappe.conf, CLOUD_CONF), patch("requests.post") as post:
			post.return_value = self.response(body={"name": "a.app", "status": "Active", "group": "", "x": 1})
			self.assertEqual(
				frappe_cloud.get_site("a.app"), {"name": "a.app", "status": "Active", "group": None}
			)
			frappe_cloud.update_config("a.app", {"books_control_url": "https://c"})
			frappe_cloud.archive("a.app")
		urls = [c.args[0] for c in post.call_args_list]
		self.assertEqual(
			urls,
			[
				"https://cloud.frappe.io/api/method/press.api.site.get",
				"https://cloud.frappe.io/api/method/press.api.site.update_config",
				"https://cloud.frappe.io/api/method/press.api.site.archive",
			],
		)
		for c in post.call_args_list:
			self.assertEqual(c.kwargs["headers"]["Authorization"], "token key-123:secret-456")
			self.assertEqual(c.kwargs["headers"]["X-Press-Team"], "team@example.com")
			self.assertEqual(c.kwargs["timeout"], 30)
		self.assertEqual(post.call_args_list[2].kwargs["json"], {"name": "a.app", "force": False})
		self.assertEqual(
			post.call_args_list[1].kwargs["json"]["config"],
			'[{"key": "books_control_url", "value": "https://c", "type": "String"}]',
		)

	def test_tells_a_clear_error_apart_from_an_unknown_outcome_and_leaks_nothing(self):
		# covers: 0011 AC-1, 0010 AC-13
		refused = requests.ConnectionError(NewConnectionError(None, "refused"))
		cases = {
			"http 500": (self.response(500), FrappeCloudError),
			"connect timeout": (requests.ConnectTimeout("t"), FrappeCloudError),
			"connect refused": (refused, FrappeCloudError),
			"read timeout": (requests.ReadTimeout("t"), OutcomeUnknown),
			"reset": (requests.ConnectionError("Connection aborted"), OutcomeUnknown),
		}
		for name, (outcome, expected) in cases.items():
			with self.subTest(name), patch.dict(frappe.conf, CLOUD_CONF), patch("requests.post") as post:
				if isinstance(outcome, Exception):
					post.side_effect = outcome
				else:
					post.return_value = outcome
				with self.assertRaises(FrappeCloudError) as raised:
					frappe_cloud.archive("a.app")
				self.assertIs(type(raised.exception), expected)
				for value in CLOUD_CONF.values():
					self.assertNotIn(value, str(raised.exception))

	def test_missing_credentials_fail_before_any_call(self):
		with patch.dict(frappe.conf, {"frappe_cloud_api_key": None}), patch("requests.post") as post:
			with self.assertRaises(FrappeCloudError):
				frappe_cloud.get_site("a.app")
		post.assert_not_called()
