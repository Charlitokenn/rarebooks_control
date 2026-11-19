"""PayPal against a stub (spec 0008 Milestone 2): the module's verify body, `start_time`, `create_checkout`
for PayPal and `already_subscribed`, the webhook and its verify call, and `confirm_paypal` (AC-4, AC-5,
AC-9, AC-10, AC-11, AC-17)."""

import copy
import json
import secrets
from datetime import date, datetime
from typing import ClassVar
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from rarebooks_control import api, webhooks
from rarebooks_control.providers import paypal
from rarebooks_control.providers.paypal import PayPalError
from rarebooks_control.state import paypal_paid_until, paypal_start_time
from rarebooks_control.tests.control import answer, call, make_plan, reset_billing
from rarebooks_control.tests.data_model import make_site
from rarebooks_control.tests.test_api import checkout_body

EVENT = "Books Payment Event"
SITE = "Books Hosted Site"
CHECKOUT = "Books Checkout"
APPROVE = "https://www.sandbox.paypal.com/webapps/billing/subscriptions?ba_token=BA-1"
MONTHLY_PLAN = "P-MONTHLY"
YEARLY_PLAN = "P-YEARLY"
TRIAL_ENDS_ON = "2026-12-08"
ZONE = "Africa/Dar_es_Salaam"


def subscription(
	subscription_id: str,
	site: str,
	status="ACTIVE",
	plan_id=MONTHLY_PLAN,
	created="2026-11-20T10:00:00Z",
	paid: tuple[str, str] | None = None,
	currency="USD",
	next_billing="2026-12-09T10:00:00Z",
) -> dict:
	"""A subscription as `GET /v1/billing/subscriptions/{id}` answers it. `paid` is (time, value)."""
	billing_info = {"next_billing_time": next_billing}
	if paid:
		billing_info["last_payment"] = {
			"time": paid[0],
			"amount": {"value": paid[1], "currency_code": currency},
		}
	return {
		"id": subscription_id,
		"status": status,
		"plan_id": plan_id,
		"custom_id": site,
		"create_time": created,
		"billing_info": billing_info,
		"subscriber": {"name": {"given_name": "Jane", "surname": "Doe"}, "email_address": "jane@example.com"},
	}


class FakePayPal:
	"""Holds subscriptions by id and answers as PayPal would; a cancel turns one `CANCELLED`."""

	def __init__(self):
		self.subscriptions: dict[str, dict] = {}
		self.cancelled: list[str] = []
		self.created: list[dict] = []
		self.error: Exception | None = None

	def put(self, sub: dict):
		self.subscriptions[sub["id"]] = sub

	def get(self, subscription_id):
		if self.error:
			raise self.error
		sub = self.subscriptions.get(subscription_id)
		return copy.deepcopy(sub) if sub else None

	def cancel(self, subscription_id, reason):
		self.cancelled.append(subscription_id)
		self.subscriptions[subscription_id]["status"] = "CANCELLED"

	def create(self, plan_id, custom_id, return_url, start_time, brand_name):
		if self.error:
			raise self.error
		self.created.append(
			{"plan_id": plan_id, "custom_id": custom_id, "return_url": return_url, "start_time": start_time}
		)
		return f"I-NEW{len(self.created)}", APPROVE


def event_body(event_type: str, resource: dict, event_id: str | None = None) -> dict:
	return {
		"id": event_id or f"WH-{secrets.token_hex(6)}",
		"event_type": event_type,
		"resource": resource,
	}


