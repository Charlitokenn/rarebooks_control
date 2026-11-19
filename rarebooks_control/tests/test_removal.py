"""Spec 0007 Milestone 4: the control site's removal side. The runner's signed calls (`report_sites`,
`export_ready`, `step_failed`, `drop_starting`, `drop_done`), the warnings and export email, the download link,
the drop gates, the Removal Manager actions and the operator alerts. R2 is patched in `export_storage`.

Times are moved by back dating stored timestamps, never the clock, so every request stays fresh (AC-15).
"""

from datetime import timedelta
from email import message_from_string
from email.policy import default as default_policy
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from rarebooks_control import api, billing, removal, removal_mail
from rarebooks_control.signing import utc_now
from rarebooks_control.tests.control import (
	RUNNER_SECRET,
	answer,
	call,
	get,
	issued,
	reset_billing,
)
from rarebooks_control.tests.data_model import (
	BENCH,
	BILLING_MANAGER,
	REMOVAL_MANAGER,
	SYSTEM_MANAGER,
	make_bench,
	make_outgoing_email_account,
	make_role_users,
	make_site,
)

CASE = "Books Removal Case"
SITE = "Books Hosted Site"
OPS = "ops@example.com"
OWNER = "owner@example.com"
OTHER_BENCH = "bench-test-2"
OTHER_SECRET = "other-runner-secret-0123456789abcdef0123456789abcdef"
EXPORT_BYTES = 4096
DIGEST = "a" * 64
UUID = "0b7c1f3e-8d2a-4c5b-9e6f-1a2b3c4d5e6f"


def email_text(name: str) -> str:
	"""The plain text part of a queued email, decoded."""
	mail = message_from_string(frappe.db.get_value("Email Queue", name, "message"), policy=default_policy)
	return mail.get_body(preferencelist=("plain",)).get_content()


def site_today():
	return utc_now().date()


def status(site: str, removal_in: int | None = 0, kind: str | None = "Trial", **values) -> dict:
	"""A `removal_status()` entry, for a site whose removal date is `removal_in` days after its today."""
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
		make_bench()
		make_bench(OTHER_BENCH)
		frappe.db.set_value("Books Hosting Bench", BENCH, {"removal_paused": 0, "last_report_on": None})
		self.start(
			patch.dict(
				frappe.conf,
				{
					"books_runner_secrets": {BENCH: RUNNER_SECRET, OTHER_BENCH: OTHER_SECRET},
					"books_ops_email": OPS,
					"books_removal_require_approval": True,
					"books_removal_max_drops_per_day": 5,
				},
			)
		)
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
		self.queue_push = self.start(patch("rarebooks_control.push.queue_push"))

	def tearDown(self):
		frappe.set_user("Administrator")

	def start(self, patcher):
		mocked = patcher.start()
		self.addCleanup(patcher.stop)
		return mocked

	# Helpers

	def runner(self, method, mode: str = "live", bench: str = BENCH, secret: str = RUNNER_SECRET, **body):
		payload = {"bench": bench, "issued_at": issued(), **body}
		if method is removal.report_sites:
			payload.setdefault("mode", mode)
			payload.setdefault("deferred", [])
		response = call(method, payload, secret)
		return response, (answer(response, secret) if response.status_code != 401 else None)

	def report(self, *sites, mode: str = "live", deferred=()) -> list[dict]:
		response, data = self.runner(removal.report_sites, mode, sites=list(sites), deferred=list(deferred))
		self.assertEqual(response.status_code, 200)
		return data["instructions"]

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

	def to_exporting(self, site: str, mode: str = "live") -> list[dict]:
		"""Open a case due today, its warning 25 hours old, and claim the export."""
		self.report(status(site), mode=mode)
		self.age_warnings(site)
		return self.report(status(site), mode=mode)

	def to_holding(self, site: str, mode: str = "live") -> list[dict]:
		self.to_exporting(site, mode)
		case = self.case(site)
		self.export_ready(case)
		self.mark_email(self.case(site).export_email)
		return self.report(status(site), mode=mode)

	def export_ready(self, case, key: str | None = None):
		return self.runner(
			removal.export_ready,
			case=case.name,
			site=case.site,
			key=key or f"exports/{case.name}/{UUID}.zip",
			bytes=EXPORT_BYTES,
			sha256=DIGEST,
		)

	def to_drop_due(self, site: str, approve: bool = True):
		self.to_holding(site)
		case = self.case(site)
		frappe.db.set_value(CASE, case.name, "hold_until", utc_now() - timedelta(minutes=1))
		if approve:
			frappe.db.set_value(CASE, case.name, {"approved_by": REMOVAL_MANAGER, "approved_on": utc_now()})
		return self.case(site)

	def drop_starting(self, case):
		_response, data = self.runner(removal.drop_starting, case=case.name, site=case.site)
		return data

	def actions(self, instructions, action: str) -> list[dict]:
		return [item for item in instructions if item["action"] == action]

	def queued_to(self, recipient: str) -> list:
		return frappe.get_all(
			"Email Queue Recipient", filters={"recipient": recipient}, pluck="parent", order_by="creation asc"
		)


