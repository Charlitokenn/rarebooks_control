# Copyright (c) 2026, RareBooks and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class BooksRemovalSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		books_bench_group: DF.Data | None
		last_run_on: DF.Datetime | None
		max_drops_per_day: DF.Int
		mode: DF.Literal["off", "dry_run", "live"]
		removal_paused: DF.Check
		require_approval: DF.Check
	# end: auto-generated types

	def validate(self):
		# Spec 0010 AC-9: the drop guard and registration compare Frappe Cloud's `group` with this value, so it
		# must be set before anything runs.
		self.books_bench_group = (self.books_bench_group or "").strip() or None
		if self.mode != "off" and not self.books_bench_group:
			frappe.throw(_("Set the Books bench group before turning removal on."))
		if (self.max_drops_per_day or 0) < 0:
			frappe.throw(_("Max drops per day can't be negative."))

	def on_update(self):
		"""Spec 0010 AC-9: going from dry_run to live cancels every open dry_run case and deletes its export, so
		live starts with real warnings to real owners."""
		before = self.get_doc_before_save()
		if before and before.mode == "dry_run" and self.mode == "live":
			from rarebooks_control.removal import cancel_dry_runs

			cancel_dry_runs()
