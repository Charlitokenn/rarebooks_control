// Copyright (c) 2026, RareBooks and contributors
// For license information, please see license.txt

// Spec 0025: a spreadsheet grid of rows for one hosted site and one document type. The site gives the columns,
// runs the dry run (Check) and the import (Import). The server checks the Billing Manager role and every rule
// again; this form only edits the rows and shows what the server answers.
const VENDOR = "/assets/rarebooks_control/js/vendor/jspreadsheet/";
const ASSETS = ["jsuites.css", "jspreadsheet.css", "jsuites.js", "jspreadsheet.js"].map((file) => VENDOR + file);
const METHOD = "rarebooks_control.bulk_import.";
const POLL_MS = 5000;
const ID_COLUMN = 0;
const RESULT_COLUMN = 1;
const FIRST_CELL_COLUMN = 2;

frappe.ui.form.on("Books Bulk Import", {
	setup(frm) {
		frm.set_query("hosted_site", () => ({ query: METHOD + "site_query" }));
	},

	onload(frm) {
		frm.bulk = { types: null, grid: null, warnings: [], poll: null };
	},

	refresh(frm) {
		frm.bulk = frm.bulk || { types: null, grid: null, warnings: [], poll: null };
		stop_polling(frm);
		set_type_options(frm);
		add_buttons(frm);
		show_headline(frm);
		frappe.require(ASSETS, () => render_grid(frm));
		if (frm.doc.status === "Importing") {
			start_polling(frm);
		}
	},

	hosted_site(frm) {
		ask_to_clear(frm, "hosted_site", () => {
			frm.bulk.types = null;
			frm.set_value("import_type", "");
			set_type_options(frm);
		});
	},

	import_type(frm) {
		ask_to_clear(frm, "import_type", () => frappe.require(ASSETS, () => render_grid(frm)));
	},

	before_unload(frm) {
		stop_polling(frm);
	},
});

// The pickers

function set_type_options(frm) {
	if (!frm.doc.hosted_site || frm.doc.attempt) {
		return;
	}
	frappe
		.call({ method: METHOD + "load_columns", args: { site: frm.doc.hosted_site } })
		.then(({ message }) => {
			frm.bulk.types = message.types || [];
			const options = frm.bulk.types.map((kind) => ({ label: __(kind.label), value: kind.doctype }));
			frm.set_df_property("import_type", "options", options);
			if (!frm.doc.columns) {
				render_grid(frm);
			}
		});
}

function ask_to_clear(frm, field, then) {
	const rows = parse(frm.doc.rows, []);
	if (!rows.length) {
		then();
		return;
	}
	frappe.confirm(
		__("Changing the site or the type clears the rows in the grid. Continue?"),
		() => {
			frm.set_value("rows", "[]");
			frm.set_value("columns", "");
			then();
		},
		() => frm.reload_doc()
	);
}

// The buttons

function add_buttons(frm) {
	if (frm.is_new() || !frappe.user.has_role("Billing Manager") || frm.doc.rows_cleared_on) {
		return;
	}
	const status = frm.doc.status;
	if (status === "Importing") {
		frm.add_custom_button(__("Refresh"), () => refresh_import(frm));
		return;
	}
	if (status === "Success") {
		return;
	}
	frm.add_custom_button(__("Check"), () => run_check(frm));
	if (status === "Checked") {
		frm.add_custom_button(__("Import"), () => run_send(frm)).addClass("btn-primary");
	}
	frm.add_custom_button(__("Load CSV"), () => load_csv(frm), __("Rows"));
	frm.add_custom_button(__("Download Template"), () => download_template(frm), __("Rows"));
	frm.add_custom_button(__("Add 10 Rows"), () => frm.bulk.grid && frm.bulk.grid.insertRow(10), __("Rows"));
	frm.add_custom_button(__("Reload Columns"), () => reload_columns(frm), __("Rows"));
}