class TestRunnerAuth(RemovalTestCase):
	def test_bad_signature_unknown_bench_and_stale_issued_at_get_401(self):
		# covers: AC-15
		make_site("auth.rarebooks.app")
		body = {"mode": "live", "sites": [status("auth.rarebooks.app")], "deferred": []}
		cases = {
			"bad signature": ({"bench": BENCH, "issued_at": issued(), **body}, "wrong-secret"),
			"unknown bench": ({"bench": "nobody", "issued_at": issued(), **body}, RUNNER_SECRET),
			"stale": ({"bench": BENCH, "issued_at": issued(-timedelta(minutes=6)), **body}, RUNNER_SECRET),
			"site secret": ({"bench": BENCH, "issued_at": issued(), **body}, "f" * 64),
		}
		for label, (payload, secret) in cases.items():
			with self.subTest(label):
				self.assertEqual(call(removal.report_sites, payload, secret).status_code, 401)
		self.assertIsNone(self.case("auth.rarebooks.app"))

	def test_answers_are_signed_and_echo_issued_at(self):
		# covers: AC-15
		payload = {"bench": BENCH, "issued_at": issued(), "mode": "live", "sites": [], "deferred": []}
		data = answer(call(removal.report_sites, payload, RUNNER_SECRET), RUNNER_SECRET)
		self.assertEqual(data, {"issued_at": payload["issued_at"], "instructions": []})
		bench = frappe.get_doc("Books Hosting Bench", BENCH)
		self.assertEqual(bench.last_mode, "live")
		self.assertTrue(bench.last_report_on)

	def test_a_site_on_another_bench_and_a_bad_entry_are_skipped_not_fatal(self):
		# covers: AC-15
		make_site("ours.rarebooks.app")
		make_site("theirs.rarebooks.app", hosting_bench=OTHER_BENCH)
		self.report(
			status("theirs.rarebooks.app"),
			"not a dict",
			{"site": "ours.rarebooks.app", "today": "not a date"},
			status("ours.rarebooks.app"),
		)
		self.assertIsNone(self.case("theirs.rarebooks.app"))
		self.assertEqual(self.case("ours.rarebooks.app").status, "Scheduled")

	def test_runner_calls_are_limited_per_bench(self):
		# covers: AC-15
		with patch("rarebooks_control.removal.signing.over_limit", return_value=True):
			response, _data = self.runner(removal.report_sites, sites=[])
		self.assertEqual(response.status_code, 429)


