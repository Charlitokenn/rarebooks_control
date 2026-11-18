# Copyright (c) 2026, RareBooks and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class BooksHostingBench(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		bench_name: DF.Data
		last_mode: DF.Literal["", "off", "dry_run", "live"]
		last_report_on: DF.Datetime | None
		removal_paused: DF.Check
	# end: auto-generated types

	pass