function show_headline(frm) {
	if (frm.doc.rows_cleared_on) {
		frm.dashboard.set_headline(__("The rows of this import were cleared on {0}. It is final.", [frm.doc.rows_cleared_on]));
	} else if (frm.doc.last_error) {
		// Set as text, never HTML (AC-16).
		frm.dashboard.set_headline(frappe.utils.escape_html(frm.doc.last_error), "orange");
	} else if (frm.doc.status === "Importing") {
		frm.dashboard.set_headline(__("Importing into {0}. This page refreshes on its own.", [frm.doc.hosted_site]));
	}
}

function save_first(frm) {
	sync_rows(frm);
	return frm.is_dirty() ? frm.save() : Promise.resolve();
}

function run_check(frm) {
	save_first(frm).then(() =>
		frappe.call({ method: METHOD + "check", args: { name: frm.doc.name }, freeze: true }).then(({ message }) => {
			frm.bulk.warnings = message.warnings || [];
			frm.reload_doc().then(() => {
				if (message.status === "Checked") {
					frappe.show_alert({ message: __("Every row passed the check. You can import now."), indicator: "green" });
				} else if (frm.bulk.warnings.length) {
					frappe.show_alert({ message: __("Fix the marked cells, then check again."), indicator: "orange" });
				}
			});
		})
	);
}

function run_send(frm) {
	frappe.confirm(
		__("Import {0} rows into {1}? This saves them on the customer's site.", [count_sendable(frm), frm.doc.hosted_site]),
		() =>
			frappe
				.call({ method: METHOD + "send", args: { name: frm.doc.name }, freeze: true })
				.then(() => frm.reload_doc())
	);
}

function refresh_import(frm) {
	return frappe
		.call({ method: METHOD + "refresh", args: { name: frm.doc.name } })
		.then(({ message }) => {
			if (message.status !== "Importing") {
				stop_polling(frm);
				frm.reload_doc();
			}
		});
}

function start_polling(frm) {
	frm.bulk.poll = setInterval(() => {
		if (frm.doc.status !== "Importing" || document.hidden) {
			return;
		}
		refresh_import(frm);
	}, POLL_MS);
}

function stop_polling(frm) {
	if (frm.bulk && frm.bulk.poll) {
		clearInterval(frm.bulk.poll);
		frm.bulk.poll = null;
	}
}

function reload_columns(frm) {
	save_first(frm).then(() =>
		frappe.call({ method: METHOD + "reload_columns", args: { name: frm.doc.name }, freeze: true }).then(({ message }) => {
			if (message.gone.length) {
				frappe.msgprint(__("These columns are gone from the site, and their cells were removed: {0}", [message.gone.join(", ")]));
			}
			frm.reload_doc();
		})
	);
}

// The grid

function columns_of(frm) {
	const stored = parse(frm.doc.columns, null);
	if (stored && stored.columns) {
		return stored;
	}
	const kind = (frm.bulk.types || []).find((item) => item.doctype === frm.doc.import_type);
	return kind ? { columns: kind.columns, name_key: kind.name_key } : null;
}

