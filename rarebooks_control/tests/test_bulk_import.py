"""Bulk import into one hosted site from the control site (spec 0025), against a stub of the site's calls."""

import json
from datetime import timedelta
from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from rarebooks_control import bulk_import, onboarding, removal
from rarebooks_control.signing import SIGNATURE_HEADER, is_valid_signature, utc_now
from rarebooks_control.tests.control import SiteResponse
from rarebooks_control.tests.data_model import (
	BILLING_MANAGER,
	REMOVAL_MANAGER,
	SYSTEM_MANAGER,
	make_role_users,
	make_site,
)

DOCTYPE = "Books Bulk Import"
ITEM = "Books Item"
COLUMNS = [
	{
		"key": "name",
		"label": "Item Name",
		"fieldtype": "Data",
		"required": True,
		"options": None,
		"table_label": None,
	},
	{
		"key": "rate",
		"label": "Rate",
		"fieldtype": "Currency",
		"required": False,
		"options": None,
		"table_label": None,
	},
	{
		"key": "item_usage",
		"label": "Purpose",
		"fieldtype": "Select",
		"required": True,
		"options": ["Sales", "Purchases", "Both"],
		"table_label": None,
	},
]
TYPES = [
	{
		"doctype": ITEM,
		"schema_name": "Item",
		"label": "Item",
		"submittable": False,
		"name_key": "name",
		"columns": COLUMNS,
	},
	{
		"doctype": "Books Expense",
		"schema_name": "Expense",
		"label": "Expense",
		"submittable": True,
		"name_key": "name",
		"columns": COLUMNS,
	},
]


class BulkSite:
	"""Stands in for a customer site's four bulk import calls, as `frappe_books.subscription.bulk_import` answers."""

	def __init__(self):
		self.secrets: dict[str, str] = {}
		self.calls: list[tuple[str, str, dict]] = []
		self.started: dict[str, list[dict]] = {}
		self.reports: dict[str, dict] = {}
		self.warnings: list[dict] = []
		self.answers: dict[str, tuple[int, dict]] = {}
		self.down: set[str] = set()
		# A start that runs on the site but whose answer is lost on the way back.
		self.lose_answer = False
		# Something that happens on the control site while this call is on its way (a send, a clear), once.
		self.during: dict[str, object] = {}

	def hold(self, row):
		self.secrets[row.name] = row.get_password("control_secret")

	def bodies(self, name: str) -> list[dict]:
		return [body for _site, called, body in self.calls if called == name]

	def __call__(self, url, data, headers, timeout):
		site = url.split("://", 1)[1].split("/", 1)[0]
		name = url.rsplit(".", 1)[1]
		body = json.loads(data)
		secret = self.secrets.get(site)
		echo = {"issued_at": body.get("issued_at")}
		if name in self.down:
			raise requests.ConnectionError("down")
		if not is_valid_signature(data, headers.get(SIGNATURE_HEADER), secret) or body.get("site") != site:
			return SiteResponse(401, echo, secret)
		self.calls.append((site, name, body))
		if name in self.during:
			self.during.pop(name)()
		if name in self.answers:
			status, answer = self.answers[name]
			return SiteResponse(status, {**answer, **echo}, secret)
		status, answer = getattr(self, name)(body)
		if name == "start_import" and self.lose_answer:
			raise requests.Timeout("lost")
		return SiteResponse(status, {**answer, **echo}, secret)

	def import_columns(self, body):
		return 200, {"types": TYPES, "date_format": "dd-mm-yyyy", "number_format": "#,###.##"}

	def preview_import(self, body):
		return 200, {"warnings": self.warnings}

	def start_import(self, body):
		data_import = f"CTRL-{body['import_ref']}"
		if data_import in self.started:
			if self.started[data_import] != body["rows"]:
				return 409, {"status": "ref_conflict", "data_import": data_import}
			return 200, {"data_import": data_import, "status": "In Progress", "started": False}
		self.started[data_import] = body["rows"]
		return 200, {"data_import": data_import, "status": "Pending", "started": True}

	def import_report(self, body):
		data_import = body["data_import"]
		if data_import in self.reports:
			return 200, self.reports[data_import]
		if data_import in self.started:
			return 200, {"status": "In Progress", "total": 1, "processed": 0}
		return 404, {"status": "not_found"}

	def finish(self, data_import: str, saved: dict[str, str], failed: tuple[str, ...] = (), status=None):
		"""The site's report once the import ran: `saved` row id → docname, `failed` row ids."""
		logs = [
			{"log_index": index, "row_ids": [row_id], "success": True, "docname": docname, "message": ""}
			for index, (row_id, docname) in enumerate(saved.items())
		]
		logs += [
			{
				"log_index": len(logs) + index,
				"row_ids": [row_id],
				"success": False,
				"docname": None,
				"message": "<b>No item</b>",
			}
			for index, row_id in enumerate(failed)
		]
		total = len(saved) + len(failed)
		final = status or ("Success" if not failed else "Partial Success" if saved else "Error")
		self.reports[data_import] = {
			"status": final,
			"total": total,
			"processed": total,
			"logs": logs,
			"more": False,
		}


