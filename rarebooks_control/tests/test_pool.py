"""The warm pool and its failures (spec 0014 AC-10, AC-15 to AC-19): the top up and its build on demand, the
Building poll into config and ping, failed builds and their pause, silent and stale pool sites, the Discard case
with its owner guard and label release, the 2 hour rule, the queue alerts and Retry."""

from datetime import timedelta
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from rarebooks_control import onboarding, pool, registry, removal
from rarebooks_control.providers import frappe_cloud
from rarebooks_control.providers.frappe_cloud import FrappeCloudError, OutcomeUnknown
from rarebooks_control.signing import utc_now
from rarebooks_control.tests.test_onboarding import ASK, CLERK, POOL, SIGNUP, OnboardingTestCase
from rarebooks_control.tests.test_registry import SITE

CASE = "Books Removal Case"
DOMAIN = "frappe.cloud"
PLAN = "books-plan"
SERVER = "books-server.frappe.cloud"
CLOUD_KEYS = {"frappe_cloud_api_key": "k", "frappe_cloud_api_secret": "s", "frappe_cloud_team": "t"}
NOT_LOGGED_IN = (200, {"owner_email": "asha.mrema@gmail.com", "owner_logged_in": False})


class PoolTestCase(OnboardingTestCase):
	def setUp(self):
		super().setUp()
		self.start(patch.dict(frappe.conf, CLOUD_KEYS))
		frappe.db.set_single_value(
			onboarding.SETTINGS,
			{
				"frappe_cloud_server": SERVER,
				"frappe_cloud_plan": PLAN,
				"frappe_cloud_domain": DOMAIN,
				"pool_builds_paused": 0,
				"pool_pause_reason": None,
				"build_failures": 0,
				"discard_mode": "live",
			},
		)
		frappe.db.set_single_value(removal.SETTINGS, {"removal_paused": 0, "max_drops_per_day": 0})
		frappe.db.commit()
		frappe.cache.delete_keys("pool-silent:")
		self.sites.answers["claim_report"] = NOT_LOGGED_IN

	def building(self) -> list[str]:
		return frappe.get_all(SITE, filters={"pool_status": "Building"}, pluck="name", order_by="name")

	def make_ready(self, name: str):
		"""Frappe Cloud finishes the install; the poll sends config and the ping makes it Ready."""
		self.cloud.sites[name]["status"] = "Active"
		pool.sweep()
		self.take_config(name)
		registry.run_pings()
		frappe.db.commit()

	def discard_case(self, site: str):
		name = frappe.db.get_value(CASE, {"site": site, "kind": "Discard"}, "name")
		return frappe.get_doc(CASE, name) if name else None

	def emails(self) -> list[str]:
		return [call.args[0] for call in self.send_ops.call_args_list]


