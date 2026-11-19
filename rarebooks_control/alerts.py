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
		frappe.sendmail(
			recipients=[recipient],
			subject=subject,
			message="<br>".join(frappe.utils.escape_html(line) for line in lines),
			now=now,
		)
	except Exception:
		frappe.log_error(f"Billing alert could not be sent: {subject}")
