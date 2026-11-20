"""Registering hosted sites and rotating their secrets through the Frappe Cloud API (spec 0010 AC-10 to AC-12,
spec 0011 AC-4 to AC-8).

No one types a secret. The control site makes it, writes `books_control_url` and `books_control_secret` into
the site's config through Frappe Cloud, and trusts the write only once the site answers a signed
`control_ping` with the new secret. Until then the site's `config_status` isn't Active, and it gets no push, no
removal round and no state.

A desk action that fails at Frappe Cloud commits what it already wrote, then raises, so a secret that may
already be on the site is never lost to a rollback.
"""

import secrets
from datetime import timedelta
from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.utils import validate_email_address

from rarebooks_control import removal_mail
from rarebooks_control.providers import frappe_cloud
from rarebooks_control.providers.frappe_cloud import FrappeCloudError
from rarebooks_control.push import mark_changed
from rarebooks_control.signing import utc_now
from rarebooks_control.site_calls import SiteCallFailed, call_site

SITE = "Books Hosted Site"
SETTINGS = "Books Removal Settings"
BILLING_MANAGER = "Billing Manager"
PING_JOB = "rarebooks_control.registry.run_pings"
CONFIG_WAIT = timedelta(hours=1)
SITE_NAME_LENGTH = 140


@frappe.whitelist(methods=["POST"])
def register_site(site: str, owner_email: str, site_url: str) -> dict:
	"""Register a Frappe Cloud site of the Books bench group, and send it its config (spec 0010 AC-10)."""
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

	row = frappe.get_doc(
		{
			"doctype": SITE,
			"site": site,
			"site_url": site_url,
			"owner_email": owner_email,
			"control_secret": secrets.token_hex(32),
			"config_status": "Pending",
		}
	).insert(ignore_permissions=True)
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
			row.save(ignore_permissions=True)
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


def _isolated(task, site: str):
	"""One site at a time: a failure is logged and rolled back, and the others still run."""
	try:
		task(site)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(f"Registry: the config ping of {site} failed")


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
