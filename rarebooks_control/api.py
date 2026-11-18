"""Signed endpoints for hosting benches and customer sites (spec 0008 AC-1 to AC-5).

Each one reads the raw body, checks the signature and `issued_at` first, and answers through
`signing.signed_response`, never `frappe.throw` or Frappe's `{"message": ...}` wrapper.
"""

import hashlib
import secrets
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlsplit

import frappe
from frappe.utils import getdate, now_datetime, validate_email_address
from werkzeug.wrappers import Response

from rarebooks_control import billing, signing
from rarebooks_control.providers.clickpesa import ClickPesaError
from rarebooks_control.rarebooks_control.doctype.books_plan.books_plan import CURRENCY_STEP
from rarebooks_control.state import site_state

SITE = "Books Hosted Site"
CHECKOUT = "Books Checkout"
PROVIDERS = ("ClickPesa", "PayPal")
BILLING_PERIODS = ("Monthly", "Yearly")
FACTS = ("trial_ends_on", "time_zone", "language", "company_name")
FACT_LENGTH = 140
REUSE_WINDOW = timedelta(hours=24)
REGISTER_LIMIT, GET_STATE_LIMIT, CHECKOUT_LIMIT = 30, 30, 10
CONFLICT, BAD_REQUEST, TOO_MANY, PROVIDER_FAILED = 409, 400, 429, 502


@frappe.whitelist(allow_guest=True, methods=["POST"])
def register_site() -> Response:
	"""A hosting bench registers one of its sites and gets its secret back (AC-1). Safe to repeat."""
	raw, body = signing.read_body()
	bench = body.get("bench") if body else None
	secret = signing.runner_secret(bench)
	request = signing.verify_request(raw, body, [secret])
	if not request:
		return signing.unauthorized(secret)
	if signing.over_limit("register_site", bench, REGISTER_LIMIT):
		return request.answer({}, TOO_MANY)

	site, site_url, owner_email = body.get("site"), body.get("site_url"), body.get("owner_email")
	if not _is_site_name(site) or not _is_https(site_url) or not _is_email(owner_email):
		return request.answer({}, BAD_REQUEST)

	echo = {"site": site, "issued_at": request.issued_at}
	existing = frappe.db.get_value(SITE, site, ["hosting_bench", "status"], as_dict=True, for_update=True)
	if existing:
		if existing.hosting_bench != bench or existing.status == "Removed":
			return request.answer(echo, CONFLICT)
		row = frappe.get_doc(SITE, site)
		return request.answer({**echo, "secret": row.get_password("control_secret")})

	_ensure_bench(bench)
	site_secret = secrets.token_hex(32)
	frappe.get_doc(
		{
			"doctype": SITE,
			"site": site,
			"site_url": site_url.rstrip("/"),
			"hosting_bench": bench,
			"owner_email": owner_email,
			"control_secret": site_secret,
		}
	).insert(ignore_permissions=True)
	return request.answer({**echo, "secret": site_secret})


@frappe.whitelist(allow_guest=True, methods=["POST"])
def get_state() -> Response:
	"""The site's state with an echo of its `issued_at`, and the plans it is offered (AC-2)."""
	verified = _verify_site("get_state", GET_STATE_LIMIT)
	if not isinstance(verified, tuple):
		return verified
	request, row = verified
	_store_facts(row, request.body)
	state = {**site_state(row), "issued_at": request.issued_at}
	return request.answer({"state": state, "plans": offered_plans(row)})


@frappe.whitelist(allow_guest=True, methods=["POST"])
def create_checkout() -> Response:
	"""A `go` link to the provider's checkout for the site's chosen plan (AC-3, AC-5)."""
	verified = _verify_site("create_checkout", CHECKOUT_LIMIT)
	if not isinstance(verified, tuple):
		return verified
	request, row = verified
	body = request.body
	provider, period, return_url = body.get("provider"), body.get("billing_period"), body.get("return_url")
	if provider not in PROVIDERS or period not in BILLING_PERIODS or not _is_return_url(return_url, row):
		return request.answer({}, BAD_REQUEST)

	row = frappe.get_doc(SITE, row.name, for_update=True)
	_store_facts(row, body)
	plan = plan_for(row, provider, period)
	if not plan:
		return request.answer({"refusal": "plan_not_offered"}, CONFLICT)

	token = secrets.token_urlsafe(32)
	reused = _reusable_checkout(row.name, plan.name)
	if reused:
		# Overwrites the hash, so the older `go` link stops working; `modified` dates the new token.
		frappe.db.set_value(CHECKOUT, reused, "token_hash", token_hash(token))
	else:
		try:
			billing.new_checkout(row, plan, return_url, token_hash(token))
		except (ClickPesaError, NotImplementedError) as error:
			frappe.db.rollback()
			frappe.log_error("Billing: create_checkout could not reach the provider", str(error))
			return request.answer({}, PROVIDER_FAILED)
	return request.answer({"checkout_url": billing.go_url(token)})


