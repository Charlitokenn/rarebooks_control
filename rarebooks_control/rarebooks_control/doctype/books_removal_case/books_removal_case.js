// Copyright (c) 2026, RareBooks and contributors
// For license information, please see license.txt

// Spec 0007 AC-13: the Removal Manager actions. The server checks the role and the status again.
const ACTIONS = [
	{ label: __("Approve"), method: "approve_case", statuses: ["Holding"] },
	{ label: __("Pause"), method: "pause_case", statuses: ["Scheduled", "Holding"], reason: true },
	{ label: __("Resume"), method: "resume_case", statuses: ["Paused"] },
	{ label: __("Retry"), method: "retry_case", statuses: ["Failed"] },
];

frappe.ui.form.on("Books Removal Case", {
	refresh(frm) {
		if (!frappe.user.has_role("Removal Manager")) return;
		for (const action of ACTIONS) {
			if (!action.statuses.includes(frm.doc.status)) continue;
			if (action.method === "approve_case" && frm.doc.approved_by) continue;
			frm.add_custom_button(action.label, () => run(frm, action));
		}
	},
});

function run(frm, action) {
	const call = (args) =>
		frappe
			.xcall(`rarebooks_control.removal.${action.method}`, { case: frm.doc.name, ...args })
			.then(() => frm.reload_doc());
	if (!action.reason) {
		call({});
		return;
	}
	frappe.prompt(
		{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
		(values) => call({ reason: values.reason }),
		__("Pause removal"),
	);
}
