"""Spec 0008 Milestone 3, reliability: push alerts and the sweep (AC-13), the return page (AC-12), the
reconcile jobs and checkout expiry (AC-16), receipts (AC-19), retention and the failure digest (AC-10,
AC-18), and `billing_state()` for spec 0007."""

from datetime import timedelta
from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase
from frappe.translate import clear_cache as clear_translations
from frappe.utils import set_request

from rarebooks_control import billing, push, reconcile, webhooks
from rarebooks_control.providers.clickpesa import ClickPesaError
from rarebooks_control.signing import utc_now
from rarebooks_control.tests.control import (
	CustomerStub,
	call,
	clickpesa_conf,
	make_plan,
	paid_item,
	reset_billing,
	webhook_body,
)
from rarebooks_control.tests.data_model import make_site
from rarebooks_control.tests.test_paypal import (
	MONTHLY_PLAN,
	FakePayPal,
	event_body,
	make_paypal_checkout,
	subscription,
)

SITE = "Books Hosted Site"
CHECKOUT = "Books Checkout"
EVENT = "Books Payment Event"
OPS = "ops@example.com"
OWNER = "owner@example.com"
RETURN_URL = "https://{site}/books/subscription?checkout=returned"


def make_clickpesa_checkout(site: str, plan: str, token: str = "tok", status="Created") -> str:
	checkout = frappe.new_doc(CHECKOUT)
	checkout.update(
		{
			"site": site,
			"plan": plan,
			"provider": "ClickPesa",
			"billing_period": "Yearly",
			"amount": 120000,
			"currency": "TZS",
			"provider_url": "https://checkout.clickpesa.com/pay/abc",
			"return_url": RETURN_URL.format(site=site),
			"token_hash": billing_token_hash(token),
			"status": status,
		}
	)
	checkout.set_new_name()
	checkout.order_reference = billing.order_reference(checkout.name)
	return checkout.insert(ignore_permissions=True).name


def billing_token_hash(token: str) -> str:
	from rarebooks_control.api import token_hash

	return token_hash(token)


def visit_return(cookie: str | None = None, **query):
	"""GET the return page in process, with the `go` cookie when given. Frappe passes the method only the
	query values it names, so PayPal's other values (`ba_token`, `token`) stay in the query string."""
	headers = {"Cookie": f"{billing.COOKIE}={cookie}"} if cookie else {}
	set_request(method="GET", path=billing.COOKIE_PATH, query_string=query, headers=headers)
	frappe.local.request_ip = "203.0.113.7"
	return billing.returned(subscription_id=query.get("subscription_id"))


def emails_to(sendmail, recipient: str) -> list[dict]:
	return [call.kwargs for call in sendmail.call_args_list if call.kwargs.get("recipients") == [recipient]]


class BillingCase(IntegrationTestCase):
	def setUp(self):
		reset_billing()
		frappe.db.delete("Books Plan")
		self.clickpesa_plan = make_plan(label="Yearly plan").name
		self.monthly = make_plan(
			provider="PayPal",
			billing_period="Monthly",
			currency="USD",
			amount=9,
			label="Monthly plan",
			paypal_plan_id=MONTHLY_PLAN,
		).name
		self.paypal = FakePayPal()
		for target, fake in (
			("get_subscription", self.paypal.get),
			("cancel_subscription", self.paypal.cancel),
		):
			patcher = patch(f"rarebooks_control.providers.paypal.{target}", fake)
			patcher.start()
			self.addCleanup(patcher.stop)
		self.queue_push = self.start(patch("rarebooks_control.push.queue_push"))
		self.sendmail = self.start(patch("frappe.sendmail"))
		self.start(patch("rarebooks_control.providers.paypal.is_verified_webhook", return_value=True))
		self.payments = self.start(patch("rarebooks_control.providers.clickpesa.payments", return_value=[]))
		self.start(clickpesa_conf())
		frappe.db.after_commit.reset()

	def sent_at_once(self) -> bool:
		"""Runs what the commit would, then tells whether a queued email was sent right away."""
		frappe.db.after_commit.run()
		return self.sendmail.return_value.send.called

	def start(self, patcher):
		value = patcher.start() if hasattr(patcher, "start") else patcher.__enter__()
		self.addCleanup(
			patcher.stop if hasattr(patcher, "stop") else lambda: patcher.__exit__(None, None, None)
		)
		return value

	def site(self, name: str, **values):
		values = {"trial_ends_on": "2026-12-08", "time_zone": "Africa/Dar_es_Salaam", **values}
		return make_site(name, **values)