class TestTopUp(PoolTestCase):
	# covers: AC-15
	def test_builds_up_to_pool_size_plus_queued_with_each_row_committed_first(self):
		seen = []

		def committed_first(label, domain, group, server, plan, apps):
			name = f"{label}.{domain}"
			# The row is already written when Frappe Cloud is asked for the site.
			seen.append(frappe.db.get_value(SITE, name, ["pool_status", "config_status"]))
			return original(label, domain, group, server, plan, apps)

		original = self.cloud.new_site
		with patch("rarebooks_control.providers.frappe_cloud.new_site", side_effect=committed_first):
			names = pool.top_up()
		self.assertEqual(len(names), 2)
		self.assertEqual(seen, [("Building", "Pending")] * 2)
		for name in names:
			row = frappe.get_doc(SITE, name)
			self.assertRegex(name, r"^rb-[0-9a-f]{8}\.frappe\.cloud$")
			self.assertEqual(row.site_url, f"https://{name}")
			self.assertEqual((row.status, row.owner_email or None, row.subdomain), ("Active", None, None))
			self.assertEqual(len(row.get_password("control_secret")), 64)
		calls = [call for call in self.cloud.calls if call[0] == "new"]
		self.assertEqual(
			{call[2:] for call in calls}, {("books-group", SERVER, PLAN, ("frappe", "frappe_books"))}
		)
		# Full already: a second run builds nothing.
		self.assertEqual(pool.top_up(), [])

	def test_queued_sign_ups_add_builds_and_a_run_starts_at_most_three(self):
		for number in range(4):
			self.ask(
				onboarding.request_site,
				ASK,
				clerk_user_id=f"user_{number}",
				email=f"u{number}@example.com",
				subdomain=f"shop{number}",
			)
		frappe.db.set_value(SIGNUP, {"status": "Claiming"}, "status", "Queued")
		frappe.db.commit()
		self.assertEqual(len(pool.top_up()), 3)
		self.assertEqual(len(pool.top_up()), 3)
		self.assertEqual(pool.pool_count(), 6)

	def test_a_pause_set_by_an_earlier_build_stops_the_rest_of_the_run(self):
		# A run of three builds where Frappe Cloud refuses each: the second failure pauses the builds, so the third
		# must not reach Frappe Cloud, and its row goes (no site was made for it).
		frappe.db.set_single_value(onboarding.SETTINGS, "pool_size", 3)
		self.cloud.errors["new"] = FrappeCloudError("press.api.site.new answered HTTP 417", 417)
		pool.top_up()
		self.assertEqual(self.cloud.count("new"), 2)
		self.assertEqual(frappe.db.count(SITE, {"pool_status": "Building"}), 0)
		self.assertEqual(frappe.db.get_single_value(onboarding.SETTINGS, "build_failures"), 2)

	def test_nothing_is_built_while_paused_or_unconfigured(self):
		frappe.db.set_single_value(onboarding.SETTINGS, "pool_builds_paused", 1)
		self.assertEqual(pool.top_up(), [])
		frappe.db.set_single_value(
			onboarding.SETTINGS, {"pool_builds_paused": 0, "frappe_cloud_domain": None}
		)
		self.assertEqual(pool.top_up(), [])
		self.assertEqual(self.cloud.count("new"), 0)

	def test_a_refused_build_deletes_its_row_and_a_lost_answer_keeps_it(self):
		self.cloud.errors["new"] = FrappeCloudError("press.api.site.new answered HTTP 417", 417)
		frappe.db.set_single_value(onboarding.SETTINGS, "pool_size", 1)
		self.assertEqual(len(pool.top_up()), 1)
		self.assertEqual(self.building(), [])
		self.assertEqual(frappe.db.get_single_value(onboarding.SETTINGS, "build_failures"), 1)

		self.cloud.errors["new"] = OutcomeUnknown("press.api.site.new sent, no answer")
		pool.top_up()
		self.assertEqual(len(self.building()), 1)
		self.assertEqual(frappe.db.get_single_value(onboarding.SETTINGS, "build_failures"), 1)

	def test_no_ready_site_for_a_claim_asks_for_a_build_at_once(self):
		self.ask(onboarding.request_site, ASK)
		self.run_jobs()
		self.assertEqual(self.signup().status, "Queued")
		# The claim's build on demand ran inside `run_jobs`: one for the request plus the pool size.
		self.assertEqual(len(self.building()), 3)


class TestBuildingToReady(PoolTestCase):
	# covers: AC-15, AC-11
	def test_config_waits_for_frappe_cloud_then_the_first_ping_makes_it_ready_and_claimable(self):
		frappe.db.set_single_value(onboarding.SETTINGS, "pool_size", 1)
		[name] = pool.top_up()
		pool.sweep()
		self.assertNotIn(name, self.cloud.configs)
		self.assertIsNone(frappe.db.get_value(SITE, name, "config_sent_on"))

		self.make_ready(name)
		row = frappe.get_doc(SITE, name)
		self.assertEqual((row.pool_status, row.config_status), ("Ready", "Active"))
		self.assertTrue(row.config_sent_on)

		self.ask(onboarding.request_site, ASK)
		self.run_jobs()
		self.assertEqual(self.signup().hosted_site, name)

	def test_a_site_on_another_group_never_gets_config(self):
		frappe.db.set_single_value(onboarding.SETTINGS, "pool_size", 1)
		[name] = pool.top_up()
		self.cloud.sites[name].update({"status": "Active", "group": "other"})
		pool.sweep()
		self.assertNotIn(name, self.cloud.configs)

	def test_a_ready_build_sets_the_failure_count_back_to_zero(self):
		frappe.db.set_single_value(onboarding.SETTINGS, {"pool_size": 1, "build_failures": 1})
		[name] = pool.top_up()
		self.make_ready(name)
		self.assertEqual(frappe.db.get_single_value(onboarding.SETTINGS, "build_failures"), 0)


