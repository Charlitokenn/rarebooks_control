"""Spec 0008 signed endpoints: `register_site` (AC-1), `get_state` (AC-2), `create_checkout` (AC-3, AC-5)
and the `go` link (AC-6)."""

import hashlib
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from rarebooks_control import api, billing
from rarebooks_control.providers.clickpesa import ClickPesaError
from rarebooks_control.tests.control import (
	CLICKPESA_LINK,
	RUNNER_SECRET,
	answer,
	call,
	get,
	issued,
	make_plan,
	reset_billing,
	runner_config,
)
from rarebooks_control.tests.data_model import BENCH, make_bench, make_site

SITE = "Books Hosted Site"


def register_body(site="new.rarebooks.app", **values):
	return {
		"bench": BENCH,
		"issued_at": issued(),
		"site": site,
		"site_url": f"https://{site}/",
		"owner_email": "owner@example.com",
		**values,
	}


def site_body(site, **values):
	return {
		"site": site,
		"issued_at": issued(),
		"trial_ends_on": "2026-12-08",
		"time_zone": "Africa/Dar_es_Salaam",
		"language": "sw",
		"company_name": "Duka la Mama",
		**values,
	}


def checkout_body(site, **values):
	values = {
		"provider": "ClickPesa",
		"billing_period": "Yearly",
		"return_url": f"https://{site}/books/subscription?checkout=returned",
		**values,
	}
	return site_body(site, **values)


class IntegrationTestRegisterSite(IntegrationTestCase):
	def setUp(self):
		reset_billing()
		make_bench()

	def test_registers_a_new_site_with_a_fresh_secret(self):
		# covers: AC-1
		body = register_body("one.rarebooks.app")
		with runner_config(BENCH):
			response = call(api.register_site, body, RUNNER_SECRET)
		data = answer(response, RUNNER_SECRET)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(data["issued_at"], body["issued_at"])
		self.assertRegex(data["secret"], r"^[0-9a-f]{64}$")
		row = frappe.get_doc(SITE, "one.rarebooks.app")
		self.assertEqual(row.get_password("control_secret"), data["secret"])
		self.assertEqual((row.site_url, row.hosting_bench), ("https://one.rarebooks.app", BENCH))

	def test_again_from_the_same_bench_returns_the_same_secret_and_changes_nothing(self):
		# covers: AC-1
		with runner_config(BENCH):
			first = answer(call(api.register_site, register_body("two.rarebooks.app"), RUNNER_SECRET))
			again = call(
				api.register_site,
				register_body("two.rarebooks.app", owner_email="other@example.com"),
				RUNNER_SECRET,
			)
		self.assertEqual(answer(again, RUNNER_SECRET)["secret"], first["secret"])
		self.assertEqual(frappe.db.get_value(SITE, "two.rarebooks.app", "owner_email"), "owner@example.com")

	def test_another_bench_or_a_removed_site_gets_409(self):
		# covers: AC-1
		make_site("taken.rarebooks.app", hosting_bench=make_bench("bench-other"))
		make_site("gone.rarebooks.app", status="Removed")
		for site in ("taken.rarebooks.app", "gone.rarebooks.app"):
			with self.subTest(site=site), runner_config(BENCH):
				response = call(api.register_site, register_body(site), RUNNER_SECRET)
				self.assertEqual(response.status_code, 409)
				self.assertNotIn("secret", answer(response, RUNNER_SECRET))

	def test_bad_signature_unknown_bench_or_stale_time_gets_401(self):
		# covers: AC-1
		cases = {
			"bad signature": (register_body("x1.rarebooks.app"), "wrong-secret"),
			"unknown bench": (register_body("x2.rarebooks.app", bench="nobody"), RUNNER_SECRET),
			"stale": (
				register_body("x3.rarebooks.app", issued_at=issued(timedelta(minutes=-6))),
				RUNNER_SECRET,
			),
		}
		for name, (body, secret) in cases.items():
			with self.subTest(name), runner_config(BENCH):
				response = call(api.register_site, body, secret)
				self.assertEqual((response.status_code, response.get_data()), (401, b"{}"))
				self.assertFalse(frappe.db.exists(SITE, body["site"]))

	def test_bad_url_or_email_gets_400(self):
		for values in ({"site_url": "http://plain.rarebooks.app"}, {"owner_email": "not-an-email"}):
			with self.subTest(**values), runner_config(BENCH):
				response = call(
					api.register_site, register_body("bad.rarebooks.app", **values), RUNNER_SECRET
				)
				self.assertEqual(response.status_code, 400)


