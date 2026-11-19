"""ClickPesa: token, hosted checkout link and payment status (spec 0008). A thin module over `requests`, so
tests patch these functions and nothing else talks to ClickPesa.

Credentials come from this site's config: `clickpesa_client_id`, `clickpesa_api_key` and
`clickpesa_checksum_key`. Never log them, a token or a checksum.
"""

import hashlib
import hmac
import json

import frappe
import requests

BASE_URL = "https://api.clickpesa.com/third-parties"
TIMEOUT_SECONDS = 10
TOKEN_CACHE_KEY = "clickpesa_token"
TOKEN_CACHE_SECONDS = 55 * 60
PAID_STATUSES = ("SUCCESS", "SETTLED")
CHECKSUM_FIELDS = ("checksum", "checksumMethod")


class ClickPesaError(Exception):
	"""ClickPesa failed, timed out, or answered with something we can't use. Never carries a secret."""


def checksum(payload: dict, key: str) -> str:
	"""ClickPesa's canonical checksum: keys sorted at every level, compact JSON, HMAC SHA256 hex, with
	`checksum` and `checksumMethod` left out (AC-7)."""
	body = {name: value for name, value in payload.items() if name not in CHECKSUM_FIELDS}
	text = json.dumps(_canonical(body), separators=(",", ":"), ensure_ascii=False)
	return hmac.new(key.encode(), text.encode(), hashlib.sha256).hexdigest()


def is_valid_checksum(payload: dict) -> bool:
	key = frappe.conf.get("clickpesa_checksum_key")
	received = payload.get("checksum")
	if not key:
		frappe.log_error("ClickPesa: clickpesa_checksum_key is not set, so every webhook is refused")
		return False
	if not isinstance(received, str) or not received:
		return False
	# Compare bytes: compare_digest raises TypeError on a str holding non ASCII characters, and the body is
	# the sender's, so a forged checksum must answer 401, not 500 (spec 0008 AC-7).
	try:
		return hmac.compare_digest(checksum(payload, key).encode(), received.encode("utf-8", "surrogatepass"))
	except UnicodeEncodeError:
		return False


def create_checkout_link(order_reference: str, amount, currency: str, customer_email: str | None) -> str:
	"""A hosted checkout link for one order. ClickPesa sends the payer back to the dashboard's return URL."""
	payload = {
		"totalPrice": str(amount),
		"orderReference": order_reference,
		"orderCurrency": currency,
	}
	if customer_email:
		payload["customerEmail"] = customer_email
	key = frappe.conf.get("clickpesa_checksum_key")
	if key:
		payload["checksum"] = checksum(payload, key)
	answer = _call("POST", "/checkout-link/generate-checkout-url", body=payload)
	link = answer.get("checkoutLink") if isinstance(answer, dict) else None
	if not isinstance(link, str) or not link.startswith("https://"):
		raise ClickPesaError("checkout link answer has no https checkoutLink")
	return link


def payments(order_reference: str) -> list[dict]:
	"""Every payment ClickPesa holds for the order. An order with no payment yet is an empty list."""
	answer = _call("GET", f"/payments/{order_reference}", not_found_ok=True)
	if answer is None:
		return []
	if isinstance(answer, dict):
		answer = [answer]
	if not isinstance(answer, list):
		raise ClickPesaError("payment status answer is not a list")
	return [item for item in answer if isinstance(item, dict)]


def token() -> str:
	"""The bearer token, cached for 55 minutes. ClickPesa's answer already starts with `Bearer `."""
	cached = frappe.cache.get_value(TOKEN_CACHE_KEY)
	if cached:
		return cached
	client_id, api_key = frappe.conf.get("clickpesa_client_id"), frappe.conf.get("clickpesa_api_key")
	if not client_id or not api_key:
		raise ClickPesaError("clickpesa_client_id or clickpesa_api_key is not set")
	try:
		response = requests.post(
			f"{BASE_URL}/generate-token",
			headers={"client-id": client_id, "api-key": api_key},
			timeout=TIMEOUT_SECONDS,
		)
		response.raise_for_status()
		value = response.json().get("token")
	except (requests.RequestException, ValueError, AttributeError):
		raise ClickPesaError("token request failed") from None
	if not isinstance(value, str) or not value:
		raise ClickPesaError("token answer has no token")
	frappe.cache.set_value(TOKEN_CACHE_KEY, value, expires_in_sec=TOKEN_CACHE_SECONDS)
	return value


def _call(method: str, path: str, body: dict | None = None, not_found_ok: bool = False):
	try:
		response = requests.request(
			method,
			f"{BASE_URL}{path}",
			json=body,
			headers={"Authorization": token()},
			timeout=TIMEOUT_SECONDS,
		)
	except requests.RequestException as error:
		raise ClickPesaError(f"{method} {path.split('/')[1]} failed: {type(error).__name__}") from None
	if response.status_code == 401:
		# A token ClickPesa no longer accepts: drop it so the next call gets a fresh one.
		frappe.cache.delete_value(TOKEN_CACHE_KEY)
	if response.status_code == 404 and not_found_ok:
		return None
	if response.status_code != 200:
		raise ClickPesaError(f"{method} {path.split('/')[1]} answered HTTP {response.status_code}")
	try:
		return response.json()
	except ValueError:
		raise ClickPesaError(f"{method} {path.split('/')[1]} answered with invalid JSON") from None


def _canonical(value):
	if isinstance(value, dict):
		return {key: _canonical(value[key]) for key in sorted(value)}
	if isinstance(value, list):
		return [_canonical(item) for item in value]
	return value