class TestFailedBuilds(PoolTestCase):
	# covers: AC-16
	def test_a_build_over_its_hour_is_discarded_and_replaced_and_two_failures_pause_with_one_email(self):
		frappe.db.set_single_value(onboarding.SETTINGS, "pool_size", 1)
		[first] = pool.top_up()
		frappe.db.set_value(SITE, first, "pool_built_on", utc_now() - timedelta(hours=1, minutes=1))
		frappe.db.commit()
		pool.sweep()
		case = self.discard_case(first)
		self.assertEqual((case.status, case.mode, case.owner_email), ("Dropping", "live", "ops@example.com"))
		# No config was ever sent, so no ping alert either: only the pool counts it.
		self.assertEqual(self.emails(), [])

		[second] = pool.top_up()
		frappe.db.set_value(SITE, second, "pool_built_on", utc_now() - timedelta(hours=2))
		frappe.db.commit()
		pool.sweep()
		settings = frappe.get_single(onboarding.SETTINGS)
		self.assertEqual((settings.build_failures, settings.pool_builds_paused), (2, 1))
		self.assertIn(second, settings.pool_pause_reason)
		self.assertEqual(len(self.emails()), 1)
		self.assertEqual(pool.top_up(), [])

		settings.pool_builds_paused = 0
		settings.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_single_value(onboarding.SETTINGS, "build_failures"), 0)

	def test_a_silent_ready_site_is_discarded_after_an_hour_and_never_claimed(self):
		self.pool_site()
		self.sites.down.add(POOL)
		pool.sweep()
		self.assertIsNone(self.discard_case(POOL))
		frappe.cache.set_value(f"pool-silent:{POOL}", (utc_now() - timedelta(hours=1)).isoformat())
		pool.sweep()
		self.assertEqual(self.discard_case(POOL).status, "Dropping")

		self.ask(onboarding.request_site, ASK)
		with patch("rarebooks_control.pool.request_build"):
			self.run_jobs()
		self.assertEqual(self.signup().status, "Queued")

	def test_a_ping_that_answers_again_clears_the_silence(self):
		self.pool_site()
		self.sites.down.add(POOL)
		pool.sweep()
		self.sites.down.clear()
		pool.sweep()
		self.assertIsNone(frappe.cache.get_value(f"pool-silent:{POOL}"))

	def test_a_ready_site_older_than_30_days_is_discarded_by_the_hourly_job(self):
		self.pool_site(built_days_ago=29)
		self.pool_site("rb-00000000.frappe.cloud", built_days_ago=31)
		frappe.db.set_single_value(onboarding.SETTINGS, "pool_builds_paused", 1)
		pool.hourly()
		self.assertIsNone(self.discard_case(POOL))
		self.assertEqual(self.discard_case("rb-00000000.frappe.cloud").kind, "Discard")


