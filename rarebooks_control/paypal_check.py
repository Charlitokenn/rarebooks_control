"""The PayPal go live check (spec 0008 Configuration, build plan task 20, AC-23): read only, so it can run on
the control site before and after `paypal_mode` changes.

	bench --site <control site> execute rarebooks_control.paypal_check.run

It compares this site's config and its enabled PayPal Books Plans with what PayPal holds: the webhook (its id,
URL and events), and each plan (active, one endless regular cycle of the right length, the same price and
currency, no setup fee, no tax, a payment failure threshold of 3). Each line is PASS, WARN or FAIL.
"""

from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import get_url

from rarebooks_control import webhooks
from rarebooks_control.providers import paypal

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
WEBHOOK_METHOD = "rarebooks_control.webhooks.paypal"
# Every type the webhook handles (spec 0008, Provider calls); PAYMENT.FAILED feeds the daily digest.
WEBHOOK_EVENTS = (
	*webhooks.SUBSCRIPTION_EVENTS,
	webhooks.SALE_COMPLETED,
	webhooks.PAYMENT_FAILED,
	*webhooks.REFUND_EVENTS,
)
FREQUENCIES = {"Monthly": ("MONTH", 1), "Yearly": ("YEAR", 1)}
# Spec 0008 Configuration: PayPal retries a failed charge before it suspends, which AC-21's first payment
# note relies on. With 1, one failed charge suspends the subscription and auto renew is lost.
FAILURE_THRESHOLD = 3


def run() -> str:
	"""Print each finding and return the summary: `FAIL` when any check failed, else `OK`."""
	results = findings()
	for level, message in results:
		print(f"{level}  {message}")
	failed = any(level == FAIL for level, _message in results)
	return FAIL if failed else "OK"


def findings() -> list[tuple[str, str]]:
	results = check_config()
	if any(level == FAIL for level, _message in results):
		return results
	try:
		paypal.token()
	except paypal.PayPalError as error:
		return [*results, (FAIL, _("PayPal refused the credentials: {0}").format(error))]
	results.append((PASS, _("PayPal gave a token for these credentials")))
	try:
		results += check_webhook()
		results += check_plans()
	except paypal.PayPalError as error:
		results.append((FAIL, _("PayPal could not answer: {0}").format(error)))
	return results


def check_config() -> list[tuple[str, str]]:
	mode = frappe.conf.get("paypal_mode")
	if mode not in paypal.BASE_URLS:
		# The module falls back to sandbox, which on a live site would quietly take no real money.
		return [(FAIL, _("paypal_mode must be set to sandbox or live, not {0}").format(mode))]
	results = [(PASS if mode == "live" else WARN, _("paypal_mode is {0}").format(mode))]
	for key in ("paypal_client_id", "paypal_client_secret", "paypal_webhook_id"):
		if not frappe.conf.get(key):
			results.append((FAIL, _("{0} is not set").format(key)))
	if not get_url().startswith("https://"):
		results.append(
			(FAIL, _("This site's URL {0} is not https, so PayPal can't reach it").format(get_url()))
		)
	return results


def check_webhook() -> list[tuple[str, str]]:
	webhook_id = frappe.conf.get("paypal_webhook_id")
	webhook = paypal.get_webhook(webhook_id)
	if webhook is None:
		return [(FAIL, _("PayPal has no webhook {0} for this app and mode").format(webhook_id))]
	results = []
	expected_url = f"{get_url()}/api/method/{WEBHOOK_METHOD}"
	if webhook.get("url") == expected_url:
		results.append((PASS, _("Webhook {0} posts to {1}").format(webhook_id, expected_url)))
	else:
		results.append(
			(
				FAIL,
				_("Webhook {0} posts to {1}, not {2}").format(webhook_id, webhook.get("url"), expected_url),
			)
		)
	subscribed = {item.get("name") for item in webhook.get("event_types") or [] if isinstance(item, dict)}
	missing = [name for name in WEBHOOK_EVENTS if name not in subscribed and "*" not in subscribed]
	if missing:
		results.append((FAIL, _("Webhook {0} is missing events: {1}").format(webhook_id, ", ".join(missing))))
	else:
		results.append((PASS, _("Webhook {0} has all {1} events").format(webhook_id, len(WEBHOOK_EVENTS))))
	return results


