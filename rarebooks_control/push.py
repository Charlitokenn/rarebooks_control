"""The push of a site's full state to its `receive_state` (spec 0008 AC-13).

One job per site (`push:<site>`, deduplicated). The job reads the row and stamps `issued_at` while holding
the row lock, so a push never carries older data with a newer `issued_at`. Milestone 1 makes one attempt
and records the outcome; the retry sweep and alerts come with the push queue (build plan task 12).
"""

import json
from datetime import timedelta

import frappe
import requests

from rarebooks_control.destinations import post
from rarebooks_control.signing import SIGNATURE_HEADER, dumps, issued_now, sign, utc_now
from rarebooks_control.state import site_state

RECEIVE_STATE = "frappe_books.subscription.receive_state"
TIMEOUT_SECONDS = 10
GOOD_RESULTS = ("applied", "ignored")
# Minutes after each failure: 1, 5, 15, 60, then every hour.
RETRY_MINUTES = (1, 5, 15, 60)
ERROR_LENGTH = 500


def mark_changed(row):
	"""Call on a locked row whose pushed fields changed, before saving it: the push goes out after commit."""
	row.state_version = (row.state_version or 0) + 1
	row.push_status = "Pending"
	queue_push(row.name)


def queue_push(site: str):
	frappe.enqueue(
		"rarebooks_control.push.push_site",
		queue="short",
		job_id=f"push:{site}",
		deduplicate=True,
		enqueue_after_commit=True,
		site=site,
	)


def push_site(site: str):
	"""Send the current state once and record the answer. A `Removed` site is never pushed.

	Only the push fields are written, so a push can never put back an older billing value.
	"""
	row = frappe.get_doc("Books Hosted Site", site, for_update=True)
	if row.status == "Removed":
		return
	version = row.state_version or 0
	body = dumps({"site": row.site, "issued_at": issued_now(), **site_state(row)})
	result, error = send(row, body)
	if result in GOOD_RESULTS:
		values = success_values(row, version)
	else:
		values = failure_values(row, error)
	frappe.db.set_value("Books Hosted Site", site, values, update_modified=False)
	frappe.db.commit()


def send(row, body: bytes) -> tuple[str | None, str | None]:
	"""The customer's `result`, or None and a short reason. A reason never holds a secret or the body."""
	secret = row.get_password("control_secret")
	try:
		response = post(
			f"{row.site_url.rstrip('/')}/api/method/{RECEIVE_STATE}",
			data=body,
			headers={"Content-Type": "application/json", SIGNATURE_HEADER: sign(body, secret)},
			timeout=TIMEOUT_SECONDS,
			allow_redirects=False,
		)
	except (requests.RequestException, ValueError) as error:
		return None, f"push failed: {type(error).__name__}"
	if response.status_code != 200:
		return None, f"push answered HTTP {response.status_code}"
	try:
		data = json.loads(response.content)
	except ValueError:
		return None, "push answered with invalid JSON"
	message = data.get("message") if isinstance(data, dict) else None
	result = message.get("result") if isinstance(message, dict) else None
	if result not in GOOD_RESULTS:
		return None, "push answered without a result"
	return result, None


def success_values(row, version: int) -> dict:
	values = {
		"pushed_version": version,
		"last_pushed_on": utc_now(),
		"push_attempts": 0,
		"next_push_on": None,
		"failing_since": None,
		"failing_alerted": 0,
		"last_push_error": None,
		"push_status": "Synced",
	}
	# Read again: a change that landed after this push read the row must be sent too.
	if frappe.db.get_value("Books Hosted Site", row.name, "state_version") != version:
		values["push_status"] = "Pending"
		queue_push(row.name)
	return values


def failure_values(row, error: str | None) -> dict:
	now = utc_now()
	attempts = (row.push_attempts or 0) + 1
	return {
		"push_status": "Failing",
		"push_attempts": attempts,
		"failing_since": row.failing_since or now,
		"next_push_on": now + timedelta(minutes=retry_minutes(attempts)),
		"last_push_error": (error or "push failed")[:ERROR_LENGTH],
	}


def retry_minutes(attempts: int) -> int:
	return RETRY_MINUTES[attempts - 1] if attempts <= len(RETRY_MINUTES) else RETRY_MINUTES[-1]


def sweep():
	"""Every minute: queue a push for each row still `Pending`, or `Failing` and due.

	A deduplicated job that is still finishing skips a new request for the same site, so a change that
	lands in that moment waits here instead of being lost.
	"""
	now = utc_now()
	pending = frappe.get_all(
		"Books Hosted Site", filters={"status": "Active", "push_status": "Pending"}, pluck="name"
	)
	due = frappe.get_all(
		"Books Hosted Site",
		filters={"status": "Active", "push_status": "Failing", "next_push_on": ("<=", now)},
		pluck="name",
	)
	for site in (*pending, *due):
		queue_push(site)
