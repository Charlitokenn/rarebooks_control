# Copyright (c) 2026, RareBooks and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class BooksOnboardingSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		build_failures: DF.Int
		discard_mode: DF.Literal["dry_run", "live"]
		frappe_cloud_domain: DF.Data | None
		frappe_cloud_plan: DF.Data | None
		frappe_cloud_server: DF.Data | None
		marketing_notify_url: DF.Data | None
		marketing_secret: DF.Password | None
		max_claims_per_day: DF.Int
		paused: DF.Check
		pool_builds_paused: DF.Check
		pool_pause_reason: DF.SmallText | None
		pool_size: DF.Int
	# end: auto-generated types

	def validate(self):
		self.marketing_notify_url = (self.marketing_notify_url or "").strip() or None
		if self.marketing_notify_url and not self.marketing_notify_url.startswith("https://"):
			frappe.throw(_("The marketing notify URL must start with https://"))
		self.frappe_cloud_domain = (self.frappe_cloud_domain or "").strip().strip(".").lower() or None
		# Spec 0014 AC-16: clearing the pause by hand also starts the failure count again.
		before = self.get_doc_before_save()
		if before and before.pool_builds_paused and not self.pool_builds_paused:
			self.build_failures = 0
			self.pool_pause_reason = None
