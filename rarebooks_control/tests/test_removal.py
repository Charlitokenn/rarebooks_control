"""The control site's removal side on Frappe Cloud (specs 0007, 0010 and 0011): the hourly round and its site
jobs, the signed site calls, the export through a presigned upload, the drop through the Frappe Cloud archive,
the Removal Manager actions and the operator alerts.

Customer sites are `SiteStub`, patched into `site_calls.post`; Frappe Cloud is `FakeFrappeCloud`; R2 is patched
in `export_storage`. Times are moved by back dating stored timestamps, never the clock, so every signed call
stays fresh.
"""

from datetime import timedelta
from email import message_from_string
from email.policy import default as default_policy
from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from rarebooks_control import api, billing, removal, removal_mail
from rarebooks_control.providers.frappe_cloud import FrappeCloudError, OutcomeUnknown
from rarebooks_control.signing import utc_now
from rarebooks_control.tests.control import (
	FakeFrappeCloud,
	SiteResponse,
	SiteStub,
	answer,
	call,
	get,
	issued,
	reset_billing,
)
from rarebooks_control.tests.data_model import (
	BILLING_MANAGER,
	REMOVAL_MANAGER,
	SYSTEM_MANAGER,
	make_outgoing_email_account,
	make_role_users,
	make_site,
)

CASE = "Books Removal Case"
SITE = "Books Hosted Site"
SETTINGS = "Books Removal Settings"
OPS = "ops@example.com"
OWNER = "owner@example.com"
EXPORT_BYTES = 4096
DIGEST = "a" * 64
UPLOAD_URL = "https://account.r2.cloudflarestorage.com/books-exports/key?X-Amz-Signature=abc"
DEFAULT_SETTINGS = {
	"mode": "live",
	"removal_paused": 0,
	"require_approval": 1,
	"max_drops_per_day": 5,
	"books_bench_group": FakeFrappeCloud.GROUP,
	"last_run_on": None,
}


def email_text(name: str) -> str:
	"""The plain text part of a queued email, decoded."""
	mail = message_from_string(frappe.db.get_value("Email Queue", name, "message"), policy=default_policy)
	return mail.get_body(preferencelist=("plain",)).get_content()


def site_today():
	return utc_now().date()


def status(site: str, removal_in: int | None = 0, kind: str | None = "Trial", **values) -> dict:
	"""A `removal_status()` answer, for a site whose removal date is `removal_in` days after its today."""
	today = site_today()
	return {
		"site": site,
		"site_url": f"https://{site}",
		"phase": "locked" if kind == "Trial" else "lapsed",
		"today": str(today),
		"removal_on": None if removal_in is None else str(today + timedelta(days=removal_in)),
		"kind": kind if removal_in is not None else None,
		"paid_until": None,
		"exempt": False,
		"payment_pending": False,
		"language": "en",
		"company_name": "Duka la Vitabu",
		"export_sent_on": None,
		"removal_hold_until": None,
		**values,
	}


def set_settings(**values):
	for field, value in {**DEFAULT_SETTINGS, **values}.items():
		frappe.db.set_single_value(SETTINGS, field, value)
	frappe.db.commit()


class RemovalTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_role_users()
		make_outgoing_email_account()

	def setUp(self):
		reset_billing()
		frappe.db.delete("Email Queue Recipient")
		frappe.db.delete("Email Queue")
		set_settings()
		frappe.cache.delete_keys("removal:exports:")
		frappe.cache.delete_keys("removal-alert:")
		self.start(patch.dict(frappe.conf, {"books_ops_email": OPS}))
		self.storage_size = self.start(
			patch("rarebooks_control.export_storage.object_size", return_value=EXPORT_BYTES)
		)
		self.storage_delete = self.start(patch("rarebooks_control.export_storage.delete_object"))
		self.start(
			patch(
				"rarebooks_control.export_storage.presigned_url",
				return_value="https://r2.example.com/exports/x.zip?sig=1",
			)
		)
		self.upload_url = self.start(
			patch("rarebooks_control.export_storage.presigned_put_url", return_value=UPLOAD_URL)
		)
		self.queue_push = self.start(patch("rarebooks_control.push.queue_push"))
		self.sites = SiteStub()
		self.post = self.start(patch("rarebooks_control.site_calls.post", side_effect=self.sites))
		self.cloud = FakeFrappeCloud()
		self.start(self.cloud.patch())

	def tearDown(self):
		frappe.set_user("Administrator")
		set_settings()

	def start(self, patcher):
		mocked = patcher.start()
		self.addCleanup(patcher.stop)
		return mocked

	# Helpers

	def site(self, name: str, **values):
		"""A registered, configured site, known to Frappe Cloud on the Books bench group."""
		row = make_site(name, **values)
		self.sites.hold(row)
		self.cloud.add(name)
		return row

	def tick(self, site: str, **report):
		"""One hourly `run_site` job, with the site reporting `status(site, **report)`."""
		self.sites.reports[site] = status(site, **report)
		removal.run_site(site)
		return self.case(site)

	def case(self, site: str):
		name = frappe.db.get_value(CASE, {"site": site}, "name", order_by="creation desc")
		return frappe.get_doc(CASE, name) if name else None

	def age_warnings(self, site: str, hours: int = 25):
		case = self.case(site)
		for row in case.warnings:
			frappe.db.set_value(
				"Books Removal Warning", row.name, "sent_on", row.sent_on - timedelta(hours=hours)
			)

	def mark_email(self, name: str, status: str = "Sent"):
		frappe.db.set_value("Email Queue", name, "status", status)

	def to_exporting(self, site: str):
		"""Open a case due today, its warning 25 hours old, and start the export."""
		self.tick(site)
		self.age_warnings(site)
		return self.tick(site)

	def export_ready(self, case, secret: str | None = None, **values):
		body = {
			"site": case.site,
			"issued_at": issued(),
			"case": case.name,
			"attempt_id": case.attempt_id,
			"bytes": EXPORT_BYTES,
			"sha256": DIGEST,
			**values,
		}
		secret = secret or frappe.get_doc(SITE, case.site).get_password("control_secret")
		response = call(removal.export_ready, body, secret)
		return response, (answer(response, secret) if response.status_code != 401 else None)

	def step_failed(self, case, step="upload", error="R2 timed out"):
		secret = frappe.get_doc(SITE, case.site).get_password("control_secret")
		body = {"site": case.site, "issued_at": issued(), "case": case.name, "step": step, "error": error}
		response = call(removal.step_failed, body, secret)
		return response, answer(response, secret)

	def to_holding(self, site: str):
		self.to_exporting(site)
		self.export_ready(self.case(site))
		self.mark_email(self.case(site).export_email)
		return self.tick(site)

	def marked(self, case) -> dict:
		"""Report values of a site whose hold fields match the case."""
		return {
			"export_sent_on": removal._iso(case.export_sent_on),
			"removal_hold_until": removal._iso(case.hold_until),
		}

	def to_drop_due(self, site: str, approve: bool = True):
		case = self.to_holding(site)
		frappe.db.set_value(CASE, case.name, "hold_until", utc_now() - timedelta(minutes=1))
		if approve:
			frappe.db.set_value(CASE, case.name, {"approved_by": REMOVAL_MANAGER, "approved_on": utc_now()})
		return self.case(site)

	def drop_after(self, case):
		return self.drop(case.site)

	def drop(self, site: str):
		"""Run the job for a site whose drop is due."""
		return self.tick(site, **self.marked(self.case(site)))

	def queued_to(self, recipient: str) -> list:
		return frappe.get_all(
			"Email Queue Recipient", filters={"recipient": recipient}, pluck="parent", order_by="creation asc"
		)


