"""Test helpers for spec 0008's signed endpoints: signed requests, ClickPesa and a customer site stub."""

import json
from contextlib import contextmanager
from datetime import UTC, timedelta
from typing import ClassVar
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
		self.hosts: dict[str, str] = {}
		self.urls: list[str] = []

	def hold(self, row, secret: str | None = None):
		self.secrets[row.name] = secret or row.get_password("control_secret")

	def names(self, site: str | None = None) -> list[str]:
		return [name for called, name, _body in self.calls if site in (None, called)]

	def bodies(self, name: str) -> list[dict]:
		return [body for _site, called, body in self.calls if called == name]

	def __call__(self, url, data, headers, timeout):
		import requests

		host = url.split("://", 1)[1].split("/", 1)[0]
		# A custom domain answers for the site it points at (spec 0012 AC-8).
		site = self.hosts.get(host, host)
		name = url.rsplit(".", 1)[1]
		assert url.endswith(f"/api/method/frappe_books.subscription.{name}")
		body = json.loads(data)
		self.urls.append(url)
		if name in self.down or site in self.down or host in self.down:
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
		# Spec 0012: each site's domains, the domains whose DNS Frappe Cloud sees, and domains held by sites of
		# other teams.
		self.domain_rows: dict[str, list[dict]] = {}
		self.dns_seen: set[str] = set()
		self.elsewhere: dict[str, str] = {}
		self.new_domain_status = "Active"
		# Spec 0013: Frappe Cloud stores each site's own name as a domain, so `domain_exists` answers it for a live
		# site; an archived site answers 403 to the team and its own name is released.
		self.own_names: set[str] = set()
		self.archived: set[str] = set()

	def add(self, name: str, status: str = "Active", group: str | None = GROUP):
		self.sites[name] = {"name": name, "status": status, "group": group}
		self.own_names.add(name)

	def finish_archive(self, name: str):
		"""What a real archive looks like to the team about 30 seconds later (spec 0013 Context): every site call
		answers 403, and the site's own name and its domains are released. An `errors` override still wins."""
		self.archived.add(name)
		self.own_names.discard(name)
		self.domain_rows.pop(name, None)

	def patch(self):
		from rarebooks_control.providers import frappe_cloud

		return patch.multiple(
			frappe_cloud,
			get_site=self.get_site,
			update_config=self.update_config,
			archive=self.archive,
			domains=self.domains,
			domain_exists=self.domain_exists,
			check_dns=self.check_dns,
			add_domain=self.add_domain,
			retry_add_domain=self.retry_add_domain,
			set_host_name=self.set_host_name,
			remove_domain=self.remove_domain,
			new_site=self.new_site,
		)

	def new_site(self, label, domain, group, plan, apps):
		"""Spec 0014 AC-15: a build starts Pending on Frappe Cloud; a test sets it Active when it is installed."""
		name = f"{label}.{domain}"
		self.calls.append(("new", name, group, plan, tuple(apps)))
		self._maybe_raise("new")
		self.add(name, status="Pending", group=group)
		return name

	def _maybe_raise(self, method: str):
		error = self.errors.get(method)
		if error:
			raise error

	def get_site(self, name):
		self.calls.append(("get", name))
		self._maybe_raise("get")
		if name in self.archived:
			self._refuse("get", 403)
		if name not in self.sites:
			from rarebooks_control.providers.frappe_cloud import FrappeCloudError

			raise FrappeCloudError("press.api.site.get answered HTTP 404", 404)
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

	def count(self, method: str) -> int:
		return sum(1 for call in self.calls if call[0] == method)

	def _refuse(self, method: str, status: int = 417):
		from rarebooks_control.providers.frappe_cloud import FrappeCloudError

		raise FrappeCloudError(f"press.api.site.{method} answered HTTP {status}", status)

	def _site(self, method: str, name: str):
		self.calls.append((method, name))
		self._maybe_raise(method)
		if name in self.archived:
			self._refuse(method, 403)
		if name not in self.sites:
			self._refuse(method, 404)

	def domains(self, name):
		self._site("domains", name)
		return [dict(row) for row in self.domain_rows.get(name, [])]

	def domain_exists(self, domain):
		self.calls.append(("domain_exists", domain))
		self._maybe_raise("domain_exists")
		for site, rows in self.domain_rows.items():
			if any(row["domain"] == domain for row in rows):
				return site
		if domain in self.own_names:
			return domain
		return self.elsewhere.get(domain)

	def check_dns(self, name, domain):
		self._site("check_dns", name)
		return domain in self.dns_seen

	def add_domain(self, name, domain):
		self._site("add_domain", name)
		if self.domain_exists(domain):
			self._refuse("add_domain")
		# Like Frappe Cloud, nothing is added while the DNS check fails.
		if domain in self.dns_seen:
			rows = self.domain_rows.setdefault(name, [])
			rows.append({"domain": domain, "status": self.new_domain_status, "primary": False})

	def retry_add_domain(self, name, domain):
		self._site("retry_add_domain", name)

	def set_host_name(self, name, domain):
		self._site("set_host_name", name)
		rows = self.domain_rows.get(name, [])
		if not any(row["domain"] == domain and row["status"] == "Active" for row in rows):
			self._refuse("set_host_name")
		for row in rows:
			row["primary"] = row["domain"] == domain

	def remove_domain(self, name, domain):
		self._site("remove_domain", name)
		if self.sites[name]["status"] != "Active":
			self._refuse("remove_domain")
		self.domain_rows[name] = [row for row in self.domain_rows.get(name, []) if row["domain"] != domain]

	def domain_status(self, name, domain, status):
		for row in self.domain_rows.get(name, []):
			if row["domain"] == domain:
				row["status"] = status


