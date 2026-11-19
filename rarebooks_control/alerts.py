"""Emails to the operator at `books_ops_email` (spec 0008). Shared by billing and the push queue."""

import frappe


def send_ops(subject: str, lines: list[str], now: bool = True):
	"""Email `books_ops_email`, at once by default ("at once" alerts use `now=True`; the digest queues).

	A failed email is logged, never raised, so it can't undo the record it is about.
	"""
	recipient = frappe.conf.get("books_ops_email")
	if not recipient:
		frappe.log_error(f"Billing alert with no books_ops_email set: {subject}", "\n".join(lines))
		return
	try:
		queue = frappe.sendmail(
			recipients=[recipient],
			subject=subject,
			message="<br>".join(frappe.utils.escape_html(line) for line in lines),
		)
	except Exception:
		frappe.log_error(f"Billing alert could not be sent: {subject}")
		return
	if now and queue:
		# Not `sendmail(now=True)`: Frappe sends that after the commit with no guard, so an SMTP outage
		# turns a saved change into HTTP 500, and a signed endpoint loses its answer (spec 0008 AC-5,
		# spec 0007 AC-12). The row stays queued, so the scheduler sends it if this try fails.
		frappe.db.after_commit.add(lambda: _send_queued(queue, subject))


def _send_queued(queue, subject: str):
	try:
		queue.send()
	except Exception:
		frappe.log_error(f"Billing alert queued, not sent yet: {subject}", defer_insert=True)