class TestDiscard(PoolTestCase):
	# covers: AC-19
	def discarded(self, site: str = POOL) -> str:
		removal.open_discard(site, "test")
		frappe.db.commit()
		return self.discard_case(site).name

	def test_the_would_discard_line_passes_frappes_default_log_level(self):
		# Frappe sets its loggers to ERROR (WARNING on a dev server), which drops INFO lines; the dry run line must
		# still be written. No assertLogs here, since it lowers the level itself and hides the bug.
		import logging

		with patch.object(frappe, "log_level", None):
			frappe.loggers.pop("rarebooks_control.removal", None)
			logger = removal.removal_logger()
		self.assertTrue(logger.isEnabledFor(logging.INFO))

	def test_dry_run_only_logs_and_never_archives(self):
		self.pool_site()
		frappe.db.set_single_value(onboarding.SETTINGS, "discard_mode", "dry_run")
		name = self.discarded()
		# Frappe sets up a logger's handlers on its first use, which would drop the one `assertLogs` adds.
		frappe.logger("rarebooks_control.removal")
		with self.assertLogs(f"rarebooks_control.removal-{frappe.local.site}", "INFO") as logs:
			removal.run_site(POOL)
		self.assertIn(f"would discard {POOL}", "\n".join(logs.output))
		self.assertEqual(self.cloud.archives(), 0)
		self.assertEqual(frappe.db.get_value(CASE, name, "mode"), "dry_run")

	def test_a_live_discard_runs_with_removal_off_checks_the_owner_twice_and_is_proven(self):
		self.pool_site()
		name = self.discarded()
		removal.run_round()
		self.assertIn(
			("rarebooks_control.removal.run_site", POOL), [(m, kw.get("site")) for m, kw in self.jobs]
		)
		self.jobs.clear()
		removal.run_site(POOL)
		self.assertEqual(self.cloud.archives(), 1)
		self.assertEqual(len(self.sites.bodies("claim_report")), 2)
		self.assertTrue(frappe.db.get_value(CASE, name, "archive_requested_on"))
		# Not counted toward the daily drop cap of 0, which would stop any removal drop.
		self.assertEqual(removal.drops_today(), 0)

		self.cloud.finish_archive(POOL)
		removal.run_site(POOL)
		self.assertEqual(frappe.db.get_value(CASE, name, "status"), "Removed")
		self.assertEqual(frappe.db.get_value(SITE, POOL, "status"), "Removed")
		self.assertEqual(self.emails(), [])

	def test_an_owner_who_logged_in_stops_the_discard_with_one_email(self):
		self.pool_site()
		name = self.discarded()
		self.sites.answers["claim_report"] = (200, {"owner_email": "a@example.com", "owner_logged_in": True})
		removal.run_site(POOL)
		self.assertEqual(frappe.db.get_value(CASE, name, "status"), "Failed")
		self.assertEqual(self.cloud.archives(), 0)
		self.assertEqual(len(self.emails()), 1)

	def test_a_login_between_the_two_checks_stops_the_archive(self):
		self.pool_site()
		name = self.discarded()
		answers = iter([NOT_LOGGED_IN, (200, {"owner_email": "a@example.com", "owner_logged_in": True})])
		original = self.sites.__call__

		def second_check_logged_in(url, data, headers, timeout):
			if url.endswith(".claim_report"):
				self.sites.answers["claim_report"] = next(answers)
			return original(url, data, headers, timeout)

		with patch("rarebooks_control.site_calls.post", side_effect=second_check_logged_in):
			removal.run_site(POOL)
		self.assertEqual(frappe.db.get_value(CASE, name, "status"), "Failed")
		self.assertEqual(self.cloud.archives(), 0)

	def test_no_answer_never_counts_as_not_logged_in_and_the_third_failure_fails_the_case(self):
		self.pool_site()
		name = self.discarded()
		self.sites.down.add(POOL)
		for _run in range(2):
			removal.run_site(POOL)
		self.assertEqual(frappe.db.get_value(CASE, name, ["status", "attempts"]), ("Dropping", 2))
		self.sites.answers["claim_report"] = (200, {"owner_email": None})
		self.sites.down.clear()
		removal.run_site(POOL)
		self.assertEqual(frappe.db.get_value(CASE, name, "status"), "Failed")
		self.assertEqual(self.cloud.archives(), 0)
		self.assertEqual(len(self.emails()), 1)

	def test_a_building_site_with_no_config_skips_the_owner_check_and_a_pending_one_may_be_archived(self):
		frappe.db.set_single_value(onboarding.SETTINGS, "pool_size", 1)
		[name] = pool.top_up()
		self.discarded(name)
		removal.run_site(name)
		self.assertEqual(self.sites.bodies("claim_report"), [])
		self.assertEqual(self.cloud.archives(), 1)

	def test_the_other_drop_guards_still_hold(self):
		self.pool_site()
		self.discarded()
		frappe.db.set_single_value(removal.SETTINGS, "removal_paused", 1)
		removal.run_site(POOL)
		self.cloud.sites[POOL]["group"] = "other"
		frappe.db.set_single_value(removal.SETTINGS, "removal_paused", 0)
		removal.run_site(POOL)
		self.cloud.sites[POOL].update({"group": "books-group", "status": "Suspended"})
		removal.run_site(POOL)
		self.assertEqual(self.cloud.archives(), 0)


