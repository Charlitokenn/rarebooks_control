# Copyright (c) 2026, RareBooks and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class BooksCheckout(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		billing_period: DF.Literal["Monthly", "Yearly"]
		currency: DF.Literal["TZS", "USD"]
		order_reference: DF.Data | None
		paid_on: DF.Datetime | None
		paypal_subscription_id: DF.Data | None
		plan: DF.Link
		provider: DF.Literal["ClickPesa", "PayPal"]
		provider_url: DF.SmallText
		return_url: DF.Data
		site: DF.Link
		status: DF.Literal["Created", "Paid", "Mismatch", "Expired"]
		token_hash: DF.Data | None
	# end: auto-generated types

	def validate(self):
		# Hidden, so Frappe can't mark it mandatory; the `go` link finds the checkout by it (spec 0008 AC-6).
		if not self.token_hash:
			frappe.throw(_("A checkout needs its token hash."))
