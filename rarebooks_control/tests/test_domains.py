"""Custom domains on registration (spec 0012): the label rules and clash checks, Add domain, the five steps of
the 5 minute job, the return URL hosts, failures and Retry domain, cleanup at archive, the digest, and the
Cloudflare client."""

from datetime import timedelta
from unittest.mock import Mock, patch

import frappe
import requests
from frappe.tests import UnitTestCase

from rarebooks_control import api, registry, removal, removal_mail
from rarebooks_control.providers import cloudflare
from rarebooks_control.providers.cloudflare import CloudflareError
from rarebooks_control.providers.frappe_cloud import FrappeCloudError, OutcomeUnknown
from rarebooks_control.signing import utc_now
from rarebooks_control.tests.control import FakeCloudflare
from rarebooks_control.tests.data_model import BILLING_MANAGER, SYSTEM_MANAGER, make_site
from rarebooks_control.tests.test_registry import NEW, SITE, RegistryTestCase

OLD = "old.frappe.cloud"
LABEL = "old"
DOMAIN = f"{LABEL}.{FakeCloudflare.ZONE_DOMAIN}"
CASE = "Books Removal Case"


class DomainTestCase(RegistryTestCase):
	def setUp(self):
		super().setUp()
		frappe.cache.delete_keys("domain-retry:")

	def existing_site(self, site: str = OLD, **values):
		"""A site registered before spec 0012: Active config, no subdomain, on its `.frappe.cloud` address."""
		row = make_site(site, **values)
		self.cloud.add(site)
		self.sites.hold(row)
		frappe.db.commit()
		return row

	def with_domain(self, site: str = OLD, label: str = LABEL, **values):
		row = self.existing_site(site, **values)
		self.as_billing_manager(registry.add_domain, site, label)
		domain = f"{label}.{FakeCloudflare.ZONE_DOMAIN}"
		self.sites.hosts[domain] = site
		self.cloud.dns_seen.add(domain)
		return row

	def runs(self, count: int = 1):
		for _run in range(count):
			registry.run_domain_steps()

	def row(self, site: str = OLD):
		return frappe.get_doc(SITE, site)

	def step(self, site: str = OLD) -> tuple:
		row = self.row(site)
		# A blank Select reads as "", like NULL.
		return row.domain_status or None, row.domain_step or None

	def age_step(self, delta: timedelta, site: str = OLD):
		frappe.db.set_value(SITE, site, "domain_step_started_on", utc_now() - delta)
		frappe.db.commit()

	def dropping_case(self, site: str = OLD):
		case = frappe.get_doc(
			{
				"doctype": CASE,
				"site": site,
				"status": "Dropping",
				"mode": "live",
				"kind": "Trial",
				"removal_on": frappe.utils.today(),
				"owner_email": "owner@example.com",
				"site_url": f"https://{site}",
			}
		)
		case.insert(ignore_permissions=True)
		frappe.db.commit()
		return case

	def refused(self, method, *args, user: str = BILLING_MANAGER) -> str:
		frappe.set_user(user)
		try:
			with self.assertRaises(frappe.ValidationError) as caught:
				method(*args)
			return str(caught.exception)
		finally:
			frappe.set_user("Administrator")
			frappe.db.rollback()


