"""Registering hosted sites and rotating their secrets through the Frappe Cloud API (spec 0010 AC-10 to AC-12,
spec 0011 AC-4 to AC-8).

No one types a secret. The control site makes it, writes `books_control_url` and `books_control_secret` into
the site's config through Frappe Cloud, and trusts the write only once the site answers a signed
`control_ping` with the new secret. Until then the site's `config_status` isn't Active, and it gets no push, no
removal round and no state.

A desk action that fails at Frappe Cloud commits what it already wrote, then raises, so a secret that may
already be on the site is never lost to a rollback.
"""

import re
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.utils import validate_email_address

from rarebooks_control import removal_mail
from rarebooks_control.providers import cloudflare, frappe_cloud
from rarebooks_control.providers.cloudflare import CloudflareError
from rarebooks_control.providers.frappe_cloud import FrappeCloudError, OutcomeUnknown
from rarebooks_control.push import mark_changed
from rarebooks_control.signing import utc_now
from rarebooks_control.site_calls import SiteCallFailed, call_site

SITE = "Books Hosted Site"
SETTINGS = "Books Removal Settings"
BILLING_MANAGER = "Billing Manager"
PING_JOB = "rarebooks_control.registry.run_pings"
CONFIG_WAIT = timedelta(hours=1)
SITE_NAME_LENGTH = 140
SIGNUP = "Books Signup"


@frappe.whitelist(methods=["POST"])
def register_site(site: str, owner_email: str, site_url: str, subdomain: str) -> dict:
	"""Register a Frappe Cloud site of the Books bench group, and send it its config (spec 0010 AC-10). It
	also stores the site's subdomain, after every check, so the 5 minute job gives it its address once its
	config is Active (spec 0012 AC-3)."""
	frappe.only_for(BILLING_MANAGER)
	site = (site or "").strip().lower()
	site_url = (site_url or "").strip().rstrip("/")
	owner_email = (owner_email or "").strip()
	if not site or len(site) > SITE_NAME_LENGTH or "/" in site:
		frappe.throw(_("Enter the site's Frappe Cloud name."))
	if not _is_https(site_url):
		frappe.throw(_("The site URL must start with https://"))
	if not validate_email_address(owner_email):
		frappe.throw(_("Enter the owner's email address."))
	if site == frappe.local.site:
		frappe.throw(_("The control site can't register itself."))
	if frappe.db.exists(SITE, site):
		frappe.throw(_("Site {0} is already registered.").format(site))
	bench_group = frappe.db.get_single_value(SETTINGS, "books_bench_group")
	if not bench_group:
		frappe.throw(_("Set the Books bench group in Books Removal Settings first."))
	try:
		cloud = frappe_cloud.get_site(site)
	except FrappeCloudError:
		frappe.log_error(f"Registry: Frappe Cloud couldn't be asked about {site}")
		frappe.throw(_("Frappe Cloud couldn't be asked about this site. Try again."))
	if cloud["name"] != site or cloud["group"] != bench_group:
		frappe.throw(_("Frappe Cloud shows no site {0} on the Books bench group.").format(site))
	label = check_subdomain(subdomain)

	row = frappe.get_doc(
		{
			"doctype": SITE,
			"site": site,
			"site_url": site_url,
			"owner_email": owner_email,
			"control_secret": secrets.token_hex(32),
			"config_status": "Pending",
			"subdomain": label,
		}
	)
	_save_label(row, label)
	if not send_config(row.name, row.get_password("control_secret")):
		# Spec 0011 AC-7: the row and its secret stay, Failed with no `config_sent_on`; Retry config finishes it.
		frappe.db.set_value(SITE, row.name, {"config_status": "Failed", "config_sent_on": None})
		frappe.db.commit()
		config_refused()
	frappe.db.set_value(SITE, row.name, "config_sent_on", utc_now())
	return {"site": row.name, "config_status": "Pending"}