def rows(*names: str) -> list[dict]:
	return [
		{"id": f"id-{name}", "cells": {"name": name, "item_usage": "Both", "rate": "5"}} for name in names
	]


class BulkImportTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_role_users()

	def setUp(self):
		self.site = make_site(f"bulk-{frappe.generate_hash(length=8)}.example.com")
		self.stub = BulkSite()
		self.stub.hold(self.site)
		self.start(patch("rarebooks_control.site_calls.post", side_effect=self.stub))
		self.send_ops = self.start(patch("rarebooks_control.bulk_import.alerts.send_ops"))
		frappe.cache.delete_keys("books_bulk_import_columns:")
		frappe.set_user(BILLING_MANAGER)

	def tearDown(self):
		frappe.set_user("Administrator")

	def start(self, patcher):
		mocked = patcher.start()
		self.addCleanup(patcher.stop)
		return mocked

	def new_import(self, grid_rows: list[dict], import_type: str = ITEM, **values):
		return frappe.get_doc(
			{
				"doctype": DOCTYPE,
				"hosted_site": self.site.name,
				"import_type": import_type,
				"rows": json.dumps(grid_rows),
				**values,
			}
		).insert()

	def reload(self, doc):
		return frappe.get_doc(DOCTYPE, doc.name)

	def checked_and_sent(self, grid_rows: list[dict]):
		doc = self.new_import(grid_rows)
		self.assertEqual(bulk_import.check(doc.name)["status"], "Checked")
		bulk_import.send(doc.name)
		return self.reload(doc)


