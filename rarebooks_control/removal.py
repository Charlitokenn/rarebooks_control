"""Trial end removal, driven by the control site on Frappe Cloud (specs 0007, 0010 and 0011).

Every hour `run_round` enqueues one `run_site` job per site. That job reads the site's `removal_report` over
signed HTTPS, applies spec 0007's case rules, and makes the calls the case needs in this order:
`clear_removal`, `mark_export_sent`, `start_export`, then the drop (spec 0010 AC-1). Customer sites build their
own export and upload it through a presigned URL; they report back with `export_ready` and `step_failed`,
signed with their own secret. The drop is a Frappe Cloud archive, made only after every spec 0007 AC-9 and
AC-20 check and every spec 0010 AC-6 guard pass in the same job, and only once `Dropping` is committed.

The customer site is the only clock for its dates: the control site stores the `removal_on` and `today` it
reports, and never works out a removal date itself.

A Discard case (spec 0014 AC-19) ends a pool site or a failed sign up's site: it starts `Dropping`, follows
Books Onboarding Settings `discard_mode` rather than removal `mode`, sends no warning, export or email, and adds
one guard of its own: the site's owner has never logged in, read twice, the second time right before the archive.
It replaces an open Trial or Lapsed case unless that case is a drop under way, and a Claimed site gets no Trial or
Lapsed case until its sign up is Ready (spec 0014 AC-20).
"""

import hashlib
import logging
import re
import secrets
import uuid
from datetime import date, datetime, timedelta

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_days, getdate
from werkzeug.utils import redirect
from werkzeug.wrappers import Response

from rarebooks_control import export_storage, removal_mail, signing
from rarebooks_control.billing import billing_state, page
from rarebooks_control.providers import frappe_cloud
from rarebooks_control.providers.frappe_cloud import FrappeCloudError, OutcomeUnknown
from rarebooks_control.rarebooks_control.doctype.books_removal_case.books_removal_case import (
	FINAL_STATUSES,
)
from rarebooks_control.site_calls import SiteCallFailed, call_site
from rarebooks_control.state import site_date

SITE = "Books Hosted Site"
CASE = "Books Removal Case"
SETTINGS = "Books Removal Settings"
EMAIL_QUEUE = "Email Queue"
REMOVAL_MANAGER = "Removal Manager"
# Spec 0010 AC-17: per site, checked right after the signature (every site calls from Frappe Cloud's IPs).
SITE_CALL_LIMIT = 60
OK, CONFLICT, TOO_MANY, BAD_REQUEST, NOT_FOUND, UNAVAILABLE = 200, 409, 429, 400, 404, 503
ERROR_CHARACTERS = 2000
# Warning offsets in days before `removal_on`, by kind (spec 0007 AC-4).
WARNING_OFFSETS = {"Trial": (4, 2, 1), "Lapsed": (14, 3, 1)}
# Spec 0005: a site is in grace for 7 days after `paid_until`, and is still a paying site then.
GRACE_DAYS = 7
EXPORT_AFTER_WARNING = timedelta(hours=24)
HOLD = timedelta(hours=72)
LINK_LIFETIME = timedelta(days=30)
EXPORT_KEPT_DAYS = 90
# Spec 0010 AC-3: an upload link lasts 6 hours, and an export with no word for that long is stuck.
CLAIM_TIMEOUT = timedelta(hours=6)
EMAIL_SEND_TIMEOUT = timedelta(hours=24)
MAX_ATTEMPTS = 3
# Spec 0010 AC-3: at most this many `start_export` calls per hourly run, across all sites.
EXPORTS_PER_RUN = 3
# Spec 0007 AC-11: no checkout starts in the last hour before `hold_until`, so no payment starts while a drop can.
CHECKOUT_FREEZE = timedelta(hours=1)
ERROR_RUNS_ALERT_AT = 3
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STEPS = ("export", "upload")
DROP_PHASES = ("locked", "lapsed")
MARK_SAVE_TRIES = 3
RUN_SITE = "rarebooks_control.removal.run_site"
RUN_SITE_TIMEOUT = 15 * 60
DISCARD = "Discard"
ONBOARDING_SETTINGS = "Books Onboarding Settings"
SIGNUP = "Books Signup"
# Spec 0014 AC-19: a site being discarded may still be building, or be broken, on Frappe Cloud.
DISCARD_CLOUD_STATUSES = ("Active", "Broken", "Pending", "Installing")


def removal_logger():
	"""The removal log, at INFO at least. Frappe sets its loggers to ERROR (WARNING on a dev server), which would
	drop the lines a dry run is read by: "would drop" (spec 0010) and "would discard" (spec 0014 AC-19)."""
	logger = frappe.logger("rarebooks_control.removal")
	if not logger.isEnabledFor(logging.INFO):
		logger.setLevel(logging.INFO)
	return logger


def removal_settings():
	"""Books Removal Settings, read fresh from the database (never the cache), since the kill switch and the
	mode must take effect at once (spec 0010 AC-9)."""
	return frappe.get_single(SETTINGS)


# Customer site calls (spec 0010 AC-4, AC-17)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def export_ready() -> Response:
	"""The site uploaded its export: check R2 has it at that size, then queue the export email (AC-4)."""
	verified = _verify_site("export_ready")
	if not isinstance(verified, tuple):
		return verified
	request, row = verified
	body, echo = request.body, {"issued_at": request.issued_at}
	attempt_id, size, digest = body.get("attempt_id"), body.get("bytes"), body.get("sha256")
	if (
		not isinstance(attempt_id, str)
		or not isinstance(size, int)
		or isinstance(size, bool)
		or size <= 0
		or not (isinstance(digest, str) and SHA256.match(digest))
	):
		return request.answer(echo, BAD_REQUEST)

	case = _site_case(body, row.name)
	if not case or case.status != "Exporting" or case.attempt_id != attempt_id:
		# Another attempt, or a case no longer waiting: the job stops, and nothing in R2 is deleted.
		return request.answer({**echo, "status": case.status if case else "unknown"}, CONFLICT)
	if case.export_sha256:
		# A repeat of a call already recorded: its email is queued.
		same = (case.export_bytes, case.export_sha256) == (size, digest)
		return request.answer({**echo, "status": case.status}, OK if same else CONFLICT)

	try:
		stored = export_storage.object_size(case.export_key)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(f"Removal: R2 could not be asked about {case.name}")
		return request.answer({**echo, "status": "retry"}, UNAVAILABLE)
	if stored != size:
		return request.answer({**echo, "status": "size_mismatch"}, CONFLICT)

	case.update(
		{
			"export_bytes": size,
			"export_sha256": digest,
			"delete_export_on": add_days(signing.utc_now().date(), EXPORT_KEPT_DAYS),
		}
	)
	queue_export_email(case)
	case.save(ignore_permissions=True)
	return request.answer({**echo, "status": case.status})