@frappe.whitelist(methods=["POST"])
def retry_config(site: str) -> dict:
	"""Send both config keys again, without the registration checks (spec 0011 AC-4).

	During a rotation it sends `pending_secret`, restarts `rotation_started_on`, and leaves the site Active, so
	both secrets stay valid. Otherwise it sends `control_secret` and sets the site Pending until its next
	verified ping, an Active site included: this is how a control site domain change reaches every site.
	"""
	frappe.only_for(BILLING_MANAGER)
	row = _site_row(site)
	if row.pending_secret:
		frappe.db.set_value(SITE, row.name, "rotation_started_on", utc_now())
		frappe.db.commit()
		if not send_config(row.name, row.get_password("pending_secret")):
			config_refused()
		return {"config_status": row.config_status, "rotation": True}

	frappe.db.set_value(SITE, row.name, {"config_status": "Pending", "config_sent_on": None})
	frappe.db.commit()
	if not send_config(row.name, row.get_password("control_secret")):
		frappe.db.set_value(SITE, row.name, "config_status", "Failed")
		frappe.db.commit()
		config_refused()
	frappe.db.set_value(SITE, row.name, "config_sent_on", utc_now())
	return {"config_status": "Pending", "rotation": False}


@frappe.whitelist(methods=["POST"])
def rotate_secret(site: str) -> dict:
	"""Start a rotation: a new `pending_secret`, written to the site as `books_control_secret` (spec 0010
	AC-12). Both secrets are accepted until a ping signed with the new one swaps it in. Never shown or
	returned."""
	frappe.only_for(BILLING_MANAGER)
	row = _site_row(site)
	if row.pending_secret:
		frappe.throw(_("A rotation is already waiting for this site. Use Retry config to send it again."))
	if row.config_status != "Active":
		# Spec 0011 AC-6: only a verified ping proves which secret the site holds.
		frappe.throw(_("The site's config isn't Active. Use Retry config instead."))
	row.pending_secret = secrets.token_hex(32)
	row.rotation_started_on = utc_now()
	row.save(ignore_permissions=True)
	# Spec 0011 AC-8: the pending secret is kept whatever Frappe Cloud answers.
	frappe.db.commit()
	if not send_config(row.name, row.get_password("pending_secret")):
		config_refused()
	return {"pending": True}


def send_config(site: str, secret: str) -> bool:
	"""Write both keys through Frappe Cloud. False on any error or timeout, which is logged without a value."""
	url = control_public_url()
	if not url:
		frappe.log_error("Registry: books_control_public_url is missing or isn't https://")
		return False
	try:
		frappe_cloud.update_config(site, {"books_control_url": url, "books_control_secret": secret})
	except FrappeCloudError as error:
		frappe.log_error(f"Registry: Frappe Cloud didn't take the config of {site}", str(error))
		return False
	return True


def config_refused():
	frappe.throw(_("Frappe Cloud didn't take the config; use Retry config."))


def control_public_url() -> str | None:
	url = (frappe.conf.get("books_control_public_url") or "").rstrip("/")
	return url if _is_https(url) else None


# The 5 minute ping (spec 0010 AC-11, AC-12, AC-16)


def queue_pings():
	frappe.enqueue(PING_JOB, queue="short", job_id="books-config-ping", deduplicate=True)


def run_pings():
	"""Ping each Pending site with its new secret and each rotating site with its pending one. A verified
	answer makes the config Active, or swaps the secret. Also the stale run alert."""
	for name in frappe.get_all(
		SITE,
		filters={"status": "Active", "config_status": "Pending", "config_sent_on": ("is", "set")},
		pluck="name",
	):
		_isolated(ping_pending, name)
	for name in frappe.get_all(
		SITE,
		filters={"status": "Active", "config_status": "Active", "pending_secret": ("is", "set")},
		pluck="name",
	):
		_isolated(ping_rotation, name)
	run_domain_steps()
	removal_mail.alert_stale_run()