class IntegrationTestGetState(IntegrationTestCase):
	def setUp(self):
		reset_billing()
		frappe.db.delete("Books Plan")
		make_plan(amount=120000, label="Yearly")
		make_plan(amount=1000, label="Launch check", test_only=1)
		make_plan(
			provider="PayPal",
			billing_period="Monthly",
			currency="USD",
			amount=9,
			paypal_plan_id="P-M",
			label="Monthly",
		)
		make_plan(
			provider="PayPal",
			billing_period="Yearly",
			currency="USD",
			amount=90,
			paypal_plan_id="P-Y",
			enabled=0,
		)

	def test_answers_the_state_with_the_echo_and_plans_signed_with_the_site_secret(self):
		# covers: AC-2
		row = make_site(
			"state.rarebooks.app", paid_until="2027-12-08", provider="ClickPesa", billing_period="Yearly"
		)
		secret = row.get_password("control_secret")
		body = site_body(row.name)
		response = call(api.get_state, body, secret)
		data = answer(response, secret)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(
			data["state"],
			{
				"paid_until": "2027-12-08",
				"provider": "ClickPesa",
				"billing_period": "Yearly",
				"auto_renews": 0,
				"provider_reference": None,
				"exempt": 0,
				"issued_at": body["issued_at"],
			},
		)
		# Disabled plans are left out, and the test plan is not offered to this site.
		self.assertEqual(
			sorted((plan["provider"], plan["label"], plan["amount"]) for plan in data["plans"]),
			[("ClickPesa", "Yearly", 120000), ("PayPal", "Monthly", 9)],
		)

	def test_stores_the_site_facts(self):
		# covers: AC-2, AC-21
		row = make_site("facts.rarebooks.app")
		call(api.get_state, site_body(row.name), row.get_password("control_secret"))
		row.reload()
		self.assertEqual(
			(str(row.trial_ends_on), row.time_zone, row.language, row.company_name),
			("2026-12-08", "Africa/Dar_es_Salaam", "sw", "Duka la Mama"),
		)

	def test_a_test_plan_replaces_the_real_one_for_a_site_that_allows_them(self):
		# covers: AC-2
		row = make_site("tester.rarebooks.app", allow_test_plans=1)
		data = answer(call(api.get_state, site_body(row.name), row.get_password("control_secret")))
		clickpesa = [plan for plan in data["plans"] if plan["provider"] == "ClickPesa"]
		self.assertEqual([(plan["label"], plan["amount"]) for plan in clickpesa], [("Launch check", 1000)])

	def test_bad_signature_stale_unknown_or_removed_gets_401(self):
		# covers: AC-2
		row = make_site("auth.rarebooks.app")
		removed = make_site("removed.rarebooks.app", status="Removed")
		secret = row.get_password("control_secret")
		cases = {
			"bad signature": (site_body(row.name), "wrong"),
			"stale": (site_body(row.name, issued_at=issued(timedelta(minutes=-6))), secret),
			"unknown": (site_body("nobody.rarebooks.app"), secret),
			"removed": (site_body(removed.name), removed.get_password("control_secret")),
		}
		for name, (body, key) in cases.items():
			with self.subTest(name):
				response = call(api.get_state, body, key)
				self.assertEqual((response.status_code, response.get_data()), (401, b"{}"))

	def test_31st_call_in_a_minute_for_one_site_is_refused(self):
		# covers: AC-22 (per site, not per IP: every site calls from the hosting server's one IP)
		rows = [make_site(f"rl{number}.rarebooks.app") for number in range(2)]
		secrets = [row.get_password("control_secret") for row in rows]
		for _ in range(30):
			self.assertEqual(call(api.get_state, site_body(rows[0].name), secrets[0]).status_code, 200)
		self.assertEqual(call(api.get_state, site_body(rows[0].name), secrets[0]).status_code, 429)
		self.assertEqual(call(api.get_state, site_body(rows[1].name), secrets[1]).status_code, 200)


