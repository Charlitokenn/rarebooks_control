"""Self serve onboarding through a warm pool of Books sites (spec 0014 AC-8 to AC-14, AC-21).

The marketing site (the `rarebooks-site` Worker) is the only caller of the four guest endpoints here. It signs
every call with Books Onboarding Settings `marketing_secret`, exactly as hosted sites sign theirs (spec 0005,
spec 0008 AC-5), and every answer, a 401 included, goes back through `signed_response`.

A sign up becomes a Books Signup in status Queued. The claim job takes the oldest Ready pool site under a row
lock with SKIP LOCKED, names its owner through the Books site's `claim_site` (the Owner step), and leaves the
rest to spec 0012's five domain steps. The Primary step's transaction sets the request Ready (`mark_ready`),
and a signed notice tells the Worker, which emails the visitor. A request that fails (AC-17) alerts you once and
its claimed site is discarded (the sweep opens the Discard case again when it was refused or later Cancelled);
only you can send it round again (`retry_signup`, AC-18). No network call is made with a row locked
(spec 0007 lock hardening): each step reads, commits, calls, then locks again and checks nothing changed.
"""

import json
import re
from datetime import timedelta
from zoneinfo import available_timezones

import frappe
import requests
from frappe import _
from frappe.utils import validate_email_address
from werkzeug.wrappers import Response

from rarebooks_control import pool, registry, removal, removal_mail
from rarebooks_control.destinations import post
from rarebooks_control.providers import cloudflare
from rarebooks_control.registry import SubdomainCheckUnavailable
from rarebooks_control.signing import (
	SIGNATURE_HEADER,
	SignedRequest,
	dumps,
	is_valid_signature,
	issued_now,
	over_limit,
	read_body,
	sign,
	signed_response,
	unauthorized,
	utc_now,
	verify_request,
)
from rarebooks_control.site_calls import SiteCallFailed, call_site

SIGNUP = "Books Signup"
SETTINGS = "Books Onboarding Settings"
SITE = "Books Hosted Site"
CASE = "Books Removal Case"
# A refused discard of a Failed request is logged at most once a day (AC-17).
DAY_SECONDS = 24 * 60 * 60
# Calls a minute per endpoint, counted with spec 0008's cache counter (AC-8).
LIMITS = {"check_subdomain": 120, "request_site": 30, "signup_status": 600, "owner_link": 60}
CLAIM_JOB = "rarebooks_control.onboarding.claim"
NOTICE_JOB = "rarebooks_control.onboarding.send_notice"
# The sixth failed owner step, about 30 minutes of 5 minute sweeps, fails the request (AC-12).
OWNER_TRIES = 6
NOTICE_WINDOW = timedelta(hours=24)
CLAIM_WINDOW = timedelta(hours=24)
# A request still Queued this long emails you once (AC-10).
QUEUE_WAIT = timedelta(hours=1)
# A claimed request not Ready this long fails (AC-17).
READY_WAIT = timedelta(hours=2)
BILLING_MANAGER = "Billing Manager"
COMPANY_NAME_MAX = 140
VALUE_MAX = 140
DEFAULT_ZONE = "UTC"
NOTICE_TIMEOUT_SECONDS = 30
OPEN = ("Queued", "Claiming", "Preparing")
CLERK_USER_ID = re.compile(r"^[A-Za-z0-9_]{1,140}$")


def email_key(email: str) -> str:
	"""One person, one key (AC-9): trimmed and lowercased; `googlemail.com` is `gmail.com`; a Gmail local part
	loses every dot and anything from the first `+`. The Worker's `emailKey` follows the same rule and cases."""
	email = email.strip().lower()
	local, _at, domain = email.rpartition("@")
	if domain == "googlemail.com":
		domain = "gmail.com"
	if domain == "gmail.com":
		local = local.split("+", 1)[0].replace(".", "")
	return f"{local}@{domain}"


# The marketing site's signed calls (AC-8)


