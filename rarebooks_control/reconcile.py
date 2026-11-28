"""Scheduled jobs that catch what a webhook missed, and keep the records tidy (spec 0008 AC-10, AC-16, AC-18).

Each check goes through the same `billing.check_*` step as the return page, so a missed webhook is applied
exactly as the webhook would have applied it. One failing provider call is logged and skipped; it never
stops the rest of the run.
"""

from datetime import timedelta

import frappe
from frappe import _

from rarebooks_control import billing
from rarebooks_control.alerts import send_ops
from rarebooks_control.providers.clickpesa import ClickPesaError
from rarebooks_control.providers.paypal import PayPalError
from rarebooks_control.signing import utc_now

CHECKOUT = "Books Checkout"
EVENT = "Books Payment Event"
CLICKPESA_WINDOW = timedelta(hours=48)
PAYPAL_CHECKOUT_WINDOW = timedelta(days=7)
CHECKOUT_LIFETIME = timedelta(hours=24)
RAW_PAYLOAD_DAYS = 90
DIGEST_WINDOW = timedelta(days=1)


def hourly():
	expire_checkouts()
	reconcile_clickpesa()


def daily():
	reconcile_paypal()
	purge_raw_payloads()
	send_failed_payment_digest()


def expire_checkouts():
	"""`Created` checkouts older than 24 hours become `Expired`. A later confirmed payment still counts."""
	frappe.db.set_value(
		CHECKOUT,
		{"status": "Created", "creation": ("<", frappe.utils.now_datetime() - CHECKOUT_LIFETIME)},
		"status",
		"Expired",
	)
	frappe.db.commit()


def reconcile_clickpesa():
	"""Every `Created` or `Expired` ClickPesa checkout less than 48 hours old (AC-16)."""
	checkouts = frappe.get_all(
		CHECKOUT,
		filters={
			"provider": "ClickPesa",
			"status": ("in", ("Created", "Expired")),
			"creation": (">", frappe.utils.now_datetime() - CLICKPESA_WINDOW),
		},
		pluck="name",
	)
	for checkout in checkouts:
		_run(billing.check_clickpesa, checkout, ClickPesaError)


def reconcile_paypal():
	"""The subscription of every renewing PayPal site, and of every unpaid PayPal checkout less than 7 days
	old (AC-16)."""
	renewing = frappe.get_all(
		"Books Hosted Site",
		filters={
			"status": "Active",
			"provider": "PayPal",
			"auto_renews": 1,
			"provider_reference": ("is", "set"),
		},
		pluck="provider_reference",
	)
	recent = frappe.get_all(
		CHECKOUT,
		filters={
			"provider": "PayPal",
			"status": ("!=", "Paid"),
			"paypal_subscription_id": ("is", "set"),
			"creation": (">", frappe.utils.now_datetime() - PAYPAL_CHECKOUT_WINDOW),
		},
		pluck="paypal_subscription_id",
	)
	for subscription_id in dict.fromkeys((*renewing, *recent)):
		_run(billing.check_paypal, subscription_id, PayPalError)


def purge_raw_payloads():
	"""Blank `raw_payload` on events older than 90 days; every other field stays (AC-18)."""
	cutoff = utc_now() - timedelta(days=RAW_PAYLOAD_DAYS)
	frappe.db.set_value(
		EVENT,
		{"received_on": ("<", cutoff), "raw_payload": ("is", "set")},
		"raw_payload",
		None,
		update_modified=False,
	)
	frappe.db.commit()


def send_failed_payment_digest():
	"""One queued email listing the last day's PayPal payment failures (AC-10). None on a quiet day."""
	failures = frappe.get_all(
		EVENT,
		filters={
			"event_type": "BILLING.SUBSCRIPTION.PAYMENT.FAILED",
			"received_on": (">=", utc_now() - DIGEST_WINDOW),
		},
		fields=["event_id", "site", "received_on"],
		order_by="received_on asc",
	)
	if not failures:
		return
	lines = [_("PayPal could not take these payments in the last day. PayPal retries them on its own.")]
	lines += [
		_("{0} UTC, site {1}, event {2}").format(row.received_on, row.site or _("unknown"), row.event_id)
		for row in failures
	]
	send_ops(_("{0} failed PayPal payments").format(len(failures)), lines, now=False)


def _run(check, reference: str, provider_error: type[Exception]):
	try:
		check(reference)
		frappe.db.commit()
	except provider_error as error:
		frappe.db.rollback()
		frappe.log_error(f"Billing reconcile: {check.__name__} failed", str(error))