class TestFailedSignup(PoolTestCase):
	# covers: AC-17, AC-18, AC-19, AC-21
	def claimed(self):
		self.pool_site()
		self.ask(onboarding.request_site, ASK)
		self.run_jobs()
		return self.signup()

	def test_not_ready_in_two_hours_fails_once_discards_the_site_and_retry_waits_for_cleanup(self):
		row = self.claimed()
		self.assertEqual(row.status, "Preparing")
		frappe.db.set_value(SIGNUP, row.name, "claimed_on", utc_now() - timedelta(hours=2, minutes=1))
		frappe.db.commit()
		onboarding.sweep()
		onboarding.sweep()
		row = self.signup()
		self.assertEqual(row.status, "Failed")
		self.assertEqual(len([e for e in self.emails() if "failed" in e]), 1)
		self.assertEqual(self.discard_case(POOL).status, "Dropping")

		refusal = self.refused(onboarding.retry_signup, row.name)
		self.assertIn("isn't discarded yet", refusal)

		# The discard is proven; the label is released only once the domain cleanup is done.
		self.cloud.finish_archive(POOL)
		removal.run_site(POOL)
		removal.run_site(POOL)
		self.assertEqual(self.discard_case(POOL).status, "Removed")
		self.assertEqual(frappe.db.get_value(SITE, POOL, "subdomain"), "acme")
		self.assertIn("isn't cleaned up", self.refused(onboarding.retry_signup, row.name))
		registry.sweep_domain_cleanup()
		frappe.db.commit()
		self.assertIsNone(frappe.db.get_value(SITE, POOL, "subdomain"))
		# A label released by a discard stays held by its sign up (AC-21).
		with self.assertRaises(frappe.ValidationError):
			registry.check_subdomain("acme")
		frappe.clear_messages()

		self.pool_site("rb-99999999.frappe.cloud")
		self.jobs.clear()
		self.as_billing_manager(onboarding.retry_signup, row.name)
		frappe.db.commit()
		self.run_jobs()
		row = self.signup()
		self.assertEqual(
			(row.status, row.hosted_site, row.subdomain), ("Preparing", "rb-99999999.frappe.cloud", "acme")
		)
		self.assertIsNone(row.failed_reason)
		self.assertEqual(frappe.db.get_value(SITE, row.hosted_site, "subdomain"), "acme")

	def test_only_a_billing_manager_retries_and_only_a_failed_sign_up(self):
		row = self.claimed()
		self.assertIn("Only a Failed", self.refused(onboarding.retry_signup, row.name))
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				onboarding.retry_signup(row.name)
		finally:
			frappe.set_user("Administrator")

	def test_a_failed_domain_discards_the_claimed_site_too(self):
		row = self.claimed()
		frappe.db.set_value(SIGNUP, row.name, "status", "Preparing")
		hosted = frappe.get_doc(SITE, POOL, for_update=True)
		hosted.domain_step = "DNS"
		registry._fail_domain(hosted, ["test"])
		frappe.db.commit()
		self.assertEqual(self.signup().status, "Failed")
		self.assertEqual(self.discard_case(POOL).kind, "Discard")


