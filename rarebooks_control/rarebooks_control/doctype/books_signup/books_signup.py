# Copyright (c) 2026, RareBooks and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

# One live sign up per person and per label; a Released row gives all three up (spec 0016 AC-3).
KEY_FIELDS = ("email_key", "clerk_user_id", "subdomain")
# Unique, and blank on some rows: a claim not made yet, or the keys of a Released sign up.
UNIQUE_FIELDS = ("hosted_site", *KEY_FIELDS)


class BooksSignup(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		alerted_on: DF.Datetime | None
		attempts: DF.Int
		claimed_on: DF.Datetime | None
		clerk_user_id: DF.Data | None
		company_name: DF.Data
		email: DF.Data
		email_key: DF.Data | None
		failed_reason: DF.SmallText | None
		first_name: DF.Data
		hosted_site: DF.Link | None
		last_name: DF.Data | None
		notified_status: DF.Data | None
		notify_alerted_on: DF.Datetime | None
		notify_started_on: DF.Datetime | None
		notify_status: DF.Data | None
		queue_alerted_on: DF.Datetime | None
		ready_on: DF.Datetime | None
		release_reason: DF.SmallText | None
		released_by: DF.Link | None
		released_clerk_user_id: DF.Data | None
		released_email_key: DF.Data | None
		released_from: DF.Literal["", "Ready", "Failed"]
		released_on: DF.Datetime | None
		released_subdomain: DF.Data | None
		requested_on: DF.Datetime
		status: DF.Literal["Queued", "Claiming", "Preparing", "Ready", "Failed", "Released"]
		step: DF.Literal["", "Owner", "Domain"]
		subdomain: DF.Data | None
		time_zone: DF.Data
	# end: auto-generated types

	def validate(self):
		# MariaDB's unique index allows many NULLs but only one empty string: one claim per site (spec 0014),
		# and the keys a Released row gives up never clash with another one (spec 0016 AC-3).
		for field in UNIQUE_FIELDS:
			if not self.get(field):
				self.set(field, None)
		# Frappe checks `mandatory_depends_on` only in the browser, so the keys' rule is kept here too.
		if self.status != "Released":
			for field in KEY_FIELDS:
				if not self.get(field):
					frappe.throw(
						_("{0} is required unless the sign up is Released.").format(
							_(self.meta.get_label(field))
						),
						frappe.MandatoryError,
					)
