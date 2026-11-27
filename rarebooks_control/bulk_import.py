"""Bulk import into one hosted site from the control site (spec 0025).

A Books Bulk Import holds a grid of rows for one site and one document type. The site owns the columns, the
import CSV, the dry run and the import, through four signed calls (`import_columns`, `preview_import`,
`start_import`, `import_report` in `frappe_books.subscription`). This module owns the eligibility rule, the
structural checks, the attempts and the history.

`check`, `send` and `refresh` take the row with `for_update` only to read and to write: the lock is never held
across a call to the site (the same rule as `state.py`). Status changes only here, through `_write`, which
saves with `flags.bulk_import_write` so the controller lets the server fields through.
"""

import hashlib
import json
from datetime import timedelta

import frappe
from frappe import _

from rarebooks_control import alerts, removal, signing
from rarebooks_control.site_calls import SiteCallFailed, call_site

DOCTYPE = "Books Bulk Import"
SITE = "Books Hosted Site"
SIGNUP = "Books Signup"
BILLING_MANAGER = "Billing Manager"
NEVER_SUBMITTED = {"Books Expense"}

MAX_ROWS = 2000
MAX_BODY_BYTES = 2 * 1024 * 1024
# A send waiting on the site this long no longer blocks another (AC-8).
SEND_WAIT = timedelta(minutes=2)
# An import with no progress this long is Failed (AC-11).
STALL = timedelta(hours=2)
RETENTION = timedelta(days=30)
COLUMNS_CACHE_SECONDS = 120

FINAL = ("Success",)
FINISHED = ("Success", "Partial Success", "Error", "Timed Out")
UNAUTHORIZED, BAD_REQUEST, NOT_FOUND, CONFLICT, TOO_LARGE, UNAVAILABLE = 401, 400, 404, 409, 413, 503


class Refusal(Exception):
	"""A site answer or a site state that refuses the call, with a translated reason."""


# Who may pick which site (AC-2)


def eligibility_refusal(site: str | None) -> str | None:
	"""Why a site can't take an import, or None when it can (AC-2)."""
	if not site or not frappe.db.exists(SITE, site):
		return _("Pick a hosted site.")
	row = frappe.db.get_value(
		SITE, site, ["status", "config_status", "owner_email", "pool_status"], as_dict=True
	)
	if row.status != "Active" or row.config_status != "Active":
		return _("Site {0} is not Active.").format(site)
	if not row.owner_email:
		return _("Site {0} has no owner yet.").format(site)
	if row.pool_status not in (None, "", "Claimed"):
		return _("Site {0} is a pool site no one has claimed.").format(site)
	signup = frappe.db.get_value(SIGNUP, {"hosted_site": site}, "status")
	if signup and signup != "Ready":
		return _("The sign up for site {0} is not Ready.").format(site)
	if removal.open_case(site):
		return _("Site {0} has an open removal case.").format(site)
	return None


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def site_query(doctype: str, txt: str, searchfield: str, start: int, page_len: int, filters: dict) -> list:
	"""The eligible sites for the site picker (AC-2)."""
	frappe.only_for(BILLING_MANAGER)
	candidates = frappe.get_all(
		SITE,
		filters={"status": "Active", "config_status": "Active", "owner_email": ("is", "set")},
		or_filters={"site": ("like", f"%{txt}%"), "owner_email": ("like", f"%{txt}%")},
		fields=["name", "owner_email"],
		order_by="name asc",
	)
	eligible = [[row.name, row.owner_email] for row in candidates if not eligibility_refusal(row.name)]
	return eligible[int(start) : int(start) + int(page_len)]


# Columns (AC-3)


@frappe.whitelist(methods=["POST"])
def load_columns(site: str) -> dict:
	"""The types the site imports, with their columns, for the type picker (AC-3). Writes nothing."""
	frappe.only_for(BILLING_MANAGER)
	refusal = eligibility_refusal(site)
	if refusal:
		frappe.throw(refusal)
	return site_columns(site)


