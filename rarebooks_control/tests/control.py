"""Test helpers for spec 0008's signed endpoints: signed requests, ClickPesa and a customer site stub."""

import json
from contextlib import contextmanager
from datetime import UTC, timedelta
from unittest.mock import patch

import frappe
from frappe.utils import set_request

from rarebooks_control.providers import clickpesa
from rarebooks_control.signing import SIGNATURE_HEADER, is_valid_signature, sign, utc_now

CHECKSUM_KEY = "clickpesa-test-checksum-key"
CLICKPESA_LINK = "https://checkout.clickpesa.com/pay/abc"


def issued(offset: timedelta = timedelta()) -> str:
	return (utc_now().replace(tzinfo=UTC) + offset).isoformat()


def call(method, body: dict | bytes, secret: str | None = None, signature: str | None = None, **kwargs):
	"""Call a guest POST endpoint in process with a raw body, signed with `secret` unless `signature` is given."""
	raw = body if isinstance(body, bytes) else json.dumps(body).encode()
	headers = {"Content-Type": "application/json"}
	if signature or secret:
		headers[SIGNATURE_HEADER] = signature or sign(raw, secret)
	set_request(
		method="POST", path=f"/api/method/{method.__module__}.{method.__name__}", data=raw, headers=headers
	)
	frappe.local.request_ip = "203.0.113.7"
	return method(**kwargs)


def get(method, **query):
	set_request(method="GET", path=f"/api/method/{method.__module__}.{method.__name__}", query_string=query)
	frappe.local.request_ip = "203.0.113.7"
	return method(**query)


def answer(response, secret: str | None = None) -> dict:
	"""The JSON of a signed answer, after checking it is raw (no `message` wrapper) and signed with `secret`."""
	if secret is not None:
		assert is_valid_signature(response.get_data(), response.headers.get(SIGNATURE_HEADER), secret), (
			"answer is not signed with the expected secret"
		)
	data = json.loads(response.get_data())
	assert "message" not in data, "a signed answer must not be wrapped"
	return data


@contextmanager
def clickpesa_conf():
	with patch.dict(
		frappe.conf,
		{"clickpesa_checksum_key": CHECKSUM_KEY, "books_ops_email": "ops@example.com"},
	):
		yield


def reset_billing():
	"""Delete every registry row, checkout, event and removal case, and commit. Webhooks and pushes commit their own work,
	so the rows outlive a test's rollback."""
	for doctype in (
		"Books Removal Warning",
		"Books Removal Case",
		"Books Payment Event",
		"Books Checkout",
		"Books Hosted Site",
	):
		frappe.db.delete(doctype)
	frappe.db.commit()
	frappe.cache.delete_keys("rl:")


def make_plan(**values):
	doc = {
		"doctype": "Books Plan",
		"provider": "ClickPesa",
		"billing_period": "Yearly",
		"currency": "TZS",
		"amount": 120000,
		"label": "Yearly",
		"enabled": 1,
		**values,
	}
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def webhook_body(order: str, payment_id: str = "PAY1", amount="120000", event="PAYMENT RECEIVED") -> dict:
	body = {
		"event": event,
		"data": {
			"id": payment_id,
			"status": "SUCCESS",
			"orderReference": order,
			"collectedAmount": amount,
			"collectedCurrency": "TZS",
		},
	}
	body["checksum"] = clickpesa.checksum(body, CHECKSUM_KEY)
	return body


def paid_item(
	order: str,
	payment_id="PAY1",
	amount=120000,
	currency="TZS",
	status="SUCCESS",
	created="2026-11-18T10:00:00Z",
):
	return {
		"id": payment_id,
		"status": status,
		"orderReference": order,
		"collectedAmount": amount,
		"collectedCurrency": currency,
		"createdAt": created,
		"customer": {"customerName": "Asha", "customerPhoneNumber": "255700000000"},
	}


class FakeResponse:
	def __init__(self, status_code: int, body: dict):
		self.status_code = status_code
		self.content = json.dumps(body).encode()