def make_paypal_checkout(site: str, subscription_id: str, plan: str, status="Created") -> str:
	"""A checkout as `create_checkout` leaves it for PayPal (AC-4)."""
	plan_doc = frappe.get_doc("Books Plan", plan)
	return (
		frappe.get_doc(
			{
				"doctype": CHECKOUT,
				"site": site,
				"plan": plan,
				"provider": "PayPal",
				"billing_period": plan_doc.billing_period,
				"amount": plan_doc.amount,
				"currency": plan_doc.currency,
				"paypal_subscription_id": subscription_id,
				"provider_url": APPROVE,
				"return_url": f"https://{site}/books/subscription?checkout=returned",
				"token_hash": secrets.token_hex(32),
				"status": status,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


class PayPalCase(IntegrationTestCase):
	def setUp(self):
		reset_billing()
		frappe.db.delete("Books Plan")
		self.monthly = make_plan(
			provider="PayPal",
			billing_period="Monthly",
			currency="USD",
			amount=9,
			label="Monthly",
			paypal_plan_id=MONTHLY_PLAN,
		).name
		self.yearly = make_plan(
			provider="PayPal",
			billing_period="Yearly",
			currency="USD",
			amount=90,
			label="Yearly",
			paypal_plan_id=YEARLY_PLAN,
		).name
		make_plan()
		self.paypal = FakePayPal()
		for target, fake in (
			("get_subscription", self.paypal.get),
			("cancel_subscription", self.paypal.cancel),
			("create_subscription", self.paypal.create),
		):
			patcher = patch(f"rarebooks_control.providers.paypal.{target}", fake)
			patcher.start()
			self.addCleanup(patcher.stop)
		patcher = patch("rarebooks_control.push.queue_push")
		self.queue_push = patcher.start()
		self.addCleanup(patcher.stop)
		patcher = patch.dict(frappe.conf, {"books_ops_email": "ops@example.com"})
		patcher.start()
		self.addCleanup(patcher.stop)

	def site(self, name: str, **values):
		return make_site(name, trial_ends_on=TRIAL_ENDS_ON, time_zone=ZONE, **values)

	def deliver(self, body: dict, verified: bool | Exception = True):
		"""Post a PayPal delivery with the verify call stubbed. Returns the response, its JSON and sendmail."""
		verify = patch("rarebooks_control.providers.paypal.is_verified_webhook")
		mock = verify.start()
		self.addCleanup(verify.stop)
		if isinstance(verified, Exception):
			mock.side_effect = verified
		else:
			mock.return_value = verified
		with patch("rarebooks_control.billing.frappe.sendmail") as sendmail:
			response = call(webhooks.paypal, body)
		return response, answer(response), sendmail

	def state(self, site: str) -> tuple:
		row = frappe.get_doc(SITE, site)
		return (
			str(row.paid_until) if row.paid_until else None,
			row.provider or None,
			row.billing_period or None,
			row.auto_renews,
			row.provider_reference or None,
		)


class UnitTestPayPalDates(UnitTestCase):
	def test_start_time_is_noon_pacific_on_the_day_after_the_trial_whatever_the_site_zone(self):
		# covers: AC-4
		now = datetime(2026, 11, 20, 9)
		# 24 November 12:00 in Los Angeles (UTC-8 in winter) is 20:00 UTC, so PayPal charges on the 24th.
		for zone in (ZONE, "", "America/New_York", "Pacific/Honolulu"):
			self.assertEqual(paypal_start_time(date(2026, 11, 23), None, zone, now), "2026-11-24T20:00:00Z")
		# In summer Los Angeles is UTC-7, so noon is 19:00 UTC.
		self.assertEqual(paypal_start_time(date(2027, 6, 30), None, ZONE, now), "2027-07-01T19:00:00Z")

	def test_start_time_takes_the_later_of_the_trial_and_paid_until(self):
		# covers: AC-4
		now = datetime(2026, 11, 20, 9)
		self.assertEqual(
			paypal_start_time(date(2026, 12, 8), date(2027, 12, 8), ZONE, now), "2027-12-09T20:00:00Z"
		)
		self.assertEqual(paypal_start_time(None, date(2027, 1, 1), ZONE, now), "2027-01-02T20:00:00Z")

	def test_no_start_time_unless_that_day_is_after_the_site_today(self):
		# covers: AC-4
		self.assertIsNone(paypal_start_time(None, None, ZONE, datetime(2026, 11, 20)))
		# A lapsed renewal sends none, so PayPal charges at approval.
		self.assertIsNone(
			paypal_start_time(date(2026, 10, 1), date(2026, 11, 1), ZONE, datetime(2026, 11, 20))
		)
		# 04:00 UTC on 19 November is already the 19th in Dar es Salaam, the day after the trial: none.
		now = datetime(2026, 11, 19, 4)
		self.assertIsNone(paypal_start_time(date(2026, 11, 18), None, ZONE, now))
		# It is still the 18th in New York, so the 19th is later than the site's today.
		self.assertEqual(
			paypal_start_time(date(2026, 11, 18), None, "America/New_York", now), "2026-11-19T20:00:00Z"
		)

	def test_paid_until_counts_the_charge_day_monthly_and_yearly(self):
		# covers: AC-9
		first_charge = datetime(2026, 11, 24, 10)
		# 24 December 10:00 UTC is 13:00 in Dar es Salaam: the 24th is paid, not only the 23rd.
		self.assertEqual(
			paypal_paid_until(None, first_charge, datetime(2026, 12, 24, 10), "Monthly", ZONE),
			date(2026, 12, 24),
		)
		self.assertEqual(
			paypal_paid_until(None, first_charge, datetime(2027, 11, 24, 10), "Yearly", ZONE),
			date(2027, 11, 24),
		)

	def test_paid_until_reads_the_payment_on_the_pacific_calendar(self):
		# covers: AC-9
		# A lapsed renewal charged at approval: 05:01 UTC on 19 November is the 18th in Los Angeles, and
		# PayPal's next charge is 18 December, so the cap and next billing agree on the 18th.
		charged = datetime(2026, 11, 19, 5, 1)
		self.assertEqual(
			paypal_paid_until(None, charged, datetime(2026, 12, 18, 10), "Monthly", ZONE), date(2026, 12, 18)
		)

	def test_paid_until_with_no_next_billing_uses_the_cap_alone(self):
		# covers: AC-9
		charge = datetime(2026, 11, 24, 10)
		self.assertEqual(paypal_paid_until(None, charge, None, "Monthly", ZONE), date(2026, 12, 24))
		self.assertEqual(paypal_paid_until(None, charge, None, "Yearly", ZONE), date(2027, 11, 24))

	def test_a_next_billing_far_out_never_grants_more_than_one_period(self):
		# covers: AC-9
		# PayPal retrying a failed renewal moves next billing later; the cap holds.
		self.assertEqual(
			paypal_paid_until(None, datetime(2026, 11, 24, 10), datetime(2027, 3, 1, 10), "Monthly", ZONE),
			date(2026, 12, 24),
		)

	def test_paid_until_never_moves_earlier(self):
		# covers: AC-10
		self.assertEqual(
			paypal_paid_until(
				date(2027, 6, 1), datetime(2026, 12, 9, 10), datetime(2027, 1, 9, 10), "Monthly", None
			),
			date(2027, 6, 1),
		)


class UnitTestPayPalVerifyBody(UnitTestCase):
	HEADERS: ClassVar[dict[str, str]] = {
		"PAYPAL-AUTH-ALGO": "SHA256withRSA",
		"PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
		"PAYPAL-TRANSMISSION-ID": "TX-1",
		"PAYPAL-TRANSMISSION-SIG": "c2lnbmF0dXJl",
		"PAYPAL-TRANSMISSION-TIME": "2026-11-20T10:00:00Z",
	}

	def test_the_event_is_sent_as_the_exact_bytes_received(self):
		# covers: AC-9 (re serializing the event would break PayPal's signature)
		raw = b'{ "id" : "WH-1",\n  "event_type":"BILLING.SUBSCRIPTION.ACTIVATED", "resource":{"id":"I-1"} }'
		sent = {}

		class Answer:
			status_code, content = 200, b'{"verification_status":"SUCCESS"}'

			def json(self):
				return json.loads(self.content)

		def request(method, url, json=None, data=None, headers=None, timeout=None):
			sent.update(url=url, data=data, json=json)
			return Answer()

		with (
			patch.dict(frappe.conf, {"paypal_webhook_id": "WEBHOOK-1", "paypal_mode": "sandbox"}),
			patch("rarebooks_control.providers.paypal.token", return_value="TOKEN"),
			patch("rarebooks_control.providers.paypal.requests.request", request),
		):
			self.assertTrue(paypal.is_verified_webhook(self.HEADERS, raw))
		self.assertEqual(
			sent["url"], "https://api-m.sandbox.paypal.com/v1/notifications/verify-webhook-signature"
		)
		self.assertIsNone(sent["json"])
		self.assertTrue(sent["data"].endswith(b',"webhook_event":' + raw + b"}"))
		verify = json.loads(sent["data"])
		self.assertEqual(
			(verify["webhook_id"], verify["transmission_id"], verify["webhook_event"]["id"]),
			("WEBHOOK-1", "TX-1", "WH-1"),
		)

	def test_missing_headers_or_webhook_id_are_refused_without_a_call(self):
		# covers: AC-9
		with (
			patch.dict(frappe.conf, {"paypal_webhook_id": "WEBHOOK-1"}),
			patch("rarebooks_control.providers.paypal.requests.request") as request,
		):
			self.assertFalse(paypal.is_verified_webhook({}, b"{}"))
		with (
			patch.dict(frappe.conf, {"paypal_webhook_id": None}),
			patch("rarebooks_control.providers.paypal.frappe.log_error"),
		):
			self.assertFalse(paypal.is_verified_webhook(self.HEADERS, b"{}"))
		request.assert_not_called()


class IntegrationTestPayPalCheckout(PayPalCase):
	def test_creates_a_subscription_that_starts_after_the_trial(self):
		# covers: AC-4
		row = self.site("pp-checkout.rarebooks.app")
		body = checkout_body(row.name, provider="PayPal", billing_period="Monthly")
		secret = row.get_password("control_secret")
		data = answer(call(api.create_checkout, body, secret), secret)
		self.assertIn("rarebooks_control.billing.go?token=", data["checkout_url"])

		[created] = self.paypal.created
		self.assertEqual(
			(created["plan_id"], created["custom_id"], created["start_time"]),
			(MONTHLY_PLAN, row.name, "2026-12-09T20:00:00Z"),
		)
		self.assertTrue(created["return_url"].endswith("/api/method/rarebooks_control.billing.returned"))
		checkout = frappe.get_last_doc(CHECKOUT, filters={"site": row.name})
		self.assertEqual(
			(checkout.paypal_subscription_id, checkout.provider_url, checkout.order_reference),
			("I-NEW1", APPROVE, None),
		)

	def test_a_live_subscription_refuses_another_checkout_for_either_provider(self):
		# covers: AC-5
		row = self.site("pp-live.rarebooks.app", provider="PayPal", provider_reference="I-LIVE")
		secret = row.get_password("control_secret")
		for status in ("ACTIVE", "APPROVED"):
			self.paypal.put(subscription("I-LIVE", row.name, status=status))
			for provider, period in (("PayPal", "Yearly"), ("ClickPesa", "Yearly")):
				with self.subTest(status=status, provider=provider):
					body = checkout_body(row.name, provider=provider, billing_period=period)
					response = call(api.create_checkout, body, secret)
					self.assertEqual(response.status_code, 409)
					self.assertEqual(answer(response, secret), {"refusal": "already_subscribed"})
		self.assertFalse(frappe.db.exists(CHECKOUT, {"site": row.name}))

		self.paypal.put(subscription("I-LIVE", row.name, status="CANCELLED"))
		body = checkout_body(row.name, provider="PayPal", billing_period="Monthly")
		self.assertEqual(call(api.create_checkout, body, secret).status_code, 200)

	def test_a_paypal_failure_answers_a_signed_502_and_keeps_no_checkout(self):
		# covers: AC-5
		row = self.site("pp-down.rarebooks.app")
		secret = row.get_password("control_secret")
		self.paypal.error = PayPalError("timeout")
		body = checkout_body(row.name, provider="PayPal", billing_period="Monthly")
		with patch("rarebooks_control.api.frappe.log_error"):
			response = call(api.create_checkout, body, secret)
		self.assertEqual(response.status_code, 502)
		answer(response, secret)
		self.assertFalse(frappe.db.exists(CHECKOUT, {"site": row.name}))


class IntegrationTestPayPalWebhook(PayPalCase):
	def test_a_failed_verify_answers_401_and_stores_nothing(self):
		# covers: AC-9, AC-23
		body = event_body("BILLING.SUBSCRIPTION.ACTIVATED", {"id": "I-X"}, event_id="WH-FORGED")
		response, data, _ = self.deliver(body, verified=False)
		self.assertEqual((response.status_code, data), (401, {"result": "unauthorized"}))
		self.assertFalse(frappe.db.exists(EVENT, {"event_id": "WH-FORGED"}))

	def test_paypal_down_answers_503_and_a_redelivery_applies_it(self):
		# covers: AC-9
		row = self.site("pp-retry.rarebooks.app")
		make_paypal_checkout(row.name, "I-RETRY", self.monthly)
		body = event_body("BILLING.SUBSCRIPTION.ACTIVATED", {"id": "I-RETRY"}, event_id="WH-RETRY")
		with patch("rarebooks_control.webhooks.frappe.log_error"):
			response, _, _ = self.deliver(body, verified=PayPalError("timeout"))
		self.assertEqual(response.status_code, 503)
		self.assertFalse(frappe.db.exists(EVENT, {"event_id": "WH-RETRY"}))

		self.paypal.error = PayPalError("timeout")
		with patch("rarebooks_control.webhooks.frappe.log_error"):
			response, _, _ = self.deliver(body)
		self.assertEqual(response.status_code, 503)
		self.assertEqual(frappe.db.get_value(EVENT, {"event_id": "WH-RETRY"}, "outcome"), "")

		self.paypal.error = None
		self.paypal.put(subscription("I-RETRY", row.name))
		_, data, _ = self.deliver(body)
		self.assertEqual(data, {"result": "applied"})
		_, data, _ = self.deliver(body)
		self.assertEqual(data, {"result": "duplicate"})
		self.assertEqual(frappe.db.count(EVENT, {"event_id": "WH-RETRY"}), 1)

	def test_activated_during_the_trial_then_the_first_payment(self):
		# covers: AC-9, AC-13
		row = self.site("pp-trial.rarebooks.app")
		checkout = make_paypal_checkout(row.name, "I-TRIAL", self.monthly)
		self.paypal.put(subscription("I-TRIAL", row.name))
		_, data, sendmail = self.deliver(event_body("BILLING.SUBSCRIPTION.ACTIVATED", {"id": "I-TRIAL"}))
		self.assertEqual(data, {"result": "applied"})
		self.assertEqual(self.state(row.name), (None, "PayPal", "Monthly", 1, "I-TRIAL"))
		self.assertEqual(frappe.db.get_value(CHECKOUT, checkout, "status"), "Created")
		self.assertEqual(frappe.db.get_value(SITE, row.name, "state_version"), 1)
		self.queue_push.assert_called_with(row.name)
		sendmail.assert_not_called()

		# The first charge, at 10:00 UTC on 9 December (13:00 in Dar es Salaam); PayPal bills again a month
		# later on its Pacific calendar, and that charge day counts as paid.
		self.paypal.put(
			subscription(
				"I-TRIAL",
				row.name,
				paid=("2026-12-09T10:00:00Z", "9.00"),
				next_billing="2027-01-09T10:00:00Z",
			)
		)
		sale = {"id": "SALE-1", "billing_agreement_id": "I-TRIAL", "amount": {"total": "9.00"}}
		_, data, _ = self.deliver(event_body("PAYMENT.SALE.COMPLETED", sale))
		self.assertEqual(data, {"result": "applied"})
		self.assertEqual(self.state(row.name), ("2027-01-09", "PayPal", "Monthly", 1, "I-TRIAL"))
		self.assertEqual(frappe.db.get_value(CHECKOUT, checkout, "status"), "Paid")
		event = frappe.get_last_doc(EVENT, filters={"event_type": "PAYMENT.SALE.COMPLETED"})
		self.assertEqual(
			(event.outcome, str(event.paid_until_after), event.currency), ("Applied", "2027-01-09", "USD")
		)

	def test_events_in_any_order_end_in_the_same_state(self):
		# covers: AC-9
		paid = subscription(
			"I-ORDER", "", paid=("2026-12-09T10:00:00Z", "9.00"), next_billing="2027-01-09T10:00:00Z"
		)
		finals = []
		for site, order in (
			("pp-forward.rarebooks.app", ("BILLING.SUBSCRIPTION.ACTIVATED", "PAYMENT.SALE.COMPLETED")),
			("pp-reverse.rarebooks.app", ("PAYMENT.SALE.COMPLETED", "BILLING.SUBSCRIPTION.ACTIVATED")),
		):
			row = self.site(site)
			sub_id = f"I-{site.split('.')[0]}"
			make_paypal_checkout(row.name, sub_id, self.monthly)
			self.paypal.put({**copy.deepcopy(paid), "id": sub_id, "custom_id": row.name})
			for event_type in (*order, order[0]):
				resource = {"id": sub_id, "billing_agreement_id": sub_id}
				self.deliver(event_body(event_type, resource))
			finals.append(self.state(row.name)[:4])
			self.assertEqual(frappe.db.get_value(SITE, row.name, "state_version"), 1)
		self.assertEqual(finals[0], finals[1])
		self.assertEqual(finals[0], ("2027-01-09", "PayPal", "Monthly", 1))

	def test_cancelled_keeps_paid_until_and_stops_renewal(self):
		# covers: AC-9, AC-10
		row = self.site(
			"pp-cancel.rarebooks.app",
			provider="PayPal",
			billing_period="Monthly",
			auto_renews=1,
			provider_reference="I-CANCEL",
			paid_until="2027-01-09",
			last_payment_on="2026-12-09 10:00:00",
		)
		make_paypal_checkout(row.name, "I-CANCEL", self.monthly, status="Paid")
		for status in ("SUSPENDED", "CANCELLED", "EXPIRED"):
			with self.subTest(status=status):
				self.paypal.put(
					subscription("I-CANCEL", row.name, status=status, paid=("2026-12-09T10:00:00Z", "9.00"))
				)
				self.deliver(event_body(f"BILLING.SUBSCRIPTION.{status}", {"id": "I-CANCEL"}))
				self.assertEqual(self.state(row.name), ("2027-01-09", "PayPal", "Monthly", 0, "I-CANCEL"))

	def test_a_retry_period_that_moves_next_billing_grants_no_days(self):
		# covers: AC-9
		row = self.site(
			"pp-retrying.rarebooks.app",
			provider="PayPal",
			billing_period="Monthly",
			auto_renews=1,
			provider_reference="I-RETRYING",
			paid_until="2027-01-09",
			last_payment_on="2026-12-09 10:00:00",
		)
		make_paypal_checkout(row.name, "I-RETRYING", self.monthly, status="Paid")
		self.paypal.put(
			subscription(
				"I-RETRYING",
				row.name,
				paid=("2026-12-09T10:00:00Z", "9.00"),
				next_billing="2027-01-16T10:00:00Z",
			)
		)
		_, data, _ = self.deliver(event_body("BILLING.SUBSCRIPTION.UPDATED", {"id": "I-RETRYING"}))
		self.assertEqual(data, {"result": "ignored"})
		self.assertEqual(self.state(row.name)[0], "2027-01-09")
		self.assertEqual(frappe.db.get_value(SITE, row.name, "state_version"), 0)

	def test_an_unknown_plan_or_a_wrong_amount_is_a_mismatch(self):
		# covers: AC-9, AC-10
		for site, values in (
			("pp-plan.rarebooks.app", {"plan_id": "P-UNKNOWN"}),
			("pp-short.rarebooks.app", {"paid": ("2026-12-09T10:00:00Z", "8.99")}),
			("pp-eur.rarebooks.app", {"paid": ("2026-12-09T10:00:00Z", "9.00"), "currency": "EUR"}),
		):
			with self.subTest(site=site):
				row = self.site(site)
				sub_id = f"I-{site.split('.')[0]}"
				make_paypal_checkout(row.name, sub_id, self.monthly)
				self.paypal.put(subscription(sub_id, row.name, **values))
				_, data, sendmail = self.deliver(
					event_body("PAYMENT.SALE.COMPLETED", {"billing_agreement_id": sub_id})
				)
				self.assertEqual(data, {"result": "mismatch"})
				self.assertEqual(self.state(row.name), (None, None, None, 0, None))
				self.assertEqual(frappe.db.get_value(SITE, row.name, "state_version"), 0)
				sendmail.assert_called_once()
				self.assertEqual(sendmail.call_args.kwargs["recipients"], ["ops@example.com"])
				self.assertIn("jane@example.com", sendmail.call_args.kwargs["message"])

		# The mismatched payment is not raised again by the next event.
		_, data, sendmail = self.deliver(event_body("BILLING.SUBSCRIPTION.UPDATED", {"id": "I-pp-eur"}))
		self.assertEqual(data, {"result": "applied"})
		self.assertEqual(self.state("pp-eur.rarebooks.app"), (None, "PayPal", "Monthly", 1, "I-pp-eur"))
		sendmail.assert_not_called()

	def test_a_refund_or_reversal_is_kept_and_changes_nothing(self):
		# covers: AC-10
		row = self.site(
			"pp-refund.rarebooks.app",
			provider="PayPal",
			billing_period="Monthly",
			auto_renews=1,
			provider_reference="I-REFUND",
			paid_until="2027-01-09",
		)
		make_paypal_checkout(row.name, "I-REFUND", self.monthly, status="Paid")
		for event_type in ("PAYMENT.SALE.REFUNDED", "PAYMENT.SALE.REVERSED"):
			with self.subTest(event_type=event_type):
				resource = {
					"id": "REF-1",
					"sale_id": "SALE-1",
					"billing_agreement_id": "I-REFUND",
					"amount": {"total": "9.00", "currency": "USD"},
				}
				_, data, sendmail = self.deliver(event_body(event_type, resource))
				self.assertEqual(data, {"result": "refund"})
				event = frappe.get_last_doc(EVENT, filters={"event_type": event_type})
				self.assertEqual((event.site, event.currency), (row.name, "USD"))
				sendmail.assert_called_once()
		self.assertEqual(self.state(row.name), ("2027-01-09", "PayPal", "Monthly", 1, "I-REFUND"))
		self.assertEqual(frappe.db.get_value(SITE, row.name, "state_version"), 0)

	def test_a_payment_failure_is_kept_and_changes_nothing(self):
		# covers: AC-10
		row = self.site(
			"pp-failed.rarebooks.app", provider="PayPal", provider_reference="I-FAILED", auto_renews=1
		)
		resource = subscription("I-FAILED", row.name)
		_, data, _ = self.deliver(event_body("BILLING.SUBSCRIPTION.PAYMENT.FAILED", resource))
		self.assertEqual(data, {"result": "ignored"})
		event = frappe.get_last_doc(EVENT, filters={"event_type": "BILLING.SUBSCRIPTION.PAYMENT.FAILED"})
		self.assertEqual(event.site, row.name)
		self.assertEqual(frappe.db.get_value(SITE, row.name, "state_version"), 0)

	def test_a_second_live_subscription_is_cancelled_and_its_cancel_changes_nothing(self):
		# covers: AC-11
		row = self.site("pp-double.rarebooks.app")
		make_paypal_checkout(row.name, "I-FIRST", self.monthly)
		make_paypal_checkout(row.name, "I-SECOND", self.yearly)
		self.paypal.put(subscription("I-FIRST", row.name, created="2026-11-20T10:00:00Z"))
		self.paypal.put(
			subscription("I-SECOND", row.name, plan_id=YEARLY_PLAN, created="2026-11-20T10:05:00Z")
		)
		self.deliver(event_body("BILLING.SUBSCRIPTION.ACTIVATED", {"id": "I-FIRST"}))

		_, data, sendmail = self.deliver(event_body("BILLING.SUBSCRIPTION.ACTIVATED", {"id": "I-SECOND"}))
		self.assertEqual(data, {"result": "ignored"})
		self.assertEqual(self.paypal.cancelled, ["I-SECOND"])
		sendmail.assert_called_once()
		self.assertIn("Refund", sendmail.call_args.kwargs["message"])

		_, data, _ = self.deliver(event_body("BILLING.SUBSCRIPTION.CANCELLED", {"id": "I-SECOND"}))
		self.assertEqual(data, {"result": "ignored"})
		self.assertEqual(self.state(row.name), (None, "PayPal", "Monthly", 1, "I-FIRST"))

	def test_the_older_subscription_wins_whichever_arrives_first(self):
		# covers: AC-11
		row = self.site("pp-older.rarebooks.app")
		make_paypal_checkout(row.name, "I-OLD", self.monthly)
		make_paypal_checkout(row.name, "I-YOUNG", self.yearly)
		self.paypal.put(subscription("I-OLD", row.name, created="2026-11-20T10:00:00Z"))
		self.paypal.put(
			subscription("I-YOUNG", row.name, plan_id=YEARLY_PLAN, created="2026-11-20T10:05:00Z")
		)
		self.deliver(event_body("BILLING.SUBSCRIPTION.ACTIVATED", {"id": "I-YOUNG"}))
		_, data, _ = self.deliver(event_body("BILLING.SUBSCRIPTION.ACTIVATED", {"id": "I-OLD"}))
		self.assertEqual(data, {"result": "applied"})
		self.assertEqual(self.paypal.cancelled, ["I-YOUNG"])
		self.assertEqual(self.state(row.name), (None, "PayPal", "Monthly", 1, "I-OLD"))

	def test_no_checkout_an_unknown_site_or_a_removed_one_is_unknown(self):
		# covers: AC-11, AC-17
		row = self.site("pp-nocheckout.rarebooks.app")
		self.paypal.put(subscription("I-NOCHECKOUT", row.name))
		removed = self.site("pp-removed.rarebooks.app", status="Removed")
		make_paypal_checkout(removed.name, "I-REMOVED", self.monthly)
		self.paypal.put(subscription("I-REMOVED", removed.name, paid=("2026-12-09T10:00:00Z", "9.00")))
		self.paypal.put(subscription("I-NOSITE", "gone.rarebooks.app"))

		for sub_id, site in (
			("I-NOCHECKOUT", row.name),
			("I-REMOVED", removed.name),
			("I-NOSITE", None),
		):
			with self.subTest(sub_id=sub_id):
				body = event_body("BILLING.SUBSCRIPTION.ACTIVATED", {"id": sub_id})
				_, data, sendmail = self.deliver(body)
				self.assertEqual(data, {"result": "unknown"})
				sendmail.assert_called_once()
				self.assertEqual(frappe.db.get_value(EVENT, {"event_id": body["id"]}, "site"), site)
		for site in (row.name, removed.name):
			self.assertEqual(self.state(site), (None, None, None, 0, None))
		self.queue_push.assert_not_called()