@frappe.whitelist(methods=["POST"])
def reload_columns(name: str) -> dict:
	"""Fetch the columns again, keep cells by column key, and name any column that is gone (AC-3)."""
	frappe.only_for(BILLING_MANAGER)
	doc = _editable(name)
	answer = site_columns(doc.hosted_site, fresh=True)
	kind = _type_from(answer, doc.import_type)
	if not kind:
		frappe.throw(_("Site {0} no longer imports {1}.").format(doc.hosted_site, doc.import_type))
	columns = _columns_value(answer, kind)
	keys = {column["key"] for column in kind["columns"]}
	old = json.loads(doc.columns or "{}").get("columns") or []
	gone = [column["label"] for column in old if column["key"] not in keys]
	rows = [
		{**row, "cells": {key: value for key, value in row["cells"].items() if key in keys}}
		for row in _rows(doc)
	]

	def apply(row):
		row.columns = json.dumps(columns)
		row.columns_fetched_on = signing.utc_now()
		row.rows = json.dumps(rows)
		row.status = "Draft"

	doc = _write(name, apply)
	return {"columns": columns, "gone": gone, "rows": rows, "status": doc.status}


def site_columns(site: str, fresh: bool = False) -> dict:
	"""The site's `import_columns` answer, cached briefly so the picker and the save fetch it once."""
	key = f"books_bulk_import_columns:{site}"
	if not fresh:
		cached = frappe.cache.get_value(key)
		if cached:
			return cached
	answer = _call(site, "import_columns", {})
	if answer.status == CONFLICT and answer.body.get("status") == "site_locked":
		frappe.throw(_("Site {0} is locked, so it imports nothing until it is paid.").format(site))
	if answer.status != 200:
		frappe.throw(_site_error(answer.status))
	value = {key: answer.body.get(key) for key in ("types", "date_format", "number_format")}
	frappe.cache.set_value(key, value, expires_in_sec=COLUMNS_CACHE_SECONDS)
	return value


def fill_columns(doc):
	"""On save of a new import, or one whose site or type changed: the columns for its type (AC-3)."""
	answer = site_columns(doc.hosted_site)
	kind = _type_from(answer, doc.import_type)
	if not kind:
		frappe.throw(_("Site {0} doesn't import {1}.").format(doc.hosted_site, doc.import_type))
	doc.columns = json.dumps(_columns_value(answer, kind))
	doc.columns_fetched_on = signing.utc_now()


# The dry run (AC-5, AC-6, AC-12)


@frappe.whitelist(methods=["POST"])
def check(name: str) -> dict:
	"""Structural checks here, then the site's dry run. Checked only with no blocking warning (AC-5, AC-6)."""
	frappe.only_for(BILLING_MANAGER)
	doc = _editable(name)
	if doc.status == "Importing":
		frappe.throw(_("This import is running. Wait for it to finish."))
	refusal = eligibility_refusal(doc.hosted_site)
	if refusal:
		_write(name, lambda row: _refuse(row, refusal))
		frappe.throw(refusal)
	rows = sendable_rows(doc)
	_structural_check(doc, rows)
	expected = checked_hash(doc)

	try:
		answer = _call(doc.hosted_site, "preview_import", {"import_type": doc.import_type, "rows": rows})
	except SiteCallFailed:
		_write(name, lambda row: _error(row, _("The site couldn't be reached. Try again.")))
		frappe.throw(_("The site couldn't be reached. Try again."))
	if answer.status == CONFLICT and answer.body.get("status") == "site_locked":
		reason = _("Site {0} is locked, so it imports nothing until it is paid.").format(doc.hosted_site)
		_write(name, lambda row: _refuse(row, reason))
		return {"status": "Refused", "warnings": []}
	if answer.status != 200:
		_answer_failed(name, doc.hosted_site, "preview_import", answer.status)

	warnings = answer.body.get("warnings") or []
	blocking = any(warning.get("blocking") for warning in warnings)

	def apply(row):
		if checked_hash(row) != expected:
			# The rows changed while the site checked them, so this check proves nothing about them.
			return
		row.last_error = None
		if blocking:
			row.status, row.checked_hash = "Draft", None
		else:
			row.status, row.checked_hash, row.checked_on = "Checked", expected, signing.utc_now()

	doc = _write(name, apply)
	return {"status": doc.status, "warnings": warnings}


