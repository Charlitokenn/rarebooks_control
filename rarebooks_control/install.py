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
	"""Every desk user signs in with an authenticator app (spec 0008 AC-20), covering every user through the
	`All` role, Administrator included.

	Written field by field rather than through `System Settings.save()`: on a new Frappe Cloud site this runs
	before the setup wizard, while `language` and `time_zone` are still empty, and a save refuses them as
	mandatory, which failed the whole install (found by /check verify, spec 0010). So this does the parts of the
	save it needs itself: the three values, the `All` role, and the settings cache."""
	from frappe.core.doctype.system_settings.system_settings import clear_system_settings_cache
	from frappe.twofactor import toggle_two_factor_auth

	issuer = frappe.db.get_single_value("System Settings", "otp_issuer_name") or "RareBooks Control"
	frappe.db.set_single_value(
		"System Settings",
		{"enable_two_factor_auth": 1, "two_factor_method": "OTP App", "otp_issuer_name": issuer},
	)
	toggle_two_factor_auth(True, roles=["All"])
	clear_system_settings_cache()
