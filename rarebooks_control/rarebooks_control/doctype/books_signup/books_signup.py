# Copyright (c) 2026, RareBooks and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BooksSignup(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		alerted_on: DF.Datetime | None
		attempts: DF.Int
		claimed_on: DF.Datetime | None
		clerk_user_id: DF.Data
		company_name: DF.Data
		email: DF.Data
		email_key: DF.Data
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
		requested_on: DF.Datetime
		status: DF.Literal["Queued", "Claiming", "Preparing", "Ready", "Failed"]
		step: DF.Literal["", "Owner", "Domain"]
		subdomain: DF.Data
		time_zone: DF.Data
	# end: auto-generated types

	def validate(self):
		# MariaDB's unique index allows many NULLs but only one empty string: one claim per site (spec 0014).
		if not self.hosted_site:
			self.hosted_site = None