def _structural_check(doc, rows: list[dict]):
	"""At least one row, a name in every row, only the site's column keys, and the caps (AC-5, AC-12)."""
	all_rows = _rows(doc)
	if len(all_rows) > MAX_ROWS:
		frappe.throw(_("An import holds at most {0} rows. Split this one.").format(MAX_ROWS))
	if len(signing.dumps({"rows": rows})) > MAX_BODY_BYTES:
		frappe.throw(_("These rows are over 2 MB. Split this import."))
	if not rows:
		frappe.throw(_("There are no rows to import."))
	columns = _columns(doc)
	keys = {column["key"] for column in columns.get("columns") or []}
	name_key = columns.get("name_key") or "name"
	for position, row in enumerate(rows, start=1):
		unknown = [key for key in row["cells"] if key not in keys]
		if unknown:
			frappe.throw(
				_("Row {0} has a column the site doesn't give. Reload the columns.").format(position)
			)
		if not (row["cells"].get(name_key) or "").strip():
			frappe.throw(_("Row {0} has no name. The name groups a document's rows.").format(position))


# Send (AC-7, AC-8, AC-10, AC-11)


@frappe.whitelist(methods=["POST"])
def send(name: str) -> dict:
	"""Start the import on the picked site as the next attempt, or adopt an unconfirmed one (AC-7, AC-8)."""
	frappe.only_for(BILLING_MANAGER)
	doc = _editable(name)
	if doc.status != "Checked" or checked_hash(doc) != doc.checked_hash:
		frappe.throw(_("Check the rows first. Import is only open once the check passes."))
	now = signing.utc_now()
	if doc.sending_since and now - doc.sending_since < SEND_WAIT:
		frappe.throw(_("A send is already waiting on the site. Try again in a minute."))
	refusal = eligibility_refusal(doc.hosted_site)
	if refusal:
		_write(name, lambda row: _refuse(row, refusal))
		return {"status": "Refused", "site_import": doc.site_import}

	unconfirmed = bool(doc.attempt) and doc.site_import != site_import_name(name, doc.attempt)
	attempt = doc.attempt if unconfirmed else doc.attempt + 1

	def claim(row):
		# Again under the row lock, so two sends at once can't both go.
		if (row.sending_since and now - row.sending_since < SEND_WAIT) or row.status != "Checked":
			frappe.throw(_("A send is already waiting on the site. Try again in a minute."))
		row.sending_since = now
		row.attempt = attempt

	_write(name, claim)
	ref = f"{name}-{attempt}"
	if unconfirmed and _adopt(name, doc.hosted_site, attempt):
		return _sent_answer(name)

	rows = sendable_rows(doc)
	payload = {
		"import_ref": ref,
		"import_type": doc.import_type,
		"submit": submit_flag(doc),
		"rows": rows,
	}
	try:
		answer = _call(doc.hosted_site, "start_import", payload)
	except SiteCallFailed:
		# The site may have started it; the next send looks it up before sending again (AC-8).
		_write(name, lambda row: _error(row, _("The site didn't answer. Send again to look it up.")))
		return _sent_answer(name)

	status = answer.body.get("status")
	if answer.status == 200 and answer.body.get("data_import") == site_import_name(name, attempt):
		_write(name, lambda row: _confirm(row, attempt))
	elif answer.status == CONFLICT and status == "site_locked":
		reason = _("Site {0} is locked, so it imports nothing until it is paid.").format(doc.hosted_site)
		_write(name, lambda row: _refuse(row, reason))
	elif answer.status == CONFLICT and status == "ref_conflict":
		_write(name, lambda row: _error(row, _("The site holds a different version of this attempt.")))
		_adopt(name, doc.hosted_site, attempt)
	elif answer.status == UNAVAILABLE:
		_write(name, lambda row: _error(row, _("The site can't run imports right now. Try again later.")))
	else:
		_answer_failed(name, doc.hosted_site, "start_import", answer.status, sending=True)
	return _sent_answer(name)


def site_import_name(name: str, attempt: int) -> str:
	return f"CTRL-{name}-{attempt}"


