"""Cloudflare DNS, the only place the control site talks to Cloudflare (spec 0012 AC-13).

Three functions only: `find_records`, `create_cname` and `delete_record`, on one zone. The token, the zone id
and the parent domain come from the control site's `site_config.json` (`cloudflare_api_token`,
`cloudflare_zone_id`, `books_site_domain`). The token is scoped to Zone DNS Edit on that zone, but it is still
treated like a password: no token, header or URL is ever logged or put in an error text. Tests patch the three
functions.
"""

import frappe
import requests

BASE_URL = "https://api.cloudflare.com/client/v4"
TIMEOUT_SECONDS = 30
CONF_KEYS = ("cloudflare_api_token", "cloudflare_zone_id", "books_site_domain")
TOO_MANY_REQUESTS = 429
SERVER_ERROR = 500
NOT_FOUND = 404
# Cloudflare's error codes for "a record with that name already exists" (spec 0012 AC-6).
EXISTS_CODES = frozenset({81053, 81057, 81058})


class CloudflareError(Exception):
	"""Cloudflare answered with an error, or couldn't be reached. The text names only the call, the HTTP status
	and Cloudflare's error codes, never the token or a header."""

	def __init__(self, text: str, status: int | None = None, codes: tuple[int, ...] = ()):
		super().__init__(text)
		self.status = status
		self.codes = codes

	@property
	def unauthorized(self) -> bool:
		"""The token is wrong or lacks the permission: no retry fixes it (spec 0012 AC-6)."""
		return self.status in (401, 403)

	@property
	def exists(self) -> bool:
		return bool(EXISTS_CODES.intersection(self.codes))


def is_configured() -> bool:
	"""All three keys are set (spec 0012 AC-13)."""
	return all(isinstance(frappe.conf.get(key), str) and frappe.conf.get(key).strip() for key in CONF_KEYS)


def site_domain() -> str:
	"""The parent zone name, `rarebooks.cc`, lowercased and without a trailing dot."""
	return (frappe.conf.get("books_site_domain") or "").strip().lower().strip(".")


def full_domain(label: str) -> str:
	return f"{label}.{site_domain()}"


def find_records(name: str) -> list[dict]:
	"""Every record of any type named `name` in the zone, as `{id, type, name, content, proxied}`."""
	result = _call("GET", "dns_records", params={"name": name, "per_page": 100})
	if not isinstance(result, list):
		raise CloudflareError("GET dns_records answered without a list")
	return [
		{
			"id": record.get("id"),
			"type": record.get("type"),
			"name": record.get("name"),
			"content": record.get("content"),
			"proxied": bool(record.get("proxied")),
		}
		for record in result
		if isinstance(record, dict)
	]


def create_cname(name: str, target: str, comment: str) -> str:
	"""A DNS only CNAME (`proxied` false, TTL automatic). Returns the new record's id. A name that already
	has a record raises a `CloudflareError` whose `exists` is true."""
	result = _call(
		"POST",
		"dns_records",
		json={
			"type": "CNAME",
			"name": name,
			"content": target,
			"proxied": False,
			"ttl": 1,
			"comment": comment,
		},
	)
	record_id = result.get("id") if isinstance(result, dict) else None
	if not record_id:
		raise CloudflareError("POST dns_records answered without a record id")
	return record_id


def delete_record(record_id: str):
	"""Delete one record by id. A record that is already gone counts as deleted (spec 0012 AC-11)."""
	try:
		_call("DELETE", f"dns_records/{record_id}")
	except CloudflareError as error:
		if error.status != NOT_FOUND:
			raise


def is_retryable(error: CloudflareError) -> bool:
	"""No answer, a 429 or a 5xx: wait for the next run (spec 0012 AC-5)."""
	return error.status is None or error.status == TOO_MANY_REQUESTS or error.status >= SERVER_ERROR


def _call(method: str, path: str, **kwargs):
	label = f"{method} {path.split('/', 1)[0]}"
	token, zone = _credentials()
	try:
		response = requests.request(
			method,
			f"{BASE_URL}/zones/{zone}/{path}",
			headers={"Authorization": f"Bearer {token}"},
			timeout=TIMEOUT_SECONDS,
			allow_redirects=False,
			**kwargs,
		)
	except requests.RequestException as error:
		raise CloudflareError(f"{label} failed: {type(error).__name__}") from None
	try:
		data = response.json()
	except ValueError:
		data = None
	codes = _error_codes(data)
	# An answer counts only when it is HTTP 200 with `success` true (spec 0012 AC-6).
	if response.status_code != 200 or not isinstance(data, dict) or data.get("success") is not True:
		raise CloudflareError(
			f"{label} answered HTTP {response.status_code} {list(codes)}", response.status_code, codes
		)
	return data.get("result")


def _error_codes(data) -> tuple[int, ...]:
	errors = data.get("errors") if isinstance(data, dict) else None
	if not isinstance(errors, list):
		return ()
	return tuple(
		error["code"] for error in errors if isinstance(error, dict) and isinstance(error.get("code"), int)
	)


def _credentials() -> tuple[str, str]:
	if not is_configured():
		raise CloudflareError("Cloudflare access is not set in site_config.json")
	return frappe.conf.get("cloudflare_api_token").strip(), frappe.conf.get("cloudflare_zone_id").strip()