class IntegrationTestPushAlerts(BillingCase):
	def failing_site(self, name: str, hours: float, **values):
		return self.site(
			name,
			state_version=1,
			push_status="Failing",
			push_attempts=5,
			failing_since=utc_now() - timedelta(hours=hours),
			**values,
		)

	def push_once(self, row, **stub):
		with patch("rarebooks_control.push.post", CustomerStub(row.get_password("control_secret"), **stub)):
			push.push_site(row.name)
		row.reload()

	def test_one_email_after_six_hours_of_failing(self):
		# covers: AC-13
		young = self.failing_site("young.push.example.com", hours=5)
		self.push_once(young, error=requests.Timeout())
		self.assertEqual(emails_to(self.sendmail, OPS), [])

		row = self.failing_site("six.push.example.com", hours=6.1)
		self.push_once(row, error=requests.Timeout())
		[email] = emails_to(self.sendmail, OPS)
		self.assertTrue(self.sent_at_once())
		self.assertIn("6 hours", email["subject"])
		self.assertEqual((row.failing_alerted, row.push_status, row.push_attempts), (1, "Failing", 6))

		self.push_once(row, error=requests.Timeout())
		self.assertEqual(len(emails_to(self.sendmail, OPS)), 1)

	def test_a_400_alerts_at_once_and_once(self):
		# covers: AC-13
		row = self.site("refuses.push.example.com", state_version=1, push_status="Pending")
		self.push_once(row, status=400)
		[_email] = emails_to(self.sendmail, OPS)
		self.assertTrue(self.sent_at_once())
		self.assertEqual(row.last_push_error, "push answered HTTP 400")
		self.push_once(row, status=400)
		self.assertEqual(len(emails_to(self.sendmail, OPS)), 1)

	def test_recovery_returns_to_synced_and_ends_the_streak(self):
		# covers: AC-13
		row = self.failing_site("recovers.push.example.com", hours=7, failing_alerted=1)
		self.push_once(row)
		self.assertEqual(
			(row.push_status, row.pushed_version, row.push_attempts, row.failing_alerted, row.failing_since),
			("Synced", 1, 0, 0, None),
		)

	def test_the_sweep_queues_pending_and_due_rows_only(self):
		# covers: AC-13
		now = utc_now()
		self.site("pending.sweep.example.com", push_status="Pending")
		self.site("due.sweep.example.com", push_status="Failing", next_push_on=now - timedelta(minutes=1))
		self.site("later.sweep.example.com", push_status="Failing", next_push_on=now + timedelta(minutes=5))
		self.site("removed.sweep.example.com", push_status="Pending", status="Removed")
		self.site("synced.sweep.example.com")
		push.sweep()
		queued = sorted(call.args[0] for call in self.queue_push.call_args_list)
		self.assertEqual(queued, ["due.sweep.example.com", "pending.sweep.example.com"])


