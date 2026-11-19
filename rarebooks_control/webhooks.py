"""Provider webhooks (spec 0008 AC-7, AC-9). A webhook only says "go and look": its body is checked, stored and
deduplicated, and then `billing.confirm_*` asks the provider for the truth."""

import frappe
from frappe import _
from werkzeug.wrappers import Response

from rarebooks_control import billing, signing
from rarebooks_control.providers import clickpesa as clickpesa_api
from rarebooks_control.providers import paypal as paypal_api

EVENT = "Books Payment Event"
UNAUTHORIZED, BAD_REQUEST, UNAVAILABLE = 401, 400, 503
# Events that name a subscription to go and read (spec 0008, Provider calls).
SUBSCRIPTION_EVENTS = (
	"BILLING.SUBSCRIPTION.ACTIVATED",
	"BILLING.SUBSCRIPTION.UPDATED",
	"BILLING.SUBSCRIPTION.CANCELLED",
	"BILLING.SUBSCRIPTION.SUSPENDED",
	"BILLING.SUBSCRIPTION.EXPIRED",
)
SALE_COMPLETED = "PAYMENT.SALE.COMPLETED"
PAYMENT_FAILED = "BILLING.SUBSCRIPTION.PAYMENT.FAILED"
REFUND_EVENTS = ("PAYMENT.SALE.REFUNDED", "PAYMENT.SALE.REVERSED")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def clickpesa() -> Response:
	if signing.over_limit("clickpesa", frappe.local.request_ip, 300):
		return answer("rate_limited", 429)
	raw, body = signing.read_body()
	if body is None or not clickpesa_api.is_valid_checksum(body):
		return answer("unauthorized", UNAUTHORIZED)
	data, event_type = body.get("data"), body.get("event")
	payment_id = data.get("id") if isinstance(data, dict) else None
	order = data.get("orderReference") if isinstance(data, dict) else None
	if not isinstance(payment_id, str) or not isinstance(event_type, str) or not isinstance(order, str):
		return answer("bad_request", BAD_REQUEST)

	event_id = f"clickpesa:{payment_id}:{event_type}"
	event = frappe.db.get_value(EVENT, {"event_id": event_id}, ["name", "outcome"], as_dict=True)
	if event and event.outcome:
		return answer("duplicate")
	if not event:
		try:
			event = billing.store_event(event_id, "ClickPesa", event_type, raw)
		except frappe.DuplicateEntryError:
			# The same delivery is being handled right now; ask ClickPesa to send it again later.
			frappe.db.rollback()
			return answer("busy", UNAVAILABLE)

	checkout = frappe.db.get_value("Books Checkout", {"order_reference": order}, "name")
	if not checkout:
		doc = frappe.get_doc(EVENT, event.name)
		billing.alert_ops(
			_("ClickPesa payment for an unknown order {0}").format(order),
			_("No checkout has this order reference. Refund it, or restore the site and apply it by hand."),
			item=data,
		)
		return answer(billing.finish(doc, "Unknown").lower())

	try:
		outcome = billing.confirm_clickpesa(checkout, event.name)
	except clickpesa_api.ClickPesaError as error:
		frappe.db.rollback()
		frappe.log_error("ClickPesa: payment status query failed", str(error))
		return answer("provider_unavailable", UNAVAILABLE)
	return answer(outcome.lower())


@frappe.whitelist(allow_guest=True, methods=["POST"])
def paypal() -> Response:
	"""PayPal's verify call decides whether the delivery is genuine; the subscription read decides what it
	means (AC-9). A 503 makes PayPal deliver it again."""
	if signing.over_limit("paypal", frappe.local.request_ip, 300):
		return answer("rate_limited", 429)
	raw, body = signing.read_body()
	if body is None:
		return answer("unauthorized", UNAUTHORIZED)
	try:
		verified = paypal_api.is_verified_webhook(frappe.request.headers, raw)
	except paypal_api.PayPalError as error:
		frappe.log_error("PayPal: webhook verify call failed", str(error))
		return answer("provider_unavailable", UNAVAILABLE)
	if not verified:
		return answer("unauthorized", UNAUTHORIZED)

	event_id, event_type, resource = body.get("id"), body.get("event_type"), body.get("resource")
	if not isinstance(event_id, str) or not isinstance(event_type, str) or not isinstance(resource, dict):
		return answer("bad_request", BAD_REQUEST)
	event = frappe.db.get_value(EVENT, {"event_id": event_id}, ["name", "outcome"], as_dict=True)
	if event and event.outcome:
		return answer("duplicate")
	if not event:
		try:
			event = billing.store_event(event_id, "PayPal", event_type, raw)
		except frappe.DuplicateEntryError:
			frappe.db.rollback()
			return answer("busy", UNAVAILABLE)
	doc = frappe.get_doc(EVENT, event.name)

	if event_type in REFUND_EVENTS:
		return answer(paypal_refund(doc, resource).lower())
	if event_type == SALE_COMPLETED:
		subscription_id = resource.get("billing_agreement_id")
	elif event_type in SUBSCRIPTION_EVENTS:
		subscription_id = resource.get("id")
	else:
		# PAYMENT.FAILED is kept for the daily digest and changes nothing; other types are noise.
		doc.site = _site_of(resource.get("custom_id"))
		return answer(billing.finish(doc, "Ignored").lower())
	if not isinstance(subscription_id, str) or not subscription_id:
		return answer(billing.finish(doc, "Ignored").lower())

	try:
		outcome = billing.confirm_paypal(subscription_id, event.name)
	except paypal_api.PayPalError as error:
		frappe.db.rollback()
		frappe.log_error("PayPal: subscription read failed", str(error))
		return answer("provider_unavailable", UNAVAILABLE)
	return answer(outcome.lower())


def paypal_refund(event, resource: dict) -> str:
	"""A refund or reversal is kept and sent to ops, and changes no state (AC-10): only a manual edit can
	move `paid_until` earlier."""
	subscription_id = resource.get("billing_agreement_id")
	checkout = None
	if isinstance(subscription_id, str) and subscription_id:
		name = frappe.db.get_value("Books Checkout", {"paypal_subscription_id": subscription_id}, "name")
		checkout = frappe.get_doc("Books Checkout", name) if name else None
	amount = resource.get("amount") or {}
	event.site = checkout.site if checkout else None
	event.checkout = checkout.name if checkout else None
	event.amount = billing.decimal_amount(amount.get("total") or amount.get("value"))
	event.currency = amount.get("currency") or amount.get("currency_code")
	billing.alert_paypal_ops(
		_("PayPal {0} for {1}").format(event.event_type, event.site or _("an unknown site")),
		_(
			"Nothing was changed. Settle it with a manual edit of the site if access should end. Sale {0}, {1} {2}."
		).format(resource.get("sale_id") or resource.get("id"), event.amount, event.currency),
		checkout,
	)
	return billing.finish(event, "Refund")


def _site_of(value) -> str | None:
	return value if isinstance(value, str) and frappe.db.exists("Books Hosted Site", value) else None


def answer(result: str, status: int = 200) -> Response:
	return signing.signed_response({"result": result}, status, None)
