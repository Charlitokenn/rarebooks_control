"""Books Plan rules (spec 0008 data model): provider terms, rounding, and one plan per provider and period."""

from decimal import Decimal

import frappe
from frappe.tests import IntegrationTestCase


def plan(**values):
	return frappe.get_doc(
		{
			"doctype": "Books Plan",
			"provider": "PayPal",
			"billing_period": "Monthly",
			"currency": "USD",
			"amount": 9,
			"label": "Monthly",
			"paypal_plan_id": "P-TEST",
			"enabled": 1,
			**values,
		}
	)


class IntegrationTestBooksPlan(IntegrationTestCase):
	def setUp(self):
		frappe.db.delete("Books Plan")

	def test_provider_terms(self):
		for values, message in (
			({"provider": "ClickPesa", "currency": "TZS", "billing_period": "Monthly"}, "yearly only"),
			({"provider": "ClickPesa", "currency": "USD", "billing_period": "Yearly"}, "priced in TZS"),
			({"currency": "TZS"}, "priced in USD"),
			({"paypal_plan_id": " "}, "plan ID from PayPal"),
			({"amount": 0}, "more than zero"),
		):
			with self.subTest(**values):
				self.assertRaisesRegex(frappe.ValidationError, message, plan(**values).insert)

	def test_amounts_round_to_the_currency(self):
		usd = plan(amount=9.995).insert()
		tzs = plan(
			provider="ClickPesa", billing_period="Yearly", currency="TZS", amount=120000.5, paypal_plan_id="X"
		).insert()
		self.assertEqual(Decimal(str(usd.amount)), Decimal("10.00"))
		self.assertEqual(Decimal(str(tzs.amount)), Decimal("120001"))
		# A ClickPesa plan keeps no PayPal plan id.
		self.assertIsNone(tzs.paypal_plan_id)

	def test_one_plan_per_provider_and_period_plus_one_test_plan(self):
		plan().insert()
		self.assertRaisesRegex(frappe.ValidationError, "already a plan", plan().insert)
		plan(test_only=1, amount=1).insert()
		self.assertRaisesRegex(frappe.ValidationError, "already a test plan", plan(test_only=1).insert)
		plan(billing_period="Yearly", amount=90).insert()