class IntegrationTestReturnPage(BillingCase):
	def test_a_paypal_return_applies_before_the_webhook_and_the_webhook_adds_nothing(self):
		# covers: AC-12, AC-19
		row = self.site("pp-return.rarebooks.app", language="en", company_name="Acme Books")
		make_paypal_checkout(row.name, "I-RETURN", self.monthly)
		self.paypal.put(
			subscription(
				"I-RETURN",
				row.name,
				paid=("2026-12-09T10:00:00Z", "9.00"),
				next_billing="2027-01-09T10:00:00Z",
			)
		)
		response = visit_return(subscription_id="I-RETURN", ba_token="BA-1")
		self.assertEqual((response.status_code, response.location), (302, RETURN_URL.format(site=row.name)))
		row.reload()
		self.assertEqual((str(row.paid_until), row.auto_renews, row.state_version), ("2027-01-09", 1, 1))
		self.assertEqual(len(emails_to(self.sendmail, OWNER)), 1)

		with patch("frappe.sendmail") as later:
			call(webhooks.paypal, event_body("PAYMENT.SALE.COMPLETED", {"billing_agreement_id": "I-RETURN"}))
		row.reload()
		self.assertEqual(row.state_version, 1)
		later.assert_not_called()

	def test_a_clickpesa_return_finds_the_checkout_by_its_cookie(self):
		# covers: AC-12, AC-6
		row = self.site("cp-return.rarebooks.app")
		checkout = make_clickpesa_checkout(row.name, self.clickpesa_plan, token="cookie-token")
		order = frappe.db.get_value(CHECKOUT, checkout, "order_reference")
		self.payments.return_value = [paid_item(order)]
		response = visit_return(cookie="cookie-token")
		self.assertEqual((response.status_code, response.location), (302, RETURN_URL.format(site=row.name)))
		self.assertEqual(frappe.db.get_value(CHECKOUT, checkout, "status"), "Paid")
		self.assertEqual(str(frappe.db.get_value(SITE, row.name, "paid_until")), "2027-12-08")

	def test_no_checkout_shows_the_close_page(self):
		# covers: AC-12
		for kwargs in ({}, {"cookie": "unknown"}, {"subscription_id": "I-NOBODY"}):
			with self.subTest(**kwargs):
				response = visit_return(**kwargs)
				self.assertEqual(response.status_code, 200)
				self.assertIn(
					"You can close this page and go back to your books.", response.get_data(as_text=True)
				)

	def test_a_provider_outage_still_sends_the_payer_back(self):
		# covers: AC-12
		row = self.site("cp-down.rarebooks.app")
		make_clickpesa_checkout(row.name, self.clickpesa_plan, token="down-token")
		self.payments.side_effect = ClickPesaError("timeout")
		with patch("rarebooks_control.billing.frappe.log_error"):
			response = visit_return(cookie="down-token")
		self.assertEqual(response.status_code, 302)
		self.assertIsNone(frappe.db.get_value(SITE, row.name, "paid_until"))