@patch("rarebooks_control.billing.clickpesa.create_checkout_link", return_value=CLICKPESA_LINK)
class IntegrationTestCreateCheckout(IntegrationTestCase):
	def setUp(self):
		reset_billing()
		frappe.db.delete("Books Plan")
		self.plan = make_plan()

	def test_clickpesa_checkout_and_go_link(self, create_link):
		# covers: AC-3, AC-6
		row = make_site("buy.rarebooks.app")
		secret = row.get_password("control_secret")
		response = call(api.create_checkout, checkout_body(row.name), secret)
		self.assertEqual(response.status_code, 200)
		url = answer(response, secret)["checkout_url"]
		self.assertRegex(url, r"/api/method/rarebooks_control\.billing\.go\?token=[\w-]+$")

		checkout = frappe.get_last_doc("Books Checkout", filters={"site": row.name})
		self.assertEqual(
			(checkout.status, checkout.provider, checkout.amount, checkout.currency, checkout.provider_url),
			("Created", "ClickPesa", 120000, "TZS", CLICKPESA_LINK),
		)
		self.assertRegex(checkout.order_reference, r"^RB\d{8}$")
		self.assertEqual(checkout.order_reference, billing.order_reference(checkout.name))
		create_link.assert_called_once_with(checkout.order_reference, "120000", "TZS", "owner@example.com")

		token = url.rsplit("=", 1)[1]
		self.assertEqual(checkout.token_hash, hashlib.sha256(token.encode()).hexdigest())
		go = get(billing.go, token=token)
		self.assertEqual((go.status_code, go.headers["Location"]), (302, CLICKPESA_LINK))
		cookie = go.headers["Set-Cookie"]
		for part in (f"books_checkout={token}", "HttpOnly", "Secure", "SameSite=Lax", "Max-Age=86400"):
			self.assertIn(part, cookie)
		self.assertIn("Path=/api/method/rarebooks_control.billing.returned", cookie)

	def test_a_recent_unpaid_checkout_is_reused_with_a_new_token(self, create_link):
		# covers: AC-3
		row = make_site("again.rarebooks.app")
		secret = row.get_password("control_secret")
		first = answer(call(api.create_checkout, checkout_body(row.name), secret))["checkout_url"]
		second = answer(call(api.create_checkout, checkout_body(row.name), secret))["checkout_url"]
		self.assertEqual(frappe.db.count("Books Checkout", {"site": row.name}), 1)
		self.assertEqual(create_link.call_count, 1)
		self.assertNotEqual(first, second)
		self.assertEqual(get(billing.go, token=first.rsplit("=", 1)[1]).status_code, 410)
		self.assertEqual(get(billing.go, token=second.rsplit("=", 1)[1]).status_code, 302)

	def test_an_old_or_unknown_token_answers_410(self, create_link):
		# covers: AC-6
		row = make_site("late.rarebooks.app")
		url = answer(call(api.create_checkout, checkout_body(row.name), row.get_password("control_secret")))[
			"checkout_url"
		]
		token = url.rsplit("=", 1)[1]
		checkout = frappe.get_last_doc("Books Checkout", filters={"site": row.name})
		frappe.db.set_value(
			"Books Checkout",
			checkout.name,
			"modified",
			add_to_date(now_datetime(), hours=-25),
			update_modified=False,
		)
		for value in (token, "unknown-token"):
			with self.subTest(token=value):
				response = get(billing.go, token=value)
				self.assertEqual(response.status_code, 410)
				self.assertIn(b"This payment link has expired", response.get_data())

	def test_refuses_a_plan_that_is_not_offered(self, create_link):
		# covers: AC-5
		row = make_site("noplan.rarebooks.app")
		secret = row.get_password("control_secret")
		frappe.db.set_value("Books Plan", self.plan.name, "test_only", 1)
		response = call(api.create_checkout, checkout_body(row.name), secret)
		self.assertEqual(response.status_code, 409)
		self.assertEqual(answer(response, secret), {"refusal": "plan_not_offered"})
		self.assertFalse(frappe.db.exists("Books Checkout", {"site": row.name}))

	def test_a_return_url_off_the_site_host_gets_400(self, create_link):
		# covers: AC-5 (no open redirect)
		row = make_site("host.rarebooks.app")
		for url in ("https://evil.example.com/books", "http://host.rarebooks.app/books"):
			with self.subTest(url=url):
				response = call(
					api.create_checkout,
					checkout_body(row.name, return_url=url),
					row.get_password("control_secret"),
				)
				self.assertEqual(response.status_code, 400)

	def test_a_clickpesa_failure_answers_a_signed_502_and_keeps_no_checkout(self, create_link):
		create_link.side_effect = ClickPesaError("timeout")
		row = make_site("down.rarebooks.app")
		secret = row.get_password("control_secret")
		response = call(api.create_checkout, checkout_body(row.name), secret)
		self.assertEqual(response.status_code, 502)
		answer(response, secret)
		self.assertFalse(frappe.db.exists("Books Checkout", {"site": row.name}))