class TestRound(RemovalTestCase):
	def test_off_only_notes_the_run_and_calls_no_site(self):
		# covers: 0010 AC-1
		set_settings(mode="off")
		self.site("quiet.rarebooks.app")
		with patch("frappe.enqueue") as enqueue:
			removal.run_round()
		enqueue.assert_not_called()
		self.assertTrue(frappe.db.get_single_value(SETTINGS, "last_run_on"))
		self.tick("quiet.rarebooks.app")
		self.assertEqual(self.sites.calls, [])

	def test_the_round_enqueues_configured_sites_and_every_dropping_case(self):
		# covers: 0010 AC-1, 0011 AC-10
		self.site("ready.rarebooks.app")
		self.site("pending.rarebooks.app", config_status="Pending")
		failed = self.site("failed.rarebooks.app", config_status="Failed")
		frappe.get_doc(
			{
				"doctype": CASE,
				"site": failed.name,
				"mode": "live",
				"kind": "Trial",
				"removal_on": site_today(),
				"status": "Dropping",
				"drop_started_on": utc_now(),
				"owner_email": OWNER,
				"site_url": failed.site_url,
			}
		).insert(ignore_permissions=True)
		with patch("frappe.enqueue") as enqueue:
			removal.run_round()
		jobs = {c.kwargs["job_id"]: c.kwargs for c in enqueue.call_args_list}
		self.assertEqual(set(jobs), {"removal:ready.rarebooks.app", "removal:failed.rarebooks.app"})
		self.assertTrue(all(job["deduplicate"] for job in jobs.values()))
		self.assertTrue(frappe.db.get_single_value(SETTINGS, "last_run_on"))

	def test_a_skipped_site_that_paid_is_cancelled_from_billing_alone(self):
		# covers: 0010 AC-1
		self.site("skip.rarebooks.app")
		case = self.to_exporting("skip.rarebooks.app")
		frappe.db.set_value(
			SITE,
			case.site,
			{"config_status": "Failed", "paid_until": site_today() + timedelta(days=365)},
		)
		with patch("frappe.enqueue"):
			removal.run_round()
		self.assertEqual(self.case(case.site).status, "Cancelled")
		self.storage_delete.assert_called_once_with(case.export_key)

	def test_saving_dry_run_to_live_cancels_dry_run_cases(self):
		# covers: 0010 AC-9
		set_settings(mode="dry_run")
		self.site("switch.rarebooks.app", owner_email=OWNER)
		dry = self.to_exporting("switch.rarebooks.app")
		self.assertEqual(dry.mode, "dry_run")
		settings = frappe.get_single(SETTINGS)
		settings.mode = "live"
		settings.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(CASE, dry.name, "status"), "Cancelled")
		self.storage_delete.assert_called_once_with(dry.export_key)
		live = self.tick("switch.rarebooks.app")
		self.assertEqual((live.mode, live.status), ("live", "Scheduled"))
		self.assertEqual(len(self.queued_to(OWNER)), 1, "live starts with a real warning")

	def test_settings_need_a_bench_group_once_removal_is_on(self):
		# covers: 0010 AC-9
		settings = frappe.get_single(SETTINGS)
		settings.books_bench_group = ""
		settings.mode = "dry_run"
		with self.assertRaises(frappe.ValidationError):
			settings.save(ignore_permissions=True)
		meta = frappe.get_meta(SETTINGS)
		self.assertTrue(meta.track_changes)
		self.assertEqual(
			{(perm.role, perm.write) for perm in meta.permissions},
			{("Removal Manager", 1), ("System Manager", 0)},
		)

	def test_dormant_days_alerts_once_a_day(self):
		# covers: 0010 AC-14
		frappe.db.set_single_value("System Settings", "dormant_days", 4)
		try:
			with patch("rarebooks_control.removal_mail.send_ops") as send_ops, patch("frappe.enqueue"):
				removal.run_round()
				removal.run_round()
			self.assertEqual(len(send_ops.call_args_list), 1)
			frappe.db.set_single_value("System Settings", "dormant_days", 0)
			frappe.cache.delete_keys("removal-alert:")
			with patch("rarebooks_control.removal_mail.send_ops") as send_ops, patch("frappe.enqueue"):
				removal.run_round()
			send_ops.assert_not_called()
		finally:
			frappe.db.set_single_value("System Settings", "dormant_days", 0)

	def test_install_sets_dormant_days_to_0(self):
		# covers: 0010 AC-14
		from rarebooks_control.install import keep_scheduler_awake

		frappe.db.set_single_value("System Settings", "dormant_days", 4)
		keep_scheduler_awake()
		self.assertEqual(frappe.db.get_single_value("System Settings", "dormant_days"), 0)


