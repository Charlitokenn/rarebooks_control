"""Checkouts, the `go` link and the one confirm step every provider event goes through (spec 0008).

`confirm_clickpesa` and `confirm_paypal` lock the site row first, then ask the provider, then write the
checkout, the event and the site in one transaction. Webhooks, the return page and the reconcile jobs all call it, so they can't
disagree, and nothing changes from a webhook body alone.
"""

from datetime import timedelta
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import get_datetime, get_url, getdate, now_datetime
from werkzeug.utils import redirect
from werkzeug.wrappers import Response

from rarebooks_control.providers import clickpesa, paypal
from rarebooks_control.push import mark_changed
from rarebooks_control.signing import utc_now
from rarebooks_control.state import (
	clickpesa_paid_until,
	parse_utc,
	paypal_paid_until,
	paypal_start_time,
	site_state,
)

SITE = "Books Hosted Site"
CHECKOUT = "Books Checkout"
EVENT = "Books Payment Event"
GO = "rarebooks_control.billing.go"
RETURNED = "rarebooks_control.billing.returned"
COOKIE = "books_checkout"
# Browsers match a cookie path only up to a `/`, so the cookie names the return page's own path.
COOKIE_PATH = f"/api/method/{RETURNED}"
TOKEN_LIFETIME = timedelta(hours=24)
GONE = 410
BRAND_NAME = "RareBooks"
# PayPal statuses that say who pays and whether it renews; any other (APPROVAL_PENDING, APPROVED) waits.
PAYPAL_SETTLED_STATUSES = ("ACTIVE", "CANCELLED", "SUSPENDED", "EXPIRED")


def go_url(token: str) -> str:
	return f"{get_url()}/api/method/{GO}?token={token}"


def returned_url() -> str:
	return f"{get_url()}/api/method/{RETURNED}"


def new_checkout(row, plan, return_url: str, token_hash: str):
	"""A `Created` checkout with the provider's link. Its name gives the ClickPesa order reference (AC-3)."""
	checkout = frappe.new_doc(CHECKOUT)
	checkout.update(
		{
			"site": row.name,
			"plan": plan.name,
			"provider": plan.provider,
			"billing_period": plan.billing_period,
			"amount": plan.amount,
			"currency": plan.currency,
			"return_url": return_url,
			"token_hash": token_hash,
			"status": "Created",
		}
	)
	checkout.set_new_name()
	if plan.provider == "PayPal":
		paypal_plan_id = frappe.db.get_value("Books Plan", plan.name, "paypal_plan_id")
		start_time = paypal_start_time(row.trial_ends_on, row.paid_until, row.time_zone, utc_now())
		checkout.paypal_subscription_id, checkout.provider_url = paypal.create_subscription(
			paypal_plan_id, row.name, returned_url(), start_time, BRAND_NAME
		)
	else:
		checkout.order_reference = order_reference(checkout.name)
		checkout.provider_url = clickpesa.create_checkout_link(
			checkout.order_reference, whole_amount(plan.amount), plan.currency, row.owner_email
		)
	return checkout.insert(ignore_permissions=True)


def order_reference(checkout_name: str) -> str:
	"""`RB` plus the checkout number, 8 digits: CHK-00042 is RB00000042."""
	return f"RB{int(checkout_name.rsplit('-', 1)[-1]):08d}"


def whole_amount(amount) -> str:
	return f"{Decimal(str(amount)):.0f}"


@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=30, seconds=60)
def go(token: str) -> Response:
	"""Set the checkout cookie and send the payer to the provider (AC-6)."""
	from rarebooks_control.api import token_hash

	checkout = frappe.db.get_value(
		CHECKOUT,
		{
			"token_hash": token_hash(token or ""),
			"status": "Created",
			# A reuse saves the new token, so `modified` (system time, like `creation`) is when it was made.
			"modified": (">", now_datetime() - TOKEN_LIFETIME),
		},
		"provider_url",
	)
	if not checkout:
		return page(_("This payment link has expired. Start again from your books."), GONE)
	response = redirect(checkout, code=302)
	response.set_cookie(
		COOKIE,
		token,
		max_age=int(TOKEN_LIFETIME.total_seconds()),
		path=COOKIE_PATH,
		secure=True,
		httponly=True,
		samesite="Lax",
	)
	return response


def page(text: str, status: int) -> Response:
	body = (
		f"<!doctype html><meta charset=utf-8><title>RareBooks</title><p>{frappe.utils.escape_html(text)}</p>"
	)
	return Response(body, status=status, content_type="text/html; charset=utf-8")