class TestCasesAndWarnings(RemovalTestCase):
	def test_a_report_with_a_removal_date_opens_one_case_from_the_registry(self):
		# covers: AC-1, AC-4
		make_site("open.rarebooks.app", owner_email=OWNER)
		self.report(status("open.rarebooks.app", removal_in=10))
		case = self.case("open.rarebooks.app")
		self.assertEqual(
			(case.status, case.mode, case.kind, case.bench, case.owner_email),
			("Scheduled", "live", "Trial", BENCH, OWNER),
		)
		self.assertEqual(str(case.removal_on), str(site_today() + timedelta(days=10)))
		self.assertEqual(case.warnings, [], "no warning is due 10 days out")
		self.report(status("open.rarebooks.app", removal_in=10))
		self.assertEqual(frappe.db.count(CASE, {"site": "open.rarebooks.app"}), 1)

	def test_no_removal_date_or_exempt_opens_no_case(self):
		# covers: AC-1
		make_site("paid.rarebooks.app")
		make_site("exempt.rarebooks.app")
		self.report(
			status("paid.rarebooks.app", removal_in=None, phase="active"),
			status("exempt.rarebooks.app", exempt=True),
		)
		self.assertIsNone(self.case("paid.rarebooks.app"))
		self.assertIsNone(self.case("exempt.rarebooks.app"))

	def test_removal_on_moves_only_later_and_freezes_after_scheduled(self):
		# covers: AC-1
		make_site("later.rarebooks.app")
		self.report(status("later.rarebooks.app", removal_in=10))
		self.report(status("later.rarebooks.app", removal_in=12))
		self.assertEqual(
			str(self.case("later.rarebooks.app").removal_on), str(site_today() + timedelta(days=12))
		)
		self.report(status("later.rarebooks.app", removal_in=11))
		self.assertEqual(
			str(self.case("later.rarebooks.app").removal_on), str(site_today() + timedelta(days=12))
		)

	def test_trial_warnings_go_4_2_and_1_days_before_once_each(self):
		# covers: AC-4
		make_site("warn.rarebooks.app", owner_email=OWNER)
		self.report(status("warn.rarebooks.app", removal_in=5))
		name = self.case("warn.rarebooks.app").name
		for removal_in, expected in ((5, []), (4, [4]), (4, [4]), (2, [4, 2]), (1, [4, 2, 1])):
			# Days pass on the site: its today moves on, so the stored date is moved to match.
			frappe.db.set_value(CASE, name, "removal_on", site_today() + timedelta(days=removal_in))
			self.report(status("warn.rarebooks.app", removal_in=removal_in))
			case = self.case("warn.rarebooks.app")
			self.assertEqual([row.offset_days for row in case.warnings], expected)
		self.assertEqual(len(self.queued_to(OWNER)), 3)
		text = email_text(self.queued_to(OWNER)[0])
		self.assertIn("https://warn.rarebooks.app/books/subscription", text)
		self.assertIn("Duka la Vitabu", text)

	def test_lapsed_warnings_use_14_3_and_1(self):
		# covers: AC-4
		self.assertEqual(removal.WARNING_OFFSETS["Lapsed"], (14, 3, 1))
		make_site("lapsed.rarebooks.app")
		self.report(status("lapsed.rarebooks.app", removal_in=14, kind="Lapsed"))
		case = self.case("lapsed.rarebooks.app")
		self.assertEqual((case.kind, [row.offset_days for row in case.warnings]), ("Lapsed", [14]))

	def test_missed_warnings_send_only_the_latest_and_the_export_waits_24_hours(self):
		# covers: AC-4, AC-5
		make_site("late.rarebooks.app", owner_email=OWNER)
		instructions = self.report(status("late.rarebooks.app", removal_in=0))
		case = self.case("late.rarebooks.app")
		self.assertEqual([row.offset_days for row in case.warnings], [1])
		self.assertEqual(
			self.actions(instructions, "export"), [], "no export before a warning is 24 hours old"
		)
		self.assertIn("very soon", email_text(case.warnings[0].email))

		self.age_warnings("late.rarebooks.app", hours=23)
		self.assertEqual(self.actions(self.report(status("late.rarebooks.app")), "export"), [])
		self.age_warnings("late.rarebooks.app", hours=2)
		[export] = self.actions(self.report(status("late.rarebooks.app")), "export")
		self.assertEqual(export["case"], case.name)
		case = self.case("late.rarebooks.app")
		self.assertEqual(case.status, "Exporting")
		self.assertTrue(case.export_claimed_on)

	def test_a_swahili_site_gets_a_swahili_warning(self):
		# covers: AC-4
		make_site("sw.rarebooks.app", owner_email=OWNER)
		self.report(status("sw.rarebooks.app", removal_in=4, language="sw"))
		self.assertIn("itaondolewa", email_text(self.case("sw.rarebooks.app").warnings[0].email))

	def test_a_paused_bench_exports_nothing(self):
		# covers: AC-20
		make_site("paused.rarebooks.app")
		frappe.db.set_value("Books Hosting Bench", BENCH, "removal_paused", 1)
		self.report(status("paused.rarebooks.app"))
		self.age_warnings("paused.rarebooks.app")
		self.assertEqual(self.actions(self.report(status("paused.rarebooks.app")), "export"), [])

	def test_cases_keep_track_changes_and_nobody_deletes_them(self):
		# covers: AC-17
		meta = frappe.get_meta(CASE)
		self.assertTrue(meta.track_changes)
		self.assertFalse(any(perm.delete or perm.create or perm.write for perm in meta.permissions))
		make_site("audit.rarebooks.app")
		self.report(status("audit.rarebooks.app"))
		name = self.case("audit.rarebooks.app").name
		for user in (REMOVAL_MANAGER, SYSTEM_MANAGER, BILLING_MANAGER):
			with self.subTest(user=user):
				frappe.set_user(user)
				self.assertFalse(frappe.has_permission(CASE, "delete", name))
		frappe.set_user(REMOVAL_MANAGER)
		self.assertTrue(frappe.has_permission(CASE, "read", name))

	def test_a_site_has_at_most_one_open_case(self):
		make_site("one.rarebooks.app")
		self.report(status("one.rarebooks.app"))
		case = self.case("one.rarebooks.app")
		copy = frappe.copy_doc(case)
		with self.assertRaises(frappe.ValidationError):
			copy.insert(ignore_permissions=True)


