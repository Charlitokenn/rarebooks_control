// Copyright (c) 2026, RareBooks and contributors
// For license information, please see license.txt

// Spec 0014 AC-18: Retry on a Failed sign up. The server refuses it until the old site is discarded and its
// address cleaned up, and checks the Billing Manager role again.
frappe.ui.form.on("Books Signup", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.status !== "Failed" || !frappe.user.has_role("Billing Manager")) {
			return;
		}
		frm.add_custom_button(__("Retry"), () => retry_signup(frm));
		frm.dashboard.set_headline(
			__("This sign up failed. Retry sends it round again with the same subdomain, once its old site is discarded.")
		);
	},
});

function retry_signup(frm) {
	frappe.confirm(__("Send sign up {0} round again with the subdomain {1}?", [frm.doc.name, frm.doc.subdomain]), () =>
		frappe
			.call({
				method: "rarebooks_control.onboarding.retry_signup",
				args: { signup: frm.doc.name },
				freeze: true,
			})
			.then(() => frm.reload_doc())
	);
}