def ping_pending(site: str):
	row = frappe.get_doc(SITE, site)
	if _pinged(row, row.get_password("control_secret", raise_exception=False)):
		row = frappe.get_doc(SITE, site, for_update=True)
		if row.config_status == "Pending":
			row.config_status = "Active"
			# The site may have missed pushes while it waited.
			if row.push_status != "Synced" or (row.pushed_version or 0) != (row.state_version or 0):
				mark_changed(row)
			if row.pool_status == "Building":
				# Spec 0014 AC-15: the first verified ping makes a pool build Ready for a sign up.
				from rarebooks_control import pool

				row.pool_status = "Ready"
				pool.became_ready()
			row.save(ignore_permissions=True)
		return
	if row.pool_status == "Building":
		# Spec 0014 AC-16: a build not Ready within its hour is discarded by the pool sweep, with its own count.
		return
	if row.config_sent_on and utc_now() - row.config_sent_on >= CONFIG_WAIT:
		frappe.db.set_value(SITE, site, "config_status", "Failed")
		removal_mail.ops_alert(
			_("Site {0} didn't take its config").format(site),
			[
				_("The site hasn't answered a ping with its new secret for an hour since {0} UTC.").format(
					row.config_sent_on
				),
				_("Open the site in Books Hosted Site and press Retry config."),
			],
		)


def ping_rotation(site: str):
	"""The first verified ping signed with `pending_secret` swaps it in, under the row lock, only while the
	pending secret is still the one that verified (spec 0010 AC-12)."""
	row = frappe.get_doc(SITE, site)
	pending = row.get_password("pending_secret", raise_exception=False)
	if _pinged(row, pending):
		row = frappe.get_doc(SITE, site, for_update=True)
		if row.pending_secret and row.get_password("pending_secret", raise_exception=False) == pending:
			row.control_secret = pending
			row.pending_secret = None
			row.rotation_started_on = None
			# A rotation is a state change: the site hears from the control site with the new secret.
			mark_changed(row)
			row.save(ignore_permissions=True)
		return
	started = row.rotation_started_on
	if (
		started
		and utc_now() - started >= CONFIG_WAIT
		and removal_mail._first_alert(f"rotation:{site}:{started}")
	):
		removal_mail.ops_alert(
			_("The secret rotation of {0} hasn't finished").format(site),
			[
				_("The site hasn't answered a ping with its new secret since {0} UTC.").format(started),
				_("Both secrets stay valid. Press Retry config on the site to send the new one again."),
			],
		)


def _pinged(row, secret: str | None) -> bool:
	if not secret:
		return False
	try:
		answer = call_site(row.name, "control_ping", secret=secret)
	except SiteCallFailed:
		return False
	return answer.status == 200 and answer.body.get("status") == "ok" and answer.body.get("site") == row.site


def _isolated(task, site: str, what: str = "the config ping"):
	"""One site at a time: a failure is logged and rolled back, and the others still run."""
	try:
		task(site)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(f"Registry: {what} of {site} failed")


def _site_row(site: str):
	if not isinstance(site, str) or not frappe.db.exists(SITE, site):
		frappe.throw(_("No hosted site {0}.").format(site), frappe.DoesNotExistError)
	row = frappe.get_doc(SITE, site, for_update=True)
	if row.status == "Removed":
		frappe.throw(_("Site {0} is removed.").format(site))
	return row


def _is_https(value) -> bool:
	try:
		return isinstance(value, str) and value.startswith("https://") and bool(urlsplit(value).hostname)
	except ValueError:
		return False


# Custom domains (spec 0012)
#
# Each site gets `<subdomain>.<books_site_domain>`. The 5 minute job moves a site one step per run: DNS, Frappe
# Cloud, Certificate, Ping, Primary. Every step reads the row `for_update` and commits before its outside call,
# then locks the row again, checks nothing changed and writes (AC-5), so no Cloudflare, Frappe Cloud or site
# call is made while the row is locked.

LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
RESERVED_LABELS = frozenset(
	{
		"www",
		"api",
		"app",
		"control",
		"billing",
		"pay",
		"mail",
		"smtp",
		"admin",
		"support",
		"help",
		"status",
		"docs",
		"demo",
		"test",
		"staging",
	}
)
DOMAIN_STEPS = ("DNS", "Frappe Cloud", "Certificate", "Ping", "Primary")
CLEANUP = "Cleanup"
WORKING = "Working"
DOMAIN_ACTIVE = "Active"
DOMAIN_FAILED = "Failed"
STEP_WAIT = timedelta(hours=2)
CLEANUP_WAIT = timedelta(hours=24)
DIGEST_WAIT = timedelta(minutes=30)
CERTIFICATE_RETRY_SECONDS = 30 * 60
WAIT_LOG_SECONDS = 3 * 60 * 60


@frappe.whitelist(methods=["POST"])
def add_domain(site: str, subdomain: str) -> dict:
	"""Give a site registered before spec 0012 (demo-site, demo-us) its address (AC-4). The 5 minute job does
	the rest."""
	frappe.only_for(BILLING_MANAGER)
	row = _site_row(site)
	_check_can_add(row)
	# The row lock is released before the clash check asks Cloudflare and Frappe Cloud (AC-5).
	frappe.db.commit()
	label = check_subdomain(subdomain)
	row = _site_row(site)
	_check_can_add(row)
	row.subdomain = label
	row.domain_status = None
	_save_label(row, label)
	return {"subdomain": label}


@frappe.whitelist(methods=["POST"])
def retry_domain(site: str) -> dict:
	"""Go on from the failed step, with its 2 hour clock restarted (AC-10)."""
	frappe.only_for(BILLING_MANAGER)
	row = _site_row(site)
	if row.domain_status != DOMAIN_FAILED or row.domain_step == CLEANUP:
		frappe.throw(_("The domain of {0} hasn't failed.").format(row.name))
	row.domain_status = WORKING
	row.domain_step_started_on = utc_now()
	row.save(ignore_permissions=True)
	return {"domain_status": row.domain_status, "domain_step": row.domain_step}


def _check_can_add(row):
	if row.subdomain:
		frappe.throw(_("Site {0} already has the subdomain {1}.").format(row.name, row.subdomain))
	if _is_dropping(row.name):
		frappe.throw(_("Site {0} is being dropped.").format(row.name))


class SubdomainCheckUnavailable(frappe.ValidationError):
	"""Cloudflare or Frappe Cloud couldn't be asked whether a label is free: try again, it isn't a refusal."""


def check_subdomain(value, signup: str | None = None) -> str:
	"""The label, trimmed and lowercased, once it passes the rules (AC-1) and is free in the registry, in the
	Cloudflare zone and on Frappe Cloud (AC-2). Writes nothing; refuses with a translated message.

	A label held by a Books Signup is taken too, other than by `signup`, the request asking (spec 0014 AC-21)."""
	if not cloudflare.is_configured():
		# Not a refusal of the label: nothing can be checked yet, so a sign up hears try again (spec 0014 AC-9).
		frappe.throw(_("Set the custom domain config first."), SubdomainCheckUnavailable)
	label = value.strip().lower() if isinstance(value, str) else ""
	if not LABEL_PATTERN.match(label) or label.startswith("xn--"):
		frappe.throw(
			_(
				"A subdomain is 3 to 40 lowercase letters, digits and hyphens. It can't start or end with a hyphen or start with xn--."
			)
		)
	if label in RESERVED_LABELS:
		frappe.throw(_("The subdomain {0} is reserved.").format(label))
	if frappe.db.exists(SITE, {"subdomain": label}):
		_taken(label, _("another hosted site holds it, or held it before it was removed"))
	if frappe.db.exists(SIGNUP, {"subdomain": label, "name": ("!=", signup or "")}):
		_taken(label, _("a sign up holds it"))
	domain = cloudflare.full_domain(label)
	try:
		records = cloudflare.find_records(domain)
		holder = frappe_cloud.domain_exists(domain)
	except (CloudflareError, FrappeCloudError):
		frappe.log_error(f"Registry: couldn't ask Cloudflare or Frappe Cloud whether {domain} is free")
		frappe.throw(
			_("Cloudflare or Frappe Cloud couldn't be asked whether {0} is free. Try again.").format(domain),
			SubdomainCheckUnavailable,
		)
	if records:
		_taken(label, _("{0} already has a DNS record").format(domain))
	if holder:
		_taken(label, _("Frappe Cloud already uses {0}").format(domain))
	return label