class TestSiteCalls(RemovalTestCase):
	def test_every_call_is_signed_and_names_the_site(self):
		# covers: 0010 AC-2
		self.site("signed.rarebooks.app")
		self.tick("signed.rarebooks.app", removal_in=10)
		[(site, name, body)] = self.sites.calls
		self.assertEqual((site, name, body["site"]), ("signed.rarebooks.app", "removal_report", site))
		self.assertTrue(body["issued_at"].endswith("+00:00"))

	def test_an_answer_with_a_bad_signature_or_echo_counts_an_error_run(self):
		# covers: 0010 AC-2
		row = self.site("forged.rarebooks.app")
		case = self.tick("forged.rarebooks.app", removal_in=10)
		secret = row.get_password("control_secret")
		for name, response in {
			"unsigned": SiteResponse(200, status(row.name), None),
			"wrong secret": SiteResponse(200, status(row.name), "not-the-secret"),
			"no echo": SiteResponse(200, {**status(row.name), "issued_at": issued()}, secret),
		}.items():
			with self.subTest(name), patch("rarebooks_control.site_calls.post", return_value=response):
				removal.run_site(row.name)
		case = self.case(row.name)
		self.assertEqual(case.error_runs, 3)
		self.assertEqual(case.status, "Scheduled")

	def test_three_error_runs_alert_once_and_a_good_one_resets(self):
		# covers: 0007 AC-21, 0010 AC-1
		self.site("err.rarebooks.app")
		self.tick("err.rarebooks.app", removal_in=10)
		self.sites.down.add("removal_report")
		with patch("rarebooks_control.removal_mail.send_ops") as send_ops:
			for _run in range(4):
				removal.run_site("err.rarebooks.app")
		send_ops.assert_called_once()
		self.assertEqual(self.case("err.rarebooks.app").error_runs, 4)
		self.sites.down.clear()
		self.assertEqual(self.tick("err.rarebooks.app", removal_in=10).error_runs, 0)

	def test_a_401_during_a_rotation_is_tried_again_with_the_pending_secret(self):
		# covers: 0010 AC-12
		row = self.site("swap.rarebooks.app")
		frappe.db.set_value(SITE, row.name, "rotation_started_on", utc_now())
		doc = frappe.get_doc(SITE, row.name)
		doc.pending_secret = "pending-secret-0123456789abcdef0123456789abcdef"
		doc.save(ignore_permissions=True)
		self.sites.hold(doc, "pending-secret-0123456789abcdef0123456789abcdef")
		case = self.tick(row.name, removal_in=10)
		self.assertEqual((case.status, case.error_runs), ("Scheduled", 0))
		self.assertEqual(self.post.call_count, 2)

	def test_export_ready_and_step_failed_refuse_a_bad_signature_or_a_stale_call(self):
		# covers: 0010 AC-2, AC-17
		self.site("auth.rarebooks.app")
		case = self.to_exporting("auth.rarebooks.app")
		response, _data = self.export_ready(case, secret="wrong-secret")
		self.assertEqual((response.status_code, response.get_data()), (401, b"{}"))
		response, _data = self.export_ready(case, issued_at=issued(timedelta(minutes=-6)))
		self.assertEqual(response.status_code, 401)

	def test_site_calls_are_limited_per_site(self):
		# covers: 0010 AC-17
		self.site("limit.rarebooks.app")
		case = self.to_exporting("limit.rarebooks.app")
		with patch.object(removal, "SITE_CALL_LIMIT", 2):
			codes = [self.export_ready(case, attempt_id="other")[0].status_code for _ in range(3)]
		self.assertEqual(codes, [409, 409, 429])


class TestCasesAndWarnings(RemovalTestCase):
	def test_a_report_with_a_removal_date_opens_one_case_from_the_registry(self):
		# covers: 0007 AC-1, AC-4
		self.site("open.rarebooks.app", owner_email=OWNER)
		case = self.tick("open.rarebooks.app", removal_in=10)
		self.assertEqual(
			(case.status, case.mode, case.kind, case.owner_email), ("Scheduled", "live", "Trial", OWNER)
		)
		self.assertEqual(str(case.removal_on), str(site_today() + timedelta(days=10)))
		self.assertEqual(case.warnings, [], "no warning is due 10 days out")
		self.tick("open.rarebooks.app", removal_in=10)
		self.assertEqual(frappe.db.count(CASE, {"site": "open.rarebooks.app"}), 1)

	def test_no_removal_date_or_exempt_opens_no_case(self):
		# covers: 0007 AC-1
		self.site("paid.rarebooks.app")
		self.site("exempt.rarebooks.app")
		self.assertIsNone(self.tick("paid.rarebooks.app", removal_in=None, phase="active"))
		self.assertIsNone(self.tick("exempt.rarebooks.app", exempt=True))

	def test_a_pending_site_is_never_read(self):
		# covers: 0010 AC-11
		self.site("pending.rarebooks.app", config_status="Pending")
		self.assertIsNone(self.tick("pending.rarebooks.app"))
		self.assertEqual(self.sites.calls, [])

	def test_removal_on_moves_only_later(self):
		# covers: 0007 AC-1
		self.site("later.rarebooks.app")
		self.tick("later.rarebooks.app", removal_in=10)
		case = self.tick("later.rarebooks.app", removal_in=12)
		self.assertEqual(str(case.removal_on), str(site_today() + timedelta(days=12)))
		case = self.tick("later.rarebooks.app", removal_in=11)
		self.assertEqual(str(case.removal_on), str(site_today() + timedelta(days=12)))

	def test_trial_warnings_go_4_2_and_1_days_before_once_each(self):
		# covers: 0007 AC-4
		self.site("warn.rarebooks.app", owner_email=OWNER)
		name = self.tick("warn.rarebooks.app", removal_in=5).name
		for removal_in, expected in ((5, []), (4, [4]), (4, [4]), (2, [4, 2]), (1, [4, 2, 1])):
			frappe.db.set_value(CASE, name, "removal_on", site_today() + timedelta(days=removal_in))
			case = self.tick("warn.rarebooks.app", removal_in=removal_in)
			self.assertEqual([row.offset_days for row in case.warnings], expected)
		self.assertEqual(len(self.queued_to(OWNER)), 3)
		text = email_text(self.queued_to(OWNER)[0])
		self.assertIn("https://warn.rarebooks.app/books/subscription", text)
		self.assertIn("Duka la Vitabu", text)

	def test_lapsed_warnings_use_14_3_and_1(self):
		# covers: 0007 AC-4
		self.site("lapsed.rarebooks.app")
		case = self.tick("lapsed.rarebooks.app", removal_in=14, kind="Lapsed")
		self.assertEqual((case.kind, [row.offset_days for row in case.warnings]), ("Lapsed", [14]))

	def test_missed_warnings_send_only_the_latest_and_the_export_waits_24_hours(self):
		# covers: 0007 AC-4, AC-5
		self.site("late.rarebooks.app", owner_email=OWNER)
		case = self.tick("late.rarebooks.app")
		self.assertEqual([row.offset_days for row in case.warnings], [1])
		self.assertIn("very soon", email_text(case.warnings[0].email))
		self.age_warnings("late.rarebooks.app", hours=23)
		self.assertEqual(self.tick("late.rarebooks.app").status, "Scheduled")
		self.assertNotIn("start_export", self.sites.names())
		self.age_warnings("late.rarebooks.app", hours=2)
		self.assertEqual(self.tick("late.rarebooks.app").status, "Exporting")

	def test_a_swahili_site_gets_a_swahili_warning(self):
		# covers: 0007 AC-4
		self.site("sw.rarebooks.app", owner_email=OWNER)
		case = self.tick("sw.rarebooks.app", removal_in=4, language="sw")
		self.assertIn("itaondolewa", email_text(case.warnings[0].email))

	def test_paused_removal_exports_nothing(self):
		# covers: 0010 AC-9
		set_settings(removal_paused=1)
		self.site("paused.rarebooks.app")
		self.tick("paused.rarebooks.app")
		self.age_warnings("paused.rarebooks.app")
		self.assertEqual(self.tick("paused.rarebooks.app").status, "Scheduled")
		self.assertNotIn("start_export", self.sites.names())

	def test_cases_keep_track_changes_and_nobody_deletes_them(self):
		# covers: 0007 AC-17, 0011 AC-9
		meta = frappe.get_meta(CASE)
		self.assertTrue(meta.track_changes)
		self.assertFalse(any(perm.delete or perm.create or perm.write for perm in meta.permissions))
		self.assertFalse(meta.has_field("archive_job") or meta.has_field("bench"))
		self.assertTrue(meta.has_field("archive_requested_on") and meta.has_field("attempt_id"))
		self.assertFalse(frappe.get_meta(SITE).has_field("config_job"))
		self.assertFalse(frappe.db.exists("DocType", "Books Hosting Bench"))
		self.site("audit.rarebooks.app")
		name = self.tick("audit.rarebooks.app").name
		for user in (REMOVAL_MANAGER, SYSTEM_MANAGER, BILLING_MANAGER):
			with self.subTest(user=user):
				frappe.set_user(user)
				self.assertFalse(frappe.has_permission(CASE, "delete", name))
		frappe.set_user(REMOVAL_MANAGER)
		self.assertTrue(frappe.has_permission(CASE, "read", name))

	def test_a_site_has_at_most_one_open_case(self):
		self.site("one.rarebooks.app")
		case = self.tick("one.rarebooks.app")
		with self.assertRaises(frappe.ValidationError):
			frappe.copy_doc(case).insert(ignore_permissions=True)


