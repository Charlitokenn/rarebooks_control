"""Checkouts, the `go` link and the one confirm step every provider event goes through (spec 0008).

`confirm_clickpesa` locks the site row first, then asks ClickPesa, then writes the checkout, the event and
the site in one transaction. Webhooks, the return page and the reconcile jobs all call it, so they can't
disagree, and nothing changes from a webhook body alone.
"""

from datetime import timedelta
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import get_url, getdate, now_datetime
from werkzeug.utils import redirect
from werkzeug.wrappers import Response

from rarebooks_control.providers import clickpesa
from rarebooks_control.push import mark_changed
from rarebooks_control.signing import utc_now
from rarebooks_control.state import clickpesa_paid_until, site_state

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


def go_url(token: str) -> str:
	return f"{get_url()}/api/method/{GO}?token={token}"


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
	if plan.provider != "ClickPesa":
		raise NotImplementedError("PayPal checkouts come with build plan task 9")
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
	event.amount, event.currency = _amount(first.get("collectedAmount")), first.get("collectedCurrency")

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
	amount = _amount(item.get("collectedAmount"))
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
	"""Email `books_ops_email` at once with the order, the amounts and the payer. A failed email is logged,
	never raised, so it can't undo the payment record it is about."""
	lines = [message]
	if checkout is not None:
		lines.append(
			_("Order {0}, site {1}, expected {2} {3}.").format(
				checkout.order_reference or checkout.name, checkout.site, checkout.amount, checkout.currency
			)
		)
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


def _amount(value) -> Decimal | None:
	try:
		return Decimal(str(value))
	except (InvalidOperation, ValueError):
		return None