@frappe.whitelist(allow_guest=True, methods=["POST"])
def step_failed() -> Response:
	"""A failed export or upload: tried again on a later run, `Failed` after the third (spec 0007 AC-12)."""
	verified = _verify_site("step_failed")
	if not isinstance(verified, tuple):
		return verified
	request, row = verified
	body, echo = request.body, {"issued_at": request.issued_at}
	if body.get("step") not in STEPS:
		return request.answer(echo, BAD_REQUEST)
	case = _site_case(body, row.name)
	if not case:
		return request.answer({**echo, "status": "unknown"}, CONFLICT)
	if case.status != "Exporting":
		return request.answer({**echo, "status": case.status}, CONFLICT)

	error = body.get("error")
	case.last_error = (error if isinstance(error, str) else "")[:ERROR_CHARACTERS]
	export_failed(case, body["step"])
	case.save(ignore_permissions=True)
	return request.answer({**echo, "status": case.status})


def export_failed(case, step: str):
	"""Count one failed export attempt. The caller saves."""
	case.attempts = (case.attempts or 0) + 1
	if case.attempts >= MAX_ATTEMPTS:
		fail(case, _("The {0} step failed {1} times.").format(step, case.attempts))
		return
	case.status = "Scheduled"
	case.export_claimed_on = None


# The owner's download link


@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=10, seconds=60 * 60)
def download(token: str | None = None) -> Response:
	"""Redirect to a fresh one hour R2 link for the export, counting the download (AC-7). Works for 30 days."""
	expired = page(_("This link has expired. Contact support."), NOT_FOUND)
	if not isinstance(token, str) or not token:
		return expired
	name = frappe.db.get_value(
		CASE,
		{
			"link_token_hash": token_hash(token),
			"link_expires_on": (">", signing.utc_now()),
			"status": ("!=", "Cancelled"),
		},
		"name",
	)
	if not name:
		return expired
	case = frappe.get_doc(CASE, name, for_update=True)
	try:
		url = export_storage.presigned_url(case.export_key, f"{case.site}-records.zip")
	except Exception:
		frappe.db.rollback()
		frappe.log_error(f"Removal: no download link could be made for {case.name}")
		return page(_("The download is not available right now. Try again later."), 503)
	case.download_count = (case.download_count or 0) + 1
	case.last_downloaded_on = signing.utc_now()
	case.save(ignore_permissions=True)
	# Frappe commits only on POST, and every download is counted on the case (AC-7, AC-17).
	frappe.db.commit()
	return redirect(url, code=302)


def token_hash(token: str) -> str:
	return hashlib.sha256(token.encode()).hexdigest()


# Removal Manager actions (spec 0007 AC-13)


@frappe.whitelist(methods=["POST"])
def approve_case(case: str) -> dict:
	"""Allow the drop of a `Holding` case. It still waits for `hold_until` and every AC-9 check (AC-20)."""
	doc = _managed_case(case, ("Holding",))
	doc.approved_by = frappe.session.user
	doc.approved_on = signing.utc_now()
	doc.save(ignore_permissions=True)
	return {"status": doc.status}


@frappe.whitelist(methods=["POST"])
def pause_case(case: str, reason: str | None = None) -> dict:
	doc = _managed_case(case, ("Scheduled", "Holding"))
	if not (reason or "").strip():
		frappe.throw(_("Give a reason for the pause."))
	doc.status = "Paused"
	doc.paused_by = frappe.session.user
	doc.pause_reason = reason.strip()[:ERROR_CHARACTERS]
	doc.save(ignore_permissions=True)
	return {"status": doc.status}


@frappe.whitelist(methods=["POST"])
def resume_case(case: str) -> dict:
	"""Back to `Holding` if the case has an accepted export, else to `Scheduled`."""
	doc = _managed_case(case, ("Paused",))
	doc.status = "Holding" if doc.export_sha256 else "Scheduled"
	doc.paused_by = None
	doc.pause_reason = None
	doc.save(ignore_permissions=True)
	return {"status": doc.status}


@frappe.whitelist(methods=["POST"])
def retry_case(case: str) -> dict:
	"""Reopen a `Failed` case with `attempts` at 0 (spec 0007 AC-13).

	A failed drop goes back to `Dropping`, so the hourly poll reads Frappe Cloud first and calls the archive again
	only when no call was marked and every guard passes (spec 0011 AC-2). An export whose email never went out
	is sent again with a new link, and the hold starts from the new sent time.
	"""
	doc = _managed_case(case, ("Failed",))
	doc.attempts = 0
	doc.paused_by = None
	doc.pause_reason = None
	if doc.drop_started_on:
		doc.status = "Dropping"
	elif doc.export_sha256 and not doc.export_sent_on:
		queue_export_email(doc)
		doc.status = "Exporting"
	elif doc.export_sent_on:
		doc.status = "Holding"
	else:
		doc.status = "Scheduled"
		doc.export_claimed_on = None
	doc.save(ignore_permissions=True)
	return {"status": doc.status}


def _managed_case(name: str, statuses: tuple[str, ...]):
	frappe.only_for(REMOVAL_MANAGER)
	if not isinstance(name, str) or not frappe.db.exists(CASE, name):
		frappe.throw(_("No removal case {0}.").format(name), frappe.DoesNotExistError)
	doc = frappe.get_doc(CASE, name, for_update=True)
	if doc.status not in statuses:
		frappe.throw(_("Case {0} is {1}, so this can't be done now.").format(doc.name, _(doc.status)))
	return doc


# What spec 0008's billing asks of the removal cases


def open_case(site: str) -> str | None:
	return frappe.db.get_value(CASE, {"site": site, "status": ("not in", FINAL_STATUSES)}, "name")


def is_being_removed(site: str) -> bool:
	"""`create_checkout` refuses while the case is `Dropping` or `Removed`, and in the last hour before
	`hold_until`, so no payment starts while a drop can start (AC-11, spec 0008 AC-5)."""
	cases = frappe.get_all(
		CASE,
		filters={"site": site, "status": ("in", ("Holding", "Dropping", "Removed"))},
		fields=["status", "hold_until"],
	)
	now = signing.utc_now()
	return any(
		case.status in ("Dropping", "Removed")
		or (case.hold_until and now >= case.hold_until - CHECKOUT_FREEZE)
		for case in cases
	)


