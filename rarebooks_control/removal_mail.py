"""The removal emails: owner warnings, the export email, and the operator's alerts and digests (spec 0007).

Owner emails go to the case's `owner_email` (copied from the registry, never read from the site), translated
into the site's language. A `dry_run` case sends them to `books_ops_email` instead, with the owner's address in
the body (AC-14).
"""

from datetime import timedelta

import frappe
from babel.dates import format_date
from frappe import _
from frappe.utils import escape_html, getdate

from rarebooks_control.alerts import send_ops
from rarebooks_control.billing import receipt_language
from rarebooks_control.signing import utc_now

CASE = "Books Removal Case"
SITE = "Books Hosted Site"
SETTINGS = "Books Removal Settings"
BRAND_NAME = "RareBooks"
SUBSCRIPTION_PATH = "/books/subscription"
LINK_DAYS = 30
# Spec 0010 AC-16: the hourly round hasn't run for this long.
RUN_STALE = timedelta(hours=3)
DROP_STUCK = timedelta(hours=2)
# An alert about one stuck drop goes once; the key outlives any real episode.
ALERT_KEY_SECONDS = 14 * 24 * 60 * 60


def recipient(case) -> str | None:
	if case.mode == "dry_run":
		return frappe.conf.get("books_ops_email")
	return case.owner_email


def send_warning(case, very_soon: bool) -> str | None:
	"""The warning for the case's kind, in the site's language; the name of its Email Queue row (AC-4)."""
	lang = receipt_language(case.site_language)
	company = case.company_name or case.site
	when = _("very soon", lang=lang) if very_soon else long_date(case.removal_on, lang)
	if case.kind == "Lapsed":
		opening = _("The subscription for {0} has ended, and your books are read only.", lang=lang)
		ask = _("Renew by {0} to keep this site: {1}", lang=lang)
	else:
		opening = _("The free trial for {0} has ended, and your books are read only.", lang=lang)
		ask = _("Subscribe by {0} to keep this site: {1}", lang=lang)
	subscribe = case.site_url.rstrip("/") + SUBSCRIPTION_PATH
	if very_soon:
		removal = _("If you don't, the site will be removed very soon.", lang=lang)
	else:
		removal = _("If you don't, the site will be removed on {0}.", lang=lang).format(when)
	lines = [
		opening.format(company),
		removal,
		_("Before it is removed, we email you a download link to a copy of all your records.", lang=lang),
		ask.format(_("now", lang=lang) if very_soon else when, subscribe),
	]
	subject = _("{0}: your site {1} will be removed {2}", lang=lang).format(
		BRAND_NAME, company, when if very_soon else _("on {0}", lang=lang).format(when)
	)
	return _send(case, subject, lines)


def send_export(case, link: str) -> str | None:
	"""The export email with the download link (AC-7). Its message is dropped from the Email Queue once it is
	sent, so the link token stays in no table of the control site."""
	lang = receipt_language(case.site_language)
	company = case.company_name or case.site
	expires = long_date(utc_now().date() + timedelta(days=LINK_DAYS), lang)
	lines = [
		_(
			"{0} was not subscribed, so the site will be removed. Here is a copy of all your records.",
			lang=lang,
		).format(company),
		_("Download it here: {0}", lang=lang).format(link),
		_("The link works until {0}. Size: {1}.", lang=lang).format(expires, size_label(case.export_bytes)),
		_(
			"The zip holds your records as CSV files you can open in Excel, the files you uploaded, and a database backup you can restore on your own Frappe 16 bench. README.txt inside explains each part.",
			lang=lang,
		),
		_(
			"Saved passwords, such as email account passwords, can't be read after a restore. Enter them again.",
			lang=lang,
		),
		_(
			"The site is removed 3 days after this email at the earliest. Subscribing before then keeps it: {0}",
			lang=lang,
		).format(case.site_url.rstrip("/") + SUBSCRIPTION_PATH),
		# Spec 0010 AC-8: Frappe Cloud takes a backup when it archives a site and keeps it under its own policy.
		_(
			"After the site is removed, the hosting provider may keep backups of it for a limited time under its own policy.",
			lang=lang,
		),
	]
	subject = _("{0}: a copy of your records from {1}", lang=lang).format(BRAND_NAME, company)
	return _send(case, subject, lines, redact=True)


