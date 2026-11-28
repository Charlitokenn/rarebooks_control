import frappe


def execute():
	"""Spec 0014: every site registered before the pool is a hand made site, with a blank `pool_status`."""
	frappe.db.sql("update `tabBooks Hosted Site` set pool_status = null where pool_status = ''")