def is_dropping(site: str) -> bool:
	"""A payment for a site being dropped is not applied (spec 0008 AC-17)."""
	return bool(frappe.db.exists(CASE, {"site": site, "status": "Dropping"}))


# The hourly round (spec 0010 AC-1, spec 0011 AC-10)


def run_round():
	"""Hourly: note the run, then one deduplicated `run_site` job per Active, configured site and per site with
	a `Dropping` case. While `mode` is off, only the note and one log line."""
	from rarebooks_control.registry import sweep_domain_cleanup

	# Spec 0012 AC-11: the domain cleanup runs before the mode check, so it runs with removal off too.
	sweep_domain_cleanup()
	settings = removal_settings()
	frappe.db.set_single_value(SETTINGS, "last_run_on", signing.utc_now())
	removal_mail.check_dormant_days()
	# Spec 0014 AC-19, AC-20: Discard cases follow `discard_mode`, so they run whatever the removal mode.
	for site in frappe.get_all(CASE, filters={"status": "Dropping", "kind": DISCARD}, pluck="site"):
		_queue_run_site(site)
	if settings.mode == "off":
		removal_logger().info("Removal round skipped: mode is off")
		return
	for site in round_sites():
		_queue_run_site(site)
	cancel_skipped_paid()


def _queue_run_site(site: str):
	frappe.enqueue(
		RUN_SITE,
		queue="default",
		timeout=RUN_SITE_TIMEOUT,
		job_id=f"removal:{site}",
		deduplicate=True,
		enqueue_after_commit=True,
		site=site,
	)


def round_sites() -> list[str]:
	"""Active sites with an Active config, less the pool sites no sign up has claimed (spec 0014 AC-20), plus
	every site with a Dropping case, Discard cases included."""
	active = [
		row.name
		for row in frappe.get_all(
			SITE, filters={"status": "Active", "config_status": "Active"}, fields=["name", "pool_status"]
		)
		if not in_pool(row.pool_status)
	]
	dropping = frappe.get_all(CASE, filters={"status": "Dropping"}, pluck="site")
	return sorted(set(active) | set(dropping))


def cancel_skipped_paid():
	"""An open case on a site the round skips (its config isn't Active) is still cancelled when the control
	site's own billing state shows it paid or exempt. The site's hold fields are cleared at its next Active
	report (spec 0010 AC-1)."""
	for case in frappe.get_all(
		CASE,
		filters={"status": ("not in", (*FINAL_STATUSES, "Dropping"))},
		fields=["name", "site"],
	):
		row = frappe.db.get_value(SITE, case.site, ["status", "config_status", "time_zone"], as_dict=True)
		if not row or (row.status == "Active" and row.config_status == "Active"):
			continue
		if paid_by_billing(case.site, site_date(signing.utc_now(), row.time_zone)):
			cancel(frappe.get_doc(CASE, case.name, for_update=True), _("The site is paid or exempt."))


def paid_by_billing(site: str, today: date) -> bool:
	state = billing_state(site)
	paid_until = state["paid_until"]
	return bool(state["exempt"]) or (bool(paid_until) and paid_until + timedelta(days=GRACE_DAYS) >= today)


def run_site(site: str):
	"""One site's part of the round, in its own job. A `Dropping` case only polls Frappe Cloud (spec 0011
	AC-10); any other site must be Active with its config Active."""
	settings = removal_settings()
	name = open_case(site)
	case = frappe.db.get_value(CASE, name, ["status", "kind"], as_dict=True) if name else None
	if case and case.kind == DISCARD:
		# A Discard case that has stopped (Failed) waits for you; the trial rules never touch it.
		if case.status == "Dropping":
			poll_discard(name)
		return
	if settings.mode == "off":
		return
	if case and case.status == "Dropping":
		poll_drop(name)
		return
	row = frappe.db.get_value(
		SITE,
		site,
		["name", "status", "config_status", "owner_email", "site_url", "pool_status"],
		as_dict=True,
	)
	if not row or row.status != "Active" or row.config_status != "Active" or in_pool(row.pool_status):
		return
	SiteRun(row, settings).run()


def in_pool(pool_status) -> bool:
	"""A pool site still waiting for a sign up (Building or Ready): no push, no removal case of kind Trial or
	Lapsed, no state (spec 0014 AC-20). A Claimed site is a normal hosted site."""
	return pool_status in ("Building", "Ready")


def awaits_signup(row) -> bool:
	"""A Claimed site whose Books Signup isn't Ready yet: the round opens no case for it (spec 0014 AC-20)."""
	if row.pool_status != "Claimed":
		return False
	return not frappe.db.exists(SIGNUP, {"hosted_site": row.name, "status": "Ready"})


def cancel_dry_runs():
	"""Saving `mode` from dry_run to live cancels every open dry run case and deletes its export (AC-9). A dry
	run never drops, so none of them is `Dropping`."""
	for name in frappe.get_all(
		CASE, filters={"mode": "dry_run", "status": ("not in", FINAL_STATUSES)}, pluck="name"
	):
		cancel(frappe.get_doc(CASE, name, for_update=True), _("Live removal started."))


