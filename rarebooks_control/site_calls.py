"""Signed calls from the control site into a customer site (spec 0010 AC-2, AC-12).

Every call is a guest POST to `{site_url}/api/method/frappe_books.subscription.<name>`, signed with the site's
secret over the raw body, with a fresh `issued_at`. The answer counts only when it is signed with the same
secret and echoes that `issued_at`; anything else raises `SiteCallFailed`, and the caller acts on nothing.
Secrets are read fresh from the row before every call. Only `destinations.post` sends, so a site URL can't
point at a private address.
"""

import json
from dataclasses import dataclass

import frappe
import requests

from rarebooks_control.destinations import post
from rarebooks_control.signing import SIGNATURE_HEADER, dumps, is_valid_signature, issued_now, sign

SITE = "Books Hosted Site"
METHOD_PATH = "frappe_books.subscription"
TIMEOUT_SECONDS = 30
UNAUTHORIZED = 401


class SiteCallFailed(Exception):
	"""No verified answer: the site couldn't be reached, or its answer wasn't signed or didn't echo. The text
	never holds a secret, a URL query or the body."""

	def __init__(self, text: str, status: int | None = None):
		super().__init__(text)
		self.status = status


@dataclass
class SiteAnswer:
	status: int
	body: dict
	secret: str


def call_site(site: str, name: str, payload: dict | None = None, secret: str | None = None) -> SiteAnswer:
	"""Call `name` on the site and return its verified answer.

	With no `secret`, the call is signed with `control_secret`; while a rotation is pending, a 401 is tried once
	more with `pending_secret`, since the site may already hold the new one (spec 0010 AC-12). A given `secret`
	(the rotation's `control_ping`) is used alone.
	"""
	row = frappe.get_doc(SITE, site)
	if secret:
		return _send(row, name, payload or {}, secret)
	current = row.get_password("control_secret", raise_exception=False)
	pending = row.get_password("pending_secret", raise_exception=False) if row.pending_secret else None
	try:
		answer = _send(row, name, payload or {}, current)
	except SiteCallFailed as error:
		# A site that already holds the new secret signs its 401 with it, so the old one can't verify it.
		if error.status == UNAUTHORIZED and pending:
			return _send(row, name, payload or {}, pending)
		raise
	if answer.status == UNAUTHORIZED and pending:
		return _send(row, name, payload or {}, pending)
	return answer


def _send(row, name: str, payload: dict, secret: str | None) -> SiteAnswer:
	if not secret:
		raise SiteCallFailed(f"{name}: the site has no secret")
	issued_at = issued_now()
	body = dumps({**payload, "site": row.site, "issued_at": issued_at})
	try:
		response = post(
			f"{row.site_url.rstrip('/')}/api/method/{METHOD_PATH}.{name}",
			data=body,
			headers={"Content-Type": "application/json", SIGNATURE_HEADER: sign(body, secret)},
			timeout=TIMEOUT_SECONDS,
		)
	except (requests.RequestException, ValueError) as error:
		raise SiteCallFailed(f"{name} failed: {type(error).__name__}") from None
	if not is_valid_signature(response.content, response.headers.get(SIGNATURE_HEADER), secret):
		raise SiteCallFailed(
			f"{name} answered HTTP {response.status_code} without a valid signature", response.status_code
		)
	try:
		data = json.loads(response.content)
	except ValueError:
		data = None
	if not isinstance(data, dict) or data.get("issued_at") != issued_at:
		raise SiteCallFailed(f"{name} answered without the echo of its issued_at")
	return SiteAnswer(status=response.status_code, body=data, secret=secret)
