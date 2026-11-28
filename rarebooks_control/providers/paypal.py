"""PayPal: token, subscriptions, webhook verification and the go live check's reads (spec 0008 AC-4, AC-9,
AC-11). A thin module over
`requests`, so tests patch these functions and nothing else talks to PayPal.

Credentials come from this site's config: `paypal_mode` (`sandbox` or `live`), `paypal_client_id`,
`paypal_client_secret` and `paypal_webhook_id`. Never log them or a token.
"""

import json
import uuid

import frappe
import requests

BASE_URLS = {"sandbox": "https://api-m.sandbox.paypal.com", "live": "https://api-m.paypal.com"}
TIMEOUT_SECONDS = 10
TOKEN_CACHE_KEY = "paypal_token"
# Refresh 60 seconds before PayPal says the token expires (spec 0008).
TOKEN_MARGIN_SECONDS = 60
LIVE_STATUSES = ("ACTIVE", "APPROVED")
# The five headers every delivery carries, passed through to the verify call as they came.
VERIFY_HEADERS = {
	"auth_algo": "PAYPAL-AUTH-ALGO",
	"cert_url": "PAYPAL-CERT-URL",
	"transmission_id": "PAYPAL-TRANSMISSION-ID",
	"transmission_sig": "PAYPAL-TRANSMISSION-SIG",
	"transmission_time": "PAYPAL-TRANSMISSION-TIME",
}


class PayPalError(Exception):
	"""PayPal failed, timed out, or answered with something we can't use. Never carries a secret."""


def base_url() -> str:
	mode = frappe.conf.get("paypal_mode") or "sandbox"
	if mode not in BASE_URLS:
		raise PayPalError("paypal_mode must be sandbox or live")
	return BASE_URLS[mode]


def create_subscription(
	plan_id: str, custom_id: str, return_url: str, start_time: str | None, brand_name: str
) -> tuple[str, str]:
	"""A new subscription on the plan, and the `approve` link the payer goes to (AC-4).

	With no `start_time`, PayPal charges at approval.
	"""
	body = {
		"plan_id": plan_id,
		"custom_id": custom_id,
		"application_context": {
			"brand_name": brand_name,
			"shipping_preference": "NO_SHIPPING",
			"user_action": "SUBSCRIBE_NOW",
			"return_url": return_url,
			"cancel_url": return_url,
		},
	}
	if start_time:
		body["start_time"] = start_time
	answer = _call("POST", "/v1/billing/subscriptions", body=body, expect=(200, 201))
	subscription_id = answer.get("id") if isinstance(answer, dict) else None
	approve = next(
		(
			link.get("href")
			for link in (answer.get("links") or [] if isinstance(answer, dict) else [])
			if isinstance(link, dict) and link.get("rel") == "approve"
		),
		None,
	)
	if not isinstance(subscription_id, str) or not subscription_id:
		raise PayPalError("create subscription answer has no id")
	if not isinstance(approve, str) or not approve.startswith("https://"):
		raise PayPalError("create subscription answer has no https approve link")
	return subscription_id, approve


def get_subscription(subscription_id: str) -> dict | None:
	"""The subscription as PayPal holds it now, or None when PayPal doesn't know the id."""
	answer = _call("GET", f"/v1/billing/subscriptions/{_path_id(subscription_id)}", not_found_ok=True)
	if answer is None:
		return None
	if not isinstance(answer, dict) or answer.get("id") != subscription_id:
		raise PayPalError("subscription answer is not the subscription asked for")
	return answer


def cancel_subscription(subscription_id: str, reason: str):
	"""Cancel at PayPal (AC-11). A subscription that is already cancelled answers 422, which is fine."""
	_call(
		"POST",
		f"/v1/billing/subscriptions/{_path_id(subscription_id)}/cancel",
		body={"reason": reason[:128]},
		expect=(204, 422),
	)


def get_plan(plan_id: str) -> dict | None:
	"""The billing plan as PayPal holds it, or None when PayPal doesn't know the id (the go live check)."""
	answer = _call("GET", f"/v1/billing/plans/{_path_id(plan_id, 'plan')}", not_found_ok=True)
	if answer is not None and (not isinstance(answer, dict) or answer.get("id") != plan_id):
		raise PayPalError("plan answer is not the plan asked for")
	return answer