class SiteRun:
	"""One `run_site` job for a site that isn't `Dropping`: its report, its case, and the calls they need."""

	def __init__(self, row, settings):
		self.row, self.settings = row, settings
		self.mode = settings.mode
		self.now = signing.utc_now()
		self.clear = self.mark_sent = self.export = self.drop = False

	def run(self):
		try:
			answer = call_site(self.row.name, "removal_report")
		except SiteCallFailed as error:
			self.note_error(str(error))
			return
		report = SiteReport.read(answer.body) if answer.status == OK else None
		if not report:
			self.note_error(f"removal_report answered HTTP {answer.status} without a usable status")
			return

		name = open_case(self.row.name)
		case = frappe.get_doc(CASE, name, for_update=True) if name else None
		if case and case.mode != self.mode:
			# A dry run never touches a live case, and going live has already cancelled the dry run ones.
			return
		if case:
			case.error_runs = 0
		if keeps_site(report, billing_state(self.row.name)):
			if case:
				cancel(case, _("The site is paid or exempt."))
			self.clear = self.is_marked(report, None)
		elif not case and awaits_signup(self.row):
			# Spec 0014 AC-20: no Trial or Lapsed case before the sign up is Ready, so a failed one is discarded.
			self.clear = self.is_marked(report, None)
		else:
			if not case:
				case = self.new_case(report)
			self.update(case, report)
			self.advance(case, report)
			case.save(ignore_permissions=True)
			self.clear = self.is_marked(report, case)
		# The case and its queued emails are kept before any call, so a call that fails can't undo them.
		frappe.db.commit()
		self.call(case)

	def call(self, case):
		"""The calls this run needs, in spec 0010 AC-1's order. A dry run writes no site field."""
		live = self.mode == "live"
		if self.clear and live:
			self.simple_call("clear_removal", {})
		if self.mark_sent and live:
			self.simple_call(
				"mark_export_sent",
				{"export_sent_on": _iso(case.export_sent_on), "removal_hold_until": _iso(case.hold_until)},
			)
		if self.export:
			start_export(case.name)
		if self.drop:
			start_drop(case.name)

	def simple_call(self, name: str, payload: dict):
		try:
			answer = call_site(self.row.name, name, payload)
		except SiteCallFailed as error:
			frappe.log_error(f"Removal: {name} for {self.row.name} failed", str(error))
			return
		if answer.status != OK:
			frappe.log_error(f"Removal: {name} for {self.row.name} answered {answer.status}")

	def note_error(self, error: str):
		"""A site that couldn't be read counts one error run on its open case; three in a row alert once."""
		name = open_case(self.row.name)
		if not name:
			frappe.log_error(f"Removal: {self.row.name} could not be read", error[:ERROR_CHARACTERS])
			return
		case = frappe.get_doc(CASE, name, for_update=True)
		case.error_runs = (case.error_runs or 0) + 1
		case.last_error = error[:ERROR_CHARACTERS]
		case.save(ignore_permissions=True)
		if case.error_runs == ERROR_RUNS_ALERT_AT:
			removal_mail.ops_alert(
				_("Site {0} could not be read for removal").format(case.site),
				[
					_("The control site could not read {0} on {1} runs in a row.").format(
						case.site, case.error_runs
					),
					_("Case: {0}").format(case.name),
					_("Last error: {0}").format(case.last_error),
				],
			)

	def new_case(self, report):
		if not self.row.owner_email:
			raise ValueError(f"site {self.row.name} has no owner_email")
		return frappe.get_doc(
			{
				"doctype": CASE,
				"site": self.row.name,
				"mode": self.mode,
				"kind": report.kind,
				"removal_on": report.removal_on,
				"status": "Scheduled",
				"owner_email": self.row.owner_email,
				"site_url": report.site_url or self.row.site_url,
			}
		).insert(ignore_permissions=True)

	def update(self, case, report):
		"""The facts for the emails, and `removal_on`, which moves only later and only while `Scheduled`."""
		case.site_language = report.language
		case.company_name = report.company_name
		if report.site_url:
			case.site_url = report.site_url
		stored = getdate(case.removal_on)
		if report.removal_on == stored:
			return
		if case.status == "Scheduled" and report.removal_on > stored:
			case.removal_on = report.removal_on
		else:
			frappe.log_error(
				f"Removal: {case.name} kept removal_on {stored}",
				f"Site {case.site} reported {report.removal_on} while the case is {case.status}.",
			)

	def advance(self, case, report):
		if case.status == "Scheduled":
			self.warn(case, report)
			self.export = self.export_due(case, report)
		elif case.status == "Exporting":
			self.check_exporting(case)
		elif case.status == "Holding":
			self.check_holding(case, report)

	def warn(self, case, report):
		"""Send the latest due warning, if it hasn't gone yet (spec 0007 AC-4)."""
		offset = due_warning(case.kind, getdate(case.removal_on), report.today)
		if offset is None or any(row.offset_days == offset for row in case.warnings):
			return
		very_soon = report.today >= getdate(case.removal_on)
		queue = removal_mail.send_warning(case, very_soon)
		if queue:
			case.append(
				"warnings",
				{
					"offset_days": offset,
					"sent_on": self.now,
					"recipient": removal_mail.recipient(case),
					"email": queue,
				},
			)

	def export_due(self, case, report) -> bool:
		"""Spec 0007 AC-5: the site's today has reached `removal_on`, a warning went out 24 hours ago or more,
		and removal isn't paused. The case being `Scheduled` and the site unpaid were checked by the caller."""
		if self.settings.removal_paused or report.today < getdate(case.removal_on):
			return False
		sent = [row.sent_on for row in case.warnings if row.sent_on]
		return bool(sent) and self.now - min(sent) >= EXPORT_AFTER_WARNING

	def check_exporting(self, case):
		if case.export_sha256:
			self.check_export_email(case)
			return
		if case.export_claimed_on and self.now - case.export_claimed_on >= CLAIM_TIMEOUT:
			# No word for 6 hours counts one attempt, and a new attempt gets a new link (spec 0010 AC-3).
			case.attempts = (case.attempts or 0) + 1
			if case.attempts >= MAX_ATTEMPTS:
				fail(case, _("No export arrived in {0} attempts.").format(case.attempts))
				return
			case.status = "Scheduled"
			case.export_claimed_on = None
			self.export = not self.settings.removal_paused

	def check_holding(self, case, report):
		if not case.export_sent_on:
			self.check_export_email(case)
			return
		if self.mode == "live" and report.removal_hold_until != _iso(case.hold_until):
			self.mark_sent = True
		self.drop = self.drop_may_start(case)

	def check_export_email(self, case):
		"""The hold starts once the export email is `Sent`; one in `Error`, or unsent after 24 hours, fails
		the case (spec 0007 AC-8, AC-12)."""
		email = frappe.db.get_value(EMAIL_QUEUE, case.export_email, ["status", "modified"], as_dict=True)
		if email and email.status == "Sent":
			sent = email.modified.replace(microsecond=0)
			case.update(
				{
					"status": "Holding",
					"export_sent_on": sent,
					"hold_until": sent + HOLD,
					"link_expires_on": sent + LINK_LIFETIME,
					"attempts": 0,
				}
			)
			self.mark_sent = self.mode == "live"
			return
		if not email or email.status == "Error":
			fail(case, _("The export email could not be sent."))
		elif case.export_queued_on and self.now - case.export_queued_on >= EMAIL_SEND_TIMEOUT:
			fail(case, _("The export email was still not sent 24 hours after it was queued."))

	def drop_may_start(self, case) -> bool:
		"""Try the drop only once the hold has passed and the case is approved (when required). Every other
		check runs again in `start_drop`, which alone can start a drop."""
		if self.now < case.hold_until or self.settings.removal_paused:
			return False
		return bool(case.approved_by) or not self.settings.require_approval

	def is_marked(self, report, case) -> bool:
		"""The site still shows the hold fields but no case holds it: they are emptied (spec 0007 AC-11)."""
		if not (report.export_sent_on or report.removal_hold_until):
			return False
		return not (case and case.status in ("Holding", "Dropping", "Paused") and case.export_sent_on)


