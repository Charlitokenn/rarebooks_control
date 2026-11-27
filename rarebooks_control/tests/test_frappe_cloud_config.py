"""`frappe_cloud.update_config` keeps every other Site Config key (spec 0010 AC-11, spec 0011 AC-8).

press replaces a site's whole configuration with the list `press.api.site.update_config` is given, so the client
reads the rows through `press.api.client.get_list` and sends the merged list. These tests patch the HTTP call
with a stand in for press that behaves the same way: it replaces the rows, and keeps a Password's stored value
when the `*******` mask comes back."""

import json
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from rarebooks_control import registry
from rarebooks_control.providers import frappe_cloud
from rarebooks_control.providers.frappe_cloud import FrappeCloudError

CLOUD_CONF = {
	"frappe_cloud_api_key": "key-123",
	"frappe_cloud_api_secret": "secret-456",
	"frappe_cloud_team": "team@example.com",
	"books_control_public_url": "https://control.example.com",
}
SITE = "a.app"
MASK = "*******"
GET_LIST = "https://cloud.frappe.io/api/method/press.api.client.get_list"
UPDATE_CONFIG = "https://cloud.frappe.io/api/method/press.api.site.update_config"


def config_rows(count: int = 0) -> list[dict]:
	"""One row of each type press stores, plus `count` more String rows."""
	rows = [
		{"key": "mail_footer", "value": "Thanks", "type": "String"},
		{"key": "smtp_password", "value": "stored-pass", "type": "Password"},
		{"key": "max_file_size", "value": "10485760", "type": "Number"},
		{"key": "developer_mode", "value": "0", "type": "Boolean"},
		{"key": "limits", "value": '{"users": 5}', "type": "JSON"},
		{"key": "books_control_url", "value": "https://old.example.com", "type": "String"},
	]
	return rows + [{"key": f"extra_{n}", "value": str(n), "type": "String"} for n in range(count)]


class FakePress:
	"""Answers `get_list` on Site Config and `update_config` like press does."""

	def __init__(self, rows: list[dict]):
		self.rows = [dict(row) for row in rows]
		self.posts = []

	def __call__(self, url, json, headers, timeout, allow_redirects):
		self.posts.append((url, json))
		if url == GET_LIST:
			return self.answer(self.get_list(json))
		if url == UPDATE_CONFIG:
			return self.answer(self.update_config(json))
		raise AssertionError(f"unexpected call {url}")

	def answer(self, message, status=200):
		response = Mock(status_code=status)
		response.json.return_value = {"message": message}
		return response

	def get_list(self, body):
		assert body["doctype"] == "Site Config"
		assert body["filters"] == {"parent": SITE, "parenttype": "Site"}
		page = self.rows[body["start"] : body["start"] + body["limit"]]
		return [{**row, "value": MASK if row["type"] == "Password" else row["value"]} for row in page]

	def update_config(self, body):
		# press: `Site._set_configuration` replaces the list; a masked Password keeps its stored value.
		assert body["name"] == SITE
		stored = {row["key"]: row for row in self.rows}
		sent = json.loads(body["config"])
		for row in sent:
			if row["type"] == "Password" and row["value"] == MASK:
				row["value"] = stored[row["key"]]["value"]
		self.rows = sent
		return self.rows

	def values(self) -> dict:
		return {row["key"]: (row["value"], row["type"]) for row in self.rows}

	def urls(self) -> list[str]:
		return [url for url, _ in self.posts]