class TestLabel(DomainTestCase):
	# covers: AC-1
	def test_only_good_labels_pass_and_input_is_trimmed_and_lowercased(self):
		for bad in ("ab", "-ab", "ab-", "xn--ab", "www", "Ab Cd", "a" * 41, "a.b", "", None):
			with self.assertRaises(frappe.ValidationError, msg=repr(bad)):
				registry.check_subdomain(bad)
		self.assertEqual(registry.check_subdomain(" Site1 "), "site1")
		self.assertEqual(registry.check_subdomain("a" * 40), "a" * 40)
		self.assertEqual(registry.check_subdomain("my-shop"), "my-shop")

	# covers: AC-2
	def test_a_label_held_anywhere_is_taken_and_nothing_is_written(self):
		make_site("gone.frappe.cloud", status="Removed", subdomain="gone")
		self.dns.add("arecord.rarebooks.cc", "A", "203.0.113.9")
		self.cloud.elsewhere["elsewhere.rarebooks.cc"] = "theirs.frappe.cloud"
		for label in ("gone", "arecord", "elsewhere"):
			with self.assertRaises(frappe.ValidationError) as caught:
				registry.check_subdomain(label)
			self.assertIn("taken", str(caught.exception))

	# covers: AC-2
	def test_cloudflare_or_frappe_cloud_unreachable_refuses_with_try_again(self):
		self.dns.errors["find"] = CloudflareError("GET dns_records failed: ConnectTimeout")
		with self.assertRaises(frappe.ValidationError) as caught:
			registry.check_subdomain("free")
		self.assertIn("Try again", str(caught.exception))
		del self.dns.errors["find"]
		self.cloud.errors["domain_exists"] = FrappeCloudError(
			"press.api.site.domain_exists could not be reached"
		)
		with self.assertRaises(frappe.ValidationError):
			registry.check_subdomain("free")

	# covers: AC-2
	def test_the_unique_column_stops_a_second_row_and_blank_is_null(self):
		make_site("one.frappe.cloud", subdomain="")
		make_site("two.frappe.cloud")
		self.assertIsNone(self.row("one.frappe.cloud").subdomain)
		frappe.db.set_value(SITE, "one.frappe.cloud", "subdomain", "same")
		row = self.row("two.frappe.cloud")
		row.subdomain = "same"
		with self.assertRaises(frappe.ValidationError) as caught:
			registry._save_label(row, "same")
		self.assertIn("taken", str(caught.exception))

	# covers: AC-13
	def test_missing_cloudflare_config_refuses_register_and_add_domain_and_runs_no_step(self):
		self.with_domain()
		self.existing_site("other.frappe.cloud")
		with patch.dict(frappe.conf, {"cloudflare_api_token": None}):
			self.assertIn(
				"custom domain config", self.refused(registry.add_domain, "other.frappe.cloud", "other")
			)
			self.cloud.add(NEW)
			self.assertIn("custom domain config", self.refused(self.register))
			self.runs(3)
		self.assertFalse(frappe.db.exists(SITE, NEW))
		self.assertEqual(self.step(), (None, None))
		self.assertEqual(self.dns.calls, [("find", DOMAIN)])


class TestRegisterWithSubdomain(DomainTestCase):
	# covers: AC-3, AC-5, AC-8
	def test_a_new_site_registers_on_frappe_cloud_then_walks_to_its_address_in_five_runs(self):
		self.cloud.add(NEW)
		self.register(subdomain=" New ")
		row = self.row(NEW)
		self.assertEqual(
			(row.subdomain, row.domain_status or None, row.site_url), ("new", None, f"https://{NEW}")
		)
		domain = "new.rarebooks.cc"
		self.sites.hosts[domain] = NEW
		self.cloud.dns_seen.add(domain)
		# No step while the config is Pending.
		self.runs()
		self.assertEqual(self.step(NEW), (None, None))
		self.take_config()
		registry.run_pings()
		self.assertEqual(self.row(NEW).config_status, "Active")
		self.assertEqual(self.step(NEW), ("Working", "Frappe Cloud"))
		self.runs(4)
		row = self.row(NEW)
		self.assertEqual((row.domain_status, row.site_url), ("Active", f"https://{domain}"))
		self.assertEqual(self.dns.named(domain)[0]["content"], NEW)
		self.assertFalse(self.dns.named(domain)[0]["proxied"])
		self.assertIn(("create", domain, NEW, f"rarebooks {NEW}"), self.dns.calls)

	# covers: AC-2, AC-3
	def test_a_taken_subdomain_refuses_registration_before_anything_is_written(self):
		self.cloud.add(NEW)
		make_site("held.frappe.cloud", subdomain="new")
		frappe.db.commit()
		self.assertIn("taken", self.refused(self.register))
		self.assertFalse(frappe.db.exists(SITE, NEW))
		self.assertEqual(self.cloud.count("update_config"), 0)