class SiteReport:
	"""The fields of one `removal_status()` answer that the control site uses, checked."""

	def __init__(self, entry: dict):
		self.today = _date(entry.get("today"))
		self.removal_on = _date(entry.get("removal_on"))
		self.paid_until = _date(entry.get("paid_until"))
		self.kind = entry.get("kind") if entry.get("kind") in WARNING_OFFSETS else None
		self.exempt = bool(entry.get("exempt"))
		self.phase = entry.get("phase")
		self.payment_pending = entry.get("payment_pending")
		self.site_url = _https(entry.get("site_url"))
		self.language = _text(entry.get("language"))
		self.company_name = _text(entry.get("company_name"))
		self.export_sent_on = entry.get("export_sent_on") or None
		self.removal_hold_until = _normal_iso(entry.get("removal_hold_until"))

	@classmethod
	def read(cls, entry):
		if not isinstance(entry, dict):
			return None
		report = cls(entry)
		if not report.today or (report.removal_on and not report.kind):
			return None
		return report


def keeps_site(report: SiteReport, state: dict) -> bool:
	"""The site is never removed: by its own report (exempt, or no removal date) or by the control site's
	billing state (exempt, or paid until a day whose grace hasn't ended). Either one is enough (AC-11)."""
	if report.exempt or not report.removal_on or state["exempt"]:
		return True
	paid_until = state["paid_until"]
	return bool(paid_until) and paid_until + timedelta(days=GRACE_DAYS) >= report.today


def due_warning(kind: str, removal_on: date, today: date) -> int | None:
	"""The smallest offset whose date has come, or None when no warning is due yet (AC-4)."""
	due = [offset for offset in WARNING_OFFSETS[kind] if removal_on - timedelta(days=offset) <= today]
	return min(due) if due else None


# The export (spec 0010 AC-3)


def start_export(name: str):
	"""Claim one of the run's export slots, store a new attempt and its key, commit, then ask the site to build
	and upload. A call that doesn't get through gives the slot back and counts no attempt."""
	case = frappe.get_doc(CASE, name, for_update=True)
	if case.status != "Scheduled":
		return
	slot = _export_slot_key()
	if frappe.cache.incrby(slot, 1) > EXPORTS_PER_RUN:
		frappe.cache.incrby(slot, -1)
		return
	frappe.cache.expire(slot, 2 * 60 * 60)
	attempt_id = str(uuid.uuid4())
	key = f"exports/{case.name}/{attempt_id}.zip"
	expires_at = signing.utc_now() + CLAIM_TIMEOUT
	try:
		upload_url = export_storage.presigned_put_url(key)
	except Exception:
		frappe.cache.incrby(slot, -1)
		frappe.log_error(f"Removal: no upload link could be made for {case.name}")
		return
	case.update(
		{
			"status": "Exporting",
			"export_claimed_on": signing.utc_now(),
			"attempt_id": attempt_id,
			"export_key": key,
			"export_bytes": 0,
			"export_sha256": None,
			"export_email": None,
		}
	)
	case.save(ignore_permissions=True)
	frappe.db.commit()

	try:
		answer = call_site(
			case.site,
			"start_export",
			{
				"case": case.name,
				"attempt_id": attempt_id,
				"upload_url": upload_url,
				"expires_at": _iso(expires_at),
			},
		)
	except SiteCallFailed as error:
		frappe.cache.incrby(slot, -1)
		_unclaim(case.name, str(error))
		return
	if answer.status == OK and answer.body.get("status") == "queued":
		return
	case = frappe.get_doc(CASE, name, for_update=True)
	if case.status == "Exporting" and case.attempt_id == attempt_id:
		case.last_error = f"start_export answered HTTP {answer.status}"
		export_failed(case, "export")
		case.save(ignore_permissions=True)
	frappe.db.commit()


def _unclaim(name: str, error: str):
	case = frappe.get_doc(CASE, name, for_update=True)
	if case.status == "Exporting" and not case.export_sha256:
		case.status = "Scheduled"
		case.export_claimed_on = None
		case.last_error = error[:ERROR_CHARACTERS]
		case.save(ignore_permissions=True)
	frappe.db.commit()


def _export_slot_key() -> str:
	return frappe.cache.make_key(f"removal:exports:{signing.utc_now().strftime('%Y%m%d%H')}")


# The drop (spec 0010 AC-6, AC-7; spec 0011 AC-1 to AC-3)


def start_drop(name: str):
	"""Every check and guard, then `Dropping` committed, then the archive. In dry run only a log line."""
	settings = removal_settings()
	case = frappe.get_doc(CASE, name, for_update=True)
	if settings.mode != "live" or case.mode != "live":
		removal_logger().info(f"Removal: would drop {case.site}")
		return
	reason = drop_refusal(case, settings) or fresh_report_refusal(case.site)
	if reason:
		_wait(case, reason)
		return
	# Spec 0013 AC-3: before `Dropping` is saved only `Archived` proves an archive; a 403 is a failed step.
	try:
		proof, cloud = site_archived(case.site, drop_started=False)
	except FrappeCloudError as error:
		drop_failed(case, str(error))
		return
	if proof:
		mark_removed(case)
		return
	reason = cloud_refusal(case.site, cloud, settings)
	if reason:
		_wait(case, reason)
		return

	# The daily cap is counted under the settings row lock, so parallel site jobs can't both pass it.
	frappe.db.get_singles_dict(SETTINGS, for_update=True)
	if drops_today() >= (settings.max_drops_per_day or 0):
		_wait(case, "today's drop cap is reached")
		return
	case.update(
		{
			"status": "Dropping",
			"drop_started_on": signing.utc_now(),
			"attempts": 0,
			"archive_requested_on": None,
		}
	)
	case.save(ignore_permissions=True)
	# Nothing is archived before its `Dropping` row is committed; this also releases both locks.
	frappe.db.commit()
	case = frappe.get_doc(CASE, name, for_update=True)
	if case.status == "Dropping" and not case.archive_requested_on:
		archive_now(case)