def _taken(label: str, reason: str):
	frappe.throw(_("The subdomain {0} is taken: {1}.").format(label, reason))


def _save_label(doc, label: str):
	"""Save or insert, turning the unique index's duplicate key error into the "taken" refusal, so two
	concurrent requests for one label can't both win (AC-2)."""
	try:
		if doc.is_new():
			doc.insert(ignore_permissions=True)
		else:
			doc.save(ignore_permissions=True)
	except frappe.UniqueValidationError:
		frappe.clear_messages()
		_taken(label, _("another hosted site holds it"))


# The forward steps, from the 5 minute job (AC-5 to AC-8, AC-10)


@dataclass
class StepOutcome:
	"""What a step's outside calls found. `done` moves the site on (with `values` written); `failure` makes it
	Failed at once with an ops email; otherwise it waits for the next run, `reason` logged once."""

	done: bool = False
	values: dict = field(default_factory=dict)
	failure: list[str] | None = None
	reason: str = ""


def run_domain_steps():
	for row in frappe.get_all(
		SITE,
		filters={"status": "Active", "subdomain": ("is", "set")},
		fields=["name", "domain_status", "domain_step"],
	):
		if row.domain_status in (None, "", WORKING) and row.domain_step != CLEANUP:
			_isolated(move_domain, row.name, "the domain step")


def move_domain(site: str):
	"""At most one step for one site, under the AC-5 lock rule."""
	row = frappe.get_doc(SITE, site, for_update=True)
	if not _domain_movable(row):
		return
	if not _domain_eligible(row):
		# Only time the site could move counts toward the 2 hour rule (AC-5).
		if row.domain_status == WORKING:
			frappe.db.set_value(SITE, site, "domain_step_started_on", utc_now())
		return
	if not row.domain_status:
		row.domain_status = WORKING
		row.domain_step = DOMAIN_STEPS[0]
		row.domain_step_started_on = utc_now()
		row.save(ignore_permissions=True)
	elif utc_now() - row.domain_step_started_on >= STEP_WAIT:
		_fail_domain(
			row,
			[
				_("The step has run for 2 hours since {0} UTC without finishing.").format(
					row.domain_step_started_on
				)
			],
		)
		return
	step, started = row.domain_step, row.domain_step_started_on
	frappe.db.commit()

	outcome = _run_step(row, step)

	row = frappe.get_doc(SITE, site, for_update=True)
	if not (_domain_eligible(row) and row.domain_status == WORKING and row.domain_step == step):
		return
	if outcome.failure:
		_fail_domain(row, outcome.failure)
	elif outcome.done:
		row.update(outcome.values)
		if step == DOMAIN_STEPS[-1]:
			# The only place `site_url` changes, with `domain_status` Active in the same transaction (AC-8).
			row.site_url = f"https://{_domain(row)}"
			row.domain_status = DOMAIN_ACTIVE
			row.domain_step = None
			row.domain_step_started_on = None
			if row.pool_status == "Claimed":
				# Spec 0014 AC-13: the sign up is Ready in this same transaction.
				from rarebooks_control import onboarding

				onboarding.mark_ready(row.name)
		else:
			row.domain_step = DOMAIN_STEPS[DOMAIN_STEPS.index(step) + 1]
			row.domain_step_started_on = utc_now()
		row.save(ignore_permissions=True)
	elif removal_mail._first_alert(f"domain-wait:{site}:{step}:{started}"):
		frappe.logger("rarebooks_control.registry").info(
			f"Registry: the {step} step of {site} waits: {outcome.reason}"
		)


def _domain_movable(row) -> bool:
	return bool(row.subdomain) and row.domain_status in (None, "", WORKING) and row.domain_step != CLEANUP