class TestExportAndHold(RemovalTestCase):
	def test_happy_path_export_hold_approve_drop_and_remove(self):
		# covers: AC-5, AC-7, AC-8, AC-9, AC-10, AC-20
		make_site("happy.rarebooks.app", owner_email=OWNER)
		self.to_exporting("happy.rarebooks.app")
		case = self.case("happy.rarebooks.app")
		response, data = self.export_ready(case)
		self.assertEqual((response.status_code, data["status"]), (200, "Exporting"))
		case = self.case("happy.rarebooks.app")
		self.assertEqual((case.export_bytes, case.export_sha256), (EXPORT_BYTES, DIGEST))
		self.assertEqual(str(case.delete_export_on), str(site_today() + timedelta(days=90)))
		email = frappe.get_doc("Email Queue", case.export_email)
		self.assertEqual(self.queued_to(OWNER)[-1], email.name)
		self.assertTrue(email.redact_message_after_send)
		self.assertNotIn(
			case.link_token_hash, email_text(email.name), "the email holds the token, not its hash"
		)

		self.assertEqual(self.report(status("happy.rarebooks.app")), [], "no hold before the email is sent")
		self.mark_email(case.export_email)
		[mark] = self.report(status("happy.rarebooks.app"))
		case = self.case("happy.rarebooks.app")
		self.assertEqual(case.status, "Holding")
		self.assertEqual(case.hold_until - case.export_sent_on, timedelta(hours=72))
		self.assertEqual(case.link_expires_on - case.export_sent_on, timedelta(days=30))
		self.assertEqual(
			mark,
			{
				"site": case.site,
				"case": case.name,
				"action": "mark_sent",
				"export_sent_on": case.export_sent_on.isoformat() + "+00:00",
				"removal_hold_until": case.hold_until.isoformat() + "+00:00",
			},
		)
		marked = status(
			"happy.rarebooks.app",
			export_sent_on=mark["export_sent_on"],
			removal_hold_until=mark["removal_hold_until"],
		)
		self.assertEqual(self.report(marked), [], "the site has the fields, so nothing is sent again")
		self.assertEqual(self.drop_starting(case)["proceed"], False, "the hold hasn't passed")

		frappe.db.set_value(CASE, case.name, "hold_until", utc_now() - timedelta(minutes=1))
		self.assertEqual(self.actions(self.report(marked), "drop"), [], "no drop without approval")
		frappe.set_user(REMOVAL_MANAGER)
		removal.approve_case(case.name)
		frappe.set_user("Administrator")
		[drop] = self.actions(self.report(marked), "drop")
		self.assertEqual(drop, {"site": case.site, "case": case.name, "action": "drop"})

		data = self.drop_starting(case)
		self.assertEqual((data["proceed"], data["reason"]), (True, None))
		self.assertEqual(self.case(case.site).status, "Dropping")
		self.assertEqual(self.actions(self.report(marked), "drop"), [drop], "a drop is sent until it is done")
		response, data = self.runner(removal.drop_done, case=case.name, site=case.site)
		self.assertEqual((response.status_code, data["status"]), (200, "Removed"))
		row = frappe.get_doc(SITE, case.site)
		self.assertEqual(row.status, "Removed")
		self.assertTrue(row.removed_on and self.case(case.site).removed_on)
		self.assertEqual(self.runner(removal.drop_done, case=case.name, site=case.site)[0].status_code, 409)

	def test_export_ready_for_a_case_not_exporting_is_refused_and_deleted(self):
		# covers: AC-7
		make_site("stray.rarebooks.app")
		self.report(status("stray.rarebooks.app"))
		case = self.case("stray.rarebooks.app")
		key = f"exports/{case.name}/{UUID}.zip"
		response, _data = self.export_ready(case, key)
		self.assertEqual(response.status_code, 409)
		self.storage_delete.assert_called_once_with(key)

	def test_export_ready_checks_its_values(self):
		make_site("values.rarebooks.app")
		self.to_exporting("values.rarebooks.app")
		case = self.case("values.rarebooks.app")
		for key in ("exports/other/x.zip", f"exports/{case.name}/../x.zip", "elsewhere.zip"):
			with self.subTest(key=key):
				response, _data = self.export_ready(case, key)
				self.assertEqual(response.status_code, 400)

	def test_download_redirects_counts_and_expires(self):
		# covers: AC-7, AC-16, AC-17
		make_site("dl.rarebooks.app")
		self.to_exporting("dl.rarebooks.app")
		with patch("rarebooks_control.removal.secrets.token_urlsafe", return_value="the-token"):
			self.export_ready(self.case("dl.rarebooks.app"))
		case = self.case("dl.rarebooks.app")
		self.assertIn("rarebooks_control.removal.download?token=the-token", email_text(case.export_email))
		self.mark_email(case.export_email)
		self.report(status("dl.rarebooks.app"))

		response = get(removal.download, token="the-token")
		self.assertEqual(response.status_code, 302)
		self.assertTrue(response.headers["Location"].startswith("https://r2.example.com/"))
		case = self.case("dl.rarebooks.app")
		self.assertEqual(case.download_count, 1)
		self.assertTrue(case.last_downloaded_on)

		for token in ("wrong", ""):
			with self.subTest(token=token):
				gone = get(removal.download, token=token)
				self.assertEqual(gone.status_code, 404)
				self.assertIn("This link has expired. Contact support.", gone.get_data(as_text=True))
		frappe.db.set_value(CASE, case.name, "link_expires_on", utc_now() - timedelta(seconds=1))
		self.assertEqual(get(removal.download, token="the-token").status_code, 404)

	def test_holding_site_with_empty_fields_gets_mark_sent_again(self):
		# covers: AC-8
		make_site("again.rarebooks.app")
		self.to_holding("again.rarebooks.app")
		[mark] = self.report(status("again.rarebooks.app"))
		self.assertEqual(mark["action"], "mark_sent")