def get_webhook(webhook_id: str) -> dict | None:
	"""The webhook as PayPal holds it, or None when PayPal doesn't know the id (the go live check)."""
	answer = _call("GET", f"/v1/notifications/webhooks/{_path_id(webhook_id, 'webhook')}", not_found_ok=True)
	if answer is not None and (not isinstance(answer, dict) or answer.get("id") != webhook_id):
		raise PayPalError("webhook answer is not the webhook asked for")
	return answer


def is_verified_webhook(headers, raw: bytes) -> bool:
	"""Ask PayPal whether this delivery is genuine (AC-9). Raises `PayPalError` when PayPal can't answer.

	The event goes into the verify body as the exact bytes received: parsing and serializing it again
	would change the bytes PayPal signed.
	"""
	webhook_id = frappe.conf.get("paypal_webhook_id")
	if not webhook_id:
		frappe.log_error("PayPal: paypal_webhook_id is not set, so every webhook is refused")
		return False
	values = {name: headers.get(header) for name, header in VERIFY_HEADERS.items()}
	if not all(isinstance(value, str) and value for value in values.values()):
		return False
	values["webhook_id"] = webhook_id
	prefix = json.dumps(values, separators=(",", ":"))[:-1]
	body = f'{prefix},"webhook_event":'.encode() + raw + b"}"
	answer = _call("POST", "/v1/notifications/verify-webhook-signature", data=body)
	return isinstance(answer, dict) and answer.get("verification_status") == "SUCCESS"


def token() -> str:
	"""The OAuth access token, cached until 60 seconds before it expires."""
	client_id, secret = frappe.conf.get("paypal_client_id"), frappe.conf.get("paypal_client_secret")
	if not client_id or not secret:
		raise PayPalError("paypal_client_id or paypal_client_secret is not set")
	cached = frappe.cache.get_value(token_cache_key())
	if cached:
		return cached
	try:
		response = requests.post(
			f"{base_url()}/v1/oauth2/token",
			data={"grant_type": "client_credentials"},
			auth=(client_id, secret),
			timeout=TIMEOUT_SECONDS,
		)
		response.raise_for_status()
		answer = response.json()
		value, expires_in = answer.get("access_token"), int(answer.get("expires_in") or 0)
	except (requests.RequestException, ValueError, AttributeError, TypeError):
		raise PayPalError("token request failed") from None
	if not isinstance(value, str) or not value:
		raise PayPalError("token answer has no access_token")
	if expires_in > TOKEN_MARGIN_SECONDS:
		frappe.cache.set_value(token_cache_key(), value, expires_in_sec=expires_in - TOKEN_MARGIN_SECONDS)
	return value


def token_cache_key() -> str:
	"""One cached token per mode and app, so switching `paypal_mode` or the app's credentials to live never
	sends a sandbox token to PayPal live. The client id is public; the secret is never part of a key."""
	return f"{TOKEN_CACHE_KEY}:{base_url()}:{frappe.conf.get('paypal_client_id')}"


def _call(
	method: str,
	path: str,
	body: dict | None = None,
	data: bytes | None = None,
	expect: tuple[int, ...] = (200,),
	not_found_ok: bool = False,
):
	headers = {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}
	if method == "POST":
		# A fresh id per call: PayPal treats a repeated id as a retry of the same request.
		headers["PayPal-Request-Id"] = str(uuid.uuid4())
	name = path.split("/")[3] if path.count("/") >= 3 else path
	try:
		response = requests.request(
			method,
			f"{base_url()}{path}",
			json=body,
			data=data,
			headers=headers,
			timeout=TIMEOUT_SECONDS,
		)
	except requests.RequestException as error:
		raise PayPalError(f"{method} {name} failed: {type(error).__name__}") from None
	if response.status_code == 401:
		# A token PayPal no longer accepts: drop it so the next call gets a fresh one.
		frappe.cache.delete_value(token_cache_key())
	if response.status_code == 404 and not_found_ok:
		return None
	if response.status_code not in expect:
		raise PayPalError(f"{method} {name} answered HTTP {response.status_code}")
	if response.status_code in (204, 422) or not response.content:
		return {}
	try:
		return response.json()
	except ValueError:
		raise PayPalError(f"{method} {name} answered with invalid JSON") from None


def _path_id(value: str, kind: str = "subscription") -> str:
	"""An id as PayPal makes them (`I-` or `P-` plus letters and digits), so it can't change the path."""
	if not isinstance(value, str) or not value.replace("-", "").isalnum():
		raise PayPalError(f"not a PayPal {kind} id")
	return value
