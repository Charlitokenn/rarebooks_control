# Copyright (c) 2026, RareBooks and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class BooksPaymentEvent(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		checkout: DF.Link | None
		currency: DF.Data | None
		event_id: DF.Data
		event_type: DF.Data
		outcome: DF.Literal["", "Applied", "Ignored", "Mismatch", "Refund", "Unknown"]
		paid_until_after: DF.Date | None
		provider: DF.Literal["ClickPesa", "PayPal"]
		raw_payload: DF.LongText | None
		received_on: DF.Datetime
		site: DF.Link | None
	# end: auto-generated types

	pass