class TestDropGates(RemovalTestCase):
	def test_each_gate_refuses_the_drop(self):
		# covers: AC-9, AC-20
		gates = {
			"not approved": lambda case: frappe.db.set_value(CASE, case.name, "approved_by", None),
			"paused bench": lambda case: frappe.db.set_value(
				"Books Hosting Bench", BENCH, "removal_paused", 1
			),
			"cap": lambda case: frappe.conf.update({"books_removal_max_drops_per_day": 0}),
			"email not sent": lambda case: self.mark_email(case.export_email, "Error"),
			"R2 missing": lambda case: setattr(self.storage_size, "return_value", None),
			"R2 size": lambda case: setattr(self.storage_size, "return_value", EXPORT_BYTES - 1),
			"paid": lambda case: frappe.db.set_value(SITE, case.site, "paid_until", site_today()),
			"exempt": lambda case: frappe.db.set_value(SITE, case.site, "exempt", 1),
			"auto renews": lambda case: frappe.db.set_value(SITE, case.site, "auto_renews", 1),
			"recent checkout": lambda case: self.start(
				patch(
					"rarebooks_control.removal.billing_state",
					return_value={
						"paid_until": None,
						"exempt": 0,
						"auto_renews": 0,
						"checkout_in_last_24h": True,
					},
				)
			),
		}
		for index, (label, close_gate) in enumerate(gates.items()):
			with self.subTest(label):
				site = f"gate{index}.rarebooks.app"
				self.storage_size.return_value = EXPORT_BYTES
				frappe.conf.update({"books_removal_max_drops_per_day": 5})
				frappe.db.set_value("Books Hosting Bench", BENCH, "removal_paused", 0)
				make_site(site)
				case = self.to_drop_due(site)
				close_gate(case)
				data = self.drop_starting(case)
				self.assertEqual(data["proceed"], False)
				self.assertTrue(data["reason"])
				self.assertEqual(self.case(site).status, "Holding")

	def test_the_daily_cap_counts_drops_started_today(self):
		# covers: AC-20
		frappe.conf.update({"books_removal_max_drops_per_day": 1})
		make_site("cap1.rarebooks.app")
		make_site("cap2.rarebooks.app")
		first, second = self.to_drop_due("cap1.rarebooks.app"), self.to_drop_due("cap2.rarebooks.app")
		self.assertTrue(self.drop_starting(first)["proceed"])
		self.assertEqual(self.drop_starting(second)["reason"], "the bench reached today's drop cap")

	def test_a_dry_run_case_never_passes(self):
		# covers: AC-14
		make_site("dry.rarebooks.app")
		self.to_holding("dry.rarebooks.app", mode="dry_run")
		case = self.case("dry.rarebooks.app")
		frappe.db.set_value(CASE, case.name, {"hold_until": utc_now(), "approved_by": REMOVAL_MANAGER})
		self.assertEqual(self.drop_starting(case)["reason"], "the case was opened in dry_run")

	def test_a_case_of_another_bench_is_refused(self):
		# covers: AC-15
		make_site("mine2.rarebooks.app")
		case = self.to_drop_due("mine2.rarebooks.app")
		_response, data = self.runner(
			removal.drop_starting, bench=OTHER_BENCH, secret=OTHER_SECRET, case=case.name, site=case.site
		)
		self.assertEqual(data["proceed"], False)


