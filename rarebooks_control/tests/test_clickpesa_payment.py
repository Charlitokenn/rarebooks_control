"""One ClickPesa payment end to end against stubs (spec 0008 Milestone 1): the webhook and its checksum,
event dedup, confirm (lock, then fetch, then write), the AC-8 date rule and the push (AC-7, AC-8, AC-10,
AC-13, AC-17)."""

from datetime import date, datetime
from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase, UnitTestCase

from rarebooks_control import api, push, webhooks
from rarebooks_control.providers.clickpesa import ClickPesaError
from rarebooks_control.state import clickpesa_paid_until
from rarebooks_control.tests.control import (
	CLICKPESA_LINK,
	CustomerStub,
	answer,
	call,
	clickpesa_conf,
	make_plan,
	paid_item,
	reset_billing,
	webhook_body,
)
from rarebooks_control.tests.data_model import make_site
from rarebooks_control.tests.test_api import checkout_body

EVENT = "Books Payment Event"
SITE = "Books Hosted Site"


class UnitTestClickPesaDates(UnitTestCase):
	def test_paying_during_the_trial_starts_the_year_after_it(self):
		# covers: AC-8
		self.assertEqual(
			clickpesa_paid_until(None, datetime(2026, 11, 28, 9), date(2026, 12, 8), "Africa/Dar_es_Salaam"),
			date(2027, 12, 8),
		)

	def test_an_early_renewal_extends_from_the_old_end(self):
		# covers: AC-8
		self.assertEqual(
			clickpesa_paid_until(date(2027, 12, 8), datetime(2027, 11, 20), date(2026, 12, 8), None),
			date(2028, 12, 8),
		)

	def test_a_renewal_after_a_lapse_starts_on_the_payment_day(self):
		# covers: AC-8
		self.assertEqual(
			clickpesa_paid_until(date(2027, 12, 8), datetime(2028, 1, 7, 12), date(2026, 12, 8), None),
			date(2029, 1, 6),
		)

	def test_a_start_on_29_february_ends_on_27_february(self):
		# covers: AC-8
		self.assertEqual(clickpesa_paid_until(None, datetime(2028, 2, 29, 12), None, None), date(2029, 2, 27))

	def test_dar_es_salaam_near_midnight_is_the_next_day(self):
		# covers: AC-8
		# 22:30 UTC is 01:30 the next day in Dar es Salaam (UTC+3).
		paid_on = datetime(2027, 3, 1, 22, 30)
		self.assertEqual(clickpesa_paid_until(None, paid_on, None, "Africa/Dar_es_Salaam"), date(2028, 3, 1))
		self.assertEqual(clickpesa_paid_until(None, paid_on, None, "Not/AZone"), date(2028, 2, 29))