def confirm_clickpesa(checkout_name: str, event_name: str) -> str:
	"""Ask ClickPesa about the checkout's order and apply what it says (AC-7, AC-8, AC-10, AC-17).

	Returns the event's outcome. Raises `ClickPesaError` before anything is written when ClickPesa can't
	be reached, so the caller can answer 503 and the event stays open for the next delivery.
	"""
	site = frappe.db.get_value(CHECKOUT, checkout_name, "site")
	row = frappe.get_doc(SITE, site, for_update=True)
	checkout = frappe.get_doc(CHECKOUT, checkout_name, for_update=True)
	paid = paid_items(clickpesa.payments(checkout.order_reference))
	event = frappe.get_doc(EVENT, event_name)
	event.site, event.checkout = row.name, checkout.name

	if not paid:
		return finish(event, "Ignored", row)
	first, extras = paid[0], paid[1:]
	event.amount, event.currency = (
		decimal_amount(first.get("collectedAmount")),
		first.get("collectedCurrency"),
	)

	if row.status == "Removed":
		alert_ops(
			_("Payment for a removed site {0}").format(row.name),
			_("Refund it, or restore the site and apply it by hand."),
			checkout,
			first,
		)
		return finish(event, "Unknown", row)
	if checkout.status == "Mismatch":
		return finish(event, "Ignored", row)
	if checkout.status == "Paid":
		if not extras:
			return finish(event, "Ignored", row)
		alert_ops(
			_("A second payment for order {0}").format(checkout.order_reference),
			_("The order was already paid, so no extra time was given. Refund the second payment."),
			checkout,
			extras[-1],
		)
		return finish(event, "Unknown", row)
	if not matches_checkout(first, checkout):
		checkout.status = "Mismatch"
		checkout.save(ignore_permissions=True)
		alert_ops(
			_("Payment does not match order {0}").format(checkout.order_reference),
			_("Nothing was unlocked. Settle it with a manual edit of the site."),
			checkout,
			first,
		)
		return finish(event, "Mismatch", row)

	apply_clickpesa(row, checkout)
	if extras:
		alert_ops(
			_("A second payment for order {0}").format(checkout.order_reference),
			_("Only one year was given. Refund the second payment."),
			checkout,
			extras[-1],
		)
	return finish(event, "Applied", row)


def apply_clickpesa(row, checkout):
	"""One year from the AC-8 start. A provider never moves `paid_until` earlier (AC-10)."""
	now = utc_now()
	checkout.status = "Paid"
	checkout.paid_on = checkout.paid_on or now
	checkout.save(ignore_permissions=True)

	before = site_state(row)
	paid_until = clickpesa_paid_until(row.paid_until, checkout.paid_on, row.trial_ends_on, row.time_zone)
	if row.paid_until and getdate(row.paid_until) > paid_until:
		paid_until = getdate(row.paid_until)
	row.update(
		{
			"paid_until": paid_until,
			"provider": "ClickPesa",
			"billing_period": "Yearly",
			"auto_renews": 0,
			"provider_reference": checkout.order_reference,
			"last_payment_on": checkout.paid_on,
		}
	)
	if site_state(row) != before:
		mark_changed(row)
	row.save(ignore_permissions=True)


def paid_items(items: list[dict]) -> list[dict]:
	"""ClickPesa's paid items for an order, oldest first: the first one is the payment that counts."""
	paid = [item for item in items if item.get("status") in clickpesa.PAID_STATUSES]
	return sorted(paid, key=lambda item: str(item.get("createdAt") or ""))


def matches_checkout(item: dict, checkout) -> bool:
	"""Same currency, and at least the checkout's amount, compared as Decimal (AC-7)."""
	amount = decimal_amount(item.get("collectedAmount"))
	return (
		item.get("collectedCurrency") == checkout.currency
		and amount is not None
		and amount >= Decimal(str(checkout.amount))
	)


def finish(event, outcome: str, row=None) -> str:
	event.outcome = outcome
	if row is not None:
		event.paid_until_after = row.paid_until
	event.save(ignore_permissions=True)
	return outcome


def alert_ops(subject: str, message: str, checkout=None, item: dict | None = None):
	"""Email `books_ops_email` at once with the order, the amounts and the ClickPesa payer."""
	lines = [message, *_checkout_lines(checkout)]
	if item:
		customer = item.get("customer") or {}
		lines.append(
			_("Paid {0} {1}, ClickPesa id {2}, payer {3}, {4}, {5}.").format(
				item.get("collectedAmount"),
				item.get("collectedCurrency"),
				item.get("id"),
				customer.get("customerName"),
				customer.get("customerPhoneNumber"),
				customer.get("customerEmail"),
			)
		)
	send_ops(subject, lines)


