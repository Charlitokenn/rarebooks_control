// Copyright (c) 2026, RareBooks and contributors
// For license information, please see license.txt

// Spec 0010 AC-10: register a Frappe Cloud site. The server checks every rule again.
frappe.listview_settings["Books Hosted Site"] = {
	onload(listview) {
		if (!frappe.user.has_role("Billing Manager")) return;
		listview.page.add_inner_button(__("Register site"), () => register_site(listview));
	},
};

function register_site(listview) {
	const dialog = new frappe.ui.Dialog({
		title: __("Register site"),
		fields: [
			{
				fieldname: "site",
				fieldtype: "Data",
				label: __("Frappe Cloud site name"),
				reqd: 1,
			},
			{ fieldname: "owner_email", fieldtype: "Data", options: "Email", label: __("Owner email"), reqd: 1 },
			{ fieldname: "site_url", fieldtype: "Data", label: __("Site URL (https://)"), reqd: 1 },
		],
		primary_action_label: __("Register"),
		primary_action(values) {
			frappe.call({
				method: "rarebooks_control.registry.register_site",
				args: values,
				freeze: true,
				callback() {
					dialog.hide();
					frappe.show_alert({ message: __("Site registered, config sent"), indicator: "green" });
					listview.refresh();
				},
				error() {
					listview.refresh();
				},
			});
		},
	});
	dialog.show();
}