class TestAddDomain(DomainTestCase):
	# covers: AC-4, AC-5, AC-6, AC-7, AC-8
	def test_an_existing_site_walks_every_step_and_switches_site_url_once(self):
		self.with_domain()
		self.assertEqual((self.row().subdomain, self.row().domain_status or None), (LABEL, None))
		expected = [
			("Working", "Frappe Cloud"),
			("Working", "Certificate"),
			("Working", "Ping"),
			("Working", "Primary"),
			("Active", None),
		]
		for status in expected:
			self.runs()
			self.assertEqual(self.step(), status)
		row = self.row()
		self.assertEqual(row.site_url, f"https://{DOMAIN}")
		self.assertTrue(row.dns_record_id)
		self.assertIn(f"https://{DOMAIN}/api/method/frappe_books.subscription.control_ping", self.sites.urls)
		self.assertEqual(self.cloud.domain_rows[OLD][0]["primary"], True)
		self.runs(2)
		self.assertEqual(self.cloud.count("set_host_name"), 1)

	# covers: AC-4
	def test_refused_on_a_removed_site_a_dropping_site_and_one_with_a_subdomain(self):
		self.existing_site("removed.frappe.cloud", status="Removed")
		self.existing_site("dropping.frappe.cloud")
		self.dropping_case("dropping.frappe.cloud")
		self.with_domain()
		frappe.db.commit()
		self.assertIn("removed", self.refused(registry.add_domain, "removed.frappe.cloud", "removed"))
		self.assertIn("dropped", self.refused(registry.add_domain, "dropping.frappe.cloud", "dropping"))
		self.assertIn("already has", self.refused(registry.add_domain, OLD, "another"))

	# covers: AC-12
	def test_only_a_billing_manager_adds_or_retries_and_no_one_edits_the_fields(self):
		self.existing_site()
		for method, args in ((registry.add_domain, (OLD, LABEL)), (registry.retry_domain, (OLD,))):
			frappe.set_user(SYSTEM_MANAGER)
			with self.assertRaises(frappe.PermissionError):
				method(*args)
			frappe.set_user("Administrator")
		frappe.set_user(BILLING_MANAGER)
		try:
			row = self.row()
			row.subdomain = "byhand"
			with self.assertRaises(frappe.ValidationError):
				row.save()
		finally:
			frappe.set_user("Administrator")
			frappe.db.rollback()
		meta = frappe.get_meta(SITE)
		for field in ("subdomain", "domain_status", "domain_step", "domain_step_started_on"):
			self.assertTrue(meta.get_field(field).read_only, field)

	# covers: AC-12
	def test_a_subdomain_is_never_changed_once_set(self):
		self.with_domain()
		row = self.row()
		row.subdomain = "renamed"
		with self.assertRaises(frappe.ValidationError):
			row.save(ignore_permissions=True)