class TestCancelAndFailure(RemovalTestCase):
	def test_a_payment_while_holding_cancels_deletes_and_clears(self):
		# covers: AC-11
		make_site("late-pay.rarebooks.app")
		self.to_holding("late-pay.rarebooks.app")
		case = self.case("late-pay.rarebooks.app")
		frappe.db.set_value(SITE, case.site, "paid_until", site_today() + timedelta(days=365))
		marked = status(
			case.site,
			export_sent_on=case.export_sent_on.isoformat() + "+00:00",
			removal_hold_until=case.hold_until.isoformat() + "+00:00",
		)
		[clear] = self.report(marked)
		self.assertEqual(clear, {"site": case.site, "case": case.name, "action": "clear"})
		case = self.case(case.site)
		self.assertEqual(case.status, "Cancelled")
		self.assertFalse(case.link_token_hash)
		self.storage_delete.assert_called_once_with(case.export_key)
		self.assertEqual(self.drop_starting(case)["proceed"], False)
		self.assertEqual(self.report(status(case.site, removal_in=None, phase="active")), [])

	def test_the_sites_own_report_of_payment_cancels_too(self):
		# covers: AC-11
		make_site("self-pay.rarebooks.app")
		self.report(status("self-pay.rarebooks.app"))
		self.report(status("self-pay.rarebooks.app", removal_in=None, phase="active"))
		self.assertEqual(self.case("self-pay.rarebooks.app").status, "Cancelled")
		self.report(status("self-pay.rarebooks.app"))
		self.assertEqual(
			frappe.db.count(CASE, {"site": "self-pay.rarebooks.app"}), 2, "a new lapse opens a new case"
		)

	def test_create_checkout_refuses_while_being_removed(self):
		# covers: AC-11
		make_site("freeze.rarebooks.app")
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

		frappe.db.set_value(
			CASE, case.name, {"status": "Dropping", "hold_until": utc_now() + timedelta(days=1)}
		)
		self.assertTrue(removal.is_being_removed(case.site))
		self.assertTrue(billing.is_removed(row), "a payment for a dropping site is not applied")

	def test_three_failed_steps_fail_the_case_and_alert_once(self):
		# covers: AC-12
		make_site("fail.rarebooks.app")
		self.to_exporting("fail.rarebooks.app")
		case = self.case("fail.rarebooks.app")
		with patch("rarebooks_control.removal_mail.send_ops") as send_ops:
			for attempt in (1, 2, 3):
				_response, data = self.runner(
					removal.step_failed, case=case.name, site=case.site, step="upload", error="R2 timed out"
				)
				if attempt < 3:
					self.assertEqual(data["status"], "Scheduled")
					[export] = self.actions(self.report(status(case.site)), "export")
					self.assertEqual(export["case"], case.name)
			self.assertEqual(data["status"], "Failed")
			send_ops.assert_called_once()
		self.assertEqual(self.report(status(case.site)), [], "a failed case gets no instruction")
		response, _data = self.runner(
			removal.step_failed, case=case.name, site=case.site, step="nope", error=""
		)
		self.assertEqual(response.status_code, 400)

	def test_a_drop_failing_before_drop_starting_still_counts(self):
		# covers: AC-12 (a runner with no root login fails the drop while the case is still `Holding`)
		make_site("no-root.rarebooks.app")
		case = self.to_drop_due("no-root.rarebooks.app")
		with patch("rarebooks_control.removal_mail.send_ops") as send_ops:
			for attempt in (1, 2, 3):
				_response, data = self.runner(
					removal.step_failed, case=case.name, site=case.site, step="drop", error="no root login"
				)
				self.assertEqual(data["status"], "Holding" if attempt < 3 else "Failed")
			send_ops.assert_called_once()
		self.assertEqual(self.case(case.site).attempts, 3)

	def test_a_deferred_export_counts_no_attempt(self):
		# covers: AC-12
		make_site("defer.rarebooks.app")
		self.to_exporting("defer.rarebooks.app")
		case = self.case("defer.rarebooks.app")
		[export] = self.actions(self.report(status(case.site), deferred=[case.name]), "export")
		self.assertEqual(export["case"], case.name)
		self.assertEqual(self.case(case.site).attempts, 0)

	def test_six_silent_hours_count_an_attempt_and_resend_export(self):
		# covers: AC-12
		make_site("silent.rarebooks.app")
		self.to_exporting("silent.rarebooks.app")
		case = self.case("silent.rarebooks.app")
		self.assertEqual(self.report(status(case.site)), [])
		frappe.db.set_value(CASE, case.name, "export_claimed_on", utc_now() - timedelta(hours=6, minutes=1))
		[export] = self.actions(self.report(status(case.site)), "export")
		self.assertEqual((export["case"], self.case(case.site).attempts), (case.name, 1))

	def test_an_export_email_error_fails_and_retry_sends_it_again(self):
		# covers: AC-12, AC-13
		make_site("mailfail.rarebooks.app", owner_email=OWNER)
		self.to_exporting("mailfail.rarebooks.app")
		case = self.case("mailfail.rarebooks.app")
		self.export_ready(case)
		first = self.case(case.site).export_email
		self.mark_email(first, "Error")
		self.report(status(case.site))
		self.assertEqual(self.case(case.site).status, "Failed")

		frappe.set_user(REMOVAL_MANAGER)
		self.assertEqual(removal.retry_case(case.name), {"status": "Holding"})
		frappe.set_user("Administrator")
		case = self.case(case.site)
		self.assertNotEqual(case.export_email, first)
		self.assertFalse(case.export_sent_on)
		self.mark_email(case.export_email)
		[mark] = self.report(status(case.site))
		self.assertEqual(mark["action"], "mark_sent")
		self.assertTrue(self.case(case.site).hold_until)

	def test_an_email_unsent_after_24_hours_fails(self):
		# covers: AC-12
		make_site("slowmail.rarebooks.app")
		self.to_exporting("slowmail.rarebooks.app")
		case = self.case("slowmail.rarebooks.app")
		self.export_ready(case)
		frappe.db.set_value(CASE, case.name, "export_queued_on", utc_now() - timedelta(hours=24))
		self.report(status(case.site))
		self.assertEqual(self.case(case.site).status, "Failed")


