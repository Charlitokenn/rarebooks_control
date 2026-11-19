"""Runner calls from the hosting benches (spec 0007), and the secret rotation they carry (spec 0008 AC-15).

Every call carries `bench` and is signed with that bench's runner secret; every answer is signed the same way
and echoes the request's `issued_at`. The rest of spec 0007's removal side (`report_sites`, the removal cases)
joins this module in its Milestone 4.
"""

import frappe
from werkzeug.wrappers import Response

from rarebooks_control import signing
from rarebooks_control.push import mark_changed

SITE = "Books Hosted Site"
# Spec 0007: runner calls are limited per bench, since every bench calls from the hosting server's one IP.
RUNNER_LIMIT = 120
CONFLICT, TOO_MANY = 409, 429


def rotation_instructions(bench: str) -> list[dict]:
	"""A `rotate_secret` instruction for each active site of the bench with a rotation pending (AC-15).

	`report_sites` adds these to its answer, the one place a secret is sent, and only to the bench that hosts
	the site, in an answer signed with that bench's runner secret.
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


@frappe.whitelist(allow_guest=True, methods=["POST"])
def secret_rotated() -> Response:
	"""The bench wrote the pending secret into the site's config: swap it in and push (spec 0008 AC-15).

	From here on only the new secret works. The push is signed with it; if the site's web workers still hold
	the old config (up to 60 seconds), the push retries cover it (AC-13). Safe to repeat: a second call finds
	no rotation pending and gets 409.
	"""
	raw, body = signing.read_body()
	bench = body.get("bench") if body else None
	secret = signing.runner_secret(bench)
	request = signing.verify_request(raw, body, [secret])
	if not request:
		return signing.unauthorized(secret)
	echo = {"issued_at": request.issued_at}
	if signing.over_limit("secret_rotated", bench, RUNNER_LIMIT):
		return request.answer(echo, TOO_MANY)

	row = _rotating_site(body.get("site"), bench)
	if not row:
		return request.answer({**echo, "status": "no_rotation"}, CONFLICT)
	row.control_secret = row.get_password("pending_secret")
	row.pending_secret = None
	# A rotation is a state change: the site hears from the control site with the new secret (AC-13).
	mark_changed(row)
	row.save(ignore_permissions=True)
	return request.answer({**echo, "site": row.name, "status": "rotated"})


def _rotating_site(site, bench: str):
	"""The locked row of an active site on this bench with a rotation pending, else None. A site on another
	bench gets the same answer as one with no rotation, so a bench learns nothing about other benches."""
	if not isinstance(site, str) or not frappe.db.exists(SITE, site):
		return None
	row = frappe.get_doc(SITE, site, for_update=True)
	if row.hosting_bench != bench or row.status != "Active" or not row.pending_secret:
		return None
	return row
