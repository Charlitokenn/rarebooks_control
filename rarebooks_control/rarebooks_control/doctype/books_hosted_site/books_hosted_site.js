// Copyright (c) 2026, RareBooks and contributors
// For license information, please see license.txt

// Spec 0008 AC-14, spec 0010 AC-11 and AC-12: the manual edit, Push now, Retry config and Rotate secret.
// Spec 0012 AC-4, AC-10 and AC-14: Add domain, Retry domain and the domain headline.
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
		const headlines = [];
		if (frm.doc.pending_secret) {
			headlines.push(__("A secret rotation waits for the site to answer a ping with its new secret."));
		} else if (frm.doc.config_status === "Active") {
			frm.add_custom_button(__("Rotate secret"), () => rotate_secret(frm));
		} else {
			headlines.push(
				__("The site's config is {0}. It gets no push or removal round until it is Active.", [
					__(frm.doc.config_status),
				])
			);
		}
		if (!frm.doc.subdomain) {
			frm.add_custom_button(__("Add domain"), () => add_domain(frm));
		} else if (frm.doc.domain_status === "Failed" && frm.doc.domain_step !== "Cleanup") {
			frm.add_custom_button(__("Retry domain"), () => retry_domain(frm));
		}
		if (frm.doc.domain_status && frm.doc.domain_status !== "Active") {
			headlines.push(
				__("The domain is {0} at the {1} step. The site keeps working on {2}.", [
					__(frm.doc.domain_status),
					__(frm.doc.domain_step),
					frm.doc.site_url,
				])
			);
		}
		if (headlines.length) {
			frm.dashboard.set_headline(headlines.map((line) => frappe.utils.escape_html(line)).join("<br>"));
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

function add_domain(frm) {
	frappe.prompt(
		{
			fieldname: "subdomain",
			fieldtype: "Data",
			label: __("Subdomain"),
			reqd: 1,
			default: frm.doc.site.split(".")[0],
		},
		(values) =>
			frappe.call({
				method: "rarebooks_control.registry.add_domain",
				args: { site: frm.doc.name, subdomain: values.subdomain },
				freeze: true,
				callback() {
					frappe.show_alert({ message: __("Subdomain saved"), indicator: "green" });
					frm.reload_doc();
				},
			}),
		__("Add domain"),
		__("Add")
	);
}

function retry_domain(frm) {
	frappe.call({
		method: "rarebooks_control.registry.retry_domain",
		args: { site: frm.doc.name },
		freeze: true,
		callback() {
			frappe.show_alert({ message: __("Domain steps resumed"), indicator: "green" });
			frm.reload_doc();
		},
	});
}