def _adopt(name: str, site: str, attempt: int) -> bool:
	"""Adopt an attempt the site already started, instead of sending it again (AC-8)."""
	try:
		answer = _call(site, "import_report", {"data_import": site_import_name(name, attempt)})
	except SiteCallFailed:
		return False
	if answer.status != 200:
		return False
	_write(name, lambda row: _confirm(row, attempt))
	return True


def _confirm(row, attempt: int):
	now = signing.utc_now()
	row.status = "Importing"
	row.attempt = attempt
	row.site_import = site_import_name(row.name, attempt)
	row.sent_on, row.last_progress_on, row.finished_on = now, now, None
	row.sending_since, row.last_error, row.site_processed = None, None, 0


def _sent_answer(name: str) -> dict:
	doc = frappe.get_doc(DOCTYPE, name)
	return {"status": doc.status, "site_import": doc.site_import, "last_error": doc.last_error}


# Refresh (AC-9, AC-11)


@frappe.whitelist(methods=["POST"])
def refresh(name: str) -> dict:
	"""Read the site's report for an Importing import and write the results per row (AC-9)."""
	frappe.only_for(BILLING_MANAGER)
	if not frappe.db.exists(DOCTYPE, name):
		frappe.throw(_("No bulk import {0}.").format(name), frappe.DoesNotExistError)
	doc = frappe.get_doc(DOCTYPE, name)
	doc.check_permission("read")
	if doc.rows_cleared_on:
		frappe.throw(_("This import's rows were cleared, so it is final."))
	_refresh(name)
	doc = frappe.get_doc(DOCTYPE, name)
	return {
		"status": doc.status,
		"success_count": doc.success_count,
		"failed_count": doc.failed_count,
		"results": json.loads(doc.results or "{}"),
		"last_error": doc.last_error,
	}


def refresh_job(name: str):
	"""The sweep's job for one import, so one slow site never holds up the others (AC-9)."""
	_refresh(name)


def sweep():
	"""Every 5 minutes: queue a refresh for every import still Importing, one job per import (AC-9)."""
	for name in frappe.get_all(DOCTYPE, filters={"status": "Importing"}, pluck="name"):
		frappe.enqueue(
			"rarebooks_control.bulk_import.refresh_job",
			queue="short",
			job_id=f"books-bulk-import-refresh:{name}",
			deduplicate=True,
			name=name,
		)


def _refresh(name: str):
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.status != "Importing" or not doc.site_import or doc.rows_cleared_on:
		return
	try:
		report, logs = _read_report(doc.hosted_site, doc.site_import)
	except SiteCallFailed:
		_write(name, lambda row: _stalled(row) or _error(row, _("The site couldn't be reached. Try again.")))
		return
	except Refusal as refusal:
		reason = str(refusal)
		_write(name, lambda row: _finish(row, [], "Failed", reason))
		return

	status = report.get("status")
	if status in FINISHED:
		stopped = status in ("Error", "Timed Out")
		reason = _("The import stopped on the site ({0}).").format(status) if stopped else None
		_write(name, lambda row: _finish(row, logs, "Failed" if stopped else None, reason))
	elif status == "Pending" and report.get("blocking_warnings"):
		reason = _("The site found blocking warnings and didn't import. Check the rows again.")
		_write(name, lambda row: _finish(row, [], "Failed", reason))
	else:
		_write(name, lambda row: _progress(row, report.get("processed") or 0))


def _read_report(site: str, data_import: str) -> tuple[dict, list[dict]]:
	"""The report and all its logs, page by page."""
	logs, after = [], -1
	while True:
		answer = _call(site, "import_report", {"data_import": data_import, "after_log_index": after})
		if answer.status == NOT_FOUND:
			raise Refusal(_("The site no longer has this import."))
		if answer.status != 200:
			raise SiteCallFailed(f"import_report answered HTTP {answer.status}", answer.status)
		page = answer.body.get("logs") or []
		logs += page
		if not answer.body.get("more") or not page:
			return answer.body, logs
		after = page[-1]["log_index"]


def _progress(row, processed: int):
	if processed > (row.site_processed or 0):
		row.site_processed = processed
		row.last_progress_on = signing.utc_now()
	row.last_error = None
	_stalled(row)