class TestSteps(DomainTestCase):
	# covers: AC-5, AC-10
	def test_no_step_while_config_isnt_active_a_rotation_is_pending_or_a_case_is_dropping(self):
		self.with_domain()
		self.runs()
		self.age_step(timedelta(hours=3))
		for blocker in ({"config_status": "Pending"}, {"pending_secret": "rotating"}):
			row = self.row()
			row.update(blocker)
			row.save(ignore_permissions=True)
			frappe.db.commit()
			self.runs()
			self.assertEqual(self.step(), ("Working", "Frappe Cloud"))
			frappe.db.set_value(SITE, OLD, {"config_status": "Active", "pending_secret": None})
			frappe.db.commit()
		case = self.dropping_case()
		self.runs()
		self.assertEqual(self.step(), ("Working", "Frappe Cloud"))
		case.delete(ignore_permissions=True)
		frappe.db.commit()
		# The clock restarted while skipped, so the site goes on instead of failing.
		self.runs()
		self.assertEqual(self.step(), ("Working", "Certificate"))
		self.send_ops.assert_not_called()

	# covers: AC-6
	def test_dns_adopts_our_record_and_a_lost_create_answer(self):
		self.with_domain()
		record = self.dns.add(DOMAIN, "CNAME", OLD.upper() + ".")
		self.runs()
		self.assertEqual(self.row().dns_record_id, record)
		self.assertNotIn("create", [call[0] for call in self.dns.calls])

		self.with_domain("lost.frappe.cloud", "lost")
		self.dns.lose_create_answer = True
		self.runs()
		self.assertEqual(self.step("lost.frappe.cloud"), ("Working", "DNS"))
		self.runs()
		row = self.row("lost.frappe.cloud")
		self.assertEqual(row.domain_step, "Frappe Cloud")
		self.assertEqual(row.dns_record_id, self.dns.named("lost.rarebooks.cc")[0]["id"])

	# covers: AC-6
	def test_a_foreign_or_proxied_record_or_a_bad_token_fails_at_once_with_one_email(self):
		self.with_domain()
		foreign = self.dns.add(DOMAIN, "CNAME", "elsewhere.example.com")
		self.runs(2)
		self.assertEqual(self.step(), ("Failed", "DNS"))
		self.assertIn(foreign, self.dns.records)
		self.assertEqual(self.send_ops.call_count, 1)

		self.with_domain("proxied.frappe.cloud", "proxied")
		self.dns.add("proxied.rarebooks.cc", "CNAME", "proxied.frappe.cloud", proxied=True)
		self.runs()
		self.assertEqual(self.step("proxied.frappe.cloud"), ("Failed", "DNS"))
		self.assertIn("DNS only", str(self.send_ops.call_args))

		self.with_domain("token.frappe.cloud", "token")
		self.dns.errors["find"] = CloudflareError("GET dns_records answered HTTP 403 []", 403)
		self.runs()
		self.assertEqual(self.step("token.frappe.cloud"), ("Failed", "DNS"))

	# covers: AC-5
	def test_a_cloudflare_5xx_or_frappe_cloud_error_waits_on_the_step(self):
		self.with_domain()
		self.dns.errors["find"] = CloudflareError("GET dns_records answered HTTP 503 []", 503)
		self.runs(2)
		self.assertEqual(self.step(), ("Working", "DNS"))
		del self.dns.errors["find"]
		self.runs()
		self.cloud.errors["domains"] = OutcomeUnknown("press.api.site.domains sent, no answer (ReadTimeout)")
		self.runs(2)
		self.assertEqual(self.step(), ("Working", "Frappe Cloud"))
		self.send_ops.assert_not_called()

	# covers: AC-7
	def test_frappe_cloud_waits_for_dns_and_a_broken_certificate_is_retried_once_in_30_minutes(self):
		self.with_domain()
		self.cloud.dns_seen.clear()
		self.cloud.new_domain_status = "Pending"
		self.runs(3)
		self.assertEqual(self.step(), ("Working", "Frappe Cloud"))
		self.assertEqual(self.cloud.count("add_domain"), 0)
		self.cloud.dns_seen.add(DOMAIN)
		self.runs()
		self.assertEqual(self.step(), ("Working", "Certificate"))
		self.runs()
		self.assertEqual(self.step(), ("Working", "Certificate"))
		self.cloud.domain_status(OLD, DOMAIN, "Broken")
		self.runs(3)
		self.assertEqual(self.cloud.count("retry_add_domain"), 1)
		self.cloud.domain_status(OLD, DOMAIN, "Active")
		self.runs()
		self.assertEqual(self.step(), ("Working", "Ping"))

	# covers: AC-7
	def test_a_domain_in_use_elsewhere_on_frappe_cloud_fails_at_once(self):
		self.with_domain()
		self.runs()
		self.cloud.elsewhere[DOMAIN] = "theirs.frappe.cloud"
		self.runs()
		self.assertEqual(self.step(), ("Failed", "Frappe Cloud"))
		self.assertEqual(self.send_ops.call_count, 1)

	# covers: AC-8
	def test_a_failed_ping_never_moves_site_url(self):
		self.with_domain()
		self.runs(3)
		self.sites.down.add(DOMAIN)
		self.runs(2)
		self.assertEqual(self.step(), ("Working", "Ping"))
		self.sites.down.clear()
		self.sites.hosts[DOMAIN] = "someone-else.frappe.cloud"
		self.runs()
		self.assertEqual(self.step(), ("Working", "Ping"))
		self.assertEqual(self.row().site_url, f"https://{OLD}")

	# covers: AC-8
	def test_an_unknown_set_host_name_keeps_the_step_and_switches_once_next_run(self):
		self.with_domain()
		self.runs(4)
		self.cloud.errors["set_host_name"] = OutcomeUnknown("press.api.site.set_host_name sent, no answer")
		self.runs()
		self.assertEqual(self.step(), ("Working", "Primary"))
		self.assertEqual(self.row().site_url, f"https://{OLD}")
		del self.cloud.errors["set_host_name"]
		self.runs(2)
		self.assertEqual(self.step(), ("Active", None))
		self.assertEqual(self.cloud.count("set_host_name"), 2)

	# covers: AC-9
	def test_a_return_url_on_either_address_passes_before_and_after_the_switch(self):
		self.with_domain()
		other = "https://evil.example.com/books"
		for phase in ("before", "after"):
			row = self.row()
			self.assertTrue(api._is_return_url(f"https://{OLD}/books/billing", row), phase)
			self.assertTrue(api._is_return_url(f"https://{DOMAIN}/books/billing", row), phase)
			self.assertFalse(api._is_return_url(other, row), phase)
			self.assertFalse(api._is_return_url(f"http://{DOMAIN}/books", row), phase)
			self.runs(5)
		self.assertEqual(self.row().site_url, f"https://{DOMAIN}")