class FakeCloudflare:
	"""Stands in for `providers.cloudflare`: one zone's records by id, every call recorded, and an exception to
	raise per function (spec 0012 AC-13)."""

	ZONE_DOMAIN = "rarebooks.cc"
	CONF: ClassVar[dict] = {
		"cloudflare_api_token": "cf-test-token-never-logged",
		"cloudflare_zone_id": "zone123",
		"books_site_domain": ZONE_DOMAIN,
	}

	def __init__(self):
		self.records: dict[str, dict] = {}
		self.calls: list[tuple] = []
		self.errors: dict[str, Exception] = {}
		self.lose_create_answer = False
		self._next = 0

	def patch(self):
		from rarebooks_control.providers import cloudflare

		return patch.multiple(
			cloudflare,
			find_records=self.find_records,
			create_cname=self.create_cname,
			delete_record=self.delete_record,
		)

	def add(self, name: str, type_: str = "CNAME", content: str = "", proxied: bool = False) -> str:
		self._next += 1
		record_id = f"rec{self._next}"
		self.records[record_id] = {
			"id": record_id,
			"type": type_,
			"name": name,
			"content": content,
			"proxied": proxied,
		}
		return record_id

	def named(self, name: str) -> list[dict]:
		return [dict(record) for record in self.records.values() if record["name"] == name]

	def _maybe_raise(self, method: str):
		error = self.errors.get(method)
		if error:
			raise error

	def find_records(self, name):
		self.calls.append(("find", name))
		self._maybe_raise("find")
		return self.named(name)

	def create_cname(self, name, target, comment):
		from rarebooks_control.providers.cloudflare import CloudflareError

		self.calls.append(("create", name, target, comment))
		self._maybe_raise("create")
		if self.named(name):
			raise CloudflareError("POST dns_records answered HTTP 400 [81053]", 400, (81053,))
		record_id = self.add(name, "CNAME", target)
		if self.lose_create_answer:
			self.lose_create_answer = False
			raise CloudflareError("POST dns_records failed: ReadTimeout")
		return record_id

	def delete_record(self, record_id):
		self.calls.append(("delete", record_id))
		self._maybe_raise("delete")
		self.records.pop(record_id, None)