def poll_drop(name: str):
	"""Spec 0011 AC-2 and spec 0013 AC-2: one hourly job for a `Dropping` case, under the case row lock. A site
	proven archived is `Removed`, marked or not; a read that proves nothing and errors counts a failed step; a
	marked case only waits; an unmarked one with the site still Active runs every guard again and calls the
	archive once more."""
	case = frappe.get_doc(CASE, name, for_update=True)
	if case.status != "Dropping":
		return
	try:
		proof, cloud = site_archived(case.site, drop_started=bool(case.drop_started_on))
	except FrappeCloudError as error:
		drop_failed(case, str(error))
		return
	if proof:
		mark_removed(case)
		log_proof(case, proof)
		return
	if case.archive_requested_on or cloud["status"] != frappe_cloud.ACTIVE:
		frappe.db.commit()
		return
	settings = removal_settings()
	reason = (
		drop_refusal(case, settings)
		or cloud_refusal(case.site, cloud, settings)
		or fresh_report_refusal(case.site)
	)
	if reason:
		_wait(case, reason)
		return
	archive_now(case)


def archive_now(case):
	"""Call the archive for a locked `Dropping` case with no mark. A success or an unknown outcome is marked;
	only a clear error counts a failed drop step (spec 0011 AC-1). A Discard case asks the site once more,
	right before the call, whether its owner has logged in (spec 0014 AC-19)."""
	if case.kind == DISCARD:
		refusal = owner_refusal(case.site)
		if refusal:
			_owner_refused(case, refusal)
			return
	try:
		frappe_cloud.archive(case.site)
	except OutcomeUnknown:
		pass
	except FrappeCloudError as error:
		drop_failed(case, str(error))
		return
	save_archive_mark(case.name)


def save_archive_mark(name: str):
	"""`archive_requested_on` in its own commit, tried 3 times. If every try fails, the case keeps no mark, and
	the next poll may call again under the same guards (spec 0011 AC-1)."""
	for _try in range(MARK_SAVE_TRIES):
		try:
			_write_archive_mark(name, signing.utc_now())
			frappe.db.commit()
			return
		except Exception:
			frappe.db.rollback()
	case_site = frappe.db.get_value(CASE, name, "site")
	frappe.log_error(f"Removal: the archive of {case_site} was requested, but case {name} couldn't be marked")


def _write_archive_mark(name: str, when: datetime):
	# Spec 0013 AC-5: the mark doesn't start the domain cleanup. An unknown outcome counts as requested, and the
	# site behind the domain may still be running; only `mark_removed` starts it.
	frappe.db.set_value(CASE, name, "archive_requested_on", when)


# Proving an archive (spec 0013 AC-1, AC-7, AC-8)

PROOF_STATUS = "status"
PROOF_RELEASED = "released"


def site_archived(site: str, drop_started: bool) -> tuple[str | None, dict | None]:
	"""Whether Frappe Cloud shows the site archived, from reads in this job. `("status", cloud)` when `get` shows
	`Archived`; `("released", None)` when `get` answers 403 for a drop we started and the site's own name is
	released; `(None, cloud)` for any other site `get` answers. Anything that proves nothing and isn't an answer
	raises `FrappeCloudError`, with a text that names no other site and no secret (AC-8)."""
	try:
		cloud = frappe_cloud.get_site(site)
	except FrappeCloudError as error:
		# A 403 also means a wrong or revoked key, so it is only worth a second fact for a drop we started.
		if error.status != frappe_cloud.FORBIDDEN or not drop_started:
			raise
		_check_name_released(site)
		return PROOF_RELEASED, None
	if cloud["status"] == frappe_cloud.ARCHIVED:
		return PROOF_STATUS, cloud
	return None, cloud


def _check_name_released(site: str):
	"""Raise unless `domain_exists` answers that no Frappe Cloud site holds the site's own name. An unauthorized
	call fails rather than answering None, so a broken key can't pass here (AC-9 checks that live)."""
	try:
		holder = frappe_cloud.domain_exists(site)
	except FrappeCloudError as error:
		answer = f"HTTP {error.status}" if error.status else "no answer"
		raise FrappeCloudError(
			f"press.api.site.get answered HTTP 403 and domain_exists failed ({answer})", error.status
		) from None
	if holder:
		raise FrappeCloudError(
			"press.api.site.get answered HTTP 403 and the site's name is still held", frappe_cloud.FORBIDDEN
		)


def log_proof(case, proof: str):
	"""AC-7: a removal proven by the released name gets its own log line, so it stands apart from `Archived`."""
	if proof == PROOF_RELEASED:
		removal_logger().info(f"Removal: {case.name} of {case.site} is removed: 403 and name released")


def drop_failed(case, error: str):
	"""A Frappe Cloud error during the drop counts one attempt; the third makes the case `Failed` with an ops
	email (spec 0010 AC-7). Saves and commits."""
	case.last_error = error[:ERROR_CHARACTERS]
	case.attempts = (case.attempts or 0) + 1
	if case.attempts >= MAX_ATTEMPTS:
		fail(case, _("The drop step failed {0} times.").format(case.attempts))
	case.save(ignore_permissions=True)
	frappe.db.commit()


def mark_removed(case):
	"""The site is proven archived (spec 0013 AC-1): the case is `Removed` and the registry row too, whatever its
	config status, and the domain cleanup starts. Safe to repeat. Saves and commits."""
	now = signing.utc_now()
	case.status = "Removed"
	case.removed_on = now
	case.save(ignore_permissions=True)
	row = frappe.get_doc(SITE, case.site, for_update=True)
	if row.status != "Removed":
		row.status = "Removed"
		row.removed_on = now
		row.save(ignore_permissions=True)
	# Spec 0013 AC-5: the only place the domain cleanup starts, in the transaction that removes the row; a
	# repeat changes nothing.
	from rarebooks_control.registry import start_domain_cleanup

	start_domain_cleanup(row.name)
	frappe.db.commit()


def _wait(case, reason: str):
	removal_logger().info(f"Removal: {case.name} waits: {reason}")
	frappe.db.commit()


