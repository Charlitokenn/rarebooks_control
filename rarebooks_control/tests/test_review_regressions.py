"""Regression checks for signed input, provider dates, and push destination boundaries."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from rarebooks_control import api, billing, destinations, signing, webhooks
from rarebooks_control.providers import clickpesa
from rarebooks_control.tests.control import clickpesa_conf


class UnitTestReviewRegressions(UnitTestCase):
	def test_malformed_urls_and_overflowing_dates_are_refused(self):
		# covers: AC-5
		self.assertFalse(api._is_https("https://[broken"))
		self.assertIsNone(signing.parse_issued_at("0001-01-01T00:00:00+23:00"))
		self.assertEqual(signing.parse_issued_at("2026-11-18T12:00:00+02:00"), datetime(2026, 11, 18, 10))

	def test_checksum_rejects_surrogates_and_accepts_unicode(self):
		# covers: AC-7
		with clickpesa_conf():
			body = {"name": "Asha café"}
			body["checksum"] = clickpesa.checksum(body, frappe.conf.clickpesa_checksum_key)
			self.assertTrue(clickpesa.is_valid_checksum(body))
			body["name"] = "\ud800"
			self.assertFalse(clickpesa.is_valid_checksum(body))

	def test_paid_items_sort_instants_and_break_ties_by_id(self):
		# covers: AC-7, AC-8
		items = [
			{"id": "B", "status": "SUCCESS", "createdAt": "2026-11-18T09:00:00Z"},
			{"id": "A", "status": "SUCCESS", "createdAt": "2026-11-18T12:00:00+03:00"},
			{"id": "C", "status": "SETTLED", "createdAt": "2026-11-18T10:00:00+02:00"},
		]
		self.assertEqual([item["id"] for item in billing.paid_items(items)], ["C", "A", "B"])
		for invalid in (None, "bad", "2026-11-18T10:00:00"):
			with self.subTest(invalid=invalid), self.assertRaises(clickpesa.ClickPesaError):
				billing.paid_items([{"status": "SUCCESS", "createdAt": invalid}])

	def test_reuse_requires_matching_terms_and_a_provider_link(self):
		# covers: AC-3
		plan = SimpleNamespace(name="plan", amount=120000, currency="TZS")
		with (
			# Patching every get_value would also catch the time zone lookup inside now_datetime.
			patch.object(api, "now_datetime", return_value=datetime(2026, 11, 19)),
			patch.object(frappe.db, "get_value", return_value="checkout") as lookup,
		):
			self.assertEqual(api._reusable_checkout("site", plan), "checkout")
		filters = lookup.call_args.args[1]
		self.assertEqual((filters["amount"], filters["currency"]), (120000, "TZS"))
		self.assertEqual(filters["provider_url"], ("is", "set"))

	def test_provider_call_follows_durable_allocation(self):
		# covers: AC-3, AC-7
		checkout = Mock(name="checkout")
		checkout.name = "CHK-00042"
		steps = Mock()
		steps.attach_mock(checkout.insert, "insert")
		plan = SimpleNamespace(
			name="plan", amount=120000, currency="TZS", provider="ClickPesa", billing_period="Yearly"
		)
		row = SimpleNamespace(name="site", owner_email="owner@example.com")
		with (
			patch.object(frappe, "new_doc", return_value=checkout),
			patch.object(frappe.db, "commit") as commit,
			patch.object(
				clickpesa, "create_checkout_link", side_effect=clickpesa.ClickPesaError("lost")
			) as provider,
		):
			steps.attach_mock(commit, "commit")
			steps.attach_mock(provider, "provider")
			with self.assertRaises(clickpesa.ClickPesaError):
				billing.new_checkout(row, plan, "https://site/", "hash")
		self.assertEqual([call[0] for call in steps.mock_calls], ["insert", "commit", "provider"])
		checkout.save.assert_not_called()

	def test_public_address_policy_rejects_internal_and_mixed_dns(self):
		# covers: AC-13
		for address in (
			"127.0.0.1",
			"10.0.0.1",
			"169.254.169.254",
			"::1",
			"fc00::1",
			"224.0.0.1",
			"::ffff:8.8.8.8",
		):
			self.assertFalse(destinations.allowed_address(address))
		self.assertTrue(destinations.allowed_address("8.8.8.8"))
		with patch.object(
			destinations.socket,
			"getaddrinfo",
			return_value=[(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("127.0.0.1", 443))],
		):
			self.assertFalse(destinations.public_destination("https://example.com"))
		self.assertFalse(destinations.public_destination("https://[broken"))

	def test_transport_pins_address_and_preserves_tls_hostname(self):
		# covers: AC-13
		with (
			patch.object(destinations, "resolve_destination", return_value=("books.example.com", "8.8.8.8")),
			patch.object(destinations.requests, "Session") as factory,
		):
			destinations.post("https://books.example.com/api", headers={}, data=b"{}", timeout=10)
			session = factory.return_value.__enter__.return_value
			adapter = session.mount.call_args.args[1]
			self.assertFalse(session.trust_env)
			self.assertEqual(adapter.pool.host, "8.8.8.8")
			self.assertEqual(adapter.pool.assert_hostname, "books.example.com")
			self.assertEqual(adapter.pool.conn_kw["server_hostname"], "books.example.com")
			self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
			adapter.close()

	def test_webhook_limit_uses_response_path(self):
		# covers: AC-7, AC-22
		with (
			patch.object(signing, "over_limit", return_value=True),
			patch.object(webhooks, "answer", return_value="limited") as answer,
		):
			self.assertEqual(webhooks.clickpesa(), "limited")
		answer.assert_called_once_with("rate_limited", 429)

	def test_shared_hook_versions_only_changed_payloads(self):
		# covers: AC-13
		from rarebooks_control.rarebooks_control.doctype.books_hosted_site.books_hosted_site import (
			BooksHostedSite,
		)

		before = SimpleNamespace(
			paid_until=None,
			provider=None,
			billing_period=None,
			auto_renews=0,
			provider_reference=None,
			exempt=0,
			state_version=4,
		)
		row = SimpleNamespace(**vars(before), name="site", get_doc_before_save=lambda: before)
		with patch("rarebooks_control.push.queue_push") as queue:
			BooksHostedSite.before_save(row)
			queue.assert_not_called()
			self.assertEqual(row.state_version, 4)
			row.exempt = 1
			BooksHostedSite.before_save(row)
			self.assertEqual((row.state_version, row.push_status), (5, "Pending"))
			queue.assert_called_once_with("site")

	def test_payment_uses_provider_time_in_utc(self):
		# covers: AC-8
		row = Mock(paid_until=None, trial_ends_on=None, time_zone=None)
		checkout = Mock(paid_on=None, order_reference="RB00000042")
		with (
			patch.object(billing, "clickpesa_paid_until", return_value="2027-11-17") as entitlement,
			patch.object(billing, "send_receipt"),
		):
			billing.apply_clickpesa(row, checkout, {"createdAt": "2026-11-18T01:00:00+03:00"})
		self.assertEqual(checkout.paid_on, datetime(2026, 11, 17, 22))
		self.assertEqual(entitlement.call_args.args[1], checkout.paid_on)

	def test_existing_registration_uses_stored_secret_or_conflict(self):
		# covers: AC-1
		request = Mock()
		existing = SimpleNamespace(hosting_bench="bench", status="Active")
		with patch.object(frappe, "get_doc") as get_doc:
			get_doc.return_value.get_password.return_value = "stored"
			api._registered(request, {"site": "site"}, existing, "bench", "site")
			request.answer.assert_called_with({"site": "site", "secret": "stored"})
			api._registered(request, {"site": "site"}, existing, "other", "site")
			request.answer.assert_called_with({"site": "site"}, 409)

	def test_registration_race_rechecks_under_lock_and_propagates_other_errors(self):
		# covers: AC-1
		body = {
			"site": "site",
			"site_url": "https://site",
			"owner_email": "owner@example.com",
			"bench": "bench",
		}
		request = Mock(issued_at="2026-11-18T12:00:00Z")
		existing = SimpleNamespace(hosting_bench="bench", status="Active")
		with (
			patch.object(signing, "read_body", return_value=(b"{}", body)),
			patch.object(signing, "runner_secret", return_value="secret"),
			patch.object(signing, "verify_request", return_value=request),
			patch.object(signing, "over_limit", return_value=False),
			patch.object(api, "public_destination", return_value=True),
			patch.object(api, "_ensure_bench"),
			patch.object(frappe, "get_doc") as doc,
			patch.object(frappe.db, "get_value") as lookup,
		):
			doc.return_value.insert.side_effect = frappe.DuplicateEntryError("duplicate")
			doc.return_value.get_password.return_value = "stored"
			lookup.side_effect = [None, existing]
			api.register_site()
			request.answer.assert_called_with(
				{"site": "site", "issued_at": request.issued_at, "secret": "stored"}
			)
			self.assertTrue(lookup.call_args.kwargs["for_update"])
			lookup.side_effect = [None, None]
			with self.assertRaises(frappe.DuplicateEntryError):
				api.register_site()
			doc.return_value.insert.side_effect = RuntimeError("unrelated")
			lookup.side_effect = [None]
			with self.assertRaisesRegex(RuntimeError, "unrelated"):
				api.register_site()