class TestDryRun(RemovalTestCase):
	def test_dry_run_mails_ops_writes_no_site_field_and_logs_the_drop(self):
		# covers: AC-14
		make_site("rehearse.rarebooks.app", owner_email=OWNER)
		self.to_holding("rehearse.rarebooks.app", mode="dry_run")
		case = self.case("rehearse.rarebooks.app")
		self.assertEqual(case.mode, "dry_run")
		self.assertEqual(self.queued_to(OWNER), [], "no owner email in a dry run")
		self.assertTrue(self.queued_to(OPS))
		self.assertIn(OWNER, email_text(case.warnings[0].email))
		self.assertEqual(self.report(status(case.site), mode="dry_run"), [], "no mark_sent in a dry run")
		frappe.db.set_value(CASE, case.name, {"hold_until": utc_now(), "approved_by": REMOVAL_MANAGER})
		[drop] = self.report(status(case.site), mode="dry_run")
		self.assertEqual(drop["action"], "drop")

	def test_the_first_live_report_cancels_dry_run_cases(self):
		# covers: AC-14
		make_site("switch.rarebooks.app", owner_email=OWNER)
		self.to_holding("switch.rarebooks.app", mode="dry_run")
		dry = self.case("switch.rarebooks.app")
		self.report(status("switch.rarebooks.app"))
		self.assertEqual(frappe.db.get_value(CASE, dry.name, "status"), "Cancelled")
		self.storage_delete.assert_called_once_with(dry.export_key)
		live = self.case("switch.rarebooks.app")
		self.assertEqual((live.mode, live.status), ("live", "Scheduled"))
		self.assertEqual(len(self.queued_to(OWNER)), 1, "live starts with a real warning")