def _domain_eligible(row) -> bool:
	return (
		_domain_movable(row)
		and row.status == "Active"
		and row.config_status == "Active"
		and not row.pending_secret
		and not _is_dropping(row.name)
		and cloudflare.is_configured()
		and _owner_ready(row)
	)


def _owner_ready(row) -> bool:
	"""A pool site moves only once claimed, and a claimed one only once its owner step is done (spec 0014
	AC-12). A site with a blank `pool_status` is unchanged."""
	if not row.pool_status:
		return True
	if row.pool_status != "Claimed":
		return False
	return frappe.db.get_value(SIGNUP, {"hosted_site": row.name}, "step") == "Domain"


def _fail_domain(row, lines: list[str]):
	row.domain_status = DOMAIN_FAILED
	row.save(ignore_permissions=True)
	if row.pool_status == "Claimed":
		# Spec 0014 AC-17: the sign up fails instead, with its own single email, not this one too.
		from rarebooks_control import onboarding

		onboarding.fail_signup_of_site(
			row.name, [_("The {0} step of its domain failed.").format(_(row.domain_step)), *lines]
		)
		return
	if removal_mail._first_alert(f"domain:{row.name}:{row.domain_step}:{row.domain_step_started_on}"):
		removal_mail.ops_alert(
			_("The {0} step of the domain of {1} failed").format(_(row.domain_step), row.name),
			[
				*lines,
				_("The site keeps working on {0}.").format(row.site_url),
				_("Open the site in Books Hosted Site and press Retry domain."),
			],
		)


def _run_step(row, step: str) -> StepOutcome:
	try:
		return STEP_FUNCTIONS[step](row)
	except CloudflareError as error:
		if error.unauthorized:
			return StepOutcome(
				failure=[
					_("Cloudflare refused the token ({0}). Check cloudflare_api_token.").format(error.status)
				]
			)
		return StepOutcome(reason=str(error))
	except (FrappeCloudError, SiteCallFailed) as error:
		return StepOutcome(reason=str(error))


def _dns_step(row) -> StepOutcome:
	"""Adopt our DNS only CNAME to `site`, or create it; never touch any other record (AC-6)."""
	domain = _domain(row)
	outcome = _check_records(row, cloudflare.find_records(domain))
	if outcome:
		return outcome
	try:
		record_id = cloudflare.create_cname(domain, row.site, f"rarebooks {row.site}")
	except CloudflareError as error:
		if not error.exists:
			raise
		return _check_records(row, cloudflare.find_records(domain)) or StepOutcome(reason=str(error))
	return StepOutcome(done=True, values={"dns_record_id": record_id})


def _check_records(row, records: list[dict]) -> StepOutcome | None:
	domain = _domain(row)
	ours = [record for record in records if _is_our_cname(record, row.site)]
	if len(ours) != len(records):
		return StepOutcome(
			failure=[
				_("{0} has a DNS record that isn't the CNAME to {1}. Remove it first.").format(
					domain, row.site
				)
			]
		)
	if any(record["proxied"] for record in ours):
		return StepOutcome(
			failure=[_("The CNAME of {0} is proxied on Cloudflare. Switch it to DNS only.").format(domain)]
		)
	if ours:
		return StepOutcome(done=True, values={"dns_record_id": ours[0]["id"]})
	return None


def _frappe_cloud_step(row) -> StepOutcome:
	"""Add the domain once Frappe Cloud sees the DNS record (AC-7)."""
	domain = _domain(row)
	if _cloud_domain(row):
		return StepOutcome(done=True)
	if not frappe_cloud.check_dns(row.site, domain):
		return StepOutcome(reason="Frappe Cloud doesn't see the DNS record yet")
	_check_bench_group(row.site)
	try:
		frappe_cloud.add_domain(row.site, domain)
	except OutcomeUnknown:
		raise
	except FrappeCloudError:
		holder = frappe_cloud.domain_exists(domain)
		if holder and holder != row.site:
			return StepOutcome(
				failure=[_("Frappe Cloud says {0} is already used by another site.").format(domain)]
			)
		raise
	if _cloud_domain(row):
		return StepOutcome(done=True)
	return StepOutcome(reason="Frappe Cloud hasn't listed the domain yet")


