"""The PayPal go live check and the token cache key (spec 0008 Configuration, build plan task 20, AC-23)."""

import copy
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from rarebooks_control import paypal_check
from rarebooks_control.providers import paypal
from rarebooks_control.tests.control import make_plan, reset_billing

URL = "https://control.example.com"
WEBHOOK_ID = "73911102J1804474H"
CONF = {
	"paypal_mode": "live",
	"paypal_client_id": "CLIENT",
	"paypal_client_secret": "SECRET",
	"paypal_webhook_id": WEBHOOK_ID,
}


def held_plan(plan_id: str, unit="MONTH", value="39.0", currency="USD", threshold=3, version=1) -> dict:
	"""A plan as `GET /v1/billing/plans/{id}` answers it."""
	return {
		"id": plan_id,
		"status": "ACTIVE",
		"billing_cycles": [
			{
				"pricing_scheme": {
					"version": version,
					"fixed_price": {"currency_code": currency, "value": value},
				},
				"frequency": {"interval_unit": unit, "interval_count": 1},
				"tenure_type": "REGULAR",
				"sequence": 1,
				"total_cycles": 0,
			}
		],
		"payment_preferences": {
			"auto_bill_outstanding": True,
			"setup_fee": {"currency_code": currency, "value": "0.0"},
			"payment_failure_threshold": threshold,
		},
	}


def held_webhook(
	url=f"{URL}/api/method/rarebooks_control.webhooks.paypal", events=paypal_check.WEBHOOK_EVENTS
):
	return {"id": WEBHOOK_ID, "url": url, "event_types": [{"name": name} for name in events]}


class UnitTestPayPalPlanProblems(UnitTestCase):
	PLAN = frappe._dict(name="PLAN-1", billing_period="Monthly", amount=39, currency="USD")

	def test_a_plan_like_the_books_plan_has_no_problems(self):
		self.assertEqual(paypal_check.plan_problems(self.PLAN, held_plan("P-1")), ([], []))

	def test_a_threshold_of_one_fails(self):
		# The sandbox plans of 2026-11-21 had 1: one failed charge suspends, so auto renew is lost.
		problems, _warnings = paypal_check.plan_problems(self.PLAN, held_plan("P-1", threshold=1))
		self.assertEqual(len(problems), 1)
		self.assertIn("payment_failure_threshold is 1", problems[0])

	def test_a_price_or_currency_unlike_the_books_plan_fails(self):
		# 390 against a pricing page saying 399: every yearly payment would be Mismatch.
		for value, currency in (("38.99", "USD"), ("39.0", "EUR")):
			problems, _warnings = paypal_check.plan_problems(
				self.PLAN, held_plan("P-1", value=value, currency=currency)
			)
			self.assertEqual(len(problems), 1, (value, currency))
			self.assertIn("the Books Plan expects USD 39", problems[0])

	def test_a_yearly_cycle_on_a_monthly_plan_fails(self):
		problems, _warnings = paypal_check.plan_problems(self.PLAN, held_plan("P-1", unit="YEAR"))
		self.assertIn("not every 1 MONTH", problems[0])

	def test_a_trial_cycle_setup_fee_tax_or_end_fails(self):
		trial = held_plan("P-1")
		trial["billing_cycles"].insert(0, {**trial["billing_cycles"][0], "tenure_type": "TRIAL"})
		fee = held_plan("P-1")
		fee["payment_preferences"]["setup_fee"]["value"] = "5.0"
		tax = {**held_plan("P-1"), "taxes": {"percentage": "18", "inclusive": False}}
		ending = held_plan("P-1")
		ending["billing_cycles"][0]["total_cycles"] = 12
		for held in (trial, fee, tax, ending):
			problems, _warnings = paypal_check.plan_problems(self.PLAN, held)
			self.assertEqual(len(problems), 1, held)

	def test_a_changed_price_only_warns(self):
		# Seen in the sandbox: after a price change on 2026-11-21, the 24 November charges were still 9.00.
		problems, warnings = paypal_check.plan_problems(self.PLAN, held_plan("P-1", version=2))
		self.assertEqual(problems, [])
		self.assertIn("pricing version 2", warnings[0])