class UnitTestUpdateConfigKeepsKeys(UnitTestCase):
	def run_update(self, press: FakePress, config: dict):
		with patch.dict(frappe.conf, CLOUD_CONF), patch("requests.post", side_effect=press):
			frappe_cloud.update_config(SITE, config)

	def test_other_keys_survive_and_new_keys_are_set_or_replaced(self):
		# covers: 0010 AC-11
		before = config_rows()
		press = FakePress(before)
		self.run_update(press, {"books_control_url": "https://c.example.com", "books_control_secret": "s1"})
		values = press.values()
		for row in before:
			if row["key"] != "books_control_url":
				self.assertEqual(values[row["key"]], (row["value"], row["type"]), row["key"])
		self.assertEqual(values["books_control_url"], ("https://c.example.com", "String"))
		self.assertEqual(values["books_control_secret"], ("s1", "String"))
		self.assertEqual(len(press.rows), len(before) + 1)
		self.assertEqual(press.urls(), [GET_LIST, UPDATE_CONFIG])

	def test_a_password_row_is_sent_back_masked_never_as_a_value(self):
		# covers: 0010 AC-11
		press = FakePress(config_rows())
		self.run_update(press, {"books_control_secret": "s1"})
		sent = json.loads(press.posts[-1][1]["config"])
		self.assertIn({"key": "smtp_password", "value": MASK, "type": "Password"}, sent)
		self.assertEqual(press.values()["smtp_password"], ("stored-pass", "Password"))

	def test_a_replaced_key_keeps_its_place_and_appears_once(self):
		# covers: 0010 AC-11
		rows = [*config_rows(), {"key": "books_control_url", "value": "https://older", "type": "String"}]
		press = FakePress(rows)
		self.run_update(press, {"books_control_url": "https://c.example.com"})
		keys = [row["key"] for row in press.rows]
		self.assertEqual(keys.count("books_control_url"), 1)
		self.assertEqual(keys.index("books_control_url"), 5)

	def test_reads_every_page_before_writing(self):
		# covers: 0010 AC-11
		before = config_rows(count=45)
		press = FakePress(before)
		self.run_update(press, {"books_control_secret": "s1"})
		self.assertEqual(press.urls(), [GET_LIST] * 3 + [UPDATE_CONFIG])
		self.assertEqual([body["start"] for _, body in press.posts[:3]], [0, 20, 40])
		self.assertEqual(len(press.rows), len(before) + 1)

	def test_a_site_with_no_rows_gets_just_the_new_keys(self):
		press = FakePress([])
		self.run_update(press, {"books_control_secret": "s1"})
		self.assertEqual(press.rows, [{"key": "books_control_secret", "value": "s1", "type": "String"}])

	def test_a_failed_or_odd_read_writes_nothing_and_leaks_no_value(self):
		# covers: 0010 AC-11
		cases = {
			"http 403": Mock(status_code=403),
			"not a list": FakePress([]).answer({"key": "x"}),
			"not a row": FakePress([]).answer([{"value": "stored-pass"}]),
			"unknown type": FakePress([]).answer([{"key": "x", "value": "stored-pass", "type": "Blob"}]),
		}
		for name, response in cases.items():
			with (
				self.subTest(name),
				patch.dict(frappe.conf, CLOUD_CONF),
				patch("requests.post", return_value=response) as post,
			):
				with self.assertRaises(FrappeCloudError) as raised:
					frappe_cloud.update_config(SITE, {"books_control_secret": "s1"})
				self.assertEqual([c.args[0] for c in post.call_args_list], [GET_LIST])
				for value in ("stored-pass", "s1", *CLOUD_CONF.values()):
					self.assertNotIn(value, str(raised.exception))

	def test_send_config_keeps_the_sites_other_keys(self):
		# covers: 0010 AC-11, 0011 AC-8
		before = config_rows()
		press = FakePress(before)
		with patch.dict(frappe.conf, CLOUD_CONF), patch("requests.post", side_effect=press):
			self.assertTrue(registry.send_config(SITE, "rotated-secret"))
		values = press.values()
		for row in before:
			if row["key"] != "books_control_url":
				self.assertEqual(values[row["key"]], (row["value"], row["type"]), row["key"])
		self.assertEqual(values["books_control_url"], ("https://control.example.com", "String"))
		self.assertEqual(values["books_control_secret"], ("rotated-secret", "String"))
