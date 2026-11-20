// Copyright (c) 2026, RareBooks and contributors
// For license information, please see license.txt

// Spec 0008 AC-14, spec 0010 AC-11 and AC-12: the manual edit, Push now, Retry config and Rotate secret.
// The server checks every rule again.
frappe.ui.form.on("Books Hosted Site", {
	refresh(frm) {
		// Refresh runs after each load and save, so this is the saved provider.
		frm.saved_provider = frm.doc.provider;
		frm.set_df_property("provider_reference", "read_only", 1);
		if (frm.is_new() || frm.doc.status === "Removed" || !frappe.user.has_role("Billing Manager")) {
			return;
		}
		frm.add_custom_button(__("Push now"), () => push_now(frm));
		frm.add_custom_button(__("Retry config"), () => retry_config(frm));
		if (frm.doc.pending_secret) {
			frm.dashboard.set_headline(
				__("A secret rotation waits for the site to answer a ping with its new secret.")
			);
		} else if (frm.doc.config_status === "Active") {
			frm.add_custom_button(__("Rotate secret"), () => rotate_secret(frm));
		} else {
			frm.dashboard.set_headline(
				__("The site's config is {0}. It gets no push or removal round until it is Active.", [
					__(frm.doc.config_status),
				])
			);
		}
	},

	provider(frm) {
		// `provider_reference` is edited only in the same save as a provider change (AC-14).
		const changed = frm.doc.provider !== frm.saved_provider;
		frm.set_df_property("provider_reference", "read_only", changed ? 0 : 1);
	},
});

function push_now(frm) {
	frappe.call({
		method: "rarebooks_control.billing.push_now",
		args: { site: frm.doc.name },
		freeze: true,
		callback() {
			frappe.show_alert({ message: __("Push queued"), indicator: "green" });
			frm.reload_doc();
		},
	});
}

function rotate_secret(frm) {
	frappe.confirm(
		__(
			"Make a new secret for this site? It is written into the site's config through Frappe Cloud, and the old secret stops working once the site answers with the new one."
		),
		() =>
			frappe.call({
				method: "rarebooks_control.registry.rotate_secret",
				args: { site: frm.doc.name },
				freeze: true,
				callback() {
					frappe.show_alert({ message: __("Rotation started"), indicator: "green" });
					frm.reload_doc();
				},
			})
	);
}

function retry_config(frm) {
	frappe.call({
		method: "rarebooks_control.registry.retry_config",
		args: { site: frm.doc.name },
		freeze: true,
		callback() {
			frappe.show_alert({ message: __("Config sent"), indicator: "green" });
			frm.reload_doc();
		},
		error() {
			frm.reload_doc();
		},
	});
}