def drop_refusal(case, settings) -> str | None:
	"""Why the case must not be dropped right now, or None when every spec 0007 AC-9 and AC-20 check passes."""
	now = signing.utc_now()
	if settings.mode != "live" or case.mode != "live":
		return "removal isn't live for this case"
	if settings.removal_paused:
		return "removal is paused"
	# A `Dropping` case was approved and counted when it started, so only the checks below are asked again.
	if case.status != "Dropping":
		if case.status != "Holding" or not case.hold_until or now < case.hold_until:
			return "the case isn't holding past its hold"
		if settings.require_approval and not case.approved_by:
			return "the case isn't approved"
	email = frappe.db.get_value(EMAIL_QUEUE, case.export_email, "status") if case.export_email else None
	if email != "Sent" or not case.export_sent_on:
		return "the export email isn't sent"
	try:
		size = export_storage.object_size(case.export_key)
	except Exception:
		frappe.log_error(f"Removal: R2 could not be asked about {case.name}")
		return "the export couldn't be checked in R2"
	if size is None or size != case.export_bytes:
		return "the export isn't in R2 at its recorded size"
	row = frappe.db.get_value(SITE, case.site, ["status", "time_zone"], as_dict=True)
	if not row or row.status != "Active":
		return "the site isn't active in the registry"
	state = billing_state(case.site)
	if paid_by_billing(case.site, site_date(now, row.time_zone)):
		return "the site is paid or exempt"
	if state["auto_renews"]:
		return "the site auto renews"
	if state["checkout_in_last_24h"]:
		return "the site has a checkout from the last 24 hours"
	return None


def fresh_report_refusal(site: str) -> str | None:
	"""Spec 0010 AC-6: the site's own fresh `removal_report`, in the same job, must still show it locked or lapsed,
	due, and with no payment pending."""
	try:
		answer = call_site(site, "removal_report")
	except SiteCallFailed:
		return "the site couldn't be asked for a fresh report"
	report = SiteReport.read(answer.body) if answer.status == OK else None
	if not report or not report.removal_on:
		return "the site's fresh report has no removal date"
	if report.phase not in DROP_PHASES:
		return "the site isn't locked or lapsed"
	if report.removal_on > report.today:
		return "the site's removal date hasn't come"
	if report.payment_pending is not False:
		return "the site has a payment pending"
	return None


def cloud_refusal(site: str, cloud: dict, settings) -> str | None:
	"""Spec 0010 AC-6 and spec 0011 AC-3: only a registered, Active site of the Books bench group, never the
	control site itself. An empty `group` fails."""
	if cloud.get("name") != site:
		return "Frappe Cloud answered for another site"
	if not settings.books_bench_group or cloud.get("group") != settings.books_bench_group:
		return "the site isn't on the Books bench group"
	if cloud.get("status") != frappe_cloud.ACTIVE:
		return f"the site is {cloud.get('status')} on Frappe Cloud"
	if site == frappe.local.site:
		return "the site is the control site"
	if not frappe.db.exists(SITE, site):
		return "the site isn't registered"
	return None


def drops_today() -> int:
	"""Cases whose drop started on the current UTC date, across all sites (spec 0010 AC-9). Discard cases don't
	count (spec 0014 AC-19)."""
	start = datetime.combine(signing.utc_now().date(), datetime.min.time())
	return frappe.db.count(CASE, {"drop_started_on": (">=", start), "kind": ("!=", DISCARD)})


# The Discard case (spec 0014 AC-19)


def open_discard(site: str, reason: str) -> str | None:
	"""Open a Discard case for a pool site or a failed sign up's site, and log why when none opens. The caller
	commits."""
	name, refusal = try_discard(site, reason)
	if refusal:
		frappe.log_error(f"Removal: {site} wasn't discarded: {refusal}", reason)
	return name


def try_discard(site: str, reason: str) -> tuple[str | None, str | None]:
	"""`(case, None)` once the site has a Discard case, or `(None, why)` when it gets none. It starts `Dropping`,
	so the hourly round polls it.

	An open Trial or Lapsed case is cancelled first under its row lock, whatever its mode and status, unless it
	is a drop under way (`Dropping`, or `archive_requested_on` set): that case ends the site itself, so no Discard
	opens and Retry waits for its Removed. An open Discard case is returned as it is (spec 0014 AC-19)."""
	row = frappe.db.get_value(SITE, site, ["owner_email", "site_url"], as_dict=True)
	if not row:
		return None, "the site isn't in the registry"
	owner = row.owner_email or frappe.conf.get("books_ops_email")
	if not owner:
		return None, "set books_ops_email"
	other = open_case(site)
	if other:
		case = frappe.get_doc(CASE, other, for_update=True)
		if case.status not in FINAL_STATUSES:
			if case.kind == DISCARD:
				return case.name, None
			if case.status == "Dropping" or case.archive_requested_on:
				return None, f"case {case.name} is a drop under way, so Retry waits for its Removed"
			cancel(case, _("Replaced by a discard"))
	now = signing.utc_now()
	case = frappe.get_doc(
		{
			"doctype": CASE,
			"site": site,
			"kind": DISCARD,
			"status": "Dropping",
			"mode": discard_mode(),
			"owner_email": owner,
			"site_url": row.site_url,
			"removal_on": now.date(),
			# Started by us, so spec 0013's 403 plus released name can prove its archive.
			"drop_started_on": now,
			"attempts": 0,
			"last_error": reason[:ERROR_CHARACTERS],
		}
	).insert(ignore_permissions=True)
	return case.name, None


def discard_mode() -> str:
	return frappe.db.get_single_value(ONBOARDING_SETTINGS, "discard_mode") or "dry_run"


def poll_discard(name: str):
	"""One hourly job for a Discard case: in dry run only the log line. Otherwise the archive proof, every drop
	guard that applies, the owner check, then the archive, which checks the owner once more."""
	case = frappe.get_doc(CASE, name)
	if case.status != "Dropping" or case.kind != DISCARD:
		return
	if case.mode != "live" or discard_mode() != "live":
		removal_logger().info(f"Removal: would discard {case.site}")
		return
	try:
		proof, cloud = site_archived(case.site, drop_started=True)
	except FrappeCloudError as error:
		drop_failed(frappe.get_doc(CASE, name, for_update=True), str(error))
		return
	case = frappe.get_doc(CASE, name, for_update=True)
	if case.status != "Dropping":
		frappe.db.commit()
		return
	if proof:
		mark_removed(case)
		log_proof(case, proof)
		return
	if case.archive_requested_on:
		frappe.db.commit()
		return
	reason = discard_refusal(case, removal_settings(), cloud)
	if reason:
		_wait(case, reason)
		return
	# The first owner check is made with no lock held; `archive_now` makes the second.
	frappe.db.commit()
	refusal = owner_refusal(case.site)
	case = frappe.get_doc(CASE, name, for_update=True)
	if case.status != "Dropping" or case.archive_requested_on:
		frappe.db.commit()
		return
	if refusal:
		_owner_refused(case, refusal)
		return
	archive_now(case)