function render_grid(frm) {
	const wrapper = frm.get_field("grid_html").$wrapper;
	if (frm.bulk.grid) {
		jspreadsheet.destroy(wrapper.find(".bulk-import-grid")[0], true);
		frm.bulk.grid = null;
	}
	wrapper.empty();
	const kind = columns_of(frm);
	if (!kind) {
		wrapper.append($("<p class='text-muted'>").text(__("Pick a site and a document type to see the columns.")));
		return;
	}
	const notes = $("<div class='bulk-import-notes'>").appendTo(wrapper);
	const element = $("<div class='bulk-import-grid'>").appendTo(wrapper)[0];
	const results = parse(frm.doc.results, {});
	const rows = parse(frm.doc.rows, []);
	const locked = frm.doc.status === "Importing" || frm.doc.rows_cleared_on || frm.doc.status === "Success";
	const data = rows.map((row) => [
		row.id,
		result_text(results[row.id]),
		...kind.columns.map((column) => row.cells[column.key] || ""),
	]);
	const saved = (y) => {
		const id = frm.bulk.grid && frm.bulk.grid.getValueFromCoords(ID_COLUMN, y);
		return Boolean(id && results[id] && results[id].success);
	};

	const [grid] = jspreadsheet(element, {
		worksheets: [
			{
				data,
				minDimensions: [kind.columns.length + FIRST_CELL_COLUMN, Math.max(data.length, 20)],
				parseFormulas: false,
				allowInsertColumn: false,
				allowDeleteColumn: false,
				allowRenameColumn: false,
				editable: !locked,
				tableOverflow: true,
				tableWidth: "100%",
				tableHeight: "60vh",
				columns: [
					{ title: "id", type: "hidden" },
					{ title: __("Result"), type: "text", readOnly: true, width: 220 },
					...kind.columns.map((column) => ({
						title: column.required ? `${column.label} *` : column.label,
						type: "text",
						width: 140,
					})),
				],
			},
		],
		onbeforechange: (worksheet, cell, x, y, value) =>
			saved(Number(y)) ? worksheet.getValueFromCoords(x, y) : value,
		onbeforepaste: (worksheet, block, x) => trim_paste(block, Number(x), kind.columns.length + FIRST_CELL_COLUMN),
		onbeforedeleterow: (worksheet, removed) => {
			if (removed.some((y) => saved(Number(y)))) {
				frappe.show_alert({ message: __("A row that saved on the site can't be deleted."), indicator: "orange" });
				return false;
			}
		},
		onafterchanges: () => sync_rows(frm),
		ondeleterow: () => sync_rows(frm),
		oninsertrow: () => sync_rows(frm),
		onmoverow: () => sync_rows(frm),
		onsort: () => sync_rows(frm),
		onpaste: () => sync_rows(frm),
	});
	frm.bulk.grid = grid;
	frm.bulk.kind = kind;
	paint_warnings(frm, notes);
}

function trim_paste(block, x, width) {
	// A block from Google Sheets or Excel can be pasted at any cell (AC-4). Jspreadsheet adds columns for a
	// block wider than the grid, and those cells would be lost on save, so the block is cut to fit and you
	// hear about it. Rows past the end are added as usual.
	const room = width - x;
	if (room <= 0 || block.every((line) => line.length <= room)) {
		// Nothing returned keeps Jspreadsheet's own block (a returned block replaces it).
		return undefined;
	}
	frappe.show_alert({
		message: __("The pasted block is wider than the grid. Columns past the last one were left out."),
		indicator: "orange",
	});
	return block.map((line) => line.slice(0, room).map((cell) => cell.value));
}

function result_text(result) {
	if (!result) {
		return "";
	}
	return result.success ? `✓ ${result.docname || ""}` : `✗ ${result.message || __("Failed")}`;
}

function sync_rows(frm) {
	const grid = frm.bulk.grid;
	const kind = frm.bulk.kind;
	if (!grid || !kind) {
		return;
	}
	const rows = [];
	grid.getData().forEach((line, y) => {
		const cells = {};
		kind.columns.forEach((column, index) => {
			const value = String(line[index + FIRST_CELL_COLUMN] ?? "").trim();
			if (value) {
				cells[column.key] = value;
			}
		});
		let id = String(line[ID_COLUMN] || "");
		if (!id && !Object.keys(cells).length) {
			return;
		}
		if (!id) {
			// A stable id per grid row, moved with it when rows are inserted, sorted or deleted (AC-4).
			id = crypto.randomUUID();
			grid.setValueFromCoords(ID_COLUMN, y, id, true);
		}
		rows.push({ id, cells });
	});
	const value = JSON.stringify(rows);
	if (value !== (frm.doc.rows || "[]")) {
		frm.set_value("rows", value);
	}
}