class IntegrationTestReconcile(BillingCase):
	def test_the_hourly_job_finds_a_missed_clickpesa_webhook_once(self):
		# covers: AC-16
		row = self.site("cp-missed.rarebooks.app")
		checkout = make_clickpesa_checkout(row.name, self.clickpesa_plan)
		order = frappe.db.get_value(CHECKOUT, checkout, "order_reference")
		self.payments.return_value = [paid_item(order)]
		reconcile.hourly()
		self.assertEqual(frappe.db.get_value(CHECKOUT, checkout, "status"), "Paid")
		self.assertEqual(str(frappe.db.get_value(SITE, row.name, "paid_until")), "2027-12-08")
		self.assertEqual(
			frappe.db.get_value(EVENT, {"event_id": f"confirm:{checkout}:SUCCESS"}, "outcome"), "Applied"
		)

		# A later webhook for the same payment, and the next hourly run, change nothing more.
		with clickpesa_conf():
			call(webhooks.clickpesa, webhook_body(order))
		reconcile.hourly()
		self.assertEqual(frappe.db.get_value(SITE, row.name, "state_version"), 1)
		self.assertEqual(len(emails_to(self.sendmail, OWNER)), 1)

	def test_an_old_checkout_expires_and_a_late_payment_still_counts(self):
		# covers: AC-16
		row = self.site("cp-late.rarebooks.app")
		checkout = make_clickpesa_checkout(row.name, self.clickpesa_plan)
		frappe.db.set_value(CHECKOUT, checkout, "creation", frappe.utils.now_datetime() - timedelta(hours=30))
		reconcile.expire_checkouts()
		self.assertEqual(frappe.db.get_value(CHECKOUT, checkout, "status"), "Expired")

		order = frappe.db.get_value(CHECKOUT, checkout, "order_reference")
		self.payments.return_value = [paid_item(order)]
		reconcile.reconcile_clickpesa()
		self.assertEqual(frappe.db.get_value(CHECKOUT, checkout, "status"), "Paid")
		self.assertEqual(str(frappe.db.get_value(SITE, row.name, "paid_until")), "2027-12-08")

	def test_old_checkouts_are_left_alone_and_one_failure_does_not_stop_the_run(self):
		# covers: AC-16
		old = self.site("cp-old.rarebooks.app")
		stale = make_clickpesa_checkout(old.name, self.clickpesa_plan, token="old")
		frappe.db.set_value(CHECKOUT, stale, "creation", frappe.utils.now_datetime() - timedelta(hours=50))
		failing = self.site("cp-fail.rarebooks.app")
		make_clickpesa_checkout(failing.name, self.clickpesa_plan, token="fail")
		ok = self.site("cp-ok.rarebooks.app")
		good = make_clickpesa_checkout(ok.name, self.clickpesa_plan, token="ok")

		def payments(order):
			if order == frappe.db.get_value(CHECKOUT, good, "order_reference"):
				return [paid_item(order)]
			raise ClickPesaError("timeout")

		self.payments.side_effect = payments
		with patch("rarebooks_control.reconcile.frappe.log_error") as log_error:
			reconcile.reconcile_clickpesa()
		self.assertEqual(log_error.call_count, 1)
		self.assertEqual(frappe.db.get_value(CHECKOUT, good, "status"), "Paid")

	def test_the_daily_job_applies_a_missed_paypal_renewal(self):
		# covers: AC-16
		row = self.site(
			"pp-renewal.rarebooks.app",
			provider="PayPal",
			billing_period="Monthly",
			auto_renews=1,
			provider_reference="I-RENEW",
			paid_until="2027-01-09",
			last_payment_on="2026-12-09 10:00:00",
		)
		make_paypal_checkout(row.name, "I-RENEW", self.monthly, status="Paid")
		frappe.db.set_value(CHECKOUT, {"paypal_subscription_id": "I-RENEW"}, "creation", "2026-12-01")
		self.paypal.put(
			subscription(
				"I-RENEW",
				row.name,
				paid=("2027-01-09T10:00:00Z", "9.00"),
				next_billing="2027-02-09T10:00:00Z",
			)
		)
		reconcile.reconcile_paypal()
		self.assertEqual(str(frappe.db.get_value(SITE, row.name, "paid_until")), "2027-02-09")
		reconcile.reconcile_paypal()
		self.assertEqual(frappe.db.get_value(SITE, row.name, "state_version"), 1)


class IntegrationTestReceipts(BillingCase):
	def setUp(self):
		super().setUp()
		clear_translations()

	def pay_clickpesa(self, site: str, **values):
		row = self.site(site, company_name="Duka la Mama", **values)
		checkout = make_clickpesa_checkout(row.name, self.clickpesa_plan)
		order = frappe.db.get_value(CHECKOUT, checkout, "order_reference")
		self.payments.return_value = [paid_item(order)]
		with clickpesa_conf():
			call(webhooks.clickpesa, webhook_body(order))
		return order

	def test_a_swahili_site_gets_one_swahili_receipt(self):
		# covers: AC-19
		order = self.pay_clickpesa("sw-receipt.rarebooks.app", language="sw")
		[email] = emails_to(self.sendmail, OWNER)
		self.assertEqual(email["subject"], "Malipo yamepokelewa kwa Duka la Mama")
		for text in (
			"Mpango: Yearly plan",
			"Kiasi: 120000 TZS",
			"Kipindi cha malipo: Kila mwaka",
			"Imelipiwa hadi: 2027-12-08",
			f"Kumbukumbu: {order}",
			"Risiti hii si ankara ya kodi.",
		):
			self.assertIn(text, email["message"])
		self.assertFalse(email.get("now"))

	def test_any_other_language_gets_english(self):
		# covers: AC-19
		for language in ("fr", None):
			with self.subTest(language=language):
				reset_billing()
				self.sendmail.reset_mock()
				self.pay_clickpesa(f"en-receipt-{language}.rarebooks.app", language=language)
				[email] = emails_to(self.sendmail, OWNER)
				self.assertEqual(email["subject"], "Payment received for Duka la Mama")
				self.assertIn("Billing period: Yearly", email["message"])
				self.assertIn("This receipt is not a tax invoice.", email["message"])

	def test_a_payment_that_moves_nothing_sends_no_receipt(self):
		# covers: AC-19
		row = self.site("pp-noreceipt.rarebooks.app")
		make_paypal_checkout(row.name, "I-NORECEIPT", self.monthly)
		self.paypal.put(subscription("I-NORECEIPT", row.name))
		call(webhooks.paypal, event_body("BILLING.SUBSCRIPTION.ACTIVATED", {"id": "I-NORECEIPT"}))
		self.assertEqual(emails_to(self.sendmail, OWNER), [])