class TestFailures(DomainTestCase):
	# covers: AC-10
	def test_two_hours_in_one_step_fails_with_one_email_and_retry_resumes_there(self):
		self.with_domain()
		self.runs()
		self.cloud.dns_seen.clear()
		self.age_step(timedelta(hours=2, minutes=1))
		self.runs(3)
		self.assertEqual(self.step(), ("Failed", "Frappe Cloud"))
		self.assertEqual(self.send_ops.call_count, 1)
		self.assertIn("Retry domain", str(self.send_ops.call_args))
		self.cloud.dns_seen.add(DOMAIN)
		self.assertEqual(
			self.as_billing_manager(registry.retry_domain, OLD),
			{"domain_status": "Working", "domain_step": "Frappe Cloud"},
		)
		self.assertLess(utc_now() - self.row().domain_step_started_on, timedelta(minutes=1))
		self.runs()
		self.assertEqual(self.step(), ("Working", "Certificate"))

	# covers: AC-10
	def test_retry_is_refused_unless_failed(self):
		self.with_domain()
		self.assertIn("hasn't failed", self.refused(registry.retry_domain, OLD))


class TestCleanup(DomainTestCase):
	def active_domain(self, site: str = OLD, label: str = LABEL):
		self.with_domain(site, label)
		self.runs(5)
		self.assertEqual(self.step(site), ("Active", None))

	def archived(self, site: str = OLD):
		"""Frappe Cloud's own archive drops the host name and deletes the site's domains."""
		self.cloud.sites[site]["status"] = "Archived"
		self.cloud.domain_rows.pop(site, None)

	# covers: AC-11
	def test_the_archive_mark_starts_cleanup_and_the_sweep_runs_with_mode_off(self):
		self.active_domain()
		case = self.dropping_case()
		removal._write_archive_mark(case.name, utc_now())
		frappe.db.commit()
		self.assertEqual(self.step(), ("Working", "Cleanup"))
		started = self.row().domain_step_started_on
		removal.mark_removed(frappe.get_doc(CASE, case.name))
		self.assertEqual(self.row().domain_step_started_on, started)
		self.archived()
		removal.run_round()
		row = self.row()
		self.assertEqual(
			(row.domain_status or None, row.domain_step, row.subdomain), (None, "Cleanup", LABEL)
		)
		self.assertTrue(row.domain_cleaned_on)
		self.assertEqual(self.dns.named(DOMAIN), [])
		with self.assertRaises(frappe.ValidationError):
			registry.check_subdomain(LABEL)

	# covers: AC-11
	def test_mark_removed_starts_it_when_the_mark_didnt_and_errors_wait_then_alert_once(self):
		self.active_domain()
		case = self.dropping_case()
		removal.mark_removed(case)
		self.assertEqual(self.step(), ("Working", "Cleanup"))
		self.dns.errors["delete"] = CloudflareError("DELETE dns_records answered HTTP 500 []", 500)
		registry.sweep_domain_cleanup()
		self.assertEqual(self.step(), ("Working", "Cleanup"))
		self.send_ops.assert_not_called()
		self.age_step(timedelta(hours=25))
		registry.sweep_domain_cleanup()
		registry.sweep_domain_cleanup()
		self.assertEqual(self.send_ops.call_count, 1)
		del self.dns.errors["delete"]
		self.archived()
		registry.sweep_domain_cleanup()
		self.assertEqual(self.step(), (None, "Cleanup"))

	# covers: AC-11
	def test_a_foreign_record_is_left_alone_and_a_site_frappe_cloud_cant_find_counts_as_gone(self):
		self.with_domain()
		foreign = self.dns.add(DOMAIN, "A", "203.0.113.9")
		self.runs()
		self.assertEqual(self.step(), ("Failed", "DNS"))
		removal.mark_removed(self.dropping_case())
		del self.cloud.sites[OLD]
		registry.sweep_domain_cleanup()
		self.assertEqual(self.step(), (None, "Cleanup"))
		self.assertIn(foreign, self.dns.records)

	# covers: AC-11
	def test_a_listed_domain_is_removed_on_frappe_cloud_and_retry_isnt_offered_in_cleanup(self):
		self.with_domain()
		self.runs(2)
		removal.mark_removed(self.dropping_case())
		self.assertIn("removed", self.refused(registry.retry_domain, OLD))
		registry.sweep_domain_cleanup()
		self.assertEqual(self.cloud.count("remove_domain"), 1)
		self.assertEqual(self.cloud.domain_rows[OLD], [])
		self.assertEqual(self.step(), (None, "Cleanup"))
		registry.sweep_domain_cleanup()
		self.assertEqual(self.cloud.count("remove_domain"), 1)