class TestActions(RemovalTestCase):
	def test_only_a_removal_manager_runs_the_actions(self):
		# covers: AC-13
		make_site("roles.rarebooks.app")
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
		# covers: AC-13
		make_site("pause.rarebooks.app")
		case = self.to_drop_due("pause.rarebooks.app", approve=False)
		frappe.set_user(REMOVAL_MANAGER)
		with self.assertRaises(frappe.ValidationError):
			removal.pause_case(case.name, reason="")
		self.assertEqual(removal.pause_case(case.name, reason="Owner called")["status"], "Paused")
		doc = self.case(case.site)
		self.assertEqual((doc.paused_by, doc.pause_reason), (REMOVAL_MANAGER, "Owner called"))
		with self.assertRaises(frappe.ValidationError):
			removal.approve_case(case.name)
		frappe.set_user("Administrator")
		self.assertEqual(self.actions(self.report(status(case.site)), "drop"), [])
		frappe.set_user(REMOVAL_MANAGER)
		self.assertEqual(removal.resume_case(case.name)["status"], "Holding")
		self.assertFalse(self.case(case.site).paused_by)
		removal.approve_case(case.name)
		doc = self.case(case.site)
		self.assertEqual(doc.approved_by, REMOVAL_MANAGER)
		with self.assertRaises(frappe.ValidationError):
			removal.retry_case(case.name)

	def test_resume_of_a_case_paused_before_export_goes_back_to_scheduled(self):
		# covers: AC-13
		make_site("early.rarebooks.app")
		self.report(status("early.rarebooks.app", removal_in=10))
		case = self.case("early.rarebooks.app")
		frappe.set_user(REMOVAL_MANAGER)
		removal.pause_case(case.name, reason="wait")
		self.assertEqual(removal.resume_case(case.name)["status"], "Scheduled")


class TestAlerts(RemovalTestCase):
	def test_an_alert_the_mail_server_refuses_never_fails_the_commit(self):
		# covers: AC-12, AC-21 (and spec 0008 AC-5: a signed endpoint keeps its answer)
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

	def test_silent_bench_and_stuck_drop_alert_once(self):
		# covers: AC-21
		frappe.cache.delete_keys("removal-alert:")
		frappe.db.set_value(
			"Books Hosting Bench",
			BENCH,
			{"last_report_on": utc_now() - timedelta(hours=4), "last_mode": "live"},
		)
		make_site("stuck.rarebooks.app")
		case = self.to_drop_due("stuck.rarebooks.app")
		self.drop_starting(case)
		frappe.db.set_value(
			"Books Hosting Bench",
			BENCH,
			{"last_report_on": utc_now() - timedelta(hours=4), "last_mode": "live"},
		)
		frappe.db.set_value(CASE, case.name, "drop_started_on", utc_now() - timedelta(hours=3))
		with patch("rarebooks_control.removal_mail.send_ops") as send_ops:
			removal_mail.hourly()
			removal_mail.hourly()
		subjects = [c.args[0] for c in send_ops.call_args_list]
		self.assertEqual(len(subjects), 2, subjects)
		self.assertTrue(any(BENCH in subject for subject in subjects))
		self.assertTrue(any(case.site in subject for subject in subjects))

	def test_three_error_runs_alert_once(self):
		# covers: AC-21
		make_site("err.rarebooks.app")
		self.report(status("err.rarebooks.app", removal_in=10))
		with patch("rarebooks_control.removal_mail.send_ops") as send_ops:
			for _run in range(4):
				self.report({"site": "err.rarebooks.app", "error": "could not connect"})
		send_ops.assert_called_once()
		self.assertEqual(self.case("err.rarebooks.app").error_runs, 4)
		self.report(status("err.rarebooks.app", removal_in=10))
		self.assertEqual(self.case("err.rarebooks.app").error_runs, 0)

	def test_daily_lists_unapproved_holds_and_the_digest(self):
		# covers: AC-21
		make_site("wait.rarebooks.app")
		self.to_holding("wait.rarebooks.app")
		with patch("rarebooks_control.removal_mail.send_ops") as send_ops:
			removal_mail.daily()
		subjects = [c.args[0] for c in send_ops.call_args_list]
		self.assertIn("1 removals wait for your approval", subjects)


class TestExportReadyRepeat(RemovalTestCase):
	def test_a_repeated_export_ready_queues_no_second_email_and_deletes_nothing(self):
		# covers: AC-7
		make_site("repeat.rarebooks.app", owner_email=OWNER)
		self.to_exporting("repeat.rarebooks.app")
		case = self.case("repeat.rarebooks.app")
		self.export_ready(case)
		first = self.case(case.site).export_email
		self.mark_email(first)
		self.report(status(case.site))
		response, _data = self.export_ready(case)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(self.case(case.site).export_email, first)
		self.storage_delete.assert_not_called()