class IntegrationTestRecordKeeping(BillingCase):
	def event(self, event_id: str, days_old: int, event_type="PAYMENT.SALE.COMPLETED", site=None):
		return (
			frappe.get_doc(
				{
					"doctype": EVENT,
					"event_id": event_id,
					"provider": "PayPal",
					"event_type": event_type,
					"site": site,
					"outcome": "Ignored",
					"received_on": utc_now() - timedelta(days=days_old),
					"raw_payload": '{"id": "x"}',
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def test_payloads_older_than_90_days_are_blanked_and_the_rest_kept(self):
		# covers: AC-18
		old, young = self.event("WH-OLD", 91), self.event("WH-YOUNG", 89)
		reconcile.purge_raw_payloads()
		old_row = frappe.db.get_value(EVENT, old, ["raw_payload", "event_type", "outcome"], as_dict=True)
		self.assertEqual(
			(old_row.raw_payload, old_row.event_type, old_row.outcome),
			(None, "PAYMENT.SALE.COMPLETED", "Ignored"),
		)
		self.assertEqual(frappe.db.get_value(EVENT, young, "raw_payload"), '{"id": "x"}')

	def test_the_digest_lists_the_last_days_payment_failures(self):
		# covers: AC-10
		reconcile.send_failed_payment_digest()
		self.assertEqual(emails_to(self.sendmail, OPS), [])

		row = self.site("pp-digest.rarebooks.app")
		self.event("WH-FAILED", 0, "BILLING.SUBSCRIPTION.PAYMENT.FAILED", row.name)
		self.event("WH-FAILED-OLD", 3, "BILLING.SUBSCRIPTION.PAYMENT.FAILED", row.name)
		reconcile.send_failed_payment_digest()
		[email] = emails_to(self.sendmail, OPS)
		self.assertEqual(email["subject"], "1 failed PayPal payments")
		self.assertIn("WH-FAILED,", email["message"] + ",")
		self.assertNotIn("WH-FAILED-OLD", email["message"])
		self.assertFalse(self.sent_at_once())


class IntegrationTestBillingState(BillingCase):
	def test_billing_state_for_the_removal_checks(self):
		# covers: spec 0007 (the drop and cancel checks), AC-5
		row = self.site("state.rarebooks.app", paid_until="2027-01-08", auto_renews=1)
		self.assertEqual(
			billing.billing_state(row.name),
			{
				"paid_until": frappe.utils.getdate("2027-01-08"),
				"exempt": 0,
				"auto_renews": 1,
				"checkout_in_last_24h": False,
			},
		)
		checkout = make_clickpesa_checkout(row.name, self.clickpesa_plan)
		self.assertTrue(billing.billing_state(row.name)["checkout_in_last_24h"])

		frappe.db.set_value(CHECKOUT, checkout, "creation", frappe.utils.now_datetime() - timedelta(hours=30))
		self.assertFalse(billing.billing_state(row.name)["checkout_in_last_24h"])
		frappe.db.set_value(CHECKOUT, checkout, {"status": "Paid", "paid_on": utc_now() - timedelta(hours=2)})
		self.assertTrue(billing.billing_state(row.name)["checkout_in_last_24h"])
		self.assertFalse(billing.billing_state("nobody.rarebooks.app")["checkout_in_last_24h"])