class TestVisibility(DomainTestCase):
	# covers: AC-14
	def test_the_digest_lists_failed_and_long_working_sites_only(self):
		self.with_domain("failed.frappe.cloud", "failed")
		self.with_domain("slow.frappe.cloud", "slow")
		self.with_domain("fresh.frappe.cloud", "fresh")
		self.runs()
		frappe.db.set_value(SITE, "failed.frappe.cloud", "domain_status", "Failed")
		self.age_step(timedelta(minutes=40), "slow.frappe.cloud")
		self.age_step(timedelta(minutes=10), "fresh.frappe.cloud")
		lines = registry.domain_digest_lines()
		self.assertEqual(lines[0], "Sites whose domain is stuck or failed: 2")
		self.assertTrue(any(line.startswith("failed.frappe.cloud: Failed") for line in lines))
		self.assertTrue(any(line.startswith("slow.frappe.cloud: Working (Frappe Cloud)") for line in lines))
		self.assertFalse(any("fresh" in line for line in lines))
		removal_mail.send_digest()
		self.assertIn("slow.frappe.cloud", str(self.send_ops.call_args))

	# covers: AC-12
	def test_no_token_reaches_the_error_log(self):
		self.with_domain()
		self.dns.errors["find"] = CloudflareError("GET dns_records answered HTTP 503 []", 503)
		self.runs()
		logs = frappe.get_all(
			"Error Log", fields=["error"], filters={"creation": (">=", utc_now() - timedelta(minutes=5))}
		)
		for log in logs:
			self.assertNotIn(FakeCloudflare.CONF["cloudflare_api_token"], log.error or "")


