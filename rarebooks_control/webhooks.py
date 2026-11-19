"""Provider webhooks (spec 0008 AC-7). A webhook only says "go and look": its body is checked, stored and
deduplicated, and then `billing.confirm_*` asks the provider for the truth."""

import frappe
from frappe import _
from werkzeug.wrappers import Response

from rarebooks_control import billing, signing
from rarebooks_control.providers import clickpesa as clickpesa_api
from rarebooks_control.signing import utc_now

EVENT = "Books Payment Event"
UNAUTHORIZED, BAD_REQUEST, UNAVAILABLE = 401, 400, 503


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
			event = store_event(event_id, "ClickPesa", event_type, raw)
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


def store_event(event_id: str, provider: str, event_type: str, raw: bytes):
	"""Store the delivery with no outcome yet, and commit, so a later failure still leaves it to retry."""
	event = frappe.get_doc(
		{
			"doctype": EVENT,
			"event_id": event_id,
			"provider": provider,
			"event_type": event_type[:140],
			"received_on": utc_now(),
			"raw_payload": raw.decode(errors="replace"),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return event


def answer(result: str, status: int = 200) -> Response:
	return signing.signed_response({"result": result}, status, None)