@patch("rarebooks_control.billing.clickpesa.create_checkout_link", return_value=CLICKPESA_LINK)
@patch("rarebooks_control.push.queue_push")
class IntegrationTestClickPesaPayment(IntegrationTestCase):
	def setUp(self):
		for target in (
			"rarebooks_control.signing.utc_now",
			"rarebooks_control.tests.control.utc_now",
		):
			clock = patch(target, return_value=datetime(2026, 11, 18, 12))
			clock.start()
			self.addCleanup(clock.stop)
		reset_billing()
		frappe.db.delete("Books Plan")
		make_plan()

	def checkout(self, site: str, **values):
		row = make_site(site, **values)
		call(api.create_checkout, checkout_body(row.name), row.get_password("control_secret"))
		return row, frappe.get_last_doc("Books Checkout", filters={"site": row.name})

	def deliver(self, body: dict, items: list[dict] | Exception):
		patcher = patch("rarebooks_control.billing.clickpesa.payments")
		payments = patcher.start()
		self.addCleanup(patcher.stop)
		if isinstance(items, Exception):
			payments.side_effect = items
		else:
			payments.return_value = items
		with clickpesa_conf(), patch("rarebooks_control.billing.frappe.sendmail") as sendmail:
			response = call(webhooks.clickpesa, body)
		return response, answer(response), sendmail

	def test_a_confirmed_payment_buys_a_year_and_pushes_it(self, queue_push, create_link):
		# covers: AC-7, AC-8, AC-13, AC-19
		row, checkout = self.checkout("paid.rarebooks.app")
		reference = checkout.order_reference
		response, data, sendmail = self.deliver(webhook_body(reference), [paid_item(reference)])
		self.assertEqual((response.status_code, data), (200, {"result": "applied"}))

		checkout.reload()
		row.reload()
		self.assertEqual(checkout.status, "Paid")
		self.assertIsNotNone(checkout.paid_on)
		# Paid during the trial (ends 2026-12-08), so the year starts the day after it.
		self.assertEqual(
			(str(row.paid_until), row.provider, row.billing_period, row.auto_renews, row.provider_reference),
			("2027-12-08", "ClickPesa", "Yearly", 0, reference),
		)
		self.assertEqual((row.state_version, row.push_status), (1, "Pending"))
		queue_push.assert_called_with(row.name)
		event = frappe.get_doc(EVENT, {"event_id": "clickpesa:PAY1:PAYMENT RECEIVED"})
		self.assertEqual(
			(event.outcome, event.site, str(event.paid_until_after)), ("Applied", row.name, "2027-12-08")
		)
		# No ops alert; the only email is the owner's receipt (AC-19).
		sendmail.assert_called_once()
		self.assertEqual(sendmail.call_args.kwargs["recipients"], ["owner@example.com"])

		stub = CustomerStub(row.get_password("control_secret"))
		with patch("rarebooks_control.push.post", stub):
			push.push_site(row.name)
		row.reload()
		self.assertEqual((row.push_status, row.pushed_version), ("Synced", 1))
		self.assertEqual(stub.pushes[0]["paid_until"], "2027-12-08")
		self.assertEqual(stub.pushes[0]["provider_reference"], reference)
		self.assertTrue(stub.pushes[0]["issued_at"].endswith("+00:00"))

	def test_a_repeated_webhook_changes_nothing(self, queue_push, create_link):
		# covers: AC-7
		row, checkout = self.checkout("twice.rarebooks.app")
		body = webhook_body(checkout.order_reference)
		self.deliver(body, [paid_item(checkout.order_reference)])
		_, data, _ = self.deliver(body, [paid_item(checkout.order_reference)])
		self.assertEqual(data, {"result": "duplicate"})
		self.assertEqual(frappe.db.count(EVENT, {"site": row.name}), 1)
		self.assertEqual(frappe.db.get_value(SITE, row.name, "state_version"), 1)

	def test_a_bad_checksum_answers_401_and_stores_nothing(self, queue_push, create_link):
		# covers: AC-7, AC-23
		_, checkout = self.checkout("forged.rarebooks.app")
		body = webhook_body(checkout.order_reference, payment_id="FORGED")
		body["data"]["collectedAmount"] = "999999"
		response, _, _ = self.deliver(body, [paid_item(checkout.order_reference)])
		self.assertEqual(response.status_code, 401)
		self.assertFalse(frappe.db.exists(EVENT, {"event_id": ("like", "clickpesa:FORGED:%")}))
		self.assertEqual(frappe.db.get_value("Books Checkout", checkout.name, "status"), "Created")

	def test_a_checksum_with_non_ascii_characters_answers_401(self, queue_push, create_link):
		# covers: AC-7 (a forged checksum is refused, never a 500 from compare_digest)
		_, checkout = self.checkout("nonascii.rarebooks.app")
		body = webhook_body(checkout.order_reference, payment_id="NONASCII")
		body["checksum"] = "\u00e9" * 64
		response, _, _ = self.deliver(body, [paid_item(checkout.order_reference)])
		self.assertEqual(response.status_code, 401)
		self.assertFalse(frappe.db.exists(EVENT, {"event_id": ("like", "clickpesa:NONASCII:%")}))

	def test_clickpesa_down_answers_503_and_a_redelivery_applies_it(self, queue_push, create_link):
		# covers: AC-7
		row, checkout = self.checkout("retry.rarebooks.app")
		body = webhook_body(checkout.order_reference, payment_id="PAY503")
		response, _, _ = self.deliver(body, ClickPesaError("timeout"))
		self.assertEqual(response.status_code, 503)
		event_id = "clickpesa:PAY503:PAYMENT RECEIVED"
		self.assertEqual(frappe.db.get_value(EVENT, {"event_id": event_id}, "outcome"), "")
		self.assertIsNone(frappe.db.get_value(SITE, row.name, "paid_until"))

		_, data, _ = self.deliver(body, [paid_item(checkout.order_reference, payment_id="PAY503")])
		self.assertEqual(data, {"result": "applied"})
		self.assertEqual(frappe.db.count(EVENT, {"event_id": event_id}), 1)

	def test_the_webhook_body_is_never_trusted(self, queue_push, create_link):
		# covers: AC-7 (the status query decides, not the body)
		row, checkout = self.checkout("pending.rarebooks.app")
		body = webhook_body(checkout.order_reference, payment_id="PAYPENDING")
		_, data, _ = self.deliver(body, [paid_item(checkout.order_reference, status="PROCESSING")])
		self.assertEqual(data, {"result": "ignored"})
		self.assertEqual(frappe.db.get_value("Books Checkout", checkout.name, "status"), "Created")
		self.assertIsNone(frappe.db.get_value(SITE, row.name, "paid_until"))

	def test_an_underpayment_or_wrong_currency_is_a_mismatch(self, queue_push, create_link):
		# covers: AC-10
		for site, item in (
			("under.rarebooks.app", {"amount": 119999}),
			("usd.rarebooks.app", {"currency": "USD"}),
		):
			with self.subTest(site=site):
				row, checkout = self.checkout(site)
				body = webhook_body(checkout.order_reference, payment_id=f"PAY-{site}")
				_, data, sendmail = self.deliver(
					body, [paid_item(checkout.order_reference, payment_id=f"PAY-{site}", **item)]
				)
				self.assertEqual(data, {"result": "mismatch"})
				self.assertEqual(frappe.db.get_value("Books Checkout", checkout.name, "status"), "Mismatch")
				self.assertIsNone(frappe.db.get_value(SITE, row.name, "paid_until"))
				self.assertEqual(frappe.db.get_value(SITE, row.name, "state_version"), 0)
				sendmail.assert_called_once()
				self.assertEqual(sendmail.call_args.kwargs["recipients"], ["ops@example.com"])
				self.assertIn("Asha", sendmail.call_args.kwargs["message"])

	def test_a_second_payment_on_a_paid_order_gives_no_extra_year(self, queue_push, create_link):
		# covers: AC-7
		row, checkout = self.checkout("double.rarebooks.app")
		reference = checkout.order_reference
		first = paid_item(reference, payment_id="PAYA", created="2026-11-18T10:00:00Z")
		self.deliver(webhook_body(reference, payment_id="PAYA"), [first])
		second = paid_item(reference, payment_id="PAYB", created="2026-11-18T11:00:00Z")
		_, data, sendmail = self.deliver(webhook_body(reference, payment_id="PAYB"), [first, second])
		self.assertEqual(data, {"result": "unknown"})
		self.assertEqual(str(frappe.db.get_value(SITE, row.name, "paid_until")), "2027-12-08")
		sendmail.assert_called_once()

	def test_payment_for_a_removed_site_or_an_unknown_order_is_unknown(self, queue_push, create_link):
		# covers: AC-17
		row, checkout = self.checkout("removing.rarebooks.app")
		frappe.db.set_value(SITE, row.name, "status", "Removed")
		_, data, sendmail = self.deliver(
			webhook_body(checkout.order_reference, payment_id="PAYGONE"),
			[paid_item(checkout.order_reference, payment_id="PAYGONE")],
		)
		self.assertEqual(data, {"result": "unknown"})
		self.assertIsNone(frappe.db.get_value(SITE, row.name, "paid_until"))
		sendmail.assert_called_once()

		_, data, sendmail = self.deliver(webhook_body("RB99999999", payment_id="PAYNONE"), [])
		self.assertEqual(data, {"result": "unknown"})
		sendmail.assert_called_once()

	def test_an_exempt_site_payment_is_applied(self, queue_push, create_link):
		# covers: AC-17
		_, checkout = self.checkout("exempt.rarebooks.app", exempt=1)
		_, data, _ = self.deliver(
			webhook_body(checkout.order_reference, payment_id="PAYEX"),
			[paid_item(checkout.order_reference, payment_id="PAYEX")],
		)
		self.assertEqual(data, {"result": "applied"})