class TestExport(RemovalTestCase):
	def test_start_export_gives_a_new_attempt_its_own_key_and_six_hour_link(self):
		# covers: 0010 AC-3
		self.site("export.rarebooks.app")
		case = self.to_exporting("export.rarebooks.app")
		self.assertEqual(case.status, "Exporting")
		self.assertTrue(case.attempt_id and case.export_claimed_on)
		self.assertEqual(case.export_key, f"exports/{case.name}/{case.attempt_id}.zip")
		self.upload_url.assert_called_once_with(case.export_key)
		[body] = self.sites.bodies("start_export")
		self.assertEqual(
			(body["case"], body["attempt_id"], body["upload_url"]), (case.name, case.attempt_id, UPLOAD_URL)
		)
		expires = removal.signing.parse_issued_at(body["expires_at"])
		self.assertAlmostEqual((expires - utc_now()).total_seconds(), 6 * 3600, delta=60)

	def test_at_most_three_exports_start_per_run(self):
		# covers: 0010 AC-3
		sites = [f"cap{number}.rarebooks.app" for number in range(4)]
		for site in sites:
			self.site(site)
			self.tick(site)
			self.age_warnings(site)
		statuses = [self.tick(site).status for site in sites]
		self.assertEqual(statuses, ["Exporting"] * 3 + ["Scheduled"])
		self.assertEqual(self.case(sites[3]).attempts, 0)

	def test_a_start_export_that_doesnt_get_through_gives_its_slot_back(self):
		# covers: 0010 AC-3
		self.site("down.rarebooks.app")
		self.site("next.rarebooks.app")
		for site in ("down.rarebooks.app", "next.rarebooks.app"):
			self.tick(site)
			self.age_warnings(site)
		self.sites.down.add("start_export")
		for _run in range(3):
			case = self.tick("down.rarebooks.app")
		self.assertEqual((case.status, case.attempts), ("Scheduled", 0))
		self.sites.down.clear()
		self.assertEqual(self.tick("next.rarebooks.app").status, "Exporting", "no slot was used up")

	def test_six_silent_hours_count_an_attempt_and_start_a_new_one(self):
		# covers: 0010 AC-3, 0007 AC-12
		self.site("silent.rarebooks.app")
		first = self.to_exporting("silent.rarebooks.app")
		self.tick("silent.rarebooks.app")
		self.assertEqual(len(self.sites.bodies("start_export")), 1)
		frappe.db.set_value(CASE, first.name, "export_claimed_on", utc_now() - timedelta(hours=6, minutes=1))
		case = self.tick("silent.rarebooks.app")
		self.assertEqual((case.status, case.attempts), ("Exporting", 1))
		self.assertNotEqual(case.attempt_id, first.attempt_id)
		self.storage_delete.assert_not_called()
		response, _data = self.export_ready(case, attempt_id=first.attempt_id)
		self.assertEqual(response.status_code, 409, "the old attempt is no longer the one waited for")

	def test_export_ready_checks_r2_then_queues_the_email(self):
		# covers: 0010 AC-4, 0007 AC-7, 0010 AC-8
		self.site("ready.rarebooks.app", owner_email=OWNER)
		case = self.to_exporting("ready.rarebooks.app")
		self.storage_size.return_value = EXPORT_BYTES - 1
		response, data = self.export_ready(case)
		self.assertEqual((response.status_code, data["status"]), (409, "size_mismatch"))
		self.storage_size.side_effect = RuntimeError("R2 timed out")
		self.assertEqual(self.export_ready(case)[0].status_code, 503)
		self.storage_size.side_effect = None
		self.storage_size.return_value = EXPORT_BYTES
		response, data = self.export_ready(case)
		self.assertEqual((response.status_code, data["status"]), (200, "Exporting"))
		case = self.case(case.site)
		self.assertEqual((case.export_bytes, case.export_sha256), (EXPORT_BYTES, DIGEST))
		self.assertEqual(str(case.delete_export_on), str(site_today() + timedelta(days=90)))
		email = frappe.get_doc("Email Queue", case.export_email)
		self.assertEqual(self.queued_to(OWNER)[-1], email.name)
		self.assertTrue(email.redact_message_after_send)
		text = email_text(email.name)
		self.assertNotIn(case.link_token_hash, text, "the email holds the token, not its hash")
		self.assertIn("the hosting provider may keep backups", text)
		self.storage_delete.assert_not_called()

	def test_export_ready_refuses_another_attempt_or_a_case_not_exporting(self):
		# covers: 0010 AC-4
		self.site("stray.rarebooks.app")
		case = self.tick("stray.rarebooks.app")
		self.assertEqual(self.export_ready(case, attempt_id="none")[0].status_code, 409)
		case = self.to_exporting(case.site)
		self.assertEqual(self.export_ready(case, attempt_id="another")[0].status_code, 409)
		for values in ({"bytes": 0}, {"bytes": "4096"}, {"sha256": "x"}, {"attempt_id": None}):
			with self.subTest(**values):
				self.assertEqual(self.export_ready(case, **values)[0].status_code, 400)
		self.storage_delete.assert_not_called()

	def test_a_repeated_export_ready_queues_no_second_email(self):
		# covers: 0007 AC-7
		self.site("repeat.rarebooks.app", owner_email=OWNER)
		case = self.to_exporting("repeat.rarebooks.app")
		self.export_ready(case)
		first = self.case(case.site).export_email
		response, _data = self.export_ready(case)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(self.case(case.site).export_email, first)

	def test_the_hold_starts_when_the_email_is_sent_and_the_site_is_marked(self):
		# covers: 0007 AC-8, 0010 AC-1
		self.site("hold.rarebooks.app")
		case = self.to_exporting("hold.rarebooks.app")
		self.export_ready(case)
		self.assertEqual(self.tick(case.site).status, "Exporting", "no hold before the email is sent")
		self.mark_email(self.case(case.site).export_email)
		case = self.tick(case.site)
		self.assertEqual(case.status, "Holding")
		self.assertEqual(case.hold_until - case.export_sent_on, timedelta(hours=72))
		self.assertEqual(case.link_expires_on - case.export_sent_on, timedelta(days=30))
		[mark] = self.sites.bodies("mark_export_sent")
		self.assertEqual(
			(mark["export_sent_on"], mark["removal_hold_until"]),
			(removal._iso(case.export_sent_on), removal._iso(case.hold_until)),
		)
		self.tick(case.site, **self.marked(case))
		self.assertEqual(len(self.sites.bodies("mark_export_sent")), 1, "a marked site isn't marked again")
		self.tick(case.site)
		self.assertEqual(len(self.sites.bodies("mark_export_sent")), 2, "an emptied site is marked again")

	def test_three_failed_steps_fail_the_case_and_alert_once(self):
		# covers: 0007 AC-12
		self.site("fail.rarebooks.app")
		case = self.to_exporting("fail.rarebooks.app")
		with patch("rarebooks_control.removal_mail.send_ops") as send_ops:
			for attempt in (1, 2, 3):
				_response, data = self.step_failed(self.case(case.site))
				if attempt < 3:
					self.assertEqual(data["status"], "Scheduled")
					self.assertEqual(self.tick(case.site).status, "Exporting")
			self.assertEqual(data["status"], "Failed")
			send_ops.assert_called_once()
		self.assertEqual(self.step_failed(self.case(case.site))[0].status_code, 409)
		response, _data = self.step_failed(self.case(case.site), step="drop")
		self.assertEqual(response.status_code, 400, "a site never reports a drop")

	def test_an_export_email_error_fails_and_retry_sends_it_again(self):
		# covers: 0007 AC-12, AC-13
		self.site("mailfail.rarebooks.app", owner_email=OWNER)
		case = self.to_exporting("mailfail.rarebooks.app")
		self.export_ready(case)
		first = self.case(case.site).export_email
		self.mark_email(first, "Error")
		self.assertEqual(self.tick(case.site).status, "Failed")
		frappe.set_user(REMOVAL_MANAGER)
		self.assertEqual(removal.retry_case(case.name), {"status": "Exporting"})
		frappe.set_user("Administrator")
		case = self.case(case.site)
		self.assertNotEqual(case.export_email, first)
		self.mark_email(case.export_email)
		self.assertEqual(self.tick(case.site).status, "Holding")

	def test_an_email_unsent_after_24_hours_fails(self):
		# covers: 0007 AC-12
		self.site("slowmail.rarebooks.app")
		case = self.to_exporting("slowmail.rarebooks.app")
		self.export_ready(case)
		frappe.db.set_value(CASE, case.name, "export_queued_on", utc_now() - timedelta(hours=24))
		self.assertEqual(self.tick(case.site).status, "Failed")

	def test_download_redirects_counts_and_expires(self):
		# covers: 0007 AC-7, AC-16, AC-17
		self.site("dl.rarebooks.app")
		case = self.to_exporting("dl.rarebooks.app")
		with patch("rarebooks_control.removal.secrets.token_urlsafe", return_value="the-token"):
			self.export_ready(case)
		case = self.case("dl.rarebooks.app")
		self.assertIn("rarebooks_control.removal.download?token=the-token", email_text(case.export_email))
		response = get(removal.download, token="the-token")
		self.assertEqual(response.status_code, 404, "the link works once the email is sent")
		self.mark_email(case.export_email)
		self.tick(case.site)
		response = get(removal.download, token="the-token")
		self.assertEqual(response.status_code, 302)
		self.assertTrue(response.headers["Location"].startswith("https://r2.example.com/"))
		self.assertEqual(self.case(case.site).download_count, 1)
		self.assertEqual(get(removal.download, token="wrong").status_code, 404)
		frappe.db.set_value(CASE, case.name, "link_expires_on", utc_now() - timedelta(seconds=1))
		self.assertEqual(get(removal.download, token="the-token").status_code, 404)


