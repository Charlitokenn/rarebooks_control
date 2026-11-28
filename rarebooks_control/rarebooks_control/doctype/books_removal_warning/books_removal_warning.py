# Copyright (c) 2026, RareBooks and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BooksRemovalWarning(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		email: DF.Link | None
		offset_days: DF.Int
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		recipient: DF.Data
		sent_on: DF.Datetime
	# end: auto-generated types

	pass