def ops_alert(subject: str, lines: list[str]):
	send_ops(subject, lines)


def _send(case, subject: str, lines: list[str], redact: bool = False) -> str | None:
	"""Queue the email; the Email Queue name, or None when it couldn't be queued (logged, never raised)."""
	to = recipient(case)
	if not to:
		frappe.log_error(
			f"Removal: no recipient for {case.name}", "books_ops_email is not set for a dry run."
		)
		return None
	if case.mode == "dry_run":
		lines = [
			_("Dry run: in live mode this email goes to {0}.").format(case.owner_email),
			*lines,
		]
	try:
		queue = frappe.sendmail(
			recipients=[to],
			subject=subject,
			message="<br>".join(escape_html(line) for line in lines),
			reference_doctype=CASE,
			reference_name=case.name,
			redact_message_after_send=redact,
		)
	except Exception:
		frappe.log_error(f"Removal: an email for {case.name} could not be queued")
		return None
	return getattr(queue, "name", None)


def long_date(value, lang: str) -> str:
	"""A date as words in the email's language, for example 19 November 2026."""
	return format_date(getdate(value), "d MMMM y", locale=lang)


def size_label(size) -> str:
	size = size or 0
	if size >= 1024 * 1024 * 1024:
		return f"{size / (1024 * 1024 * 1024):.1f} GB"
	if size >= 1024 * 1024:
		return f"{size / (1024 * 1024):.1f} MB"
	return f"{max(size, 1) / 1024:.0f} KB"


# Scheduled jobs (spec 0007 AC-21, spec 0010 AC-14 and AC-16)


def hourly():
	alert_stuck_drops()


def daily():
	send_approval_list()
	send_digest()


def check_dormant_days():
	"""Spec 0010 AC-14: Frappe Cloud stops the scheduler of a site nobody visits for `dormant_days`, and the
	control site's scheduler is the only clock for removals. While it isn't 0, one email a day."""
	days = frappe.db.get_single_value("System Settings", "dormant_days")
	if not days:
		return
	if _first_alert(f"dormant:{utc_now().date()}"):
		ops_alert(
			_("The control site can go dormant"),
			[
				_(
					"System Settings dormant_days is {0}, so the scheduler stops after that many quiet days."
				).format(days),
				_("Set it to 0 so the hourly removal round keeps running."),
			],
		)


def alert_stale_run():
	"""Spec 0010 AC-16: the hourly round's last run is more than 3 hours old, once a day while it holds."""
	settings = frappe.db.get_singles_dict(SETTINGS)
	last_run = settings.get("last_run_on")
	if settings.get("mode") in (None, "off"):
		return
	last_run = frappe.utils.get_datetime(last_run) if last_run else None
	if last_run and utc_now() - last_run <= RUN_STALE:
		return
	if _first_alert(f"stale-run:{utc_now().date()}"):
		ops_alert(
			_("The removal round has stopped running"),
			[
				_("Its last run was at {0} UTC.").format(last_run or "-"),
				_("Check that the control site's scheduler is enabled and its workers are up."),
			],
		)


def alert_stuck_drops():
	"""A case `Dropping` for more than 2 hours after `drop_started_on`: one email per case."""
	for case in frappe.get_all(
		CASE,
		filters={"status": "Dropping", "drop_started_on": ("<", utc_now() - DROP_STUCK)},
		fields=["name", "site", "drop_started_on", "archive_requested_on", "last_error"],
	):
		if _first_alert(f"stuck:{case.name}"):
			ops_alert(
				_("The drop of {0} has not finished").format(case.site),
				[
					_("Case {0} has been Dropping since {1} UTC.").format(case.name, case.drop_started_on),
					_("Archive requested on: {0}").format(case.archive_requested_on or "-"),
					_("Last error: {0}").format(case.last_error or "-"),
				],
			)