class TestDrop(RemovalTestCase):
	def test_happy_path_commits_dropping_archives_marks_then_removes(self):
		# covers: 0010 AC-6, AC-7, 0011 AC-1, AC-2
		self.site("happy.rarebooks.app")
		case = self.to_drop_due("happy.rarebooks.app")
		self.sites.calls.clear()
		case = self.drop(case.site)
		self.assertEqual(self.sites.names(), ["removal_report", "removal_report"], "a fresh report first")
		self.assertEqual(self.cloud.calls, [("get", case.site), ("archive", case.site)])
		self.assertEqual(case.status, "Dropping")
		self.assertTrue(case.drop_started_on and case.archive_requested_on)
		self.assertEqual(case.attempts, 0)

		# The archive runs: Frappe Cloud shows Pending. The next poll waits, and calls nothing on the site.
		self.sites.calls.clear()
		self.assertEqual(self.drop(case.site).status, "Dropping")
		self.assertEqual((self.cloud.archives(), self.sites.calls), (1, []))
		self.cloud.sites[case.site]["status"] = "Archived"
		case = self.drop(case.site)
		self.assertEqual(case.status, "Removed")
		row = frappe.get_doc(SITE, case.site)
		self.assertEqual(row.status, "Removed")
		self.assertTrue(row.removed_on and case.removed_on)
		self.assertEqual(self.cloud.archives(), 1)

	def test_dropping_is_committed_before_the_archive_call(self):
		# covers: 0010 AC-6
		self.site("order.rarebooks.app")
		case = self.to_drop_due("order.rarebooks.app")
		seen = []

		def archive(name):
			seen.append(frappe.db.sql(f"select status from `tab{CASE}` where name=%s", case.name)[0][0])
			self.assertFalse(frappe.db.transaction_writes, "the Dropping row is committed")

		with patch("rarebooks_control.providers.frappe_cloud.archive", side_effect=archive):
			self.drop(case.site)
		self.assertEqual(seen, ["Dropping"])

	def test_each_gate_and_guard_stops_the_archive(self):
		# covers: 0007 AC-9, AC-20, 0010 AC-6, 0011 AC-3
		gates = {
			"not approved": lambda case: frappe.db.set_value(CASE, case.name, "approved_by", None),
			"hold not over": lambda case: frappe.db.set_value(
				CASE, case.name, "hold_until", utc_now() + timedelta(hours=1)
			),
			"paused": lambda case: set_settings(removal_paused=1),
			"paid": lambda case: frappe.db.set_value(
				SITE, case.site, "paid_until", site_today() + timedelta(days=30)
			),
			"auto renews": lambda case: frappe.db.set_value(SITE, case.site, "auto_renews", 1),
			"export gone": lambda case: setattr(self.storage_size, "return_value", None),
			"other bench group": lambda case: self.cloud.sites[case.site].update(group="other-group"),
			"empty bench group": lambda case: self.cloud.sites[case.site].update(group=None),
			"suspended": lambda case: self.cloud.sites[case.site].update(status="Suspended"),
			"broken": lambda case: self.cloud.sites[case.site].update(status="Broken"),
			"another name": lambda case: self.cloud.sites[case.site].update(name="other.rarebooks.app"),
			"settings group empty": lambda case: frappe.db.set_single_value(
				SETTINGS, "books_bench_group", ""
			),
		}
		report_gates = {
			"payment pending": {"payment_pending": True},
			"active phase": {"phase": "active"},
			"not due": {"removal_in": 1},
		}
		# Without any change the same steps drop, so each refusal below is the gate's own.
		self.site("gate-control.rarebooks.app")
		self.assertEqual(self.drop_after(self.to_drop_due("gate-control.rarebooks.app")).status, "Dropping")
		for name, change in [*gates.items(), *((name, None) for name in report_gates)]:
			with self.subTest(name):
				self.cloud.calls.clear()
				frappe.cache.delete_keys("removal:exports:")
				site = f"gate-{len(self.cloud.sites)}.rarebooks.app"
				self.site(site)
				case = self.to_drop_due(site)
				self.assertEqual(case.status, "Holding")
				if change:
					change(case)
				values = {**self.marked(self.case(site)), **report_gates.get(name, {})}
				case = self.tick(site, **values)
				self.assertNotIn("archive", [call[0] for call in self.cloud.calls])
				self.assertNotEqual(case.status, "Dropping")
				set_settings()
				self.storage_size.return_value = EXPORT_BYTES

	def test_the_control_site_itself_is_never_archived(self):
		# covers: 0010 AC-6
		site = frappe.local.site
		self.site(site)
		case = self.to_drop_due(site)
		self.drop(site)
		self.assertEqual(self.cloud.archives(), 0)
		self.assertEqual(self.case(case.site).status, "Holding")

	def test_dry_run_only_logs_the_drop(self):
		# covers: 0010 AC-1, 0007 AC-14
		set_settings(mode="dry_run")
		self.site("rehearse.rarebooks.app", owner_email=OWNER)
		case = self.to_drop_due("rehearse.rarebooks.app")
		self.assertEqual(case.mode, "dry_run")
		self.assertEqual(self.queued_to(OWNER), [], "no owner email in a dry run")
		self.assertIn(OWNER, email_text(case.warnings[0].email))
		self.drop(case.site)
		self.assertEqual((self.cloud.calls, self.case(case.site).status), ([], "Holding"))
		self.assertFalse({"mark_export_sent", "clear_removal"} & set(self.sites.names()))

	def test_a_timeout_after_sending_counts_as_requested_and_an_error_doesnt(self):
		# covers: 0011 AC-1
		self.site("timeout.rarebooks.app")
		self.site("error.rarebooks.app")
		self.to_drop_due("timeout.rarebooks.app")
		self.to_drop_due("error.rarebooks.app")
		self.cloud.errors["archive"] = OutcomeUnknown("press.api.site.archive sent, no answer")
		case = self.drop("timeout.rarebooks.app")
		self.assertTrue(case.archive_requested_on)
		self.assertEqual(case.attempts, 0)
		self.cloud.errors["archive"] = FrappeCloudError("press.api.site.archive answered HTTP 500")
		case = self.drop("error.rarebooks.app")
		self.assertEqual((case.status, case.archive_requested_on, case.attempts), ("Dropping", None, 1))
		self.assertIn("HTTP 500", case.last_error)

	def test_three_archive_errors_fail_the_case(self):
		# covers: 0010 AC-7, AC-13
		self.site("errors.rarebooks.app")
		self.to_drop_due("errors.rarebooks.app")
		self.cloud.errors["archive"] = FrappeCloudError("press.api.site.archive answered HTTP 500")
		with patch("rarebooks_control.removal_mail.send_ops") as send_ops:
			for _run in range(3):
				case = self.drop("errors.rarebooks.app")
		self.assertEqual((case.status, case.attempts), ("Failed", 3))
		send_ops.assert_called_once()
		self.assertEqual(self.cloud.archives(), 3)
		self.assertNotIn("token", case.last_error)

	def test_a_frappe_cloud_read_error_while_dropping_counts_and_fails_on_the_third(self):
		# covers: 0011 AC-2
		self.site("readerr.rarebooks.app")
		self.to_drop_due("readerr.rarebooks.app")
		self.drop("readerr.rarebooks.app")
		self.cloud.errors["get"] = FrappeCloudError("press.api.site.get could not be reached")
		with patch("rarebooks_control.removal_mail.send_ops"):
			statuses = [self.drop("readerr.rarebooks.app").status for _run in range(3)]
		self.assertEqual(statuses, ["Dropping", "Dropping", "Failed"])

	def test_the_mark_save_is_retried_and_the_archive_called_once(self):
		# covers: 0011 AC-1
		self.site("mark.rarebooks.app")
		self.to_drop_due("mark.rarebooks.app")
		real, tries = removal._write_archive_mark, []

		def flaky(name, when):
			tries.append(when)
			if len(tries) < 3:
				raise RuntimeError("lost")
			real(name, when)

		with patch.object(removal, "_write_archive_mark", side_effect=flaky):
			case = self.drop("mark.rarebooks.app")
		self.assertEqual(len(tries), 3)
		self.assertTrue(case.archive_requested_on)
		self.assertEqual(self.cloud.archives(), 1)

	def test_a_lost_mark_calls_again_only_while_frappe_cloud_shows_active(self):
		# covers: 0011 AC-1, AC-2
		self.site("lost.rarebooks.app")
		self.to_drop_due("lost.rarebooks.app")
		with patch.object(removal, "_write_archive_mark", side_effect=RuntimeError("lost")):
			case = self.drop("lost.rarebooks.app")
		self.assertEqual((case.status, case.archive_requested_on), ("Dropping", None))
		self.assertTrue(frappe.db.exists("Error Log", {"method": ("like", "%couldn't be marked%")}))
		# Frappe Cloud took the first call: Pending waits, no second call.
		self.drop("lost.rarebooks.app")
		self.assertEqual(self.cloud.archives(), 1)
		# A crash before the call (the site is still Active): every guard again, then one more call.
		self.cloud.sites["lost.rarebooks.app"]["status"] = "Active"
		self.cloud.sites["lost.rarebooks.app"]["group"] = "other-group"
		self.drop("lost.rarebooks.app")
		self.assertEqual(self.cloud.archives(), 1, "the guards run again")
		self.cloud.sites["lost.rarebooks.app"]["group"] = FakeFrappeCloud.GROUP
		case = self.drop("lost.rarebooks.app")
		self.assertEqual(self.cloud.archives(), 2)
		self.assertTrue(case.archive_requested_on)

	def test_a_marked_case_never_calls_again_and_alerts_once_after_2_hours(self):
		# covers: 0011 AC-2, 0007 AC-21
		self.site("stuck.rarebooks.app")
		self.to_drop_due("stuck.rarebooks.app")
		case = self.drop("stuck.rarebooks.app")
		frappe.db.set_value(CASE, case.name, "drop_started_on", utc_now() - timedelta(hours=3))
		for cloud_status in ("Broken", "Active"):
			self.cloud.sites[case.site]["status"] = cloud_status
			self.assertEqual(self.drop(case.site).status, "Dropping")
		self.assertEqual(self.cloud.archives(), 1, "a set mark only waits, even with the site Active")
		with patch("rarebooks_control.removal_mail.send_ops") as send_ops:
			removal_mail.hourly()
			removal_mail.hourly()
		[alert] = send_ops.call_args_list
		self.assertIn(case.site, alert.args[0])

	def test_a_dropping_case_is_polled_whatever_the_config_status(self):
		# covers: 0011 AC-10
		self.site("config.rarebooks.app")
		self.to_drop_due("config.rarebooks.app")
		case = self.drop("config.rarebooks.app")
		frappe.db.set_value(SITE, case.site, "config_status", "Failed")
		self.cloud.sites[case.site]["status"] = "Archived"
		self.sites.calls.clear()
		removal.run_site(case.site)
		self.assertEqual(self.case(case.site).status, "Removed")
		self.assertEqual(self.sites.calls, [], "only Frappe Cloud is asked")

	def test_a_site_already_archived_goes_straight_to_removed(self):
		# covers: 0010 AC-7
		self.site("already.rarebooks.app")
		self.to_drop_due("already.rarebooks.app")
		self.cloud.sites["already.rarebooks.app"]["status"] = "Archived"
		self.assertEqual(self.drop("already.rarebooks.app").status, "Removed")
		self.assertEqual(self.cloud.archives(), 0)

	def test_the_daily_cap_counts_drops_across_sites(self):
		# covers: 0010 AC-6, AC-9
		set_settings(max_drops_per_day=5)
		sites = [f"day{number}.rarebooks.app" for number in range(6)]
		for site in sites:
			self.site(site)
			self.to_drop_due(site)
			frappe.cache.delete_keys("removal:exports:")
		statuses = [self.drop(site).status for site in sites]
		self.assertEqual(statuses, ["Dropping"] * 5 + ["Holding"])
		self.assertEqual(self.cloud.archives(), 5)

	def test_retry_of_a_failed_drop_goes_back_to_the_poll(self):
		# covers: 0007 AC-13, 0011 AC-2
		self.site("retry.rarebooks.app")
		self.to_drop_due("retry.rarebooks.app")
		self.cloud.errors["archive"] = FrappeCloudError("press.api.site.archive answered HTTP 500")
		with patch("rarebooks_control.removal_mail.send_ops"):
			for _run in range(3):
				case = self.drop("retry.rarebooks.app")
		self.assertEqual(case.status, "Failed")
		frappe.set_user(REMOVAL_MANAGER)
		self.assertEqual(removal.retry_case(case.name), {"status": "Dropping"})
		frappe.set_user("Administrator")
		del self.cloud.errors["archive"]
		case = self.drop("retry.rarebooks.app")
		self.assertTrue(case.archive_requested_on)
		self.assertEqual(case.attempts, 0)

	def test_the_digest_removes_a_failed_drop_that_frappe_cloud_finished(self):
		# covers: 0011 AC-11, 0010 AC-16
		self.site("late.rarebooks.app")
		self.site("waiting.rarebooks.app", config_status="Pending")
		self.to_drop_due("late.rarebooks.app")
		self.cloud.errors["archive"] = FrappeCloudError("press.api.site.archive answered HTTP 500")
		with patch("rarebooks_control.removal_mail.send_ops"):
			for _run in range(3):
				case = self.drop("late.rarebooks.app")
		self.assertEqual(case.status, "Failed")
		self.cloud.sites[case.site]["status"] = "Archived"
		frappe.db.set_single_value(SETTINGS, "last_run_on", utc_now())
		with patch("rarebooks_control.removal_mail.send_ops") as send_ops:
			removal_mail.send_digest()
		self.assertEqual(self.case(case.site).status, "Removed")
		self.assertEqual(frappe.db.get_value(SITE, case.site, "status"), "Removed")
		lines = "\n".join(send_ops.call_args.args[1])
		self.assertIn("archived after failing", lines)
		self.assertIn("Last removal run:", lines)
		self.assertIn("waiting.rarebooks.app: Pending", lines)


