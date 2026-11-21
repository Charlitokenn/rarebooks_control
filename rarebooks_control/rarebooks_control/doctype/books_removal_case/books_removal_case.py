# Copyright (c) 2026, RareBooks and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

# A case in one of these is over; any other status is open (spec 0007).
FINAL_STATUSES = ("Removed", "Cancelled")


class BooksRemovalCase(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from rarebooks_control.rarebooks_control.doctype.books_removal_warning.books_removal_warning import (
			BooksRemovalWarning,
		)

		approved_by: DF.Link | None
		approved_on: DF.Datetime | None
		archive_requested_on: DF.Datetime | None
		attempt_id: DF.Data | None
		attempts: DF.Int
		company_name: DF.Data | None
		delete_export_on: DF.Date | None
		download_count: DF.Int
		drop_started_on: DF.Datetime | None
		error_runs: DF.Int
		export_bytes: DF.Int
		export_claimed_on: DF.Datetime | None
		export_email: DF.Link | None
		export_key: DF.Data | None
		export_queued_on: DF.Datetime | None
		export_sent_on: DF.Datetime | None
		export_sha256: DF.Data | None
		hold_until: DF.Datetime | None
		kind: DF.Literal["Trial", "Lapsed"]
		last_downloaded_on: DF.Datetime | None
		last_error: DF.SmallText | None
		link_expires_on: DF.Datetime | None
		link_token_hash: DF.Data | None
		mode: DF.Literal["dry_run", "live"]
		owner_email: DF.Data
		pause_reason: DF.SmallText | None
		paused_by: DF.Link | None
		removal_on: DF.Date
		removed_on: DF.Datetime | None
		site: DF.Link
		site_language: DF.Data | None
		site_url: DF.Data
		status: DF.Literal[
			"Scheduled", "Exporting", "Holding", "Paused", "Dropping", "Removed", "Cancelled", "Failed"
		]
		warnings: DF.Table[BooksRemovalWarning]
	# end: auto-generated types

	def validate(self):
		self.validate_one_open_case()
		self.validate_unique_offsets()

	def validate_one_open_case(self):
		"""A site has at most one open case (spec 0007, key invariants)."""
		if self.status in FINAL_STATUSES:
			return
		other = frappe.db.exists(
			"Books Removal Case",
			{"site": self.site, "status": ("not in", FINAL_STATUSES), "name": ("!=", self.name or "")},
		)
		if other:
			frappe.throw(_("Site {0} already has an open removal case {1}.").format(self.site, other))

	def validate_unique_offsets(self):
		"""Each warning is sent at most once per case (spec 0007 AC-4)."""
		offsets = [row.offset_days for row in self.warnings]
		if len(offsets) != len(set(offsets)):
			frappe.throw(_("A removal case can hold each warning only once."))