def offered_plans(row) -> list[dict]:
	"""Every enabled plan; a test plan only for a site that allows them, and there it replaces the real one."""
	plans = {}
	for plan in _enabled_plans():
		if plan.test_only and not row.allow_test_plans:
			continue
		key = (plan.provider, plan.billing_period)
		if plan.test_only or key not in plans:
			plans[key] = plan
	return [
		{
			"provider": plan.provider,
			"billing_period": plan.billing_period,
			"amount": json_amount(plan.amount, plan.currency),
			"currency": plan.currency,
			"label": plan.label,
		}
		for plan in plans.values()
	]


def json_amount(amount, currency: str) -> int | float:
	"""The plan amount as a JSON number, from the Decimal rounded to the currency's unit: the customer site
	formats it as a number. Whole TZS stay integers; USD keeps its two decimals exactly."""
	value = Decimal(str(amount)).quantize(CURRENCY_STEP[currency], rounding=ROUND_HALF_UP)
	return int(value) if value == value.to_integral_value() else float(value)


def plan_for(row, provider: str, period: str):
	"""The plan a checkout uses: the test plan when the site allows one for that pair, else the real one."""
	matches = [
		plan
		for plan in _enabled_plans()
		if (plan.provider, plan.billing_period) == (provider, period)
		and (row.allow_test_plans or not plan.test_only)
	]
	test_plans = [plan for plan in matches if plan.test_only]
	return (test_plans or matches or [None])[0]


def token_hash(token: str) -> str:
	return hashlib.sha256(token.encode()).hexdigest()


def _enabled_plans() -> list:
	return frappe.get_all(
		"Books Plan",
		filters={"enabled": 1},
		fields=["name", "provider", "billing_period", "amount", "currency", "label", "test_only"],
		order_by="test_only asc, name asc",
	)


def _verify_site(endpoint: str, limit: int):
	"""The verified request and its row, or the 401 or 429 answer to send. Unknown and Removed sites get 401."""
	raw, body = signing.read_body()
	site = body.get("site") if body else None
	row = frappe.get_doc(SITE, site) if isinstance(site, str) and frappe.db.exists(SITE, site) else None
	if not row or row.status == "Removed":
		return signing.unauthorized()
	request = signing.verify_request(raw, body, signing.site_secrets(row))
	if not request:
		return signing.unauthorized(row.get_password("control_secret", raise_exception=False))
	if signing.over_limit(endpoint, row.name, limit):
		return request.answer({}, TOO_MANY)
	return request, row


def _store_facts(row, body: dict):
	"""The site facts of a signed call, kept for dates and emails (AC-2, AC-3). Not billing, so no Version."""
	values = {
		"trial_ends_on": _date_or_none(body.get("trial_ends_on")),
		"time_zone": _text_or_none(body.get("time_zone")),
		"language": _text_or_none(body.get("language")),
		"company_name": _text_or_none(body.get("company_name")),
	}
	stored = {
		"trial_ends_on": getdate(row.trial_ends_on) if row.trial_ends_on else None,
		**{field: row.get(field) or None for field in FACTS[1:]},
	}
	if values != stored:
		frappe.db.set_value(SITE, row.name, values, update_modified=False)
		row.update(values)


def _reusable_checkout(site: str, plan: str) -> str | None:
	"""An unpaid `Created` checkout for the same site and plan made in the last 24 hours (AC-3)."""
	return frappe.db.get_value(
		CHECKOUT,
		{
			"site": site,
			"plan": plan,
			"status": "Created",
			"creation": (">", now_datetime() - REUSE_WINDOW),
		},
		"name",
		order_by="creation desc",
	)


def _ensure_bench(bench: str):
	if not frappe.db.exists("Books Hosting Bench", bench):
		frappe.get_doc({"doctype": "Books Hosting Bench", "bench_name": bench}).insert(
			ignore_permissions=True
		)


def _is_site_name(value) -> bool:
	return isinstance(value, str) and 0 < len(value) <= FACT_LENGTH and "/" not in value


def _is_https(value) -> bool:
	return isinstance(value, str) and value.startswith("https://") and bool(urlsplit(value).hostname)


def _is_email(value) -> bool:
	return isinstance(value, str) and bool(validate_email_address(value))


def _is_return_url(value, row) -> bool:
	"""`https://` on the host of the site's own URL, so the return can't be an open redirect (AC-5)."""
	return _is_https(value) and urlsplit(value).hostname == urlsplit(row.site_url).hostname


def _date_or_none(value):
	if not isinstance(value, str) or not value:
		return None
	try:
		return getdate(value)
	except Exception:
		return None


def _text_or_none(value) -> str | None:
	return value[:FACT_LENGTH] if isinstance(value, str) and value else None
