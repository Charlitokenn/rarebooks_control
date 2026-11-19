# Copyright (c) 2026, RareBooks and contributors
# For license information, please see license.txt

from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe import _
from frappe.model.document import Document

# ClickPesa takes whole shillings, PayPal takes US cents (spec 0008).
CURRENCY_STEP = {"TZS": Decimal("1"), "USD": Decimal("0.01")}
PROVIDER_CURRENCY = {"ClickPesa": "TZS", "PayPal": "USD"}


class BooksPlan(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		billing_period: DF.Literal["Monthly", "Yearly"]
		currency: DF.Literal["TZS", "USD"]
		enabled: DF.Check
		label: DF.Data
		paypal_plan_id: DF.Data | None
		provider: DF.Literal["ClickPesa", "PayPal"]
		test_only: DF.Check
	# end: auto-generated types

	def validate(self):
		self.validate_provider_terms()
		self.round_amount()
		self.validate_unique()

	def validate_provider_terms(self):
		"""ClickPesa is yearly in TZS; PayPal is in USD and needs the plan id made in PayPal."""
		if self.provider == "ClickPesa" and self.billing_period != "Yearly":
			frappe.throw(_("ClickPesa plans are yearly only."))
		if self.currency != PROVIDER_CURRENCY[self.provider]:
			frappe.throw(
				_("{0} plans are priced in {1}.").format(self.provider, PROVIDER_CURRENCY[self.provider])
			)
		if self.provider == "PayPal" and not (self.paypal_plan_id or "").strip():
			frappe.throw(_("Paste the plan ID from PayPal."))
		if self.provider != "PayPal":
			self.paypal_plan_id = None

	def round_amount(self):
		"""Round to the currency's smallest unit, as a Decimal, so 9.995 USD is 10.00 and never 9.99."""
		amount = Decimal(str(self.amount or 0)).quantize(CURRENCY_STEP[self.currency], rounding=ROUND_HALF_UP)
		if amount <= 0:
			frappe.throw(_("The amount must be more than zero."))
		self.amount = amount

	def validate_unique(self):
		"""One plan per provider and period, plus at most one test plan beside it."""
		# Serialize validation and save on a row that exists even before the first plan.
		frappe.db.get_value("DocType", "Books Plan", "name", for_update=True)
		filters = {
			"provider": self.provider,
			"billing_period": self.billing_period,
			"test_only": self.test_only,
			"name": ("!=", self.name),
		}
		if frappe.db.get_value("Books Plan", filters, "name", for_update=True):
			kind = _("test plan") if self.test_only else _("plan")
			frappe.throw(
				_("There is already a {0} for {1} {2}.").format(kind, self.provider, _(self.billing_period))
			)