def _marketing(endpoint: str) -> SignedRequest | Response:
	"""The verified call, or the signed refusal to send back: 429 over the endpoint's limit, 401 for a bad
	signature or a stale `issued_at` (which counts toward spec 0008 AC-22's alert)."""
	secret = marketing_secret()
	if over_limit(f"onboarding:{endpoint}", "marketing", LIMITS[endpoint]):
		return signed_response({"refusal": "slow_down"}, 429, secret)
	raw, body = read_body()
	call = verify_request(raw, body, [secret] if secret else [])
	if call is None:
		return unauthorized(secret)
	return call


def _answer(call: SignedRequest, body: dict, status: int = 200) -> Response:
	"""Every answer echoes the call's `issued_at`, so the Worker can tell it from a replayed one."""
	return call.answer({**body, "issued_at": call.issued_at}, status)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def check_subdomain() -> Response:
	"""Whether a label is free under spec 0012 AC-1 and AC-2, plus AC-21's sign up clash. Writes nothing."""
	call = _marketing("check_subdomain")
	if isinstance(call, Response):
		return call
	if not cloudflare.is_configured():
		return _answer(call, {"refusal": "try_again"}, 503)
	try:
		label = registry.check_subdomain(call.body.get("subdomain"))
	except SubdomainCheckUnavailable:
		frappe.clear_messages()
		return _answer(call, {"refusal": "try_again"}, 503)
	except frappe.ValidationError as error:
		frappe.clear_messages()
		return _answer(call, {"available": False, "reason": str(error)})
	return _answer(call, {"available": True, "subdomain": label})


