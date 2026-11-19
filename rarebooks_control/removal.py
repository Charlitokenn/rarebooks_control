"""Runner calls from the hosting benches, and the removal cases they drive (spec 0007, Milestone 4).

Every runner call carries `bench` and is signed with that bench's runner secret; every answer is signed the
same way and echoes the request's `issued_at` (AC-15). The control site decides; the runner only carries out
the instructions in a signed answer. Nothing is dropped unless `drop_starting` moves the case to `Dropping`
after every AC-9 and AC-20 check passes in that same request.

The customer site is the only clock: the control site stores the `removal_on` and `today` it reports, and
never works out a removal date itself.
"""

import hashlib
import re
import secrets
from datetime import date, datetime, timedelta

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_days, getdate
from werkzeug.utils import redirect
from werkzeug.wrappers import Response

from rarebooks_control import export_storage, removal_mail, signing
from rarebooks_control.billing import billing_state, page
from rarebooks_control.push import mark_changed
from rarebooks_control.rarebooks_control.doctype.books_removal_case.books_removal_case import (
	FINAL_STATUSES,
)
from rarebooks_control.state import site_date

SITE = "Books Hosted Site"
BENCH = "Books Hosting Bench"
CASE = "Books Removal Case"
EMAIL_QUEUE = "Email Queue"
REMOVAL_MANAGER = "Removal Manager"
# Spec 0007: runner calls are limited per bench, since every bench calls from the hosting server's one IP.
RUNNER_LIMIT = 120
CONFLICT, TOO_MANY, BAD_REQUEST, NOT_FOUND = 409, 429, 400, 404
MODES = ("dry_run", "live")
MAX_SITES = 200
ERROR_CHARACTERS = 2000
# Warning offsets in days before `removal_on`, by kind (AC-4).
WARNING_OFFSETS = {"Trial": (4, 2, 1), "Lapsed": (14, 3, 1)}
# Spec 0005: a site is in grace for 7 days after `paid_until`, and is still a paying site then.
GRACE_DAYS = 7
EXPORT_AFTER_WARNING = timedelta(hours=24)
HOLD = timedelta(hours=72)
LINK_LIFETIME = timedelta(days=30)
EXPORT_KEPT_DAYS = 90
CLAIM_TIMEOUT = timedelta(hours=6)
EMAIL_SEND_TIMEOUT = timedelta(hours=24)
MAX_ATTEMPTS = 3
# AC-11: no checkout starts in the last hour before `hold_until`, so no payment starts while a drop can.
CHECKOUT_FREEZE = timedelta(hours=1)
DEFAULT_MAX_DROPS_PER_DAY = 5
ERROR_RUNS_ALERT_AT = 3
EXPORT_KEY = re.compile(r"^exports/(?P<case>[A-Za-z0-9-]+)/[0-9a-f-]{36}\.zip$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STEPS = ("export", "upload", "drop")


# Runner endpoints


@frappe.whitelist(allow_guest=True, methods=["POST"])
def report_sites() -> Response:
	"""The bench's hourly report: open, update, warn, cancel and advance its cases, and answer with what the
	runner must do (AC-3 to AC-5, AC-8, AC-11, AC-14). One bad site entry never fails the whole call."""
	verified = _verify_runner("report_sites")
	if not isinstance(verified, tuple):
		return verified
	request, bench = verified
	body, echo = request.body, {"issued_at": request.issued_at}
	mode, sites, deferred = body.get("mode"), body.get("sites"), body.get("deferred") or []
	if mode not in MODES or not isinstance(sites, list) or len(sites) > MAX_SITES:
		return request.answer(echo, BAD_REQUEST)
	if not isinstance(deferred, list):
		deferred = []

	report = Report(bench, mode)
	report.start()
	report.take_deferred(deferred)
	for entry in sites:
		report.take_site(entry)
	instructions = report.instructions + rotation_instructions(bench.name)
	return request.answer({**echo, "instructions": instructions})


@frappe.whitelist(allow_guest=True, methods=["POST"])
def export_ready() -> Response:
	"""The runner uploaded the export: record it and queue the export email with a fresh link (AC-7)."""
	verified = _verify_runner("export_ready")
	if not isinstance(verified, tuple):
		return verified
	request, bench = verified
	body, echo = request.body, {"issued_at": request.issued_at}
	key, size, digest = body.get("key"), body.get("bytes"), body.get("sha256")
	match = EXPORT_KEY.match(key) if isinstance(key, str) else None
	if (
		not match
		or match["case"] != body.get("case")
		or not isinstance(size, int)
		or isinstance(size, bool)
		or size <= 0
		or not (isinstance(digest, str) and SHA256.match(digest))
	):
		return request.answer(echo, BAD_REQUEST)

	case = _bench_case(body, bench.name)
	if case and case.export_key == key:
		# A repeat of a call already recorded: its email is queued, and its object is the case's export.
		return request.answer({**echo, "status": case.status})
	if not case or case.status != "Exporting":
		# The upload belongs to no export the control site is waiting for, so it doesn't keep it (AC-7).
		_delete_export(key, case.name if case else body.get("case"))
		return request.answer({**echo, "status": case.status if case else "unknown"}, CONFLICT)

	if case.export_key and case.export_key != key:
		_delete_export(case.export_key, case.name)
	case.update(
		{
			"export_key": key,
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
	"""A failed export, upload or drop: retried on the next runs, `Failed` after the third (AC-12)."""
	verified = _verify_runner("step_failed")
	if not isinstance(verified, tuple):
		return verified
	request, bench = verified
	body, echo = request.body, {"issued_at": request.issued_at}
	if body.get("step") not in STEPS:
		return request.answer(echo, BAD_REQUEST)
	case = _bench_case(body, bench.name)
	if not case:
		return request.answer({**echo, "status": "unknown"}, CONFLICT)
	if case.status in FINAL_STATUSES:
		return request.answer({**echo, "status": case.status}, CONFLICT)

	error = body.get("error")
	case.last_error = (error if isinstance(error, str) else "")[:ERROR_CHARACTERS]
	# A drop can fail before `drop_starting` (no root login, a site it can't read), so the case is still
	# `Holding`. It counts too, or a broken runner would retry every hour with no alert (AC-12).
	if case.status in ("Exporting", "Dropping") or (case.status == "Holding" and body["step"] == "drop"):
		case.attempts = (case.attempts or 0) + 1
		if case.attempts >= MAX_ATTEMPTS:
			fail(case, _("The {0} step failed {1} times.").format(body["step"], case.attempts))
		elif case.status == "Exporting":
			case.status = "Scheduled"
			case.export_claimed_on = None
	case.save(ignore_permissions=True)
	return request.answer({**echo, "status": case.status})


@frappe.whitelist(allow_guest=True, methods=["POST"])
def drop_starting() -> Response:
	"""The last go ahead before a drop: every AC-9 and AC-20 check, in this one request (AC-9, AC-20)."""
	verified = _verify_runner("drop_starting")
	if not isinstance(verified, tuple):
		return verified
	request, bench = verified
	echo = {"issued_at": request.issued_at}
	# The bench row lock serializes drops of one bench, so two calls can't both pass the daily cap.
	bench = frappe.get_doc(BENCH, bench.name, for_update=True)
	case = _bench_case(request.body, bench.name)
	reason = drop_refusal(case, bench) if case else "unknown case"
	if reason:
		return request.answer({**echo, "proceed": False, "reason": reason})
	if case.status != "Dropping":
		case.status = "Dropping"
		case.drop_started_on = signing.utc_now()
		case.attempts = 0
		case.save(ignore_permissions=True)
	return request.answer({**echo, "proceed": True, "reason": None})


@frappe.whitelist(allow_guest=True, methods=["POST"])
def drop_done() -> Response:
	"""The site is gone: the case is `Removed` and the registry row is marked removed (AC-10)."""
	verified = _verify_runner("drop_done")
	if not isinstance(verified, tuple):
		return verified
	request, bench = verified
	echo = {"issued_at": request.issued_at}
	case = _bench_case(request.body, bench.name)
	if not case or case.status != "Dropping":
		return request.answer({**echo, "status": case.status if case else "unknown"}, CONFLICT)
	now = signing.utc_now()
	case.status = "Removed"
	case.removed_on = now
	case.save(ignore_permissions=True)
	row = frappe.get_doc(SITE, case.site, for_update=True)
	row.status = "Removed"
	row.removed_on = now
	row.save(ignore_permissions=True)
	return request.answer({**echo, "status": case.status})


@frappe.whitelist(allow_guest=True, methods=["POST"])
def secret_rotated() -> Response:
	"""The bench wrote the pending secret into the site's config: swap it in and push (spec 0008 AC-15).

	From here on only the new secret works. The push is signed with it; if the site's web workers still hold
	the old config (up to 60 seconds), the push retries cover it (AC-13). Safe to repeat: a second call finds
	no rotation pending and gets 409.
	"""
	verified = _verify_runner("secret_rotated")
	if not isinstance(verified, tuple):
		return verified
	request, bench = verified
	echo = {"issued_at": request.issued_at}
	row = _rotating_site(request.body.get("site"), bench.name)
	if not row:
		return request.answer({**echo, "status": "no_rotation"}, CONFLICT)
	row.control_secret = row.get_password("pending_secret")
	row.pending_secret = None
	# A rotation is a state change: the site hears from the control site with the new secret (AC-13).
	mark_changed(row)
	row.save(ignore_permissions=True)
	return request.answer({**echo, "site": row.name, "status": "rotated"})


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


# Removal Manager actions (AC-13)


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
	"""Back to `Holding` if the case has an export, else to `Scheduled`."""
	doc = _managed_case(case, ("Paused",))
	doc.status = "Holding" if doc.export_key else "Scheduled"
	doc.paused_by = None
	doc.pause_reason = None
	doc.save(ignore_permissions=True)
	return {"status": doc.status}


@frappe.whitelist(methods=["POST"])
def retry_case(case: str) -> dict:
	"""Reopen a `Failed` case with `attempts` at 0. An export whose email never went out is sent again with a
	new link, and the hold starts from the new sent time (AC-13)."""
	doc = _managed_case(case, ("Failed",))
	doc.attempts = 0
	doc.paused_by = None
	doc.pause_reason = None
	if doc.export_key and not doc.export_sent_on:
		queue_export_email(doc)
		doc.status = "Holding"
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


# The report


class Report:
	"""One `report_sites` call from one bench: its cases, and the instructions they give."""

	def __init__(self, bench, mode: str):
		self.bench, self.mode = bench, mode
		self.now = signing.utc_now()
		self.instructions: list[dict] = []

	def start(self):
		"""Note the report on the bench. The first `live` report cancels every open `dry_run` case (AC-14)."""
		if self.mode == "live":
			for name in frappe.get_all(
				CASE,
				filters={
					"bench": self.bench.name,
					"mode": "dry_run",
					"status": ("not in", (*FINAL_STATUSES, "Dropping")),
				},
				pluck="name",
			):
				cancel(frappe.get_doc(CASE, name, for_update=True), _("Live removal started on the bench."))
		frappe.db.set_value(
			BENCH,
			self.bench.name,
			{"last_report_on": self.now, "last_mode": self.mode},
			update_modified=False,
		)

	def take_deferred(self, names: list):
		"""Exports the runner put off for its per run cap go back to `Scheduled` with no attempt counted, so
		this same report can send them again (AC-12)."""
		for name in names:
			if not isinstance(name, str) or not frappe.db.exists(CASE, name):
				continue
			case = frappe.get_doc(CASE, name, for_update=True)
			if case.bench == self.bench.name and case.status == "Exporting" and not case.export_key:
				case.status = "Scheduled"
				case.export_claimed_on = None
				case.save(ignore_permissions=True)

	def take_site(self, entry):
		"""One site of the report, inside a savepoint, so a bad entry is logged and skipped (AC-15)."""
		site = entry.get("site") if isinstance(entry, dict) else None
		frappe.db.savepoint("removal_site")
		try:
			self.take_entry(entry)
		except Exception:
			frappe.db.rollback(save_point="removal_site")
			frappe.log_error(f"Removal: report entry for {site!r} from bench {self.bench.name} skipped")

	def take_entry(self, entry):
		site = entry.get("site") if isinstance(entry, dict) else None
		row = self.bench_site(site)
		if not row:
			frappe.log_error(
				f"Removal: bench {self.bench.name} reported a site it doesn't host",
				repr(site)[:ERROR_CHARACTERS],
			)
			return
		name = open_case(row.name)
		case = frappe.get_doc(CASE, name, for_update=True) if name else None
		if case and case.mode != self.mode:
			# A dry run never touches a live case, and a live report has already cancelled the dry run ones.
			return
		if "error" in entry:
			self.note_error(case, entry["error"])
			return

		report = SiteReport.read(entry)
		if not report:
			raise ValueError("bad removal status")
		if case:
			case.error_runs = 0
		if keeps_site(report, billing_state(row.name)):
			if case and case.status != "Dropping":
				cancel(case, _("The site is paid or exempt."))
			self.clear_if_marked(row.name, report, None)
			return
		if not case:
			case = self.new_case(row, report)
		self.update(case, report)
		self.advance(case, report)
		case.save(ignore_permissions=True)
		self.clear_if_marked(row.name, report, case)

	def bench_site(self, site):
		"""The registry row, only for an active site that the registry places on this bench (AC-15)."""
		if not isinstance(site, str) or not site:
			return None
		row = frappe.db.get_value(
			SITE, site, ["name", "hosting_bench", "status", "owner_email", "site_url"], as_dict=True
		)
		if not row or row.hosting_bench != self.bench.name or row.status != "Active":
			return None
		return row

	def note_error(self, case, error):
		"""A site the runner couldn't read. Three in a row alert the operator once (AC-21)."""
		if not case:
			return
		case.error_runs = (case.error_runs or 0) + 1
		case.last_error = (error if isinstance(error, str) else "")[:ERROR_CHARACTERS]
		case.save(ignore_permissions=True)
		if case.error_runs == ERROR_RUNS_ALERT_AT:
			removal_mail.ops_alert(
				_("Site {0} could not be read for removal").format(case.site),
				[
					_("The runner reported an error for {0} on {1} runs in a row.").format(
						case.site, case.error_runs
					),
					_("Case: {0}").format(case.name),
					_("Last error: {0}").format(case.last_error),
				],
			)

	def new_case(self, row, report):
		if not row.owner_email:
			raise ValueError(f"site {row.name} has no owner_email")
		return frappe.get_doc(
			{
				"doctype": CASE,
				"site": row.name,
				"bench": self.bench.name,
				"mode": self.mode,
				"kind": report.kind,
				"removal_on": report.removal_on,
				"status": "Scheduled",
				"owner_email": row.owner_email,
				"site_url": report.site_url or row.site_url,
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
			if self.export_due(case, report):
				case.status = "Exporting"
				case.export_claimed_on = self.now
				self.add(case, "export")
		elif case.status == "Exporting":
			self.check_exporting(case)
		elif case.status == "Holding":
			self.check_holding(case, report)
		elif case.status == "Dropping":
			# A drop is safe to repeat, so every report sends it again until `drop_done` (AC-10).
			self.add(case, "drop")

	def warn(self, case, report):
		"""Send the latest due warning, if it hasn't gone yet (AC-4)."""
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
		"""AC-5: the site's today has reached `removal_on`, a warning went out 24 hours ago or more, and the
		bench isn't paused. The case being `Scheduled` and the site unpaid were checked by the caller."""
		if self.bench.removal_paused or report.today < getdate(case.removal_on):
			return False
		sent = [row.sent_on for row in case.warnings if row.sent_on]
		return bool(sent) and self.now - min(sent) >= EXPORT_AFTER_WARNING

	def check_exporting(self, case):
		if case.export_key:
			self.check_export_email(case)
			return
		if case.export_claimed_on and self.now - case.export_claimed_on >= CLAIM_TIMEOUT:
			# No word for 6 hours counts one attempt, and the export is sent again (AC-12).
			case.attempts = (case.attempts or 0) + 1
			if case.attempts >= MAX_ATTEMPTS:
				fail(case, _("No export arrived in {0} attempts.").format(case.attempts))
				return
			if self.bench.removal_paused:
				case.status = "Scheduled"
				case.export_claimed_on = None
				return
			case.export_claimed_on = self.now
			self.add(case, "export")

	def check_holding(self, case, report):
		if not case.export_sent_on:
			# Only after a Retry of a case whose export email failed: the hold waits for the new email.
			self.check_export_email(case)
			return
		if self.mode == "live" and report.removal_hold_until != _iso(case.hold_until):
			self.add_mark_sent(case)
		if self.drop_may_start(case):
			self.add(case, "drop")

	def check_export_email(self, case):
		"""The hold starts once the export email is `Sent`; one in `Error`, or unsent after 24 hours, fails
		the case (AC-8, AC-12)."""
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
			if self.mode == "live":
				self.add_mark_sent(case)
			return
		if not email or email.status == "Error":
			fail(case, _("The export email could not be sent."))
		elif case.export_queued_on and self.now - case.export_queued_on >= EMAIL_SEND_TIMEOUT:
			fail(case, _("The export email was still not sent 24 hours after it was queued."))

	def drop_may_start(self, case) -> bool:
		"""Send `drop` only once the hold has passed and the case is approved (when required). The rest of
		AC-9 and AC-20 is checked again in `drop_starting`, which alone can start a drop."""
		if self.now < case.hold_until or self.bench.removal_paused:
			return False
		return bool(case.approved_by) or not approval_required()

	def add_mark_sent(self, case):
		self.add(
			case,
			"mark_sent",
			export_sent_on=_iso(case.export_sent_on),
			removal_hold_until=_iso(case.hold_until),
		)

	def clear_if_marked(self, site: str, report, case):
		"""The site still shows the hold fields but has no case holding it: the runner empties them (AC-11)."""
		if not (report.export_sent_on or report.removal_hold_until):
			return
		if case and case.status in ("Holding", "Dropping", "Paused") and case.export_sent_on:
			return
		latest = (
			case.name if case else frappe.db.get_value(CASE, {"site": site}, "name", order_by="creation desc")
		)
		if latest:
			self.instructions.append({"site": site, "case": latest, "action": "clear"})

	def add(self, case, action: str, **values):
		self.instructions.append({"site": case.site, "case": case.name, "action": action, **values})


class SiteReport:
	"""The fields of one `removal_status()` entry that the control site uses, checked."""

	FIELDS = ("today", "removal_on", "kind", "paid_until", "exempt", "phase")

	def __init__(self, entry: dict):
		self.today = _date(entry.get("today"))
		self.removal_on = _date(entry.get("removal_on"))
		self.paid_until = _date(entry.get("paid_until"))
		self.kind = entry.get("kind") if entry.get("kind") in WARNING_OFFSETS else None
		self.exempt = bool(entry.get("exempt"))
		self.site_url = _https(entry.get("site_url"))
		self.language = _text(entry.get("language"))
		self.company_name = _text(entry.get("company_name"))
		self.export_sent_on = entry.get("export_sent_on") or None
		self.removal_hold_until = _normal_iso(entry.get("removal_hold_until"))

	@classmethod
	def read(cls, entry: dict):
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


def drop_refusal(case, bench) -> str | None:
	"""Why the case must not be dropped right now, or None when every AC-9 and AC-20 check passes."""
	now = signing.utc_now()
	if case.mode != "live":
		return "the case was opened in dry_run"
	if bench.removal_paused:
		return "removal is paused for the bench"
	# A drop cut short is repeated (AC-10): it was approved and counted when it started, so only the
	# checks below, which can change, are asked again.
	if case.status != "Dropping":
		if case.status != "Holding" or not case.hold_until or now < case.hold_until:
			return "the case isn't holding past its hold"
		if approval_required() and not case.approved_by:
			return "the case isn't approved"
		if drops_today(bench.name) >= max_drops_per_day():
			return "the bench reached today's drop cap"
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
	today = site_date(now, row.time_zone)
	if state["exempt"] or (state["paid_until"] and state["paid_until"] + timedelta(days=GRACE_DAYS) >= today):
		return "the site is paid or exempt"
	if state["auto_renews"]:
		return "the site auto renews"
	if state["checkout_in_last_24h"]:
		return "the site has a checkout from the last 24 hours"
	return None


def drops_today(bench: str) -> int:
	"""Cases of the bench whose drop started on the current UTC date (AC-20)."""
	start = datetime.combine(signing.utc_now().date(), datetime.min.time())
	return frappe.db.count(CASE, {"bench": bench, "drop_started_on": (">=", start)})


def approval_required() -> bool:
	return bool(frappe.conf.get("books_removal_require_approval", True))


def max_drops_per_day() -> int:
	value = frappe.conf.get("books_removal_max_drops_per_day", DEFAULT_MAX_DROPS_PER_DAY)
	return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


# Case changes shared by the report, the endpoints and the actions


def cancel(case, reason: str):
	"""The site paid or was made exempt, or a dry run gave way to live: delete the export and stop the link
	(AC-11, AC-14)."""
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


def rotation_instructions(bench: str) -> list[dict]:
	"""A `rotate_secret` instruction for each active site of the bench with a rotation pending (spec 0008
	AC-15). `report_sites` adds these to its answer, the one place a secret is sent, and only to the bench
	that hosts the site, in an answer signed with that bench's runner secret.
	"""
	rows = frappe.get_all(
		SITE,
		filters={"hosting_bench": bench, "status": "Active", "pending_secret": ("is", "set")},
		pluck="name",
		order_by="name asc",
	)
	return [
		{
			"site": name,
			"action": "rotate_secret",
			"secret": frappe.get_doc(SITE, name).get_password("pending_secret"),
		}
		for name in rows
	]


def _verify_runner(endpoint: str):
	"""The verified request and its bench, or the 401 or 429 answer to send (AC-15)."""
	raw, body = signing.read_body()
	bench = body.get("bench") if body else None
	secret = signing.runner_secret(bench)
	request = signing.verify_request(raw, body, [secret])
	if not request:
		return signing.unauthorized(secret)
	if signing.over_limit(endpoint, bench, RUNNER_LIMIT):
		return request.answer({"issued_at": request.issued_at}, TOO_MANY)
	if not frappe.db.exists(BENCH, bench):
		frappe.get_doc({"doctype": BENCH, "bench_name": bench}).insert(
			ignore_permissions=True, ignore_if_duplicate=True
		)
	return request, frappe.get_doc(BENCH, bench)


def _bench_case(body: dict, bench: str):
	"""The locked case named in the body, only when it is of this bench and for the site named, else None."""
	name, site = body.get("case"), body.get("site")
	if not isinstance(name, str) or not frappe.db.exists(CASE, name):
		return None
	case = frappe.get_doc(CASE, name, for_update=True)
	if case.bench != bench or (site is not None and case.site != site):
		return None
	return case


def _rotating_site(site, bench: str):
	"""The locked row of an active site on this bench with a rotation pending, else None. A site on another
	bench gets the same answer as one with no rotation, so a bench learns nothing about other benches."""
	if not isinstance(site, str) or not frappe.db.exists(SITE, site):
		return None
	row = frappe.get_doc(SITE, site, for_update=True)
	if row.hosting_bench != bench or row.status != "Active" or not row.pending_secret:
		return None
	return row


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