class IntegrationTestBulkImportFlow(BulkImportTestCase):
	def test_happy_path_imports_three_items_once(self):
		# covers: AC-3, AC-7, AC-9
		doc = self.new_import(rows("Pen", "Ink", "Pad"))
		self.assertEqual(json.loads(doc.columns)["columns"][0]["key"], "name")
		self.assertEqual(doc.row_count, 3)

		self.assertEqual(bulk_import.check(doc.name)["status"], "Checked")
		answer = bulk_import.send(doc.name)
		self.assertEqual(answer["status"], "Importing")
		self.assertEqual(answer["site_import"], f"CTRL-{doc.name}-1")
		[start] = self.stub.bodies("start_import")
		self.assertEqual(
			(start["import_ref"], start["import_type"], start["submit"]), (f"{doc.name}-1", ITEM, 0)
		)

		self.stub.finish(f"CTRL-{doc.name}-1", {"id-Pen": "Pen", "id-Ink": "Ink", "id-Pad": "Pad"})
		result = bulk_import.refresh(doc.name)
		self.assertEqual(result["status"], "Success")
		self.assertEqual((result["success_count"], result["failed_count"]), (3, 0))
		self.assertEqual(result["results"]["id-Ink"]["docname"], "Ink")
		self.assertEqual(len(self.stub.bodies("start_import")), 1)
		comments = frappe.get_all(
			"Comment", {"reference_name": doc.name, "comment_type": "Info"}, pluck="content"
		)
		self.assertTrue(any("Success" in comment for comment in comments))

	def test_a_blocking_warning_keeps_draft_and_import_closed(self):
		# covers: AC-5, AC-6
		self.stub.warnings = [
			{"row_ids": ["id-Pen"], "column_key": "rate", "message": "bad", "blocking": True}
		]
		doc = self.new_import(rows("Pen"))
		answer = bulk_import.check(doc.name)
		self.assertEqual(answer["status"], "Draft")
		self.assertEqual(answer["warnings"][0]["column_key"], "rate")
		with self.assertRaises(frappe.ValidationError):
			bulk_import.send(doc.name)
		self.assertFalse(self.stub.bodies("start_import"))

	def test_a_real_change_resets_checked_and_a_plain_save_keeps_it(self):
		# covers: AC-6
		doc = self.new_import(rows("Pen"))
		bulk_import.check(doc.name)
		doc = self.reload(doc)
		doc.save()
		self.assertEqual(self.reload(doc).status, "Checked")

		doc = self.reload(doc)
		doc.rows = json.dumps(rows("Pencil"))
		doc.save()
		self.assertEqual(self.reload(doc).status, "Draft")
		with self.assertRaises(frappe.ValidationError):
			bulk_import.send(doc.name)

	def test_structural_rules_and_caps_refuse_before_any_site_call(self):
		# covers: AC-5, AC-12
		no_name = self.new_import([{"id": "a", "cells": {"rate": "5"}}])
		big = self.new_import([{"id": f"r{index}", "cells": {"name": f"N{index}"}} for index in range(2001)])
		heavy = self.new_import([{"id": f"h{index}", "cells": {"name": "x" * 9000}} for index in range(240)])
		for doc in (no_name, big, heavy):
			with self.subTest(doc.name), self.assertRaises(frappe.ValidationError):
				bulk_import.check(doc.name)
		self.assertFalse(self.stub.bodies("preview_import"))

	def test_a_lost_answer_is_adopted_on_the_next_send(self):
		# covers: AC-8, AC-11
		doc = self.new_import(rows("Pen"))
		bulk_import.check(doc.name)
		self.stub.lose_answer = True
		answer = bulk_import.send(doc.name)
		doc = self.reload(doc)
		self.assertEqual((answer["status"], doc.attempt, doc.site_import), ("Checked", 1, None))
		self.assertTrue(doc.last_error)

		self.stub.lose_answer = False
		frappe.db.set_value(DOCTYPE, doc.name, "sending_since", None)
		answer = bulk_import.send(doc.name)
		self.assertEqual(
			answer, {"status": "Importing", "site_import": f"CTRL-{doc.name}-1", "last_error": None}
		)
		self.assertEqual(len(self.stub.bodies("start_import")), 1)

	def test_a_second_send_while_one_waits_is_refused(self):
		# covers: AC-8
		doc = self.new_import(rows("Pen"))
		bulk_import.check(doc.name)
		frappe.db.set_value(DOCTYPE, doc.name, "sending_since", utc_now())
		with self.assertRaises(frappe.ValidationError):
			bulk_import.send(doc.name)
		self.assertFalse(self.stub.bodies("start_import"))

	def test_partial_then_retry_sends_only_the_failed_rows(self):
		# covers: AC-9, AC-10
		doc = self.checked_and_sent(rows("Pen", "Ink", "Pad"))
		self.stub.finish(f"CTRL-{doc.name}-1", {"id-Pen": "Pen"}, failed=("id-Ink", "id-Pad"))
		bulk_import.refresh(doc.name)
		doc = self.reload(doc)
		self.assertEqual((doc.status, doc.success_count, doc.failed_count), ("Partial Success", 1, 2))
		self.assertEqual(json.loads(doc.results)["id-Ink"]["message"], "<b>No item</b>")

		# A saved row can't be edited or deleted; a failed one can, and that puts the import back to Draft.
		for changed in (
			rows("Ink", "Pad"),
			[{"id": "id-Pen", "cells": {"name": "Pen", "rate": "9"}}, *rows("Ink", "Pad")],
		):
			doc = self.reload(doc)
			doc.rows = json.dumps(changed)
			with self.assertRaises(frappe.ValidationError):
				doc.save()
		doc = self.reload(doc)
		fixed = rows("Pad", "Pen", "Ink")
		fixed[0]["cells"]["rate"] = "6"
		doc.rows = json.dumps(fixed)
		doc.save()
		self.assertEqual(self.reload(doc).status, "Draft")

		bulk_import.check(doc.name)
		bulk_import.send(doc.name)
		second = self.stub.bodies("start_import")[-1]
		self.assertEqual(second["import_ref"], f"{doc.name}-2")
		self.assertEqual(sorted(row["id"] for row in second["rows"]), ["id-Ink", "id-Pad"])
		self.stub.finish(f"CTRL-{doc.name}-2", {"id-Ink": "Ink", "id-Pad": "Pad"})
		result = bulk_import.refresh(doc.name)
		self.assertEqual(
			(result["status"], result["success_count"], result["failed_count"]), ("Success", 3, 0)
		)

	def test_a_site_that_stopped_marks_the_import_failed(self):
		# covers: AC-9
		doc = self.checked_and_sent(rows("Pen"))
		self.stub.finish(f"CTRL-{doc.name}-1", {}, failed=("id-Pen",), status="Error")
		self.assertEqual(bulk_import.refresh(doc.name)["status"], "Failed")

	def test_no_progress_for_two_hours_is_failed(self):
		# covers: AC-11
		doc = self.checked_and_sent(rows("Pen"))
		frappe.db.set_value(DOCTYPE, doc.name, "last_progress_on", utc_now() - timedelta(hours=3))
		result = bulk_import.refresh(doc.name)
		self.assertEqual(result["status"], "Failed")
		self.assertIn("2 hours", result["last_error"])

	def test_a_confirmed_import_the_site_lost_is_failed(self):
		# covers: AC-11
		doc = self.checked_and_sent(rows("Pen"))
		self.stub.started.clear()
		self.assertEqual(bulk_import.refresh(doc.name)["status"], "Failed")

	def test_the_sweep_queues_one_refresh_per_importing_import(self):
		# covers: AC-9
		doc = self.checked_and_sent(rows("Pen"))
		frappe.set_user("Administrator")
		with patch("rarebooks_control.bulk_import.frappe.enqueue") as enqueue:
			bulk_import.sweep()
		calls = [call for call in enqueue.call_args_list if call.kwargs["name"] == doc.name]
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0].kwargs["job_id"], f"books-bulk-import-refresh:{doc.name}")
		self.assertTrue(calls[0].kwargs["deduplicate"])

	def test_reload_columns_keeps_cells_by_key_and_names_the_gone_ones(self):
		# covers: AC-3
		doc = self.new_import(rows("Pen"))
		bulk_import.check(doc.name)
		item = {**TYPES[0], "columns": [COLUMNS[0], COLUMNS[2]]}
		with patch.object(BulkSite, "import_columns", lambda _self, body: (200, {"types": [item]})):
			answer = bulk_import.reload_columns(doc.name)
		self.assertEqual(answer["gone"], ["Rate"])
		self.assertEqual(answer["rows"][0]["cells"], {"name": "Pen", "item_usage": "Both"})
		doc = self.reload(doc)
		self.assertEqual(doc.status, "Draft")
		self.assertEqual(
			[column["key"] for column in json.loads(doc.columns)["columns"]], ["name", "item_usage"]
		)

	def test_reload_columns_is_refused_while_importing(self):
		# covers: AC-3, AC-10
		doc = self.checked_and_sent(rows("Pen"))
		calls = len(self.stub.bodies("import_columns"))
		with self.assertRaises(frappe.ValidationError):
			bulk_import.reload_columns(doc.name)
		self.assertEqual(len(self.stub.bodies("import_columns")), calls)
		self.assertEqual(self.reload(doc).status, "Importing")

	def test_a_refresh_the_site_refuses_keeps_importing_with_the_reason(self):
		# covers: AC-11
		doc = self.checked_and_sent(rows("Pen"))
		for status, words in ((502, "HTTP 502"), (401, "signature")):
			with self.subTest(status):
				self.stub.answers["import_report"] = (status, {"status": "error"})
				result = bulk_import.refresh(doc.name)
				self.assertEqual(result["status"], "Importing")
				self.assertIn(words, result["last_error"])
				self.assertNotIn("Pen", result["last_error"])

	def test_expenses_are_never_submitted(self):
		# covers: AC-7
		doc = self.new_import(rows("Rent"), import_type="Books Expense", submit_after_import=1)
		self.assertEqual(doc.submit_after_import, 0)
		bulk_import.check(doc.name)
		bulk_import.send(doc.name)
		self.assertEqual(self.stub.bodies("start_import")[0]["submit"], 0)