class TestCancel(RemovalTestCase):
	def test_a_payment_while_holding_cancels_deletes_and_clears(self):
		# covers: 0007 AC-11, 0010 AC-1
		self.site("late-pay.rarebooks.app")
		case = self.to_holding("late-pay.rarebooks.app")
		frappe.db.set_value(SITE, case.site, "paid_until", site_today() + timedelta(days=365))
		self.sites.calls.clear()
		self.tick(case.site, **self.marked(case))
		self.assertEqual(self.sites.names(), ["removal_report", "clear_removal"])
		case = self.case(case.site)
		self.assertEqual(case.status, "Cancelled")
		self.assertFalse(case.link_token_hash)
		self.storage_delete.assert_called_once_with(case.export_key)

	def test_the_sites_own_report_of_payment_cancels_too(self):
		# covers: 0007 AC-11
		self.site("self-pay.rarebooks.app")
		self.tick("self-pay.rarebooks.app")
		case = self.tick("self-pay.rarebooks.app", removal_in=None, phase="active")
		self.assertEqual(case.status, "Cancelled")
		self.tick("self-pay.rarebooks.app")
		self.assertEqual(frappe.db.count(CASE, {"site": "self-pay.rarebooks.app"}), 2)

	def test_create_checkout_refuses_while_being_removed(self):
		# covers: 0007 AC-11
		self.site("freeze.rarebooks.app")
		case = self.to_drop_due("freeze.rarebooks.app")
		frappe.db.set_value(CASE, case.name, "hold_until", utc_now() + timedelta(hours=2))
		self.assertFalse(removal.is_being_removed(case.site))
		frappe.db.set_value(CASE, case.name, "hold_until", utc_now() + timedelta(minutes=59))
		self.assertTrue(removal.is_being_removed(case.site))
		row = frappe.get_doc(SITE, case.site)
		body = {
			"site": case.site,
			"issued_at": issued(),
			"provider": "ClickPesa",
			"billing_period": "Yearly",
			"return_url": f"https://{case.site}/books/subscription",
		}
		response = call(api.create_checkout, body, row.get_password("control_secret"))
		self.assertEqual(response.status_code, 409)
		self.assertEqual(answer(response)["refusal"], "being_removed")
		frappe.db.set_value(CASE, case.name, "status", "Dropping")
		self.assertTrue(billing.is_removed(row), "a payment for a dropping site is not applied")