@frappe.whitelist(allow_guest=True, methods=["POST"])
def request_site() -> Response:
	"""Ask for a site for a Clerk verified user (AC-9). Checks in the spec's order and writes nothing until
	every check passes; then a Books Signup in status Queued and its claim job (AC-10)."""
	call = _marketing("request_site")
	if isinstance(call, Response):
		return call
	body = call.body
	clerk_user_id = body.get("clerk_user_id")
	if not isinstance(clerk_user_id, str) or not CLERK_USER_ID.match(clerk_user_id):
		return _answer(call, {"refusal": "invalid", "reason": _("The sign in is missing.")}, 422)

	# (1) The same person again, in any status: the request as it is (idempotent).
	existing = frappe.db.get_value(SIGNUP, {"clerk_user_id": clerk_user_id}, "name")
	if existing:
		return _answer(call, _summary(existing))
	# (2) Sign ups are paused.
	if frappe.db.get_single_value(SETTINGS, "paused"):
		return _answer(call, {"refusal": "paused"}, 503)
	# (3) One site per email, ever.
	values = _signup_values(body)
	if isinstance(values, str):
		return _answer(call, {"refusal": "invalid", "reason": values}, 422)
	if frappe.db.exists(SIGNUP, {"email_key": values["email_key"]}):
		return _answer(call, {"refusal": "already_used"}, 409)
	# (4) Company and time zone.
	company = body.get("company_name")
	company = company.strip() if isinstance(company, str) else ""
	if not 0 < len(company) <= COMPANY_NAME_MAX:
		return _answer(
			call, {"refusal": "invalid", "reason": _("A company name is 1 to 140 characters.")}, 422
		)
	zone = body.get("time_zone")
	# (5) The subdomain rules and clashes.
	try:
		label = registry.check_subdomain(body.get("subdomain"))
	except SubdomainCheckUnavailable:
		frappe.clear_messages()
		return _answer(call, {"refusal": "try_again"}, 503)
	except frappe.ValidationError as error:
		frappe.clear_messages()
		return _answer(call, {"refusal": "subdomain", "reason": str(error)}, 422)

	row = frappe.get_doc(
		{
			"doctype": SIGNUP,
			**values,
			"clerk_user_id": clerk_user_id,
			"company_name": company,
			"subdomain": label,
			"time_zone": zone if isinstance(zone, str) and zone in _zones() else DEFAULT_ZONE,
			"status": "Queued",
			"attempts": 0,
			"requested_on": utc_now(),
		}
	)
	try:
		row.insert(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		# A concurrent request won the unique index: answer as step 1, 3 or 5 would now (AC-9).
		frappe.db.rollback()
		frappe.clear_messages()
		return _after_duplicate(call, clerk_user_id, values["email_key"], label)
	frappe.db.commit()
	queue_claim(row.name)
	return _answer(call, _summary(row.name))


@frappe.whitelist(allow_guest=True, methods=["POST"])
def signup_status() -> Response:
	"""The status of one person's request, found by `clerk_user_id` (and `request_id` when given) (AC-4)."""
	call = _marketing("signup_status")
	if isinstance(call, Response):
		return call
	name = _signup_of(call.body.get("clerk_user_id"), call.body.get("request_id"), require_request=False)
	if not name:
		return _answer(call, {}, 404)
	return _answer(call, {**_summary(name), "email": frappe.db.get_value(SIGNUP, name, "email")})


@frappe.whitelist(allow_guest=True, methods=["POST"])
def owner_link() -> Response:
	"""The owner's sign in link from their Books site, for a Ready request matching both ids (AC-14).

	The URL is passed through, never logged or stored. Any other case is a signed 404 with no detail.
	"""
	call = _marketing("owner_link")
	if isinstance(call, Response):
		return call
	name = _signup_of(call.body.get("clerk_user_id"), call.body.get("request_id"), require_request=True)
	row = frappe.db.get_value(SIGNUP, name, ["status", "hosted_site"], as_dict=True) if name else None
	if not row or row.status != "Ready" or not row.hosted_site:
		return _answer(call, {}, 404)
	try:
		answer = call_site(row.hosted_site, "owner_link")
	except SiteCallFailed:
		return _answer(call, {"refusal": "try_again"}, 503)
	url = answer.body.get("url")
	if answer.status != 200 or not _is_https(url):
		return _answer(call, {"refusal": "try_again"}, 503)
	return _answer(call, {"url": url})


def _signup_values(body: dict) -> dict | str:
	"""The owner's values from the call, or the reason they are refused. The Worker took them from Clerk."""
	email, first_name, last_name = body.get("email"), body.get("first_name"), body.get("last_name") or ""
	if not isinstance(email, str) or not validate_email_address(email.strip()):
		return _("The email address is missing.")
	email = email.strip()
	first_name = first_name.strip() if isinstance(first_name, str) else ""
	last_name = last_name.strip() if isinstance(last_name, str) else ""
	# A blank first name becomes the part of the email before the @ (AC-1).
	first_name = first_name or email.split("@", 1)[0]
	if len(first_name) > VALUE_MAX or len(last_name) > VALUE_MAX or len(email) > VALUE_MAX:
		return _("A name or email is too long.")
	return {"email": email, "email_key": email_key(email), "first_name": first_name, "last_name": last_name}


def _after_duplicate(call: SignedRequest, clerk_user_id: str, key: str, label: str) -> Response:
	existing = frappe.db.get_value(SIGNUP, {"clerk_user_id": clerk_user_id}, "name")
	if existing:
		return _answer(call, _summary(existing))
	if frappe.db.exists(SIGNUP, {"email_key": key}):
		return _answer(call, {"refusal": "already_used"}, 409)
	return _answer(
		call,
		{
			"refusal": "subdomain",
			"reason": _("The subdomain {0} is taken: a sign up holds it.").format(label),
		},
		422,
	)


def _signup_of(clerk_user_id, request_id, require_request: bool) -> str | None:
	"""Every read is keyed by `clerk_user_id`, and by `request_id` too when given, so one id alone is never
	enough (security model)."""
	if not isinstance(clerk_user_id, str) or not clerk_user_id:
		return None
	if require_request and not isinstance(request_id, str):
		return None
	name = frappe.db.get_value(SIGNUP, {"clerk_user_id": clerk_user_id}, "name")
	if not name or (request_id is not None and request_id != name):
		return None
	return name


def _summary(name: str) -> dict:
	"""`request_id`, `status`, `step`, and the address only once Ready (AC-4)."""
	row = frappe.db.get_value(SIGNUP, name, ["name", "status", "step", "subdomain"], as_dict=True)
	summary = {"request_id": row.name, "status": row.status, "step": row.step or None}
	if row.status == "Ready":
		summary["address"] = f"https://{cloudflare.full_domain(row.subdomain)}"
	return summary


# The claim and the Owner step (AC-10 to AC-12)


def queue_claim(signup: str):
	frappe.enqueue(
		CLAIM_JOB,
		queue="short",
		job_id=f"signup-claim:{signup}",
		deduplicate=True,
		enqueue_after_commit=True,
		signup=signup,
	)


def sweep():
	"""The 5 minute sweep: a claim for every Queued request (and one left Claiming by a crash), the Owner step
	for every request waiting on it, and the notices the Worker hasn't confirmed yet (AC-10, AC-12, AC-13)."""
	for name in frappe.get_all(
		SIGNUP, filters={"status": ("in", ("Queued", "Claiming"))}, pluck="name", order_by="requested_on"
	):
		queue_claim(name)
	for name in frappe.get_all(SIGNUP, filters={"status": "Preparing", "step": "Owner"}, pluck="name"):
		queue_claim(name)
	for name in frappe.get_all(SIGNUP, filters={"notify_status": ("is", "set")}, pluck="name"):
		_isolated(retry_notice, name, "the notice")
	since = utc_now() - READY_WAIT
	for name in frappe.get_all(
		SIGNUP, filters={"status": "Preparing", "claimed_on": ("<=", since)}, pluck="name"
	):
		_isolated(fail_if_slow, name, "the 2 hour check")
	for name in frappe.get_all(
		SIGNUP, filters={"status": "Failed", "hosted_site": ("is", "set")}, pluck="name"
	):
		_isolated(discard_missing, name, "the missing discard")
	_isolated(lambda _name: alert_queue(), "the queue", "the queue alert")


def fail_if_slow(signup: str):
	"""A request not Ready 2 hours after its claim fails (AC-17)."""
	row = frappe.get_doc(SIGNUP, signup, for_update=True)
	if row.status != "Preparing" or not row.claimed_on or utc_now() - row.claimed_on < READY_WAIT:
		return
	fail_signup(row, [_("The site wasn't Ready 2 hours after its claim at {0} UTC.").format(row.claimed_on)])


def discard_missing(signup: str):
	"""A Failed request whose site has no open case and no Removed case gets its Discard case now, under the
	current `discard_mode`: the first one was refused, or it was Cancelled later. An open case (a Failed one too)
	is left alone, so a Failed Discard waits for its own Retry. A refusal is logged at most once a day (AC-17)."""
	row = frappe.get_doc(SIGNUP, signup, for_update=True)
	site = row.hosted_site
	if row.status != "Failed" or not site or removal.open_case(site) or _site_removed(site):
		return
	_name, refusal = removal.try_discard(site, _("Its sign up {0} failed.").format(row.name))
	if not refusal:
		return
	key = f"signup-discard-refused:{row.name}:{utc_now().date().isoformat()}"
	if not frappe.cache.get_value(key):
		frappe.cache.set_value(key, 1, expires_in_sec=DAY_SECONDS)
		frappe.log_error(f"Onboarding: the site {site} of {row.name} wasn't discarded: {refusal}")


def _site_removed(site: str) -> bool:
	return bool(frappe.db.exists(CASE, {"site": site, "status": "Removed"}))


def alert_queue():
	"""Over the daily cap, one email a day while it holds; each request Queued over an hour, one email naming
	why (AC-10)."""
	queued = frappe.get_all(
		SIGNUP, filters={"status": "Queued"}, fields=["name", "requested_on", "queue_alerted_on"]
	)
	if not queued:
		return
	over_cap = _over_daily_cap()
	if over_cap and removal_mail._first_alert(f"signup-cap:{utc_now().date()}"):
		removal_mail.ops_alert(
			_("Sign ups reached the daily cap of {0} claims").format(_daily_cap()),
			[
				_("{0} sign ups wait Queued until claims in the last 24 hours fall under the cap.").format(
					len(queued)
				),
				_("Raise Max claims per day in Books Onboarding Settings to let them through sooner."),
			],
		)
	since = utc_now() - QUEUE_WAIT
	for row in queued:
		if row.queue_alerted_on or not row.requested_on or row.requested_on > since:
			continue
		frappe.db.set_value(SIGNUP, row.name, "queue_alerted_on", utc_now())
		removal_mail.ops_alert(
			_("The sign up {0} has waited over an hour for a site").format(row.name),
			[
				_("Requested {0} UTC.").format(row.requested_on),
				_queue_cause(over_cap),
				_("It is claimed as soon as a Ready pool site is there and the cap allows it."),
			],
		)


def _queue_cause(over_cap: bool) -> str:
	if over_cap:
		return _("Cause: the daily cap of {0} claims is reached.").format(_daily_cap())
	if frappe.db.get_single_value(SETTINGS, "pool_builds_paused"):
		reason = frappe.db.get_single_value(SETTINGS, "pool_pause_reason") or ""
		return _("Cause: pool builds are paused. {0}").format(reason).strip()
	return _("Cause: no Ready pool site.")


def claim(signup: str):
	"""Claim a Ready pool site for one request, then run its Owner step (AC-11, AC-12)."""
	row = frappe.get_doc(SIGNUP, signup, for_update=True)
	if row.status == "Preparing" and row.step == "Owner":
		frappe.db.commit()
		run_owner_step(signup)
		return
	if row.status not in ("Queued", "Claiming") or _over_daily_cap():
		return
	row.status = "Claiming"
	row.save(ignore_permissions=True)
	# Committed before it looks for a site, so a crash leaves a request the sweep picks up again.
	frappe.db.commit()

	row = frappe.get_doc(SIGNUP, signup, for_update=True)
	if row.status != "Claiming":
		return
	site = _take_ready_site()
	if not site:
		row.status = "Queued"
		row.save(ignore_permissions=True)
		frappe.db.commit()
		request_pool_build()
		return
	hosted = frappe.get_doc(SITE, site)
	hosted.pool_status = "Claimed"
	hosted.owner_email = row.email
	hosted.company_name = row.company_name
	hosted.subdomain = row.subdomain
	hosted.domain_status = None
	try:
		registry._save_label(hosted, row.subdomain)
	except frappe.ValidationError:
		# Only another holder of the label could stop this; check_subdomain refused them all at sign up.
		frappe.db.rollback()
		frappe.clear_messages()
		frappe.log_error(f"Onboarding: the label of {signup} couldn't be given to {site}")
		return
	row.hosted_site = site
	row.status = "Preparing"
	row.step = "Owner"
	row.attempts = 0
	row.claimed_on = utc_now()
	row.save(ignore_permissions=True)
	frappe.db.commit()
	run_owner_step(signup)


def _take_ready_site() -> str | None:
	"""The oldest Ready pool site, locked; a site another claim holds is skipped, never waited for (AC-11). A
	site being discarded (AC-16) is never claimed."""
	rows = frappe.db.sql(
		"""select site.name from `tabBooks Hosted Site` site
		where site.pool_status = 'Ready' and site.status = 'Active'
		and not exists (
			select 1 from `tabBooks Removal Case` c
			where c.site = site.name and c.status not in ('Removed', 'Cancelled')
		)
		order by site.pool_built_on, site.creation limit 1
		for update skip locked"""
	)
	return rows[0][0] if rows else None


def _over_daily_cap() -> bool:
	"""More than `max_claims_per_day` claims in the last 24 hours leave new requests Queued (AC-10)."""
	claimed = frappe.db.count(SIGNUP, {"claimed_on": (">=", utc_now() - CLAIM_WINDOW)})
	return claimed >= _daily_cap()


def _daily_cap() -> int:
	return frappe.db.get_single_value(SETTINGS, "max_claims_per_day") or 0


def request_pool_build():
	"""No Ready site for a request: a pool build starts at once (AC-11, AC-15)."""
	pool.request_build()


def run_owner_step(signup: str):
	"""Name the owner on the claimed site (AC-12). A verified `claimed` moves the request to Domain; a failure
	counts one try, and the sixth fails the request."""
	row = frappe.get_doc(SIGNUP, signup)
	if not (row.status == "Preparing" and row.step == "Owner" and row.hosted_site):
		return
	payload = {
		"owner_email": row.email,
		"first_name": row.first_name,
		"last_name": row.last_name or "",
		"company_name": row.company_name,
		"time_zone": row.time_zone,
	}
	reason = None
	try:
		answer = call_site(row.hosted_site, "claim_site", payload)
		if not (answer.status == 200 and answer.body.get("status") == "claimed"):
			reason = f"claim_site answered HTTP {answer.status}"
	except SiteCallFailed as error:
		reason = str(error)

	row = frappe.get_doc(SIGNUP, signup, for_update=True)
	if not (row.status == "Preparing" and row.step == "Owner"):
		return
	if reason is None:
		row.step = "Domain"
		row.save(ignore_permissions=True)
	else:
		row.attempts = (row.attempts or 0) + 1
		if row.attempts >= OWNER_TRIES:
			fail_signup(
				row, [_("The owner step failed {0} times. Last answer: {1}").format(OWNER_TRIES, reason)]
			)
		else:
			row.save(ignore_permissions=True)
	frappe.db.commit()


# Ready and Failed, and the notice to the Worker (AC-13, AC-17)


def mark_ready(site: str):
	"""Called only in the Primary step's transaction for a Claimed site (AC-13): the request is Ready in the
	same commit that makes its address primary."""
	name = frappe.db.get_value(SIGNUP, {"hosted_site": site}, "name", for_update=True)
	if not name:
		return
	row = frappe.get_doc(SIGNUP, name)
	if row.status != "Preparing":
		return
	row.status = "Ready"
	row.ready_on = utc_now()
	_start_notice(row, "Ready")
	row.save(ignore_permissions=True)


def fail_signup_of_site(site: str, lines: list[str]):
	"""The claimed site's domain failed (AC-17): its request fails, with this one email instead of spec 0012's."""
	name = frappe.db.get_value(SIGNUP, {"hosted_site": site}, "name", for_update=True)
	if name:
		fail_signup(frappe.get_doc(SIGNUP, name), lines)


def fail_signup(row, lines: list[str]):
	"""Failed with its reason, one ops email, the Failed notice, and a Discard case for the claimed site (AC-17,
	AC-19). The row is saved, not committed."""
	if row.status in ("Ready", "Failed"):
		return
	row.status = "Failed"
	row.failed_reason = "\n".join(lines)
	_start_notice(row, "Failed")
	if row.hosted_site:
		removal.open_discard(row.hosted_site, _("Its sign up {0} failed.").format(row.name))
	if not row.alerted_on:
		row.alerted_on = utc_now()
		removal_mail.ops_alert(
			_("The sign up {0} for {1} failed").format(row.name, row.subdomain),
			[
				*lines,
				_("Site: {0}").format(row.hosted_site or _("none claimed")),
				_("The visitor was told you are on it. Open the Books Signup to see it."),
			],
		)
	row.save(ignore_permissions=True)


def _start_notice(row, status: str):
	row.notify_status = status
	row.notify_started_on = utc_now()
	row.notify_alerted_on = None
	frappe.enqueue(
		NOTICE_JOB,
		queue="short",
		job_id=f"signup-notice:{row.name}:{status}",
		deduplicate=True,
		enqueue_after_commit=True,
		signup=row.name,
	)


def retry_notice(signup: str):
	"""From the sweep: send a pending notice again, for at most 24 hours, then email you once (AC-13)."""
	row = frappe.db.get_value(
		SIGNUP,
		signup,
		["notify_status", "notified_status", "notify_started_on", "notify_alerted_on"],
		as_dict=True,
	)
	if not row.notify_status or row.notified_status == row.notify_status:
		return
	if row.notify_started_on and utc_now() - row.notify_started_on < NOTICE_WINDOW:
		send_notice(signup)
		return
	if not row.notify_alerted_on:
		frappe.db.set_value(SIGNUP, signup, "notify_alerted_on", utc_now())
		removal_mail.ops_alert(
			_("The marketing site hasn't taken the {0} notice of sign up {1}").format(
				_(row.notify_status), signup
			),
			[
				_("It was first sent {0} UTC and retried for 24 hours.").format(row.notify_started_on),
				_("The visitor may not have had their email. Check the marketing site and its notify URL."),
			],
		)


def send_notice(signup: str) -> bool:
	"""POST the signed notice to `marketing_notify_url` (AC-6, AC-13). Only a 200 signed with the marketing
	secret, echoing this `issued_at`, marks it delivered. Sent with no row lock held."""
	row = frappe.db.get_value(
		SIGNUP, signup, ["name", "email", "first_name", "notify_status", "notified_status"], as_dict=True
	)
	if not row or not row.notify_status or row.notified_status == row.notify_status:
		return False
	settings = frappe.get_single(SETTINGS)
	url, secret = settings.marketing_notify_url, marketing_secret()
	if not _is_https(url) or not secret:
		frappe.log_error("Onboarding: set the marketing notify URL and secret in Books Onboarding Settings")
		return False
	status, issued_at = row.notify_status, issued_now()
	body = dumps(
		{
			"request_id": row.name,
			"status": status,
			"email": row.email,
			"first_name": row.first_name,
			"issued_at": issued_at,
		}
	)
	try:
		response = post(
			url,
			data=body,
			headers={"Content-Type": "application/json", SIGNATURE_HEADER: sign(body, secret)},
			timeout=NOTICE_TIMEOUT_SECONDS,
		)
	except (requests.RequestException, ValueError):
		return False
	if response.status_code != 200 or not is_valid_signature(
		response.content, response.headers.get(SIGNATURE_HEADER), secret
	):
		return False
	try:
		data = json.loads(response.content)
	except ValueError:
		data = None
	if not isinstance(data, dict) or data.get("issued_at") != issued_at:
		return False
	locked = frappe.get_doc(SIGNUP, signup, for_update=True)
	if locked.notify_status == status:
		locked.notified_status = status
		locked.save(ignore_permissions=True)
	frappe.db.commit()
	return True


# Retry (AC-18)


@frappe.whitelist(methods=["POST"])
def retry_signup(signup: str) -> dict:
	"""Send a Failed request round again with the same subdomain, once its old site is discarded and the label
	released (AC-18)."""
	frappe.only_for(BILLING_MANAGER)
	if not isinstance(signup, str) or not frappe.db.exists(SIGNUP, signup):
		frappe.throw(_("No sign up {0}.").format(signup), frappe.DoesNotExistError)
	row = frappe.get_doc(SIGNUP, signup, for_update=True)
	if row.status != "Failed":
		frappe.throw(_("Only a Failed sign up can be retried."))
	if row.hosted_site:
		refusal = _cleanup_pending(row.hosted_site)
		if refusal:
			frappe.throw(refusal)
	row.update(
		{
			"status": "Queued",
			"hosted_site": None,
			"attempts": 0,
			"step": None,
			"failed_reason": None,
			"alerted_on": None,
			"notify_status": None,
			"notified_status": None,
			"notify_started_on": None,
			"notify_alerted_on": None,
		}
	)
	row.save(ignore_permissions=True)
	queue_claim(row.name)
	return {"status": row.status}


def _cleanup_pending(site: str) -> str | None:
	"""Why the old site still holds the request back: it has an open case, or no Removed case of any kind (a
	Discard, or the drop that was already under way), or its domain isn't cleaned up, so its label isn't released
	yet (AC-18)."""
	open_name = removal.open_case(site)
	if open_name:
		case = frappe.db.get_value(CASE, open_name, ["kind", "status"], as_dict=True)
		if case.kind == removal.DISCARD and case.status == "Failed":
			return _("Discard case {0} is Failed. Retry that case first.").format(open_name)
		return _("The old site {0} isn't discarded yet. Retry once its case {1} is Removed.").format(
			site, open_name
		)
	if not _site_removed(site):
		return _("The old site {0} isn't discarded yet. Retry once its Discard case is Removed.").format(site)
	if frappe.db.get_value(SITE, site, "subdomain"):
		return _("The address of the old site {0} isn't cleaned up yet. Retry once it is.").format(site)
	return None


def marketing_secret() -> str | None:
	return frappe.get_single(SETTINGS).get_password("marketing_secret", raise_exception=False) or None


def _zones() -> set[str]:
	return available_timezones()


def _is_https(value) -> bool:
	return isinstance(value, str) and value.startswith("https://")


def _isolated(task, name: str, what: str):
	try:
		task(name)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(f"Onboarding: {what} of {name} failed")