class TestDiscardOverTrialCase(PoolTestCase):
	"""A claimed site that got a Trial case before its sign up failed (found in the staging sign up, 2026-11-23)."""

	def setUp(self):
		super().setUp()
		frappe.cache.delete_keys("signup-discard-refused:")

	def claimed(self):
		self.pool_site()
		self.ask(onboarding.request_site, ASK)
		self.run_jobs()
		return self.signup()

	def trial_case(self, site: str = POOL, status: str = "Scheduled", **values):
		case = frappe.get_doc(
			{
				"doctype": CASE,
				"site": site,
				"kind": "Trial",
				"mode": "live",
				"status": status,
				"removal_on": frappe.utils.add_days(frappe.utils.today(), 4),
				"owner_email": "asha.mrema@gmail.com",
				"site_url": f"https://{site}",
				**values,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return case.name

	def fail(self, row):
		frappe.db.set_value(SIGNUP, row.name, "claimed_on", utc_now() - timedelta(hours=2, minutes=1))
		frappe.db.commit()
		onboarding.sweep()

	def case_status(self, name: str) -> str:
		return frappe.db.get_value(CASE, name, "status")

	def refusals_logged(self) -> int:
		return frappe.db.count("Error Log", {"method": ("like", "Onboarding: the site % wasn't discarded%")})

	# covers: AC-19
	def test_a_scheduled_trial_case_is_cancelled_and_a_discard_opens(self):
		row = self.claimed()
		trial = self.trial_case()
		self.fail(row)
		self.assertEqual(self.signup().status, "Failed")
		self.assertEqual(self.case_status(trial), "Cancelled")
		self.assertEqual(frappe.db.get_value(CASE, trial, "last_error"), "Replaced by a discard")
		self.assertEqual(self.discard_case(POOL).status, "Dropping")

	# covers: AC-19
	def test_any_trial_case_not_under_way_gives_way_whatever_its_status_and_mode(self):
		for index, status in enumerate(("Paused", "Exporting", "Holding", "Failed")):
			site = f"rb-0000000{index}.frappe.cloud"
			self.existing_site(site, owner_email="asha.mrema@gmail.com", pool_status="Claimed")
			trial = self.trial_case(site, status, mode="dry_run" if index % 2 else "live")
			discard = removal.open_discard(site, "test")
			frappe.db.commit()
			self.assertEqual((status, self.case_status(trial)), (status, "Cancelled"))
			self.assertEqual(frappe.db.get_value(CASE, discard, "kind"), "Discard")
		# An open Discard case is returned as it is, and no second one opens.
		self.assertEqual(removal.open_discard(site, "again"), discard)
		self.assertEqual(frappe.db.count(CASE, {"site": site, "kind": "Discard"}), 1)

	# covers: AC-17, AC-18, AC-19
	def test_a_drop_under_way_opens_no_discard_and_retry_waits_for_its_removed(self):
		row = self.claimed()
		trial = self.trial_case(status="Dropping", drop_started_on=utc_now())
		self.fail(row)
		onboarding.sweep()
		self.assertEqual(self.case_status(trial), "Dropping")
		self.assertIsNone(self.discard_case(POOL))
		self.assertIn(trial, self.refused(onboarding.retry_signup, row.name))

		frappe.db.set_value(CASE, trial, "status", "Removed")
		frappe.db.set_value(SITE, POOL, "subdomain", None)
		frappe.db.commit()
		onboarding.sweep()
		self.assertIsNone(self.discard_case(POOL), "a Removed case of any kind ends the site")
		self.pool_site("rb-99999999.frappe.cloud")
		self.as_billing_manager(onboarding.retry_signup, row.name)
		self.assertEqual(self.signup().status, "Queued")

	# covers: AC-19
	def test_a_case_with_its_archive_requested_is_under_way_too(self):
		self.claimed()
		trial = self.trial_case(status="Holding", archive_requested_on=utc_now())
		self.assertIsNone(removal.open_discard(POOL, "test"))
		self.assertEqual(self.case_status(trial), "Holding")

	# covers: AC-17
	def test_a_refused_discard_is_opened_by_the_sweep_and_logged_once_a_day(self):
		row = self.claimed()
		frappe.db.set_value(SITE, POOL, "owner_email", "")
		frappe.db.commit()
		before = self.refusals_logged()
		with patch.dict(frappe.conf, {"books_ops_email": None}):
			self.fail(row)
			onboarding.sweep()
			onboarding.sweep()
		self.assertEqual(self.signup().status, "Failed")
		self.assertIsNone(self.discard_case(POOL))
		self.assertEqual(self.refusals_logged() - before, 1)

		onboarding.sweep()
		self.assertEqual(self.discard_case(POOL).status, "Dropping")

	# covers: AC-17
	def test_a_cancelled_discard_is_replaced_under_the_current_mode_and_a_failed_one_never(self):
		row = self.claimed()
		self.fail(row)
		first = self.discard_case(POOL).name
		frappe.db.set_value(CASE, first, "status", "Cancelled")
		frappe.db.set_single_value(onboarding.SETTINGS, "discard_mode", "dry_run")
		frappe.db.commit()
		onboarding.sweep()
		second = frappe.db.get_value(CASE, {"site": POOL, "kind": "Discard", "status": "Dropping"}, "name")
		self.assertNotEqual(second, first)
		self.assertEqual(frappe.db.get_value(CASE, second, "mode"), "dry_run")

		frappe.db.set_value(CASE, second, "status", "Failed")
		frappe.db.commit()
		onboarding.sweep()
		self.assertEqual(frappe.db.count(CASE, {"site": POOL, "kind": "Discard"}), 2)
		self.assertIn(f"Discard case {second} is Failed", self.refused(onboarding.retry_signup, row.name))

	# covers: AC-20
	def test_no_trial_case_for_a_claimed_site_until_its_sign_up_is_ready(self):
		from rarebooks_control.tests.test_removal import status

		frappe.db.set_single_value(removal.SETTINGS, "mode", "live")
		frappe.db.commit()
		self.claimed()
		self.sites.reports[POOL] = status(POOL, removal_in=4)
		removal.run_site(POOL)
		self.assertEqual(frappe.db.count(CASE, {"site": POOL}), 0)

		self.walk_domain()
		self.assertEqual(self.signup().status, "Ready")
		removal.run_site(POOL)
		self.assertEqual(
			frappe.db.get_value(CASE, {"site": POOL}, ["kind", "status"]), ("Trial", "Scheduled")
		)


class TestQueueAlerts(PoolTestCase):
	# covers: AC-10
	def test_the_cap_emails_once_a_day_and_an_old_request_once_naming_the_cause(self):
		frappe.db.set_single_value(onboarding.SETTINGS, "max_claims_per_day", 0)
		self.ask(onboarding.request_site, ASK)
		self.run_jobs()
		frappe.cache.delete_keys("removal-alert:signup-cap")
		onboarding.sweep()
		onboarding.sweep()
		self.assertEqual(len([e for e in self.emails() if "daily cap" in e]), 1)

		frappe.db.set_value(SIGNUP, {"clerk_user_id": CLERK}, "requested_on", utc_now() - timedelta(hours=2))
		frappe.db.commit()
		onboarding.sweep()
		onboarding.sweep()
		waited = [call for call in self.send_ops.call_args_list if "waited over an hour" in call.args[0]]
		self.assertEqual(len(waited), 1)
		self.assertIn("daily cap", "\n".join(waited[0].args[1]))
		self.assertTrue(self.signup().queue_alerted_on)

	def test_the_cause_names_paused_builds_or_no_ready_site(self):
		self.assertIn("no Ready pool site", onboarding._queue_cause(False))
		frappe.db.set_single_value(
			onboarding.SETTINGS, {"pool_builds_paused": 1, "pool_pause_reason": "two failed builds"}
		)
		self.assertIn("two failed builds", onboarding._queue_cause(False))


class UnitTestNewSite(UnitTestCase):
	def answer(self, message, status=200):
		response = Mock(status_code=status)
		response.json.return_value = {"message": message}
		return response

	# covers: AC-15
	def test_new_sends_server_cluster_and_version_read_from_the_server_and_group(self):
		answers = {
			"press.api.bench.get": self.answer({"name": "books-group", "version": "Version 16"}),
			"press.api.server.get": self.answer({"name": SERVER, "region_info": {"name": "Falkenstein-3"}}),
			"press.api.site.new": self.answer({"site": "RB-0A1B2C3D.frappe.cloud", "job": "j"}),
		}
		with (
			patch.dict(frappe.conf, CLOUD_KEYS),
			patch("rarebooks_control.providers.frappe_cloud.requests.post") as post,
		):
			post.side_effect = lambda url, **kwargs: answers[url.rsplit("/", 1)[1]]
			name = frappe_cloud.new_site("rb-0a1b2c3d", DOMAIN, "books-group", SERVER, PLAN, ["frappe", "x"])
		self.assertEqual(name, "rb-0a1b2c3d.frappe.cloud")
		self.assertEqual(
			post.call_args.kwargs["json"],
			{
				"site": {
					"name": "rb-0a1b2c3d",
					"domain": DOMAIN,
					"group": "books-group",
					"server": SERVER,
					"cluster": "Falkenstein-3",
					"version": "Version 16",
					"plan": PLAN,
					"apps": ["frappe", "x"],
				}
			},
		)
		self.assertFalse(post.call_args.kwargs["allow_redirects"])

	# covers: AC-15
	def test_a_failed_read_refuses_before_new_is_sent(self):
		with (
			patch.dict(frappe.conf, CLOUD_KEYS),
			patch("rarebooks_control.providers.frappe_cloud.requests.post") as post,
		):
			post.return_value = self.answer({"name": SERVER, "region_info": None})
			with self.assertRaises(FrappeCloudError) as raised:
				frappe_cloud.new_site("rb-0a1b2c3d", DOMAIN, "books-group", SERVER, PLAN, ["frappe"])
		self.assertNotIsInstance(raised.exception, OutcomeUnknown)
		self.assertNotIn(
			"press.api.site.new", [call.args[0].rsplit("/", 1)[1] for call in post.call_args_list]
		)
