// Copyright (c) 2026, RareBooks and contributors
// For license information, please see license.txt

// Spec 0014 AC-18: Retry on a Failed sign up. The server refuses it until the old site is discarded and its
// address cleaned up, and checks the Billing Manager role again.
// Spec 0016 AC-1: Release sign up on a Ready or Failed one, so the person can sign up again. The server checks
// the role, the status and the old site again; the dialog only explains and asks for the reason.
const REASON_MAX = 280;

frappe.ui.form.on("Books Signup", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role("Billing Manager")) {
			return;
		}
		if (frm.doc.status === "Failed") {
			frm.add_custom_button(__("Retry"), () => retry_signup(frm));
			frm.dashboard.set_headline(
				__("This sign up failed. Retry sends it round again with the same subdomain, once its old site is discarded.")
			);
		}
		if (["Ready", "Failed"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Release sign up"), () => release_signup(frm));
		}
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

async function release_signup(frm) {
	const released_before = frm.doc.email_key
		? await frappe.db.count("Books Signup", {
				filters: { status: "Released", released_email_key: frm.doc.email_key },
		  })
		: 0;
	const dialog = new frappe.ui.Dialog({
		title: __("Release sign up {0}", [frm.doc.name]),
		fields: [
			{ fieldname: "notes", fieldtype: "HTML", options: release_notes(frm, released_before) },
			{
				fieldname: "reason",
				fieldtype: "Small Text",
				label: __("Reason"),
				reqd: 1,
				description: __("1 to {0} characters.", [REASON_MAX]),
			},
		],
		primary_action_label: __("Release sign up"),
		primary_action({ reason }) {
			const trimmed = (reason || "").trim();
			if (!trimmed || trimmed.length > REASON_MAX) {
				frappe.msgprint(__("A reason is 1 to 280 characters."));
				return;
			}
			frappe
				.call({
					method: "rarebooks_control.onboarding.release_signup",
					args: { signup: frm.doc.name, reason: trimmed },
					freeze: true,
				})
				.then(() => {
					dialog.hide();
					frm.reload_doc();
				});
		},
	});
	dialog.show();
}

function release_notes(frm, released_before) {
	const escape = frappe.utils.escape_html;
	const lines = [
		`<p><b>${__("Email")}:</b> ${escape(frm.doc.email || "")}<br><b>${__("Site")}:</b> ${escape(
			frm.doc.hosted_site || __("none")
		)}</p>`,
		`<ul>
			<li>${__("The old site keeps running and is not changed.")}</li>
			<li>${__(
				"The person reaches the old site through its own /login address, because their dashboard will show only their new request."
			)}</li>
			<li>${__("Their next sign up uses whatever email their Clerk account holds then.")}</li>
		</ul>`,
	];
	if (released_before) {
		lines.push(
			`<div class="alert alert-warning">${__("This email was released {0} times before.", [released_before])}</div>`
		);
	}
	return lines.join("");
}