def alert_paypal_ops(subject: str, message: str, checkout=None, subscription: dict | None = None):
	"""Email `books_ops_email` at once with the checkout, the PayPal subscription and its subscriber."""
	lines = [message, *_checkout_lines(checkout)]
	if subscription:
		subscriber = subscription.get("subscriber") or {}
		name = subscriber.get("name") or {}
		payment = (subscription.get("billing_info") or {}).get("last_payment") or {}
		amount = payment.get("amount") or {}
		lines.append(
			_(
				"PayPal subscription {0} ({1}, plan {2}, site {3}), last payment {4} {5}, payer {6} {7}, {8}."
			).format(
				subscription.get("id"),
				subscription.get("status"),
				subscription.get("plan_id"),
				subscription.get("custom_id"),
				amount.get("value"),
				amount.get("currency_code"),
				name.get("given_name"),
				name.get("surname"),
				subscriber.get("email_address"),
			)
		)
	send_ops(subject, lines)


def _checkout_lines(checkout) -> list[str]:
	if checkout is None:
		return []
	return [
		_("Order {0}, site {1}, expected {2} {3}.").format(
			checkout.order_reference or checkout.paypal_subscription_id or checkout.name,
			checkout.site,
			checkout.amount,
			checkout.currency,
		)
	]


def send_ops(subject: str, lines: list[str]):
	"""Send at once. A failed email is logged, never raised, so it can't undo the payment record it is about."""
	recipient = frappe.conf.get("books_ops_email")
	if not recipient:
		frappe.log_error(f"Billing alert with no books_ops_email set: {subject}", "\n".join(lines))
		return
	try:
		frappe.sendmail(
			recipients=[recipient],
			subject=subject,
			message="<br>".join(frappe.utils.escape_html(line) for line in lines),
			now=True,
		)
	except Exception:
		frappe.log_error(f"Billing alert could not be sent: {subject}")


def decimal_amount(value) -> Decimal | None:
	try:
		return Decimal(str(value))
	except (InvalidOperation, ValueError):
		return None


def confirm_paypal(subscription_id: str, event_name: str) -> str:
	"""Ask PayPal about the subscription and apply what it says (AC-9, AC-10, AC-11, AC-17).

	Returns the event's outcome. Raises `PayPalError` before anything is written when PayPal can't be
	reached, so the caller can answer 503 and PayPal delivers the event again.
	"""
	# A first read finds the site; the row is locked before the read that counts.
	first = paypal.get_subscription(subscription_id)
	event = frappe.get_doc(EVENT, event_name)
	if first is None:
		return finish(event, "Ignored")
	site = first.get("custom_id")
	if not isinstance(site, str) or not frappe.db.exists(SITE, site):
		alert_paypal_ops(
			_("PayPal subscription for an unknown site {0}").format(site),
			_("No hosted site has this name. Refund it, or restore the site and apply it by hand."),
			subscription=first,
		)
		return finish(event, "Unknown")

	row = frappe.get_doc(SITE, site, for_update=True)
	subscription = paypal.get_subscription(subscription_id) or first
	event.site = row.name
	checkout_name = frappe.db.get_value(CHECKOUT, {"paypal_subscription_id": subscription_id}, "name")
	checkout = frappe.get_doc(CHECKOUT, checkout_name, for_update=True) if checkout_name else None
	if checkout is None or checkout.site != row.name:
		alert_paypal_ops(
			_("PayPal subscription with no checkout for {0}").format(row.name),
			_("Nothing was changed. Check it in PayPal and settle it with a manual edit of the site."),
			checkout,
			subscription,
		)
		return finish(event, "Unknown", row)
	event.checkout = checkout.name

	status = subscription.get("status")
	payment = new_paypal_payment(subscription, row)
	if row.status == "Removed":
		if not payment and status != "ACTIVE":
			return finish(event, "Ignored", row)
		alert_paypal_ops(
			_("PayPal payment for a removed site {0}").format(row.name),
			_("Refund it and cancel the subscription, or restore the site and apply it by hand."),
			checkout,
			subscription,
		)
		return finish(event, "Unknown", row)

	plan = paypal_plan(subscription.get("plan_id"))
	if not plan:
		alert_paypal_ops(
			_("PayPal subscription on an unknown plan for {0}").format(row.name),
			_("No Books Plan has this PayPal plan id. Nothing was unlocked."),
			checkout,
			subscription,
		)
		return finish(event, "Mismatch", row)
	if status not in PAYPAL_SETTLED_STATUSES:
		return finish(event, "Ignored", row)

	is_current = row.provider == "PayPal" and row.provider_reference == subscription_id
	if not is_current and not adopt_subscription(row, subscription, payment, checkout):
		return finish(event, "Ignored", row)

	if payment and not payment_matches_plan(payment, plan):
		# Not reconsidered on the next event; the payment is settled by hand (AC-10, AC-14).
		row.last_payment_on = payment["time"]
		row.save(ignore_permissions=True)
		if checkout.status == "Created":
			checkout.status = "Mismatch"
			checkout.save(ignore_permissions=True)
		alert_paypal_ops(
			_("PayPal payment does not match the plan for {0}").format(row.name),
			_("Nothing was unlocked. Settle it with a manual edit of the site."),
			checkout,
			subscription,
		)
		event.amount, event.currency = payment["amount"], payment["currency"]
		return finish(event, "Mismatch", row)

	changed = apply_paypal(row, subscription, plan, payment, checkout)
	if payment:
		event.amount, event.currency = payment["amount"], payment["currency"]
	return finish(event, "Applied" if changed or payment else "Ignored", row)


