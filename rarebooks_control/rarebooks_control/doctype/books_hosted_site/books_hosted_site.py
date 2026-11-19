# Copyright (c) 2026, RareBooks and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class BooksHostedSite(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		allow_test_plans: DF.Check
		auto_renews: DF.Check
		billing_period: DF.Literal["", "Monthly", "Yearly"]
		company_name: DF.Data | None
		control_secret: DF.Password | None
		exempt: DF.Check
		failing_alerted: DF.Check
		failing_since: DF.Datetime | None
		hosting_bench: DF.Link
		language: DF.Data | None
		last_payment_on: DF.Datetime | None
		last_push_error: DF.SmallText | None
		last_pushed_on: DF.Datetime | None
		next_push_on: DF.Datetime | None
		owner_email: DF.Data
		paid_until: DF.Date | None
		pending_secret: DF.Password | None
		provider: DF.Literal["", "ClickPesa", "PayPal"]
		provider_reference: DF.Data | None
		push_attempts: DF.Int
		push_status: DF.Literal["Synced", "Pending", "Failing"]
		pushed_version: DF.Int
		removed_on: DF.Datetime | None
		site: DF.Data
		site_url: DF.Data
		state_version: DF.Int
		status: DF.Literal["Active", "Removed"]
		time_zone: DF.Data | None
		trial_ends_on: DF.Date | None
	# end: auto-generated types

	def validate(self):
		# Hidden, so Frappe can't mark it mandatory; every row needs its secret (spec 0008 AC-1).
		if not self.control_secret:
			frappe.throw(_("A hosted site needs its control secret."))
		if not (self.site_url or "").startswith("https://"):
			frappe.throw(_("The site URL must start with https://"))
		# A ClickPesa payment always buys one year (spec 0008 AC-8, AC-14).
		if self.provider == "ClickPesa" and self.billing_period != "Yearly":
			frappe.throw(_("A ClickPesa site is billed yearly. Set the billing period to Yearly."))
		if self.is_manual_edit():
			self.validate_provider_reference()

	def is_manual_edit(self) -> bool:
		"""A save from desk or the API by a person. The control site's own writes (payments, pushes, rotation)
		save with `ignore_permissions`, so they may set `provider_reference` on their own (spec 0008 AC-9)."""
		return not self.is_new() and not self.flags.ignore_permissions

	def validate_provider_reference(self):
		"""`provider_reference` changes by hand only in the same save as a `provider` change (spec 0008 AC-14),
		so an edit can't point a site at another PayPal subscription by mistake."""
		if self.has_value_changed("provider_reference") and not self.has_value_changed("provider"):
			frappe.throw(_("Change the provider reference only together with the provider."))

	def before_save(self):
		from rarebooks_control.push import mark_changed
		from rarebooks_control.state import site_state

		before = self.get_doc_before_save()
		if before and site_state(self) != site_state(before):
			self.state_version = before.state_version
			mark_changed(self)
