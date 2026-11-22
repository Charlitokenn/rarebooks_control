"""Self serve onboarding, the thread (spec 0014 AC-8 to AC-14, AC-21): the marketing site's signed calls, the
claim of a pool site, the Owner step, Ready in the Primary step, the notice to the Worker and the owner link."""

import json
import os
from datetime import timedelta
from unittest.mock import patch

import frappe

from rarebooks_control import onboarding, registry
from rarebooks_control.signing import SIGNATURE_HEADER, is_valid_signature, sign, utc_now
from rarebooks_control.tests.control import SiteResponse, answer, call, issued
from rarebooks_control.tests.test_domains import DomainTestCase
from rarebooks_control.tests.test_registry import SITE

MARKETING = "marketing-test-secret-0123456789abcdef0123456789abcdef"
NOTIFY_URL = "https://rarebooks.example/api/onboarding/notify"
SIGNUP = onboarding.SIGNUP
SETTINGS = onboarding.SETTINGS
POOL = "rb-1a2b3c4d.frappe.cloud"
CLERK = "user_2abcDEF"
ASK = {
	"clerk_user_id": CLERK,
	"email": "Asha.Mrema@Gmail.com",
	"first_name": "Asha",
	"last_name": "Mrema",
	"company_name": " Acme Traders ",
	"subdomain": "acme",
	"time_zone": "Africa/Dar_es_Salaam",
}
CASES = os.path.join(os.path.dirname(__file__), "email_key_cases.json")


class MarketingSite:
	"""Stands in for the Worker's `/api/onboarding/notify`: checks the signature and answers signed, echoing."""

	def __init__(self, status: int = 200):
		self.status = status
		self.notices: list[dict] = []
		self.down = False

	def __call__(self, url, data, headers, timeout):
		import requests

		assert url == NOTIFY_URL
		if self.down:
			raise requests.ConnectionError("down")
		assert is_valid_signature(data, headers.get(SIGNATURE_HEADER), MARKETING)
		body = json.loads(data)
		self.notices.append(body)
		return SiteResponse(
			self.status, {"ok": self.status == 200, "issued_at": body["issued_at"]}, MARKETING
		)