def send_approval_list():
	"""One email a day listing the `Holding` cases that wait for a Removal Manager's approval (AC-20)."""
	if not frappe.db.get_single_value(SETTINGS, "require_approval"):
		return
	cases = frappe.get_all(
		CASE,
		filters={"status": "Holding", "approved_by": ("is", "not set"), "mode": "live"},
		fields=["name", "site", "hold_until"],
		order_by="hold_until asc",
	)
	if not cases:
		return
	send_ops(
		_("{0} removals wait for your approval").format(len(cases)),
		[
			_("{0}: {1}, hold ends {2} UTC").format(case.name, case.site, case.hold_until or "-")
			for case in cases
		],
		now=False,
	)


def send_digest():
	"""The removals and cancellations of the last day, the round's last run, the sites whose config isn't
	Active (spec 0010 AC-16), failed drops Frappe Cloud finished after all (spec 0011 AC-11), and the sites
	whose domain is stuck or failed (spec 0012 AC-14)."""
	from rarebooks_control.registry import domain_digest_lines

	late = reconcile_failed_drops()
	since = utc_now() - timedelta(days=1)
	removed = frappe.get_all(
		CASE, filters={"status": "Removed", "removed_on": (">=", since)}, fields=["name", "site", "mode"]
	)
	cancelled = frappe.get_all(
		CASE,
		filters={"status": "Cancelled", "modified": (">=", frappe.utils.now_datetime() - timedelta(days=1))},
		fields=["name", "site", "mode", "last_error"],
	)
	unconfigured = frappe.get_all(
		SITE,
		filters={"status": "Active", "config_status": ("!=", "Active")},
		fields=["name", "config_status"],
		order_by="name asc",
	)
	last_run = frappe.db.get_single_value(SETTINGS, "last_run_on")
	lines = [_("Last removal run: {0} UTC").format(last_run or "-")]
	lines += [_("Removed: {0}").format(len(removed))]
	lines += [
		f"{case.name}: {case.site}" + (f" ({_('archived after failing')})" if case.name in late else "")
		for case in removed
	]
	lines += [_("Cancelled: {0}").format(len(cancelled))]
	lines += [f"{case.name}: {case.site} ({case.mode}) {case.last_error or ''}".strip() for case in cancelled]
	lines += [_("Sites whose config isn't Active: {0}").format(len(unconfigured))]
	lines += [f"{row.name}: {_(row.config_status)}" for row in unconfigured]
	lines += domain_digest_lines()
	send_ops(_("Removal digest"), lines, now=False)


def reconcile_failed_drops() -> set[str]:
	"""Spec 0011 AC-11: a `Failed` case whose drop started, and whose site Frappe Cloud now shows `Archived`,
	becomes `Removed`. An API error leaves it for the next day. The names reconciled are returned."""
	from rarebooks_control.providers import frappe_cloud
	from rarebooks_control.removal import mark_removed

	done = set()
	for name in frappe.get_all(
		CASE, filters={"status": "Failed", "drop_started_on": ("is", "set")}, pluck="name"
	):
		case = frappe.get_doc(CASE, name, for_update=True)
		try:
			cloud = frappe_cloud.get_site(case.site)
		except frappe_cloud.FrappeCloudError:
			frappe.db.commit()
			continue
		if cloud["status"] == frappe_cloud.ARCHIVED:
			mark_removed(case)
			done.add(name)
		else:
			frappe.db.commit()
	return done


def _first_alert(key: str) -> bool:
	"""True only the first time this alert key is seen."""
	return bool(
		frappe.cache.set(frappe.cache.make_key(f"removal-alert:{key}"), 1, ex=ALERT_KEY_SECONDS, nx=True)
	)