def _certificate_step(row) -> StepOutcome:
	"""Wait for the certificate; a Broken one is retried at most every 30 minutes (AC-7)."""
	listed = _cloud_domain(row)
	status = listed["status"] if listed else None
	if status == "Active":
		return StepOutcome(done=True)
	if status == "Broken" and frappe.cache.set(
		frappe.cache.make_key(f"domain-retry:{row.name}"), 1, ex=CERTIFICATE_RETRY_SECONDS, nx=True
	):
		_check_bench_group(row.site)
		frappe_cloud.retry_add_domain(row.site, _domain(row))
	return StepOutcome(reason=f"the domain is {status or 'not listed'} on Frappe Cloud")


def _ping_step(row) -> StepOutcome:
	"""A verified `control_ping` at the new address, echoing the row's `site` (AC-8)."""
	answer = call_site(row.name, "control_ping", url=f"https://{_domain(row)}")
	if answer.status == 200 and answer.body.get("status") == "ok" and answer.body.get("site") == row.site:
		return StepOutcome(done=True)
	return StepOutcome(reason=f"control_ping at the new address answered HTTP {answer.status}")


def _primary_step(row) -> StepOutcome:
	"""Make the domain primary on Frappe Cloud; the switch of `site_url` follows under the row lock (AC-8).
	`set_host_name` is safe to repeat, so an unknown outcome just tries again next run."""
	listed = _cloud_domain(row)
	if listed and listed["primary"]:
		return StepOutcome(done=True)
	_check_bench_group(row.site)
	frappe_cloud.set_host_name(row.site, _domain(row))
	listed = _cloud_domain(row)
	if listed and listed["primary"]:
		return StepOutcome(done=True)
	return StepOutcome(reason="Frappe Cloud doesn't show the domain as primary yet")


STEP_FUNCTIONS = {
	"DNS": _dns_step,
	"Frappe Cloud": _frappe_cloud_step,
	"Certificate": _certificate_step,
	"Ping": _ping_step,
	"Primary": _primary_step,
}


# Cleanup after archive (AC-11)


def start_domain_cleanup(site: str):
	"""Called only in `mark_removed`, in its transaction, once the site is proven archived (spec 0013 AC-5). No
	outside call; a repeat changes nothing."""
	row = frappe.db.get_value(
		SITE, site, ["subdomain", "domain_cleaned_on", "domain_step"], as_dict=True, for_update=True
	)
	if row and row.subdomain and not row.domain_cleaned_on and row.domain_step != CLEANUP:
		frappe.db.set_value(
			SITE,
			site,
			{"domain_step": CLEANUP, "domain_status": WORKING, "domain_step_started_on": utc_now()},
		)


def sweep_domain_cleanup():
	"""At the start of every hourly round, before the mode check, so it runs with removal off too."""
	if not cloudflare.is_configured():
		return
	for name in frappe.get_all(
		SITE, filters={"domain_step": CLEANUP, "domain_status": WORKING}, pluck="name"
	):
		_isolated(clean_domain, name, "the domain cleanup")