class IntegrationTestBulkImportRaces(BulkImportTestCase):
	"""Writes that land after a long site call must not undo what happened meanwhile (AC-10, AC-11)."""

	def test_a_stall_keeps_the_rows_the_site_already_saved(self):
		# covers: AC-10, AC-11
		doc = self.checked_and_sent(rows("Pen", "Ink"))
		site_import = f"CTRL-{doc.name}-1"
		# The site's job saved Pen, then stopped: its report is not finished, but its log is there.
		self.stub.reports[site_import] = {
			"status": "In Progress",
			"total": 2,
			"processed": 1,
			"logs": [
				{"log_index": 0, "row_ids": ["id-Pen"], "success": True, "docname": "Pen", "message": ""}
			],
			"more": False,
		}
		frappe.db.set_value(
			DOCTYPE, doc.name, {"site_processed": 1, "last_progress_on": utc_now() - timedelta(hours=3)}
		)
		result = bulk_import.refresh(doc.name)
		self.assertEqual(result["status"], "Failed")
		self.assertTrue(result["results"]["id-Pen"]["success"])
		self.assertEqual([row["id"] for row in bulk_import.sendable_rows(self.reload(doc))], ["id-Ink"])

	def test_a_site_unreachable_at_the_stall_keeps_importing(self):
		# covers: AC-11
		doc = self.checked_and_sent(rows("Pen"))
		frappe.db.set_value(DOCTYPE, doc.name, "last_progress_on", utc_now() - timedelta(hours=3))
		self.stub.down.add("import_report")
		result = bulk_import.refresh(doc.name)
		# The site may have saved rows we can't read, so it is not marked Failed and offered for a resend.
		self.assertEqual(result["status"], "Importing")
		self.assertTrue(result["last_error"])

	def test_a_check_that_lands_after_a_send_leaves_importing(self):
		# covers: AC-10
		doc = self.new_import(rows("Pen"))
		self.assertEqual(bulk_import.check(doc.name)["status"], "Checked")
		self.stub.during["preview_import"] = lambda: bulk_import.send(doc.name)
		bulk_import.check(doc.name)
		doc = self.reload(doc)
		self.assertEqual((doc.status, doc.attempt, doc.site_import), ("Importing", 1, f"CTRL-{doc.name}-1"))
		with self.assertRaises(frappe.ValidationError):
			bulk_import.send(doc.name)
		self.assertEqual(len(self.stub.bodies("start_import")), 1)

	def test_a_locked_answer_that_lands_after_a_send_leaves_importing(self):
		# covers: AC-10, AC-11
		doc = self.new_import(rows("Pen"))
		self.assertEqual(bulk_import.check(doc.name)["status"], "Checked")
		self.stub.answers["preview_import"] = (409, {"status": "site_locked"})
		self.stub.during["preview_import"] = lambda: bulk_import.send(doc.name)
		bulk_import.check(doc.name)
		self.assertEqual(self.reload(doc).status, "Importing")

	def test_a_reload_that_lands_after_a_send_leaves_importing(self):
		# covers: AC-3, AC-10
		doc = self.new_import(rows("Pen"))
		self.assertEqual(bulk_import.check(doc.name)["status"], "Checked")
		self.stub.during["import_columns"] = lambda: bulk_import.send(doc.name)
		with self.assertRaises(frappe.ValidationError):
			bulk_import.reload_columns(doc.name)
		doc = self.reload(doc)
		self.assertEqual((doc.status, doc.site_import), ("Importing", f"CTRL-{doc.name}-1"))
		self.assertTrue(doc.checked_hash)

	def test_a_report_that_lands_after_the_next_attempt_started_writes_nothing(self):
		# covers: AC-9, AC-10
		doc = self.checked_and_sent(rows("Pen"))
		self.stub.finish(f"CTRL-{doc.name}-1", {"id-Pen": "Pen"})

		def next_attempt():
			frappe.db.set_value(DOCTYPE, doc.name, {"attempt": 2, "site_import": f"CTRL-{doc.name}-2"})
			frappe.db.commit()

		self.stub.during["import_report"] = next_attempt
		result = bulk_import.refresh(doc.name)
		self.assertEqual(result["status"], "Importing")
		self.assertEqual(result["results"], {})

	def test_a_report_that_lands_after_the_rows_were_cleared_writes_nothing(self):
		# covers: AC-14
		doc = self.checked_and_sent(rows("Pen"))
		self.stub.finish(f"CTRL-{doc.name}-1", {"id-Pen": "Pen"})

		def removed():
			bulk_import.clear_for_site(self.site.name)
			frappe.db.commit()

		self.stub.during["import_report"] = removed
		bulk_import.refresh(doc.name)
		doc = self.reload(doc)
		self.assertTrue(doc.rows_cleared_on)
		self.assertIsNone(doc.results)
		self.assertEqual(doc.status, "Failed")