def _stalled(row) -> bool:
	"""No progress for 2 hours: a job stopped by the lock, or a dead worker (AC-11)."""
	since = row.last_progress_on or row.sent_on
	if row.status == "Importing" and since and signing.utc_now() - since > STALL:
		_finish(row, [], "Failed", _("No progress on the site for 2 hours, so this import is marked Failed."))
		return True
	return False


def _finish(row, logs: list[dict], forced: str | None, reason: str | None):
	"""Write each row's result from the logs, count again, and set the final status (AC-9)."""
	results = json.loads(row.results or "{}")
	for log in logs:
		for row_id in log.get("row_ids") or []:
			if results.get(row_id, {}).get("success"):
				continue
			results[row_id] = {
				"success": bool(log.get("success")),
				"docname": log.get("docname"),
				"message": str(log.get("message") or "")[:2000],
				"attempt": row.attempt,
			}
	row.results = json.dumps(results)
	_count(row)
	row.status = forced or _status_from_counts(row)
	row.finished_on = signing.utc_now()
	row.last_error = reason
	row.sending_since = None


def _status_from_counts(row) -> str:
	total = len(_rows(row))
	if total and row.success_count == total:
		return "Success"
	return "Partial Success" if row.success_count else "Failed"


def _count(row):
	"""`success_count` and `failed_count` count grid rows, computed again from `results` (AC-9)."""
	results = json.loads(row.results or "{}")
	ids = {item["id"] for item in _rows(row)}
	row.success_count = sum(
		1 for row_id, result in results.items() if row_id in ids and result.get("success")
	)
	row.failed_count = sum(
		1 for row_id, result in results.items() if row_id in ids and not result.get("success")
	)


# Retention (AC-14)


def clear_old_rows():
	"""Daily: clear the rows of imports finished 30 days ago, or untouched for 30 days, never while Importing."""
	cutoff = signing.utc_now() - RETENTION
	finished = frappe.get_all(
		DOCTYPE,
		filters={
			"rows_cleared_on": ("is", "not set"),
			"status": ("!=", "Importing"),
			"finished_on": ("<", cutoff),
		},
		pluck="name",
	)
	idle = frappe.get_all(
		DOCTYPE,
		filters={
			"rows_cleared_on": ("is", "not set"),
			"status": ("!=", "Importing"),
			"finished_on": ("is", "not set"),
			"modified": ("<", cutoff),
		},
		pluck="name",
	)
	for name in [*finished, *idle]:
		_clear(name)


def clear_for_site(site: str):
	"""A site reached Removed: clear all its imports at once (AC-14). Called from `removal.mark_removed`."""
	pending = frappe.get_all(
		DOCTYPE, filters={"hosted_site": site, "rows_cleared_on": ("is", "not set")}, pluck="name"
	)
	for name in pending:
		# The caller commits, in the transaction that removes the site.
		_clear(name, commit=False)


def _clear(name: str, commit: bool = True):
	def apply(row):
		row.rows = row.results = row.columns = None
		row.rows_cleared_on = signing.utc_now()
		if row.status == "Importing":
			row.status = "Failed"
			row.last_error = _("The site was removed while this import ran.")

	_write(name, apply, commit=commit)


# Shared rules


def sendable_rows(doc) -> list[dict]:
	"""The rows a check or a send carries: every row without a successful result (AC-10)."""
	results = json.loads(doc.results or "{}")
	return [row for row in _rows(doc) if not results.get(row["id"], {}).get("success")]


def submit_flag(doc) -> int:
	if doc.import_type in NEVER_SUBMITTED:
		return 0
	kind = _columns(doc)
	return 1 if doc.submit_after_import and kind.get("submittable") else 0