class UnitTestCloudflareClient(UnitTestCase):
	def response(self, status=200, body=None):
		response = Mock(status_code=status)
		response.json.return_value = body if body is not None else {"success": True, "result": []}
		return response

	# covers: AC-13
	def test_calls_the_zone_with_a_bearer_token_a_timeout_and_no_redirects(self):
		with (
			patch.dict(frappe.conf, FakeCloudflare.CONF),
			patch("rarebooks_control.providers.cloudflare.requests.request") as request,
		):
			request.return_value = self.response(body={"success": True, "result": {"id": "rec9"}})
			self.assertEqual(cloudflare.create_cname(DOMAIN, OLD, f"rarebooks {OLD}"), "rec9")
			method, url = request.call_args.args
			kwargs = request.call_args.kwargs
			self.assertEqual((method, url), ("POST", f"{cloudflare.BASE_URL}/zones/zone123/dns_records"))
			self.assertEqual(kwargs["headers"], {"Authorization": "Bearer cf-test-token-never-logged"})
			self.assertEqual((kwargs["timeout"], kwargs["allow_redirects"]), (30, False))
			self.assertEqual(
				kwargs["json"],
				{
					"type": "CNAME",
					"name": DOMAIN,
					"content": OLD,
					"proxied": False,
					"ttl": 1,
					"comment": f"rarebooks {OLD}",
				},
			)

	# covers: AC-6, AC-12, AC-13
	def test_only_http_200_with_success_counts_and_errors_hold_no_token(self):
		with (
			patch.dict(frappe.conf, FakeCloudflare.CONF),
			patch("rarebooks_control.providers.cloudflare.requests.request") as request,
		):
			request.return_value = self.response(200, {"success": False, "errors": [{"code": 81053}]})
			with self.assertRaises(CloudflareError) as caught:
				cloudflare.create_cname(DOMAIN, OLD, "c")
			self.assertTrue(caught.exception.exists)
			request.return_value = self.response(403, {"success": False, "errors": [{"code": 10000}]})
			with self.assertRaises(CloudflareError) as caught:
				cloudflare.find_records(DOMAIN)
			self.assertTrue(caught.exception.unauthorized)
			request.side_effect = requests.ReadTimeout("cf-test-token-never-logged")
			with self.assertRaises(CloudflareError) as caught:
				cloudflare.find_records(DOMAIN)
			self.assertTrue(cloudflare.is_retryable(caught.exception))
			self.assertNotIn("cf-test-token", str(caught.exception))
			request.side_effect = None
			request.return_value = self.response(404, {"success": False, "errors": [{"code": 81044}]})
			cloudflare.delete_record("gone")

	# covers: AC-13
	def test_missing_config_fails_before_any_call(self):
		with (
			patch.dict(frappe.conf, {**FakeCloudflare.CONF, "cloudflare_zone_id": ""}),
			patch("rarebooks_control.providers.cloudflare.requests.request") as request,
		):
			self.assertFalse(cloudflare.is_configured())
			with self.assertRaises(CloudflareError):
				cloudflare.find_records(DOMAIN)
			request.assert_not_called()
