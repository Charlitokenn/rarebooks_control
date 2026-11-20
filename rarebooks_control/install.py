"""Site setup the control site needs from its first minute (spec 0008)."""

import frappe


def after_install():
	enable_two_factor_auth()
	keep_scheduler_awake()


def keep_scheduler_awake():
	"""Frappe Cloud stops the scheduler of a site nobody visits for `dormant_days`. The control site's scheduler
	is the only clock for removals and pings, so it never goes dormant (spec 0010 AC-14)."""
	frappe.db.set_single_value("System Settings", "dormant_days", 0)


def enable_two_factor_auth():
	"""Every desk user signs in with an authenticator app (spec 0008 AC-20). Turning it on in System Settings
	also marks the `All` role for two factor auth, so it covers every user, Administrator included."""
	settings = frappe.get_single("System Settings")
	settings.enable_two_factor_auth = 1
	settings.two_factor_method = "OTP App"
	settings.otp_issuer_name = settings.otp_issuer_name or "RareBooks Control"
	settings.save(ignore_permissions=True)
