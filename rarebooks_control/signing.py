"""The signed contract with customer sites and hosting benches (specs 0005, 0007 and 0008).

Every body is signed as `sha256=` + hex HMAC SHA256 of its exact bytes, sent in `X-Books-Signature` both
ways. Site calls use that site's secret, runner calls use the bench's runner secret. A request's
`issued_at` must be within 5 minutes of now. Every signed answer, refusals and 401s included, goes through
`signed_response`, so Frappe adds no `message` wrapper and the bytes signed are the bytes sent (AC-5).

The rules match frappe_books' `subscription/control.py`; `tests/signing_vectors.json` holds the golden
vectors both repos must agree on.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import frappe
from frappe import _
from werkzeug.wrappers import Response

from rarebooks_control.alerts import send_ops

SIGNATURE_HEADER = "X-Books-Signature"
SIGNATURE_PREFIX = "sha256="
ISSUED_AT_WINDOW = timedelta(minutes=5)
UNAUTHORIZED = 401
# More than 20 failed checks in one hour sends one email (spec 0008 AC-22).
SIGNATURE_FAILURE_ALERT_AT = 21


def sign(body: bytes, secret: str | bytes) -> str:
	key = secret.encode() if isinstance(secret, str) else secret
	return SIGNATURE_PREFIX + hmac.new(key, body, hashlib.sha256).hexdigest()


def is_valid_signature(body: bytes, signature: str | None, secret: str | bytes | None) -> bool:
	if not secret or not isinstance(signature, str) or not signature:
		return False
	# Compare bytes: compare_digest raises TypeError on a str holding non ASCII characters, and the header
	# is the caller's, so a forged one must answer 401, not 500 (spec 0008 AC-5).
	return hmac.compare_digest(sign(body, secret).encode(), signature.encode("utf-8", "surrogatepass"))


def dumps(body: dict) -> bytes:
	"""Compact JSON, the form every signed answer is sent in."""
	return json.dumps(body, separators=(",", ":"), default=str).encode()


def signed_response(body: dict, status: int, secret: str | bytes | None) -> Response:
	"""Serialize `body` once, sign those exact bytes, and set the status.

	With no secret (an unknown site or bench) the answer goes unsigned, and the caller treats it as a failure.
	"""
	data = dumps(body)
	response = Response(data, status=status, content_type="application/json")
	if secret:
		response.headers[SIGNATURE_HEADER] = sign(data, secret)
	return response


def unauthorized(secret: str | bytes | None = None) -> Response:
	"""A 401 carries no detail (AC-5). Each one counts toward the signature failure alert (AC-22)."""
	note_signature_failure()
	return signed_response({}, UNAUTHORIZED, secret)


def note_signature_failure():
	"""Count a failed signature, checksum or verify check in this hour's counter. The email goes when the
	count reaches exactly 21, so an hour of failures sends one alert, however many more follow (AC-22)."""
	hour = utc_now().strftime("%Y%m%d%H")
	cache_key = frappe.cache.make_key(f"sigfail:{hour}")
	count = frappe.cache.incrby(cache_key, 1)
	if count == 1:
		frappe.cache.expire(cache_key, 2 * 60 * 60)
	if count == SIGNATURE_FAILURE_ALERT_AT:
		send_ops(
			_("More than 20 failed signature checks this hour"),
			[
				_(
					"Calls to the control site failed their signature or checksum check more than 20 times since {0}:00 UTC."
				).format(utc_now().strftime("%Y-%m-%d %H")),
				_(
					"Check the webhook settings at ClickPesa and PayPal, and the secrets of the hosting benches."
				),
			],
		)


def utc_now() -> datetime:
	"""Naive UTC, the form every Datetime field of this app is stored in."""
	return datetime.now(UTC).replace(tzinfo=None)


def parse_issued_at(value) -> datetime | None:
	"""Naive UTC for an ISO 8601 timestamp that carries a time zone; None for a naive or bad one."""
	if not isinstance(value, str):
		return None
	try:
		issued = datetime.fromisoformat(value)
		if issued.tzinfo is None:
			return None
		return issued.astimezone(UTC).replace(tzinfo=None)
	except (ValueError, OverflowError):
		return None


def is_fresh(value) -> bool:
	issued = parse_issued_at(value)
	return issued is not None and abs(utc_now() - issued) <= ISSUED_AT_WINDOW


def issued_now() -> str:
	"""Now as UTC with `+00:00`, the form a push carries."""
	return datetime.now(UTC).isoformat()


@dataclass
class SignedRequest:
	"""A request whose signature and `issued_at` checked out, and the secret that verified it."""

	body: dict
	secret: str
	issued_at: str

	def answer(self, body: dict, status: int = 200) -> Response:
		return signed_response(body, status, self.secret)


def read_body() -> tuple[bytes, dict | None]:
	"""The raw request bytes and their JSON object, or None when the body isn't one."""
	raw = frappe.request.get_data()
	try:
		data = json.loads(raw)
	except ValueError:
		return raw, None
	return raw, data if isinstance(data, dict) else None


def verify_request(raw: bytes, body: dict | None, secrets: list[str]) -> SignedRequest | None:
	"""The request, if one of `secrets` signed it and its `issued_at` is fresh. Tried in order (AC-15)."""
	if body is None:
		return None
	signature = frappe.get_request_header(SIGNATURE_HEADER)
	for secret in secrets:
		if secret and is_valid_signature(raw, signature, secret):
			if not is_fresh(body.get("issued_at")):
				return None
			return SignedRequest(body=body, secret=secret, issued_at=body["issued_at"])
	return None


def runner_secret(bench) -> str | None:
	"""The bench's runner secret from this site's config (spec 0007 AC-15)."""
	secrets = frappe.conf.get("books_runner_secrets") or {}
	if not isinstance(bench, str) or not isinstance(secrets, dict):
		return None
	secret = secrets.get(bench)
	return secret if isinstance(secret, str) and secret else None


def site_secrets(site_row) -> list[str]:
	"""The site's current secret, then its pending one while a rotation is open (AC-15)."""
	secrets = [site_row.get_password("control_secret", raise_exception=False)]
	if site_row.pending_secret:
		secrets.append(site_row.get_password("pending_secret", raise_exception=False))
	return [secret for secret in secrets if secret]


def over_limit(endpoint: str, key: str, limit: int) -> bool:
	"""A per site or per bench counter for this minute. Frappe's `@rate_limit` keys on IP or user, and every
	site and runner calls from the hosting server's one IP (spec 0008)."""
	minute = utc_now().strftime("%Y%m%d%H%M")
	# make_key adds this site's prefix, as every frappe.cache key has, so sites sharing a Redis never mix.
	cache_key = frappe.cache.make_key(f"rl:{endpoint}:{key}:{minute}")
	count = frappe.cache.incrby(cache_key, 1)
	if count == 1:
		frappe.cache.expire(cache_key, 120)
	return count > limit
