"""Spec 0010 AC-9: Books Hosting Bench is gone with the runner. No live rows exist (the runner never ran in
production), so the doctype and its table are simply removed."""

import frappe


def execute():
	frappe.delete_doc("DocType", "Books Hosting Bench", ignore_missing=True, force=True)
	frappe.db.sql_ddl("drop table if exists `tabBooks Hosting Bench`")