def discard_refusal(case, settings, cloud: dict) -> str | None:
	"""Spec 0010 AC-6's guards less the export checks, the fresh report and the daily cap; any of Frappe Cloud's
	statuses for a site being built or broken passes (spec 0014 AC-19)."""
	if settings.removal_paused:
		return "removal is paused"
	row = frappe.db.get_value(SITE, case.site, ["status", "time_zone"], as_dict=True)
	if not row or row.status != "Active":
		return "the site isn't active in the registry"
	state = billing_state(case.site)
	if paid_by_billing(case.site, site_date(signing.utc_now(), row.time_zone)):
		return "the site is paid or exempt"
	if state["auto_renews"]:
		return "the site auto renews"
	if state["checkout_in_last_24h"]:
		return "the site has a checkout from the last 24 hours"
	if cloud.get("name") != case.site:
		return "Frappe Cloud answered for another site"
	if not settings.books_bench_group or cloud.get("group") != settings.books_bench_group:
		return "the site isn't on the Books bench group"
	if cloud.get("status") not in DISCARD_CLOUD_STATUSES:
		return f"the site is {cloud.get('status')} on Frappe Cloud"
	if case.site == frappe.local.site:
		return "the site is the control site"
	return None


def owner_refusal(site: str) -> tuple[bool, str] | None:
	"""None when the site may be discarded: its verified `claim_report` shows `owner_logged_in` false, or it is a
	Building site with no config yet to sign one. Otherwise `(logged_in, reason)`: a missing or unverified answer
	never counts as not logged in (key invariants)."""
	row = frappe.db.get_value(SITE, site, ["pool_status", "config_status"], as_dict=True)
	if row and row.pool_status == "Building" and row.config_status != "Active":
		return None
	try:
		answer = call_site(site, "claim_report")
	except SiteCallFailed as error:
		return False, str(error)
	logged_in = answer.body.get("owner_logged_in")
	if answer.status != OK or not isinstance(logged_in, bool):
		return False, f"claim_report answered HTTP {answer.status} without owner_logged_in"
	if logged_in:
		return True, "the owner has logged in"
	return None


def _owner_refused(case, refusal: tuple[bool, str]):
	"""An owner who has logged in stops the discard for good, with an ops email; a site that didn't answer counts
	one attempt, and the third fails the case (spec 0014 AC-19). Saves and commits."""
	logged_in, reason = refusal
	if logged_in:
		case.last_error = reason
		fail(case, _("The owner of {0} has logged in, so it isn't discarded.").format(case.site))
		case.save(ignore_permissions=True)
		frappe.db.commit()
		return
	drop_failed(case, reason)


def approval_required() -> bool:
	return bool(removal_settings().require_approval)


# Case changes shared by the round, the endpoints and the actions


def cancel(case, reason: str):
	"""The site paid or was made exempt, or a dry run gave way to live: delete the export and stop the link
	(spec 0007 AC-11, AC-14)."""
	if case.export_key:
		_delete_export(case.export_key, case.name)
	case.status = "Cancelled"
	case.link_token_hash = None
	case.last_error = reason
	case.save(ignore_permissions=True)


def fail(case, reason: str):
	"""`Failed` stops the case until a Removal Manager presses Retry, and tells the operator at once (AC-12).
	The caller saves."""
	case.status = "Failed"
	case.last_error = (case.last_error + "\n" if case.last_error else "") + reason
	removal_mail.ops_alert(
		_("Removal case {0} failed").format(case.name),
		[
			_("Site: {0}").format(case.site),
			reason,
			_("Nothing will be dropped until a Removal Manager presses Retry."),
		],
	)


def queue_export_email(case):
	"""A new link token, its hash on the case, and the export email holding the token (AC-7). The token is
	never stored, so a resend always makes a new one. The caller saves."""
	token = secrets.token_urlsafe(32)
	case.link_token_hash = token_hash(token)
	case.export_queued_on = signing.utc_now()
	case.export_sent_on = None
	case.hold_until = None
	case.link_expires_on = None
	case.export_email = removal_mail.send_export(case, download_url(token))


def download_url(token: str) -> str:
	return frappe.utils.get_url(f"/api/method/rarebooks_control.removal.download?token={token}")


def _verify_site(endpoint: str):
	"""The verified request and its row, or the 401 or 429 answer to send. Unknown and Removed sites get 401."""
	raw, body = signing.read_body()
	site = body.get("site") if body else None
	row = frappe.get_doc(SITE, site) if isinstance(site, str) and frappe.db.exists(SITE, site) else None
	if not row or row.status == "Removed":
		return signing.unauthorized()
	request = signing.verify_request(raw, body, signing.site_secrets(row))
	if not request:
		return signing.unauthorized(row.get_password("control_secret", raise_exception=False))
	if signing.over_limit(endpoint, row.name, SITE_CALL_LIMIT):
		return request.answer({"issued_at": request.issued_at}, TOO_MANY)
	return request, row


def _site_case(body: dict, site: str):
	"""The locked case named in the body, only when it is this site's, else None."""
	name = body.get("case")
	if not isinstance(name, str) or not frappe.db.exists(CASE, name):
		return None
	case = frappe.get_doc(CASE, name, for_update=True)
	return case if case.site == site else None


def _delete_export(key: str, case_name):
	"""Delete an export object. A failure is logged, not raised: the 90 day lifecycle rule still deletes it."""
	try:
		export_storage.delete_object(key)
	except Exception:
		frappe.log_error(f"Removal: the export of {case_name} could not be deleted from R2", key)


def _iso(value) -> str | None:
	"""Naive UTC as the ISO form the site's `mark_export_sent` takes and `removal_status` reports."""
	return value.replace(microsecond=0).isoformat() + "+00:00" if value else None


def _normal_iso(value) -> str | None:
	parsed = signing.parse_issued_at(value)
	return _iso(parsed) if parsed else None


def _date(value) -> date | None:
	if not isinstance(value, str) or not value:
		return None
	try:
		return date.fromisoformat(value)
	except ValueError:
		return None


def _https(value) -> str | None:
	return value.rstrip("/") if isinstance(value, str) and value.startswith("https://") else None


def _text(value) -> str | None:
	if not isinstance(value, str):
		return None
	return value.strip()[:140] or None