def clean_domain(site: str):
	row = frappe.get_doc(SITE, site, for_update=True)
	if row.domain_step != CLEANUP or row.domain_status != WORKING:
		return
	started = row.domain_step_started_on
	frappe.db.commit()

	try:
		dns_gone = _remove_record(row)
		cloud_gone = _remove_cloud_domain(row)
	except (CloudflareError, FrappeCloudError) as error:
		frappe.logger("rarebooks_control.registry").info(
			f"Registry: the domain cleanup of {site} waits: {error}"
		)
		dns_gone = cloud_gone = False

	row = frappe.get_doc(SITE, site, for_update=True)
	if row.domain_step != CLEANUP or row.domain_status != WORKING:
		return
	if dns_gone and cloud_gone:
		# `subdomain` stays, so the label is never handed to anyone else (AC-2), except on a discarded site:
		# its sign up keeps the label and may take it again (spec 0014 AC-19, AC-21).
		row.domain_cleaned_on = utc_now()
		row.domain_status = None
		row.dns_record_id = None
		if _was_discarded(site):
			row.subdomain = None
			row.flags.release_label = True
		row.save(ignore_permissions=True)
		return
	if (
		started
		and utc_now() - started >= CLEANUP_WAIT
		and removal_mail._first_alert(f"domain-cleanup:{site}:{started}")
	):
		removal_mail.ops_alert(
			_("The domain of {0} isn't cleaned up").format(site),
			[
				_(
					"The DNS record or the Frappe Cloud domain of {0} is still there 24 hours after its site was archived."
				).format(_domain(row)),
				_("The cleanup keeps trying every hour. Check the Error Log and Cloudflare."),
			],
		)


def _was_discarded(site: str) -> bool:
	return bool(
		frappe.db.exists("Books Removal Case", {"site": site, "kind": "Discard", "status": "Removed"})
	)


def _remove_record(row) -> bool:
	"""Delete our CNAME. A record that isn't ours is left alone and counts as gone."""
	if row.dns_record_id:
		cloudflare.delete_record(row.dns_record_id)
	for record in cloudflare.find_records(_domain(row)):
		if _is_our_cname(record, row.site):
			cloudflare.delete_record(record["id"])
	return True


def _remove_cloud_domain(row) -> bool:
	"""Spec 0013 AC-6: done once no Frappe Cloud site holds the domain, or another site does. While our site
	holds it, it is removed if listed and not primary, and counts as done at a later sweep. Every error raises,
	so the sweep waits. Frappe Cloud's own archive releases a site's domains, so no bench group check here."""
	domain = _domain(row)
	holder = frappe_cloud.domain_exists(domain)
	if not holder or not _same_name(holder, row.site):
		return True
	listed = _cloud_domain(row)
	if listed and not listed["primary"]:
		frappe_cloud.remove_domain(row.site, domain)
	return False


# Shared by the steps and cleanup


def _domain(row) -> str:
	return cloudflare.full_domain(row.subdomain)


def _cloud_domain(row) -> dict | None:
	domain = _domain(row)
	return next((listed for listed in frappe_cloud.domains(row.site) if listed["domain"] == domain), None)


def _is_our_cname(record: dict, site: str) -> bool:
	return record.get("type") == "CNAME" and _same_name(record.get("content") or "", site)


def _same_name(one: str, other: str) -> bool:
	"""Host names compared ignoring letter case and a trailing dot."""
	return one.lower().rstrip(".") == other.lower().rstrip(".")


def _check_bench_group(site: str):
	"""Frappe Cloud writes go only to a site of the Books bench group, like every other call (spec 0010)."""
	group = frappe.db.get_single_value(SETTINGS, "books_bench_group")
	cloud = frappe_cloud.get_site(site)
	if not group or cloud["name"] != site or cloud["group"] != group:
		raise FrappeCloudError(f"{site} isn't a site of the Books bench group on Frappe Cloud")


def _is_dropping(site: str) -> bool:
	from rarebooks_control.removal import is_dropping

	return is_dropping(site)


def domain_digest_lines() -> list[str]:
	"""Sites whose domain is Working for over 30 minutes in one step, or Failed (AC-14)."""
	rows = frappe.get_all(
		SITE,
		filters={"domain_status": ("in", (WORKING, DOMAIN_FAILED))},
		fields=["name", "domain_status", "domain_step", "domain_step_started_on"],
		order_by="name asc",
	)
	since = utc_now() - DIGEST_WAIT
	stuck = [
		row
		for row in rows
		if row.domain_status == DOMAIN_FAILED
		or (row.domain_step_started_on and row.domain_step_started_on <= since)
	]
	lines = [_("Sites whose domain is stuck or failed: {0}").format(len(stuck))]
	lines += [f"{row.name}: {_(row.domain_status)} ({_(row.domain_step or '')})" for row in stuck]
	return lines