def check_plans() -> list[tuple[str, str]]:
	plans = frappe.get_all(
		"Books Plan",
		filters={"provider": "PayPal", "enabled": 1},
		fields=["name", "billing_period", "amount", "currency", "paypal_plan_id", "test_only"],
		order_by="billing_period",
	)
	results = []
	for period in FREQUENCIES:
		if not any(plan.billing_period == period and not plan.test_only for plan in plans):
			results.append((FAIL, _("No enabled PayPal Books Plan for {0}").format(period)))
	live = frappe.conf.get("paypal_mode") == "live"
	for plan in plans:
		if plan.test_only and live:
			results.append((WARN, _("{0} is a test only plan, still enabled in live").format(plan.name)))
		results += check_plan(plan)
	return results


def check_plan(plan) -> list[tuple[str, str]]:
	label = _("{0} ({1} {2} {3})").format(plan.name, plan.billing_period, plan.currency, plan.amount)
	if not plan.paypal_plan_id:
		return [(FAIL, _("{0} has no PayPal plan id").format(label))]
	held = paypal.get_plan(plan.paypal_plan_id)
	if held is None:
		return [(FAIL, _("{0}: PayPal has no plan {1} in this mode").format(label, plan.paypal_plan_id))]
	problems, warnings = plan_problems(plan, held)
	results = [(FAIL, f"{label}: {problem}") for problem in problems]
	results += [(WARN, f"{label}: {warning}") for warning in warnings]
	if not problems:
		results.append((PASS, _("{0} matches PayPal plan {1}").format(label, plan.paypal_plan_id)))
	return results


def plan_problems(plan, held: dict) -> tuple[list[str], list[str]]:
	"""What would make a payment on this PayPal plan fail the AC-9 money check, or not renew as the spec
	expects (FAIL), and what only needs a look (WARN)."""
	problems, warnings = [], []
	if held.get("status") != "ACTIVE":
		problems.append(_("PayPal plan status is {0}, not ACTIVE").format(held.get("status")))
	cycles = held.get("billing_cycles") or []
	if len(cycles) != 1 or cycles[0].get("tenure_type") != "REGULAR":
		problems.append(_("the PayPal plan must have exactly one regular cycle (no trial cycle)"))
		return problems, warnings
	cycle = cycles[0]
	frequency = cycle.get("frequency") or {}
	unit, count = FREQUENCIES[plan.billing_period]
	if (frequency.get("interval_unit"), frequency.get("interval_count")) != (unit, count):
		problems.append(
			_("PayPal bills every {0} {1}, not every {2} {3}").format(
				frequency.get("interval_count"), frequency.get("interval_unit"), count, unit
			)
		)
	if cycle.get("total_cycles") != 0:
		problems.append(
			_("PayPal stops after {0} cycles; it must renew until cancelled").format(
				cycle.get("total_cycles")
			)
		)

	scheme = cycle.get("pricing_scheme") or {}
	price = scheme.get("fixed_price") or {}
	if price.get("currency_code") != plan.currency or _decimal(price.get("value")) != _decimal(plan.amount):
		problems.append(
			_("PayPal charges {0} {1}, the Books Plan expects {2} {3}").format(
				price.get("currency_code"), price.get("value"), plan.currency, plan.amount
			)
		)
	if (scheme.get("version") or 1) > 1:
		warnings.append(
			_(
				"its price was changed at PayPal (pricing version {0}); subscribers from before can go on "
				"paying the old price for a while, and the money check marks those payments Mismatch"
			).format(scheme.get("version"))
		)

	preferences = held.get("payment_preferences") or {}
	setup_fee = (preferences.get("setup_fee") or {}).get("value")
	if setup_fee is not None and _decimal(setup_fee) != 0:
		problems.append(_("PayPal adds a setup fee of {0}").format(setup_fee))
	threshold = preferences.get("payment_failure_threshold")
	if threshold != FAILURE_THRESHOLD:
		problems.append(
			_(
				"payment_failure_threshold is {0}, the spec needs {1} so PayPal retries before it suspends"
			).format(threshold, FAILURE_THRESHOLD)
		)
	taxes = held.get("taxes") or {}
	if _decimal(taxes.get("percentage") or 0) != 0:
		problems.append(
			_("PayPal adds {0}% tax, so every charge would differ from the plan").format(
				taxes.get("percentage")
			)
		)
	return problems, warnings


def _decimal(value) -> Decimal | None:
	try:
		return Decimal(str(value))
	except (InvalidOperation, ValueError):
		return None
