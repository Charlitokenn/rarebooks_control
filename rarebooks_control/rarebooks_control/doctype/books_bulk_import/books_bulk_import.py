# Copyright (c) 2026, RareBooks and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document

from rarebooks_control import bulk_import

# Written only by `bulk_import.py` (with `flags.bulk_import_write`), never from desk or the API (spec 0025).
SERVER_FIELDS = (
	"status",
	"attempt",
	"site_import",
	"columns",
	"columns_fetched_on",
	"checked_hash",
	"checked_on",
	"sending_since",
	"sent_on",
	"finished_on",
	"last_progress_on",
	"success_count",
	"failed_count",
	"results",
	"last_error",
	"rows_cleared_on",
	"site_processed",
)
NEW_VALUES = {"status": "Draft", "attempt": 0, "success_count": 0, "failed_count": 0, "site_processed": 0}


class BooksBulkImport(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		attempt: DF.Int
		checked_hash: DF.Data | None
		checked_on: DF.Datetime | None
		columns: DF.LongText | None
		columns_fetched_on: DF.Datetime | None
		failed_count: DF.Int
		finished_on: DF.Datetime | None
		grid_html: DF.HTML | None
		hosted_site: DF.Link
		import_type: DF.Autocomplete
		last_error: DF.SmallText | None
		last_progress_on: DF.Datetime | None
		results: DF.LongText | None
		row_count: DF.Int
		rows: DF.LongText | None
		rows_cleared_on: DF.Datetime | None
		sending_since: DF.Datetime | None
		sent_on: DF.Datetime | None
		site_import: DF.Data | None
		site_processed: DF.Int
		status: DF.Literal["Draft", "Checked", "Importing", "Success", "Partial Success", "Failed", "Refused"]
		submit_after_import: DF.Check
		success_count: DF.Int
	# end: auto-generated types

	def validate(self):
		before = self.get_doc_before_save()
		server = bool(self.flags.bulk_import_write)
		if not server:
			self._keep_server_fields(before)
			if before and before.rows_cleared_on:
				frappe.throw(_("This import's rows were cleared, so it is final."))
		if self.import_type in bulk_import.NEVER_SUBMITTED:
			# Spec 0004 AC-12: an expense is always imported as a draft.
			self.submit_after_import = 0
		if self.rows_cleared_on:
			self.rows = None
		else:
			self.rows = json.dumps(bulk_import.parse_rows(self.rows))
			self.row_count = len(json.loads(self.rows))
		if not server:
			self._check_edit(before)
		if not self.rows_cleared_on:
			# A cleared import keeps its counts (AC-14).
			bulk_import._count(self)

	def on_update(self):
		before = self.get_doc_before_save()
		if before and before.status != self.status:
			# Track changes is off, so the rows are never copied into a Version (AC-14); this names who, what, when.
			self.add_comment(
				"Info",
				_("{0} set the status to {1} (attempt {2}, {3} saved, {4} failed).").format(
					frappe.session.user, _(self.status), self.attempt, self.success_count, self.failed_count
				),
			)

	def _keep_server_fields(self, before):
		for field in SERVER_FIELDS:
			self.set(field, before.get(field) if before else NEW_VALUES.get(field))

	def _check_edit(self, before):
		"""A user's save: who may change what, then the status reset of AC-6 and AC-10."""
		moved_site = (
			not before or before.hosted_site != self.hosted_site or before.import_type != self.import_type
		)
		if before and before.attempt and moved_site:
			frappe.throw(_("The site and the type are fixed once an import was sent."))
		if before and before.status == "Importing" and self._changed(before):
			frappe.throw(_("This import is running. Wait for it to finish before editing."))
		if before and before.status == "Success" and self._changed(before):
			frappe.throw(_("This import finished with every row saved."))
		refusal = bulk_import.eligibility_refusal(self.hosted_site)
		if refusal:
			frappe.throw(refusal)
		if moved_site or not self.columns:
			bulk_import.fill_columns(self)
		self._keep_saved_rows(before)
		if self.status == "Checked" and bulk_import.checked_hash(self) != self.checked_hash:
			self.status = "Draft"
		elif self.status in ("Refused", "Partial Success", "Failed") and self._changed(before):
			self.status = "Draft"

	def _changed(self, before) -> bool:
		return bool(before) and bulk_import.checked_hash(self) != bulk_import.checked_hash(before)

	def _keep_saved_rows(self, before):
		"""A row with a successful result is read only and never deleted (AC-10)."""
		results = json.loads(self.results or "{}")
		saved = {row_id for row_id, result in results.items() if result.get("success")}
		if not saved or not before:
			return
		old = {row["id"]: row["cells"] for row in bulk_import.parse_rows(before.rows)}
		new = {row["id"]: row["cells"] for row in json.loads(self.rows or "[]")}
		for row_id in saved & set(old):
			if new.get(row_id) != old[row_id]:
				frappe.throw(_("A row that saved on the site can't be edited or deleted."))