class TestActions(RemovalTestCase):
	def test_only_a_removal_manager_runs_the_actions(self):
		# covers: 0007 AC-13
		self.site("roles.rarebooks.app")
		case = self.to_drop_due("roles.rarebooks.app", approve=False)
		for user in (SYSTEM_MANAGER, BILLING_MANAGER):
			frappe.set_user(user)
			for action, args in (
				(removal.approve_case, {}),
				(removal.pause_case, {"reason": "check"}),
				(removal.resume_case, {}),
				(removal.retry_case, {}),
			):
				with self.subTest(user=user, action=action.__name__):
					with self.assertRaises(frappe.PermissionError):
						action(case.name, **args)

	def test_pause_resume_and_wrong_statuses(self):
		# covers: 0007 AC-13
		self.site("pause.rarebooks.app")
		case = self.to_drop_due("pause.rarebooks.app", approve=False)
		frappe.set_user(REMOVAL_MANAGER)
		with self.assertRaises(frappe.ValidationError):
			removal.pause_case(case.name, reason="")
		self.assertEqual(removal.pause_case(case.name, reason="Owner called")["status"], "Paused")
		with self.assertRaises(frappe.ValidationError):
			removal.approve_case(case.name)
		frappe.set_user("Administrator")
		self.drop(case.site)
		self.assertEqual(self.cloud.archives(), 0)
		frappe.set_user(REMOVAL_MANAGER)
		self.assertEqual(removal.resume_case(case.name)["status"], "Holding")
		removal.approve_case(case.name)
		self.assertEqual(self.case(case.site).approved_by, REMOVAL_MANAGER)
		with self.assertRaises(frappe.ValidationError):
			removal.retry_case(case.name)

	def test_resume_of_a_case_paused_before_export_goes_back_to_scheduled(self):
		# covers: 0007 AC-13
		self.site("early.rarebooks.app")
		case = self.tick("early.rarebooks.app", removal_in=10)
		frappe.set_user(REMOVAL_MANAGER)
		removal.pause_case(case.name, reason="wait")
		self.assertEqual(removal.resume_case(case.name)["status"], "Scheduled")