class IntegrationTestPush(IntegrationTestCase):
	def setUp(self):
		reset_billing()

	def test_a_failed_push_is_failing_with_the_next_try_due(self):
		# covers: AC-13
		row = make_site("down.push.example.com", state_version=1, push_status="Pending")
		stub = CustomerStub(row.get_password("control_secret"), error=requests.Timeout())
		with patch("rarebooks_control.push.post", stub):
			push.push_site(row.name)
		row.reload()
		self.assertEqual((row.push_status, row.push_attempts, row.pushed_version), ("Failing", 1, 0))
		self.assertEqual(row.last_push_error, "push failed: Timeout")
		self.assertEqual(round((row.next_push_on - row.failing_since).total_seconds() / 60), 1)
		self.assertEqual([push.retry_minutes(n) for n in range(1, 7)], [1, 5, 15, 60, 60, 60])

	def test_a_change_during_a_push_sends_again(self):
		# covers: AC-13 (`pushed_version` must reach `state_version` before the row is Synced)
		row = make_site("busy.push.example.com", state_version=2, push_status="Pending")

		def change_meanwhile(*args, **kwargs):
			frappe.db.set_value(SITE, row.name, "state_version", 3)
			return stub(*args, **kwargs)

		stub = CustomerStub(row.get_password("control_secret"), result="ignored")
		with (
			patch("rarebooks_control.push.post", change_meanwhile),
			patch("rarebooks_control.push.queue_push") as queue_push,
		):
			push.push_site(row.name)
		row.reload()
		self.assertEqual((row.pushed_version, row.push_status), (2, "Pending"))
		queue_push.assert_called_once_with(row.name)

	def test_a_removed_site_is_never_pushed(self):
		# covers: AC-13
		row = make_site("removed.push.example.com", status="Removed", state_version=1)
		with patch("rarebooks_control.push.post") as post:
			push.push_site(row.name)
		post.assert_not_called()