function paint_warnings(frm, notes) {
	const grid = frm.bulk.grid;
	const kind = frm.bulk.kind;
	const ids = grid.getData().map((line) => String(line[ID_COLUMN] || ""));
	const general = [];
	for (const warning of frm.bulk.warnings || []) {
		const x = kind.columns.findIndex((column) => column.key === warning.column_key);
		const targets = (warning.row_ids || []).map((id) => ids.indexOf(id)).filter((y) => y >= 0);
		if (x < 0 || !targets.length) {
			general.push(warning.message);
			continue;
		}
		for (const y of targets) {
			const name = jspreadsheet.helpers.getCellNameFromCoords(x + FIRST_CELL_COLUMN, y);
			grid.setStyle(name, "background-color", "#ffe0b2");
			grid.setComments(name, warning.message);
		}
	}
	for (const message of general) {
		// Plain text, never HTML (AC-16).
		$("<div class='alert alert-warning'>").text(message).appendTo(notes);
	}
}

function count_sendable(frm) {
	const results = parse(frm.doc.results, {});
	return parse(frm.doc.rows, []).filter((row) => !(results[row.id] && results[row.id].success)).length;
}

// CSV in and out (AC-4)

function download_template(frm) {
	const kind = columns_of(frm);
	if (!kind) {
		return;
	}
	const header = kind.columns.map((column) => csv_cell(column.label)).join(",");
	// The byte order mark makes Excel read the headers as UTF-8.
	const blob = new Blob(["﻿" + header + "\r\n"], { type: "text/csv;charset=utf-8" });
	const link = document.createElement("a");
	link.href = URL.createObjectURL(blob);
	link.download = `${frm.doc.import_type || "import"} template.csv`;
	link.click();
	URL.revokeObjectURL(link.href);
}

function csv_cell(text) {
	return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function load_csv(frm) {
	const input = $("<input type='file' accept='.csv,text/csv'>");
	input.on("change", () => {
		const file = input[0].files[0];
		if (!file) {
			return;
		}
		file.text().then((text) => add_csv_rows(frm, read_csv(text)));
	});
	input.trigger("click");
}

function read_csv(text) {
	// Excel writes a byte order mark, and on a comma decimal locale it separates with ";".
	const clean = text.replace(/^﻿/, "");
	const first = clean.split(/\r?\n/, 1)[0];
	const delimiter = (first.match(/;/g) || []).length > (first.match(/,/g) || []).length ? ";" : ",";
	return frappe.utils.csv_to_array(clean, delimiter);
}

function header_key(kind, text) {
	// A header matches a column label, with or without the grid's " *" required mark, or a column key.
	const label = String(text || "").trim().replace(/\s*\*$/, "");
	const column =
		kind.columns.find((item) => item.label === label) || kind.columns.find((item) => item.key === label);
	return column ? column.key : null;
}

function add_csv_rows(frm, matrix) {
	const kind = frm.bulk.kind;
	if (!kind || !matrix.length) {
		return;
	}
	// Keep any cell typed since the last change event before the grid is drawn again.
	sync_rows(frm);
	const [header, ...lines] = matrix;
	const keys = header.map((text) => header_key(kind, text));
	const unknown = header.filter((label, index) => String(label || "").trim() && !keys[index]);
	const rows = parse(frm.doc.rows, []);
	for (const line of lines) {
		const cells = {};
		line.forEach((value, index) => {
			if (keys[index] && String(value).trim()) {
				cells[keys[index]] = String(value).trim();
			}
		});
		if (Object.keys(cells).length) {
			rows.push({ id: crypto.randomUUID(), cells });
		}
	}
	frm.set_value("rows", JSON.stringify(rows));
	render_grid(frm);
	if (unknown.length) {
		frappe.msgprint(__("These columns are not in the template and were ignored: {0}", [unknown.join(", ")]));
	}
}

function parse(text, fallback) {
	try {
		return text ? JSON.parse(text) : fallback;
	} catch {
		return fallback;
	}
}