class IntegrationTestBulkImportRefusals(BulkImportTestCase):
	def test_no_row_value_reaches_last_error_an_alert_or_the_error_log(self):
		# covers: AC-16
		secret = f"Customer Secret {frappe.generate_hash(length=6)}"
		grid = [{"id": "id-secret", "cells": {"name": secret, "item_usage": "Both", "rate": "77131"}}]
		started = frappe.utils.now_datetime()
		errors = []

		def note(doc):
			errors.append(self.reload(doc).last_error or "")

		for method, status in (
			("preview_import", 400),
			("preview_import", 401),
			("preview_import", 413),
			("preview_import", 503),
		):
			with self.subTest(method=method, status=status):
				doc = self.new_import(grid)
				self.stub.answers[method] = (status, {"status": "error"})
				with self.assertRaises(frappe.ValidationError):
					bulk_import.check(doc.name)
				del self.stub.answers[method]
				note(doc)
		self.stub.down.add("preview_import")
		doc = self.new_import(grid)
		with self.assertRaises(frappe.ValidationError):
			bulk_import.check(doc.name)
		self.stub.down.discard("preview_import")
		note(doc)

		for status in (400, 401, 413, 503, 500):
			with self.subTest(method="start_import", status=status):
				doc = self.new_import(grid)
				bulk_import.check(doc.name)
				self.stub.answers["start_import"] = (status, {"status": "error"})
				bulk_import.send(doc.name)
				del self.stub.answers["start_import"]
				note(doc)
		doc = self.new_import(grid)
		bulk_import.check(doc.name)
		self.stub.lose_answer = True
		bulk_import.send(doc.name)
		self.stub.lose_answer = False
		note(doc)

		for status in (401, 404, 502):
			with self.subTest(method="import_report", status=status):
				doc = self.checked_and_sent(grid)
				self.stub.answers["import_report"] = (status, {"status": "error"})
				bulk_import.refresh(doc.name)
				del self.stub.answers["import_report"]
				note(doc)

		self.assertTrue(all(errors), errors)
		alerts = json.dumps([call.args for call in self.send_ops.call_args_list], default=str)
		self.assertTrue(self.send_ops.called)
		logged = frappe.get_all("Error Log", filters={"creation": (">=", started)}, pluck="error")
		for text in (*errors, alerts, *logged):
			self.assertNotIn(secret, text)
			self.assertNotIn("77131", text)

	def test_a_locked_site_refuses_the_import(self):
		# covers: AC-11
		doc = self.new_import(rows("Pen"))
		bulk_import.check(doc.name)
		self.stub.answers["start_import"] = (409, {"status": "site_locked"})
		self.assertEqual(bulk_import.send(doc.name)["status"], "Refused")
		self.assertEqual(json.loads(self.reload(doc).rows)[0]["id"], "id-Pen")

	def test_a_refused_import_checks_again_once_the_lock_lifts(self):
		# covers: AC-11
		doc = self.new_import(rows("Pen"))
		self.stub.answers["preview_import"] = (409, {"status": "site_locked"})
		self.assertEqual(bulk_import.check(doc.name)["status"], "Refused")
		self.assertIn("locked", self.reload(doc).last_error)
		self.assertEqual(json.loads(self.reload(doc).rows)[0]["id"], "id-Pen")
		del self.stub.answers["preview_import"]
		self.assertEqual(bulk_import.check(doc.name)["status"], "Checked")
		self.assertFalse(self.reload(doc).last_error)
		bulk_import.send(doc.name)
		self.assertEqual(self.reload(doc).status, "Importing")

	def test_an_unreachable_site_leaves_the_status(self):
		# covers: AC-11
		doc = self.new_import(rows("Pen"))
		self.stub.down.add("preview_import")
		with self.assertRaises(frappe.ValidationError):
			bulk_import.check(doc.name)
		doc = self.reload(doc)
		self.assertEqual(doc.status, "Draft")
		self.assertTrue(doc.last_error)

	def test_a_bad_signature_alerts_ops_and_imports_nothing(self):
		# covers: AC-11
		doc = self.new_import(rows("Pen"))
		self.stub.secrets[self.site.name] = "f" * 64
		with self.assertRaises(frappe.ValidationError):
			bulk_import.check(doc.name)
		self.assertTrue(self.send_ops.called)
		self.assertNotIn("Pen", str(self.send_ops.call_args))

	def test_ineligible_sites_are_missing_from_the_picker_and_refused(self):
		# covers: AC-2
		frappe.set_user("Administrator")
		pool = self.other_site("pool", owner_email=None, pool_status="Ready")
		removed = self.other_site("gone", status="Removed")
		blocked = {
			"live Holding": self.case_site("Holding"),
			"live Dropping": self.case_site("Dropping"),
			"live Paused with an export": self.case_site("Paused", export_sha256="ab" * 32),
		}
		allowed = {
			"live Scheduled": self.case_site("Scheduled"),
			"live Exporting": self.case_site("Exporting"),
			"live Failed": self.case_site("Failed"),
			"live Cancelled": self.case_site("Cancelled"),
			"live Paused with no export": self.case_site("Paused"),
			"dry run Holding": self.case_site("Holding", mode="dry_run"),
			"Released from Ready": self.signup_site("Released", released_from="Ready"),
			"Released from Failed": self.signup_site("Released", released_from="Failed"),
			"Ready sign up": self.signup_site("Ready"),
		}
		not_ready = self.signup_site("Failed")
		frappe.set_user(BILLING_MANAGER)
		listed = [row[0] for row in bulk_import.site_query(DOCTYPE, "example.com", "name", 0, 500, {})]
		self.assertIn(self.site.name, listed)
		for name in (pool.name, removed.name, not_ready.name, *(row.name for row in blocked.values())):
			self.assertNotIn(name, listed)
		for case, row in allowed.items():
			self.assertIn(row.name, listed, case)
			self.assertIsNone(bulk_import.eligibility_refusal(row.name), case)
		for case, row in blocked.items():
			self.assertEqual(
				bulk_import.eligibility_refusal(row.name), f"Site {row.name} is being removed.", case
			)

		doc = self.new_import(rows("Pen"))
		self.assertEqual(bulk_import.check(doc.name)["status"], "Checked")
		frappe.set_user("Administrator")
		self.make_case(self.site.name, "Holding")
		frappe.set_user(BILLING_MANAGER)
		self.assertEqual(bulk_import.send(doc.name)["status"], "Refused")
		self.assertEqual(self.reload(doc).last_error, f"Site {self.site.name} is being removed.")
		self.assertFalse(self.stub.bodies("start_import"))
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{"doctype": DOCTYPE, "hosted_site": pool.name, "import_type": ITEM, "rows": "[]"}
			).insert()

	def test_the_picker_searches_subdomain_company_and_owner_email(self):
		# covers: AC-2
		frappe.set_user("Administrator")
		label = f"kibo{frappe.generate_hash(length=6)}"
		site = self.other_site(
			"pick",
			subdomain=label,
			company_name=f"Kibo Stationers {label}",
			owner_email=f"{label}@kibo.co.tz",
		)
		frappe.set_user(BILLING_MANAGER)
		for text in (label[2:8], f"Stationers {label}", f"{label}@kibo.co.tz"):
			found = {row[0]: row for row in bulk_import.site_query(DOCTYPE, text, "name", 0, 50, {})}
			self.assertIn(site.name, found, text)
			self.assertEqual(found[site.name][1:3], [label, f"Kibo Stationers {label}"])

	def other_site(self, prefix: str, **values):
		return make_site(f"{prefix}-{frappe.generate_hash(length=6)}.example.com", **values)

	def make_case(self, site: str, status: str, mode: str = "live", **values):
		frappe.get_doc(
			{
				"doctype": "Books Removal Case",
				"site": site,
				"mode": mode,
				"kind": "Trial",
				"removal_on": frappe.utils.today(),
				"status": status,
				"owner_email": "owner@example.com",
				"site_url": f"https://{site}",
				**values,
			}
		).insert(ignore_permissions=True)

	def case_site(self, status: str, mode: str = "live", **values):
		site = self.other_site("case")
		self.make_case(site.name, status, mode, **values)
		return site

	def signup_site(self, status: str, **values):
		site = self.other_site("signup", pool_status="Claimed")
		email = f"{frappe.generate_hash(length=8)}@example.com"
		frappe.get_doc(
			{
				"doctype": "Books Signup",
				"email": email,
				"email_key": onboarding.email_key(email),
				"clerk_user_id": f"user_{frappe.generate_hash(length=8)}",
				"first_name": "Test",
				"company_name": "Test Co",
				"subdomain": site.name.split(".")[0],
				"time_zone": "UTC",
				"status": status,
				"requested_on": utc_now(),
				"hosted_site": site.name,
				**values,
			}
		).insert(ignore_permissions=True)
		return site

	def test_the_status_cannot_be_set_from_desk(self):
		# covers: AC-1
		doc = self.new_import(rows("Pen"))
		doc.status = "Success"
		doc.success_count = 9
		doc.save()
		self.assertEqual((self.reload(doc).status, self.reload(doc).success_count), ("Draft", 0))