class TestAlerts(RemovalTestCase):
	def test_an_alert_the_mail_server_refuses_never_fails_the_commit(self):
		# covers: 0007 AC-12, AC-21
		from rarebooks_control.alerts import send_ops

		frappe.db.after_commit.reset()
		before = frappe.db.count("Email Queue")
		with patch(
			"frappe.email.doctype.email_queue.email_queue.EmailQueue.send",
			side_effect=frappe.ValidationError("Connection refused"),
		) as send:
			send_ops("Removal case RC-0 failed", ["line"])
			frappe.db.after_commit.run()
		send.assert_called_once()
		self.assertEqual(frappe.db.count("Email Queue"), before + 1, "it stays queued for the scheduler")

	def test_daily_lists_unapproved_holds(self):
		# covers: 0007 AC-21
		self.site("wait.rarebooks.app")
		self.to_holding("wait.rarebooks.app")
		with patch("rarebooks_control.removal_mail.send_ops") as send_ops:
			removal_mail.daily()
		subjects = [c.args[0] for c in send_ops.call_args_list]
		self.assertIn("1 removals wait for your approval", subjects)

	def test_a_stale_run_alerts_once_a_day(self):
		# covers: 0010 AC-16
		frappe.db.set_single_value(SETTINGS, "last_run_on", utc_now() - timedelta(hours=2))
		with patch("rarebooks_control.removal_mail.send_ops") as send_ops:
			removal_mail.alert_stale_run()
			send_ops.assert_not_called()
			frappe.db.set_single_value(SETTINGS, "last_run_on", utc_now() - timedelta(hours=4))
			removal_mail.alert_stale_run()
			removal_mail.alert_stale_run()
		send_ops.assert_called_once()

	def test_requests_errors_never_leak_the_upload_url(self):
		# covers: 0010 AC-13
		self.site("leak.rarebooks.app")
		self.tick("leak.rarebooks.app")
		self.age_warnings("leak.rarebooks.app")
		with patch(
			"rarebooks_control.site_calls.post", side_effect=requests.ConnectionError(f"failed {UPLOAD_URL}")
		):
			removal.start_export(self.case("leak.rarebooks.app").name)
		self.assertNotIn("X-Amz-Signature", self.case("leak.rarebooks.app").last_error or "")