class OnboardingTestCase(DomainTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.delete(SIGNUP)
		settings = frappe.get_single(SETTINGS)
		settings.update(
			{
				"paused": 0,
				"pool_size": 2,
				"max_claims_per_day": 20,
				"marketing_secret": MARKETING,
				"marketing_notify_url": NOTIFY_URL,
			}
		)
		settings.save(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(self._clear_signups)
		self.jobs: list[tuple[str, dict]] = []
		self.start(patch("frappe.enqueue", side_effect=lambda method, **kw: self.jobs.append((method, kw))))
		self.marketing = MarketingSite()
		self.start(patch("rarebooks_control.onboarding.post", side_effect=self.marketing))
		self.sites.answers["claim_site"] = (200, {"status": "claimed"})
		self.sites.answers["owner_link"] = (
			200,
			{"url": "https://acme.rarebooks.cc/update-password?key=k1", "password_set": False},
		)

	def _clear_signups(self):
		frappe.db.rollback()
		frappe.db.delete(SIGNUP)
		frappe.db.commit()

	def ask(self, method, values: dict | None = None, secret: str = MARKETING, **overrides):
		body = {"issued_at": issued(), **(values or {}), **overrides}
		response = call(method, body, secret=secret)
		return response.status_code, answer(response, MARKETING if response.status_code != 401 else None)

	def pool_site(self, site: str = POOL, built_days_ago: int = 1):
		row = self.existing_site(
			site,
			owner_email="",
			pool_status="Ready",
			pool_built_on=utc_now() - timedelta(days=built_days_ago),
		)
		return row

	def run_jobs(self):
		"""Run the queued claim and notice jobs in turn, as the short queue would."""
		while self.jobs:
			method, kwargs = self.jobs.pop(0)
			args = {key: value for key, value in kwargs.items() if key in ("signup", "site")}
			frappe.get_attr(method)(**args)
			frappe.db.commit()

	def signup(self, name: str | None = None):
		name = name or frappe.db.get_value(SIGNUP, {"clerk_user_id": CLERK})
		return frappe.get_doc(SIGNUP, name)

	def walk_domain(self, site: str = POOL, label: str = "acme"):
		domain = f"{label}.rarebooks.cc"
		self.sites.hosts[domain] = site
		self.cloud.dns_seen.add(domain)
		self.runs(5)


class TestEmailKey(OnboardingTestCase):
	# covers: AC-9 (the Worker's emailKey is tested against the same file)
	def test_the_shared_cases(self):
		with open(CASES) as cases:
			for email, key in json.load(cases):
				with self.subTest(email=email):
					self.assertEqual(onboarding.email_key(email), key)


class TestMarketingCalls(OnboardingTestCase):
	# covers: AC-8
	def test_a_forged_or_stale_call_gets_a_401_and_counts_toward_the_alert(self):
		with patch("rarebooks_control.signing.note_signature_failure") as noted:
			status, _body = self.ask(onboarding.check_subdomain, {"subdomain": "acme"}, secret="wrong")
			self.assertEqual(status, 401)
			response = call(
				onboarding.check_subdomain,
				{"issued_at": issued(timedelta(minutes=-6)), "subdomain": "acme"},
				secret=MARKETING,
			)
			self.assertEqual(response.status_code, 401)
			answer(response, MARKETING)
		self.assertEqual(noted.call_count, 2)

	# covers: AC-8
	def test_each_endpoint_has_its_own_limit_a_minute(self):
		with patch.dict(onboarding.LIMITS, {"check_subdomain": 2}):
			statuses = [self.ask(onboarding.check_subdomain, {"subdomain": "acme"})[0] for _n in range(3)]
		self.assertEqual(statuses, [200, 200, 429])
		self.assertEqual(self.ask(onboarding.signup_status, {"clerk_user_id": CLERK})[0], 404)

	# covers: AC-2, AC-21
	def test_check_subdomain_answers_available_or_the_reason_and_writes_nothing(self):
		status, body = self.ask(onboarding.check_subdomain, {"subdomain": " Acme "})
		self.assertEqual((status, body["available"], body["subdomain"]), (200, True, "acme"))
		status, body = self.ask(onboarding.check_subdomain, {"subdomain": "www"})
		self.assertEqual((status, body["available"]), (200, False))
		self.assertIn("reserved", body["reason"])
		self.assertEqual(frappe.db.count(SIGNUP), 0)

		self.ask(onboarding.request_site, ASK)
		status, body = self.ask(onboarding.check_subdomain, {"subdomain": "acme"})
		self.assertFalse(body["available"])
		self.assertIn("a sign up holds it", body["reason"])

	# covers: AC-2, AC-9
	def test_cloudflare_down_answers_try_again(self):
		self.dns.errors["find"] = registry.CloudflareError("GET dns_records failed: ConnectTimeout")
		self.assertEqual(
			self.ask(onboarding.check_subdomain, {"subdomain": "acme"})[1]["refusal"], "try_again"
		)
		status, body = self.ask(onboarding.request_site, ASK)
		self.assertEqual((status, body["refusal"]), (503, "try_again"))
		self.assertEqual(frappe.db.count(SIGNUP), 0)

	# covers: AC-2, AC-9 (found by /check verify: the operator's config message reached the visitor)
	def test_missing_cloudflare_config_answers_try_again_on_both_endpoints(self):
		with patch.dict(frappe.conf, {"cloudflare_api_token": ""}):
			status, body = self.ask(onboarding.request_site, ASK)
			self.assertEqual((status, body.get("refusal")), (503, "try_again"), body)
			self.assertNotIn("reason", body)
			status, body = self.ask(onboarding.check_subdomain, {"subdomain": "acme"})
			self.assertEqual((status, body.get("refusal")), (503, "try_again"), body)
		self.assertEqual(frappe.db.count(SIGNUP), 0)

	# covers: AC-21
	def test_a_label_held_by_a_sign_up_is_taken_for_registration_too(self):
		self.ask(onboarding.request_site, ASK)
		with self.assertRaises(frappe.ValidationError) as caught:
			registry.check_subdomain("acme")
		self.assertIn("taken", str(caught.exception))
		self.assertEqual(registry.check_subdomain("acme", signup=self.signup().name), "acme")


class TestRequestSite(OnboardingTestCase):
	# covers: AC-9, AC-10
	def test_a_request_is_queued_with_its_values_and_its_claim_job(self):
		status, body = self.ask(onboarding.request_site, ASK)
		self.assertEqual(status, 200, body)
		row = self.signup()
		self.assertEqual(body["request_id"], row.name)
		self.assertEqual((body["status"], body["step"]), ("Queued", None))
		self.assertNotIn("address", body)
		self.assertEqual(
			(row.email, row.email_key, row.company_name, row.subdomain, row.time_zone, row.status),
			(
				"Asha.Mrema@Gmail.com",
				"ashamrema@gmail.com",
				"Acme Traders",
				"acme",
				"Africa/Dar_es_Salaam",
				"Queued",
			),
		)
		self.assertTrue(row.requested_on)
		self.assertEqual(self.jobs[0][0], onboarding.CLAIM_JOB)
		self.assertEqual(self.jobs[0][1]["job_id"], f"signup-claim:{row.name}")

	# covers: AC-9
	def test_the_same_user_again_gets_the_same_request_whatever_they_send(self):
		first = self.ask(onboarding.request_site, ASK)[1]
		again = self.ask(onboarding.request_site, {**ASK, "subdomain": "other", "company_name": "X"})
		self.assertEqual(again, (200, {**first, "issued_at": again[1]["issued_at"]}))
		self.assertEqual(frappe.db.count(SIGNUP), 1)

	# covers: AC-9
	def test_checks_run_in_order_and_write_nothing(self):
		self.ask(onboarding.request_site, ASK)
		frappe.db.set_single_value(SETTINGS, "paused", 1)
		frappe.db.commit()
		other = {**ASK, "clerk_user_id": "user_other", "subdomain": "beta"}
		status, body = self.ask(onboarding.request_site, other)
		self.assertEqual((status, body["refusal"]), (503, "paused"))
		frappe.db.set_single_value(SETTINGS, "paused", 0)
		frappe.db.commit()
		for email in ("asha.mrema+books@gmail.com", " ASHAMREMA@googlemail.com "):
			with self.subTest(email=email):
				status, body = self.ask(onboarding.request_site, {**other, "email": email})
				self.assertEqual((status, body["refusal"]), (409, "already_used"))
		fresh = {**other, "email": "beta@example.com"}
		for bad in (
			{"company_name": "  "},
			{"company_name": "x" * 141},
			{"subdomain": "ab"},
			{"subdomain": "acme"},
		):
			with self.subTest(bad=bad):
				self.assertEqual(self.ask(onboarding.request_site, {**fresh, **bad})[0], 422)
		self.assertEqual(frappe.db.count(SIGNUP), 1)

	# covers: AC-9
	def test_an_unknown_zone_becomes_utc_and_a_blank_first_name_the_email_local_part(self):
		self.ask(onboarding.request_site, {**ASK, "time_zone": "Mars/Base", "first_name": " "})
		row = self.signup()
		self.assertEqual((row.time_zone, row.first_name), ("UTC", "Asha.Mrema"))


class TestThread(OnboardingTestCase):
	# covers: AC-4, AC-11, AC-12, AC-13, AC-14
	def test_one_sign_up_claims_a_pool_site_and_is_ready_at_its_address(self):
		self.pool_site()
		self.ask(onboarding.request_site, ASK)
		self.run_jobs()

		row = self.signup()
		self.assertEqual((row.status, row.step, row.hosted_site), ("Preparing", "Domain", POOL))
		site = frappe.get_doc(SITE, POOL)
		self.assertEqual(
			(site.pool_status, site.owner_email, site.company_name, site.subdomain),
			("Claimed", "Asha.Mrema@Gmail.com", "Acme Traders", "acme"),
		)
		claim_body = self.sites.bodies("claim_site")[0]
		self.assertEqual(
			{
				key: claim_body[key]
				for key in ("owner_email", "first_name", "last_name", "company_name", "time_zone")
			},
			{
				"owner_email": "Asha.Mrema@Gmail.com",
				"first_name": "Asha",
				"last_name": "Mrema",
				"company_name": "Acme Traders",
				"time_zone": "Africa/Dar_es_Salaam",
			},
		)
		status, body = self.ask(onboarding.signup_status, {"clerk_user_id": CLERK})
		self.assertEqual((body["status"], body["step"]), ("Preparing", "Domain"))
		self.assertNotIn("address", body)

		self.walk_domain()
		row = self.signup()
		self.assertEqual(row.status, "Ready")
		self.assertTrue(row.ready_on)
		self.assertEqual(frappe.db.get_value(SITE, POOL, "domain_status"), "Active")
		self.assertEqual(row.notify_status, "Ready")

		self.run_jobs()
		self.assertEqual(len(self.marketing.notices), 1)
		notice = self.marketing.notices[0]
		self.assertEqual(
			{key: notice[key] for key in ("request_id", "status", "email", "first_name")},
			{
				"request_id": row.name,
				"status": "Ready",
				"email": "Asha.Mrema@Gmail.com",
				"first_name": "Asha",
			},
		)
		self.assertEqual(self.signup().notified_status, "Ready")

		status, body = self.ask(onboarding.signup_status, {"clerk_user_id": CLERK, "request_id": row.name})
		self.assertEqual(
			(status, body["status"], body["address"]), (200, "Ready", "https://acme.rarebooks.cc")
		)
		status, body = self.ask(onboarding.owner_link, {"clerk_user_id": CLERK, "request_id": row.name})
		self.assertEqual((status, body["url"]), (200, "https://acme.rarebooks.cc/update-password?key=k1"))
		self.assertEqual(self.send_ops.call_count, 0)

	# covers: AC-11
	def test_with_no_ready_site_the_request_stays_queued_and_the_sweep_claims_it_later(self):
		self.ask(onboarding.request_site, ASK)
		self.run_jobs()
		self.assertEqual(self.signup().status, "Queued")
		self.pool_site()
		onboarding.sweep()
		self.run_jobs()
		self.assertEqual(self.signup().status, "Preparing")

	# covers: AC-11
	def test_two_requests_and_one_ready_site_one_wins_the_other_stays_queued(self):
		self.pool_site()
		self.ask(onboarding.request_site, ASK)
		self.ask(
			onboarding.request_site,
			{**ASK, "clerk_user_id": "user_two", "email": "two@example.com", "subdomain": "two"},
		)
		self.run_jobs()
		statuses = sorted(frappe.get_all(SIGNUP, pluck="status"))
		self.assertEqual(statuses, ["Preparing", "Queued"])

	# covers: AC-11
	def test_the_oldest_ready_site_is_claimed_and_building_or_claimed_sites_never(self):
		self.pool_site("rb-newer000.frappe.cloud", built_days_ago=1)
		self.pool_site("rb-older000.frappe.cloud", built_days_ago=5)
		self.existing_site("rb-build000.frappe.cloud", owner_email="", pool_status="Building")
		self.ask(onboarding.request_site, ASK)
		self.run_jobs()
		self.assertEqual(self.signup().hosted_site, "rb-older000.frappe.cloud")

	# covers: AC-10
	def test_over_the_daily_cap_new_requests_stay_queued(self):
		self.pool_site()
		frappe.db.set_single_value(SETTINGS, "max_claims_per_day", 0)
		frappe.db.commit()
		self.ask(onboarding.request_site, ASK)
		self.run_jobs()
		self.assertEqual(self.signup().status, "Queued")

	# covers: AC-12
	def test_the_domain_waits_for_the_owner_step_and_six_failures_fail_the_request(self):
		self.pool_site()
		self.sites.answers["claim_site"] = (409, {"status": "owned"})
		self.ask(onboarding.request_site, ASK)
		self.run_jobs()
		row = self.signup()
		self.assertEqual((row.status, row.step, row.attempts), ("Preparing", "Owner", 1))
		self.walk_domain()
		self.assertIsNone(
			frappe.db.get_value(SITE, POOL, "domain_status") or None, "no domain step before the owner"
		)

		for _try in range(5):
			onboarding.sweep()
			self.run_jobs()
		row = self.signup()
		self.assertEqual((row.status, row.attempts), ("Failed", 6))
		self.assertIn("owner step failed", row.failed_reason)
		self.assertEqual(self.send_ops.call_count, 1)
		self.assertEqual([notice["status"] for notice in self.marketing.notices], ["Failed"])

	# covers: AC-17 (one email, not two, when the domain of a claimed site fails)
	def test_a_failed_domain_fails_the_request_with_one_email(self):
		self.pool_site()
		self.ask(onboarding.request_site, ASK)
		self.run_jobs()
		self.dns.add("acme.rarebooks.cc", "A", "203.0.113.9")
		self.runs()
		self.assertEqual(frappe.db.get_value(SITE, POOL, "domain_status"), "Failed")
		self.assertEqual(self.signup().status, "Failed")
		self.assertEqual(self.send_ops.call_count, 1)

	# covers: AC-13
	def test_a_notice_is_sent_again_until_a_verified_200_then_alerts_once_after_24_hours(self):
		self.pool_site()
		self.ask(onboarding.request_site, ASK)
		self.run_jobs()
		self.marketing.status = 503
		self.walk_domain()
		self.run_jobs()
		onboarding.sweep()
		self.assertEqual(self.signup().notified_status or None, None)
		self.assertEqual(len(self.marketing.notices), 2)

		frappe.db.set_value(SIGNUP, self.signup().name, "notify_started_on", utc_now() - timedelta(hours=25))
		frappe.db.commit()
		onboarding.sweep()
		onboarding.sweep()
		self.assertEqual(len(self.marketing.notices), 2, "no more tries after 24 hours")
		self.assertEqual(self.send_ops.call_count, 1)

		self.marketing.status = 200
		frappe.db.set_value(SIGNUP, self.signup().name, "notify_started_on", utc_now())
		frappe.db.commit()
		onboarding.sweep()
		self.assertEqual(self.signup().notified_status, "Ready")
		onboarding.sweep()
		self.assertEqual(len(self.marketing.notices), 3)

	# covers: AC-14
	def test_owner_link_needs_both_ids_and_a_ready_request(self):
		self.pool_site()
		self.ask(onboarding.request_site, ASK)
		self.run_jobs()
		name = self.signup().name
		self.assertEqual(
			self.ask(onboarding.owner_link, {"clerk_user_id": CLERK, "request_id": name})[0], 404
		)
		self.walk_domain()
		self.assertEqual(
			self.ask(onboarding.owner_link, {"clerk_user_id": "user_other", "request_id": name})[0], 404
		)
		self.assertEqual(self.ask(onboarding.owner_link, {"clerk_user_id": CLERK})[0], 404)
		self.sites.down.add(POOL)
		status, body = self.ask(onboarding.owner_link, {"clerk_user_id": CLERK, "request_id": name})
		self.assertEqual((status, body["refusal"]), (503, "try_again"))

	# covers: AC-4
	def test_signup_status_needs_the_clerk_id_and_a_matching_request_id(self):
		self.ask(onboarding.request_site, ASK)
		name = self.signup().name
		self.assertEqual(self.ask(onboarding.signup_status, {"clerk_user_id": CLERK})[1]["request_id"], name)
		self.assertEqual(
			self.ask(onboarding.signup_status, {"clerk_user_id": CLERK, "request_id": "SU-99999"})[0], 404
		)
		self.assertEqual(self.ask(onboarding.signup_status, {"request_id": name})[0], 404)
		self.assertEqual(
			self.ask(onboarding.signup_status, {"clerk_user_id": CLERK})[1]["email"], ASK["email"]
		)

	def test_answers_are_signed_with_the_marketing_secret_and_echo_issued_at(self):
		body = {"issued_at": issued(), "clerk_user_id": CLERK}
		raw = json.dumps(body).encode()
		response = call(onboarding.signup_status, raw, signature=sign(raw, MARKETING))
		self.assertTrue(
			is_valid_signature(response.get_data(), response.headers.get(SIGNATURE_HEADER), MARKETING)
		)
		self.assertEqual(json.loads(response.get_data())["issued_at"], body["issued_at"])


class TestPoolSkip(OnboardingTestCase):
	# covers: AC-20
	def test_an_unclaimed_pool_site_gets_no_round_push_or_state_and_a_claimed_one_does(self):
		from rarebooks_control import api, push, removal
		from rarebooks_control.tests.test_api import site_body

		row = self.pool_site(built_days_ago=40)
		building = self.existing_site("rb-build000.frappe.cloud", owner_email="", pool_status="Building")
		frappe.db.set_value(SITE, {"name": ("in", (row.name, building.name))}, "push_status", "Pending")
		frappe.db.commit()

		self.assertNotIn(POOL, removal.round_sites())
		self.assertNotIn(building.name, removal.round_sites())
		self.queue_push.reset_mock()
		push.sweep()
		self.assertNotIn(POOL, [args.args[0] for args in self.queue_push.call_args_list])
		response = call(api.get_state, site_body(POOL), row.get_password("control_secret"))
		self.assertEqual((response.status_code, answer(response)["refusal"]), (409, "config_pending"))

		self.ask(onboarding.request_site, ASK)
		self.run_jobs()
		self.assertEqual(frappe.db.get_value(SITE, POOL, "pool_status"), "Claimed")
		self.assertIn(POOL, removal.round_sites())
		response = call(api.get_state, site_body(POOL), row.get_password("control_secret"))
		self.assertEqual(response.status_code, 200)