class IntegrationTestPayPalCheck(IntegrationTestCase):
	def setUp(self):
		reset_billing()
		frappe.db.delete("Books Plan")
		make_plan(
			provider="PayPal",
			billing_period="Monthly",
			currency="USD",
			amount=39,
			label="Monthly",
			paypal_plan_id="P-M",
		)
		make_plan(
			provider="PayPal",
			billing_period="Yearly",
			currency="USD",
			amount=399,
			label="Yearly",
			paypal_plan_id="P-Y",
		)
		make_plan()
		self.plans = {"P-M": held_plan("P-M"), "P-Y": held_plan("P-Y", unit="YEAR", value="399.00")}
		self.webhook = held_webhook()
		for target, fake in (
			("token", lambda: "TOKEN"),
			("get_plan", lambda plan_id: copy.deepcopy(self.plans.get(plan_id))),
			("get_webhook", lambda webhook_id: self.webhook if webhook_id == WEBHOOK_ID else None),
		):
			patcher = patch(f"rarebooks_control.providers.paypal.{target}", fake)
			patcher.start()
			self.addCleanup(patcher.stop)
		for patcher in (
			patch.dict(frappe.conf, CONF),
			patch("rarebooks_control.paypal_check.get_url", return_value=URL),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	def levels(self) -> list[str]:
		return [level for level, _message in paypal_check.findings()]

	def failures(self) -> list[str]:
		return [message for level, message in paypal_check.findings() if level == paypal_check.FAIL]

	def test_a_site_ready_for_live_passes_every_check(self):
		self.assertEqual(self.failures(), [])
		self.assertNotIn(paypal_check.WARN, self.levels())
		self.assertEqual(paypal_check.run(), "OK")

	def test_an_unset_mode_fails_before_paypal_is_called(self):
		# The module would fall back to sandbox and quietly take no real money.
		with (
			patch.dict(frappe.conf, {"paypal_mode": None}),
			patch("rarebooks_control.providers.paypal.token") as token,
		):
			self.assertEqual(len(self.failures()), 1)
		token.assert_not_called()

	def test_sandbox_mode_is_a_warning(self):
		with patch.dict(frappe.conf, {"paypal_mode": "sandbox"}):
			self.assertEqual(self.failures(), [])
			self.assertIn(paypal_check.WARN, self.levels())

	def test_refused_credentials_fail(self):
		with patch(
			"rarebooks_control.providers.paypal.token", side_effect=paypal.PayPalError("token request failed")
		):
			self.assertEqual(self.failures(), ["PayPal refused the credentials: token request failed"])

	def test_a_webhook_unknown_elsewhere_or_short_of_events_fails(self):
		with patch.dict(frappe.conf, {"paypal_webhook_id": "OTHER"}):
			self.assertEqual(len(self.failures()), 1)
		self.webhook = held_webhook(
			url="https://rarebooks-control.frappe.cloud/api/method/rarebooks_control.webhooks.paypal"
		)
		self.assertEqual(len(self.failures()), 1)
		self.webhook = held_webhook(events=paypal_check.WEBHOOK_EVENTS[:-1])
		self.assertIn("PAYMENT.SALE.REVERSED", self.failures()[0])
		self.webhook = held_webhook(events=("*",))
		self.assertEqual(self.failures(), [])

	def test_a_plan_missing_at_paypal_or_a_period_not_offered_fails(self):
		del self.plans["P-Y"]
		self.assertIn("PayPal has no plan P-Y", self.failures()[0])
		frappe.db.set_value("Books Plan", {"paypal_plan_id": "P-Y"}, "enabled", 0)
		self.assertEqual(self.failures(), ["No enabled PayPal Books Plan for Yearly"])

	def test_an_enabled_test_plan_warns_in_live(self):
		make_plan(
			provider="PayPal",
			billing_period="Monthly",
			currency="USD",
			amount=1,
			label="Test",
			paypal_plan_id="P-T",
			test_only=1,
		)
		self.plans["P-T"] = held_plan("P-T", value="1.00")
		self.assertEqual(self.failures(), [])
		self.assertIn(paypal_check.WARN, self.levels())


class UnitTestPayPalTokenKey(UnitTestCase):
	def test_sandbox_and_live_tokens_are_cached_apart(self):
		# One control site switched from sandbox to live must never send a cached sandbox token to live.
		keys = set()
		for mode, client in (("sandbox", "A"), ("live", "A"), ("live", "B")):
			with patch.dict(frappe.conf, {"paypal_mode": mode, "paypal_client_id": client}):
				keys.add(paypal.token_cache_key())
		self.assertEqual(len(keys), 3)
		self.assertFalse(any("SECRET" in key for key in keys))