def adopt_subscription(row, subscription: dict, payment: dict | None, checkout) -> bool:
	"""Whether a subscription that isn't the site's `provider_reference` may take its place (AC-9, AC-11).

	Only when the site has no live PayPal subscription, or this one is the older of two live ones, and only
	for an active subscription or one with a payment to apply. A second live one is cancelled at PayPal.
	"""
	current = live_subscription(row)
	if current is None:
		return subscription.get("status") == "ACTIVE" or bool(payment)
	if subscription.get("status") != "ACTIVE":
		return False
	newer, older = sorted((subscription, current), key=_created, reverse=True)
	paypal.cancel_subscription(newer["id"], "A second subscription for the same site")
	alert_paypal_ops(
		_("A second PayPal subscription for {0}").format(row.name),
		_(
			"The newer subscription {0} was cancelled and {1} was kept. Refund it by hand if it was charged."
		).format(newer["id"], older["id"]),
		checkout,
		newer,
	)
	return newer is current


def live_subscription(row) -> dict | None:
	"""The site's current PayPal subscription when PayPal says it is `ACTIVE` or `APPROVED`."""
	if row.provider != "PayPal" or not row.provider_reference:
		return None
	current = paypal.get_subscription(row.provider_reference)
	return current if current and current.get("status") in paypal.LIVE_STATUSES else None


def new_paypal_payment(subscription: dict, row) -> dict | None:
	"""The subscription's last payment, when it is newer than the latest one applied to the site (AC-9)."""
	last = (subscription.get("billing_info") or {}).get("last_payment") or {}
	moment = parse_utc(last.get("time"))
	if moment is None:
		return None
	if row.last_payment_on and moment <= get_datetime(row.last_payment_on):
		return None
	amount = last.get("amount") or {}
	return {
		"time": moment,
		"amount": decimal_amount(amount.get("value")),
		"currency": amount.get("currency_code"),
	}


def payment_matches_plan(payment: dict, plan) -> bool:
	"""The plan's amount and currency exactly, compared as Decimal (AC-9)."""
	return (
		payment["currency"] == plan.currency
		and payment["amount"] is not None
		and payment["amount"] == Decimal(str(plan.amount))
	)


def paypal_plan(plan_id):
	"""The Books Plan with this PayPal plan id, disabled plans included (AC-9)."""
	if not isinstance(plan_id, str) or not plan_id:
		return None
	name = frappe.db.get_value("Books Plan", {"provider": "PayPal", "paypal_plan_id": plan_id}, "name")
	return frappe.get_doc("Books Plan", name) if name else None


def apply_paypal(row, subscription: dict, plan, payment: dict | None, checkout) -> bool:
	"""Write what PayPal said. A provider never moves `paid_until` earlier (AC-10). True when a pushed
	field changed."""
	before = site_state(row)
	row.update(
		{
			"provider": "PayPal",
			"billing_period": plan.billing_period,
			"provider_reference": subscription["id"],
			"auto_renews": 1 if subscription.get("status") == "ACTIVE" else 0,
		}
	)
	if payment:
		next_billing = parse_utc((subscription.get("billing_info") or {}).get("next_billing_time"))
		row.paid_until = paypal_paid_until(
			row.paid_until, payment["time"], next_billing, plan.billing_period, row.time_zone
		)
		row.last_payment_on = payment["time"]
		if checkout.status != "Paid":
			checkout.status = "Paid"
			checkout.paid_on = checkout.paid_on or utc_now()
			checkout.save(ignore_permissions=True)
	changed = site_state(row) != before
	if changed:
		mark_changed(row)
	row.save(ignore_permissions=True)
	return changed


def _created(subscription: dict):
	return parse_utc(subscription.get("create_time")) or utc_now()