class CustomerStub:
	"""Stands in for a customer site's `receive_state`: checks the signature and records each push."""

	def __init__(
		self, secret: str, result: str = "applied", status: int = 200, error: Exception | None = None
	):
		self.secret, self.result, self.status, self.error = secret, result, status, error
		self.pushes: list[dict] = []

	def __call__(self, url, data, headers, timeout, allow_redirects):
		assert allow_redirects is False
		assert url.endswith("/api/method/frappe_books.subscription.receive_state")
		if self.error:
			raise self.error
		if not is_valid_signature(data, headers.get(SIGNATURE_HEADER), self.secret):
			return FakeResponse(401, {})
		self.pushes.append(json.loads(data))
		return FakeResponse(self.status, {"message": {"result": self.result}})


class SiteResponse:
	"""What `destinations.post` gives back: status, raw bytes and headers."""

	def __init__(self, status_code: int, body: dict, secret: str | None):
		self.status_code = status_code
		self.content = json.dumps(body, separators=(",", ":")).encode()
		self.headers = {SIGNATURE_HEADER: sign(self.content, secret)} if secret else {}


class SiteStub:
	"""Stands in for customer sites' signed endpoints in `frappe_books.subscription` (spec 0010 AC-2).

	Each site holds one secret (`secrets[site]`), as its `books_control_secret` would. A call signed with
	another secret gets a 401 signed with the held one. `reports[site]` is the `removal_status()` answered.
	"""

	def __init__(self):
		self.secrets: dict[str, str] = {}
		self.reports: dict[str, dict] = {}
		self.calls: list[tuple[str, str, dict]] = []
		self.down: set[str] = set()
		self.answers: dict[str, tuple[int, dict]] = {}

	def hold(self, row, secret: str | None = None):
		self.secrets[row.name] = secret or row.get_password("control_secret")

	def names(self, site: str | None = None) -> list[str]:
		return [name for called, name, _body in self.calls if site in (None, called)]

	def bodies(self, name: str) -> list[dict]:
		return [body for _site, called, body in self.calls if called == name]

	def __call__(self, url, data, headers, timeout):
		import requests

		site = url.split("://", 1)[1].split("/", 1)[0]
		name = url.rsplit(".", 1)[1]
		assert url.endswith(f"/api/method/frappe_books.subscription.{name}")
		body = json.loads(data)
		if name in self.down or site in self.down:
			raise requests.ConnectionError("down")
		secret = self.secrets.get(site)
		echo = {"issued_at": body.get("issued_at")}
		if not is_valid_signature(data, headers.get(SIGNATURE_HEADER), secret) or body.get("site") != site:
			return SiteResponse(401, echo, secret)
		self.calls.append((site, name, body))
		if name in self.answers:
			status, answer = self.answers[name]
			return SiteResponse(status, {**answer, **echo}, secret)
		if name == "removal_report":
			return SiteResponse(200, {**self.reports[site], **echo}, secret)
		if name == "control_ping":
			return SiteResponse(200, {"site": site, "status": "ok", **echo}, secret)
		status = {"start_export": "queued", "mark_export_sent": "marked", "clear_removal": "cleared"}[name]
		return SiteResponse(200, {"status": status, **echo}, secret)


class FakeFrappeCloud:
	"""Stands in for `providers.frappe_cloud`: a team's sites by name, every call recorded, and an exception to
	raise per method (spec 0010 AC-13)."""

	GROUP = "books-group"

	def __init__(self):
		self.sites: dict[str, dict] = {}
		self.calls: list[tuple] = []
		self.errors: dict[str, Exception] = {}
		self.configs: dict[str, dict] = {}

	def add(self, name: str, status: str = "Active", group: str | None = GROUP):
		self.sites[name] = {"name": name, "status": status, "group": group}

	def patch(self):
		from rarebooks_control.providers import frappe_cloud

		return patch.multiple(
			frappe_cloud, get_site=self.get_site, update_config=self.update_config, archive=self.archive
		)

	def _maybe_raise(self, method: str):
		error = self.errors.get(method)
		if error:
			raise error

	def get_site(self, name):
		self.calls.append(("get", name))
		self._maybe_raise("get")
		if name not in self.sites:
			from rarebooks_control.providers.frappe_cloud import FrappeCloudError

			raise FrappeCloudError("press.api.site.get answered HTTP 404")
		return dict(self.sites[name])

	def update_config(self, name, config):
		self.calls.append(("update_config", name))
		self._maybe_raise("update_config")
		self.configs[name] = dict(config)

	def archive(self, name):
		self.calls.append(("archive", name))
		self._maybe_raise("archive")
		self.sites[name]["status"] = "Pending"

	def archives(self) -> int:
		return sum(1 for call in self.calls if call[0] == "archive")