def checked_hash(doc) -> str:
	"""SHA-256 over the site, the type, Submit and the rows to be sent (AC-6)."""
	body = {
		"site": doc.hosted_site,
		"type": doc.import_type,
		"submit": submit_flag(doc),
		"rows": sendable_rows(doc),
	}
	return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_rows(value) -> list[dict]:
	"""`rows` as a list of `{id, cells}`, or a translated refusal."""
	try:
		rows = json.loads(value or "[]")
	except ValueError:
		frappe.throw(_("The rows could not be read. Reload the form."))
	if not isinstance(rows, list):
		frappe.throw(_("The rows could not be read. Reload the form."))
	ids = set()
	clean = []
	for row in rows:
		if (
			not isinstance(row, dict)
			or not isinstance(row.get("id"), str)
			or not isinstance(row.get("cells"), dict)
		):
			frappe.throw(_("The rows could not be read. Reload the form."))
		if row["id"] in ids:
			frappe.throw(_("Two rows share one id. Reload the form."))
		ids.add(row["id"])
		cells = {str(key): "" if value is None else str(value) for key, value in row["cells"].items()}
		clean.append(
			{"id": row["id"], "cells": {key: value for key, value in cells.items() if value.strip()}}
		)
	return clean


def _rows(doc) -> list[dict]:
	return parse_rows(doc.rows)


def _columns(doc) -> dict:
	return json.loads(doc.columns or "{}")


def _type_from(answer: dict, doctype: str | None) -> dict | None:
	return next((kind for kind in answer.get("types") or [] if kind.get("doctype") == doctype), None)


def _columns_value(answer: dict, kind: dict) -> dict:
	return {
		"doctype": kind["doctype"],
		"label": kind.get("label"),
		"submittable": bool(kind.get("submittable")),
		"name_key": kind.get("name_key") or "name",
		"columns": kind.get("columns") or [],
		"date_format": answer.get("date_format"),
		"number_format": answer.get("number_format"),
	}


def _editable(name: str):
	if not isinstance(name, str) or not frappe.db.exists(DOCTYPE, name):
		frappe.throw(_("No bulk import {0}.").format(name), frappe.DoesNotExistError)
	doc = frappe.get_doc(DOCTYPE, name)
	doc.check_permission("write")
	if doc.rows_cleared_on:
		frappe.throw(_("This import's rows were cleared, so it is final."))
	if doc.status in FINAL:
		frappe.throw(_("This import finished with every row saved."))
	return doc


def _write(name: str, apply, commit: bool = True):
	"""Lock the row, apply the change, save and commit. Never called across a site call."""
	row = frappe.get_doc(DOCTYPE, name, for_update=True)
	apply(row)
	row.flags.bulk_import_write = True
	row.save(ignore_permissions=True)
	if commit:
		frappe.db.commit()
	return row


def _refuse(row, reason: str):
	row.status, row.last_error, row.sending_since = "Refused", reason, None


def _error(row, reason: str):
	row.last_error, row.sending_since = reason, None


def _call(site: str, method: str, payload: dict):
	try:
		answer = call_site(site, method, payload)
	except SiteCallFailed as error:
		if error.status == UNAUTHORIZED:
			_alert_unauthorized(site, method)
		raise
	if answer.status == UNAUTHORIZED:
		_alert_unauthorized(site, method)
	return answer


def _answer_failed(name: str, site: str, method: str, status: int, sending: bool = False):
	"""A 400, 401, 413 or 5xx: the status stays, `last_error` says why. A 400 is a bug, so ops hear of it."""
	reason = _site_error(status)
	if status == BAD_REQUEST:
		alerts.send_ops(
			_("Bulk import {0}: site {1} refused {2} as a bad request").format(name, site, method),
			[_("HTTP {0}. This means a bug in the control site or the site.").format(status)],
		)
	_write(name, lambda row: _error(row, reason))
	if not sending:
		frappe.throw(reason)


def _site_error(status: int) -> str:
	if status == UNAUTHORIZED:
		return _("The site refused the control site's signature (HTTP 401).")
	if status == TOO_LARGE:
		return _("The site refused the rows as too large (HTTP 413). Split this import.")
	if status == BAD_REQUEST:
		return _("The site refused the request (HTTP 400). Reload the columns and check again.")
	return _("The site answered HTTP {0}. Try again.").format(status)


def _alert_unauthorized(site: str, method: str):
	alerts.send_ops(
		_("Bulk import: site {0} refused the signature").format(site),
		[_("{0} answered HTTP 401. Check the site's control secret.").format(method)],
	)