class IntegrationTestBulkImportRetention(BulkImportTestCase):
	def test_old_finished_imports_are_cleared_and_final(self):
		# covers: AC-14
		doc = self.checked_and_sent(rows("Pen"))
		self.stub.finish(f"CTRL-{doc.name}-1", {}, failed=("id-Pen",))
		bulk_import.refresh(doc.name)
		running = self.checked_and_sent(rows("Ink"))
		frappe.db.set_value(DOCTYPE, doc.name, "finished_on", utc_now() - timedelta(days=31))
		frappe.db.set_value(DOCTYPE, running.name, "modified", utc_now() - timedelta(days=31))
		frappe.set_user("Administrator")
		bulk_import.clear_old_rows()
		frappe.set_user(BILLING_MANAGER)

		doc = self.reload(doc)
		self.assertTrue(doc.rows_cleared_on)
		self.assertEqual((doc.rows, doc.results, doc.columns), (None, None, None))
		self.assertEqual((doc.row_count, doc.failed_count, doc.status), (1, 1, "Failed"))
		for method in (bulk_import.check, bulk_import.send, bulk_import.refresh):
			with self.subTest(method.__name__), self.assertRaises(frappe.ValidationError):
				method(doc.name)
		self.assertFalse(self.reload(running).rows_cleared_on)
		self.assertFalse(frappe.db.exists("Version", {"ref_doctype": DOCTYPE, "docname": doc.name}))

	def test_an_import_in_use_again_is_not_cleared_and_a_cleared_one_takes_no_edit(self):
		# covers: AC-14
		doc = self.new_import(rows("Pen"))
		frappe.set_user("Administrator")
		# Listed by the daily job a moment ago, then edited before its lock was taken.
		bulk_import._clear(doc.name, cutoff=utc_now() - timedelta(days=30))
		self.assertFalse(self.reload(doc).rows_cleared_on)
		bulk_import._clear(doc.name)
		frappe.set_user(BILLING_MANAGER)
		with self.assertRaises(frappe.ValidationError):
			bulk_import.reload_columns(doc.name)
		doc = self.reload(doc)
		doc.rows = json.dumps(rows("Ink"))
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		self.assertIsNone(self.reload(doc).rows)

	def test_a_removed_site_clears_all_its_imports(self):
		# covers: AC-14
		first, second = self.new_import(rows("Pen")), self.new_import(rows("Ink"))
		frappe.set_user("Administrator")
		case = frappe.get_doc(
			{
				"doctype": "Books Removal Case",
				"site": self.site.name,
				"mode": "live",
				"kind": "Trial",
				"status": "Dropping",
				"removal_on": utc_now().date(),
				"owner_email": self.site.owner_email,
				"site_url": self.site.site_url,
			}
		)
		with patch("rarebooks_control.registry.start_domain_cleanup"):
			case.insert(ignore_permissions=True)
			removal.mark_removed(case)
		self.assertTrue(all(self.reload(doc).rows_cleared_on for doc in (first, second)))


class IntegrationTestBulkImportPermissions(BulkImportTestCase):
	def test_only_a_billing_manager_reads_creates_or_calls(self):
		# covers: AC-1
		doc = self.new_import(rows("Pen"))
		for user in (SYSTEM_MANAGER, REMOVAL_MANAGER):
			frappe.set_user(user)
			with self.subTest(user):
				self.assertFalse(frappe.has_permission(DOCTYPE, "read"))
				self.assertFalse(frappe.has_permission(DOCTYPE, "create"))
				for method in (bulk_import.check, bulk_import.send, bulk_import.refresh):
					with self.assertRaises(frappe.PermissionError):
						method(doc.name)
				with self.assertRaises(frappe.PermissionError):
					bulk_import.load_columns(self.site.name)
		frappe.set_user(BILLING_MANAGER)
		self.assertFalse(frappe.has_permission(DOCTYPE, "delete", doc=doc.name))
