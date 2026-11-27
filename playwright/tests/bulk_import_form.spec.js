// The Books Bulk Import desk form (spec 0025), in a real browser as a Billing Manager. The data comes from
// `rarebooks_control.tests.ui_setup.prepare`; the site's dry run (`check`) and report (`refresh`) are stubbed
// here, since a test bench has no customer site to call.
const { execFileSync } = require("node:child_process");
const crypto = require("node:crypto");
const path = require("node:path");
const { test, expect } = require("@playwright/test");

const MARKED = "rgb(255, 224, 178)";
const POLL_MS = 5000;

let data;

test.describe.configure({ mode: "serial" });

test.beforeAll(({ baseURL }) => {
	data = prepare(new URL(baseURL).hostname);
});

test.beforeEach(async ({ page }) => {
	await login(page);
});

test("the type picker lists the site's types and builds the grid (AC-3)", async ({ page }) => {
	// covers: AC-3
	await page.goto("/app/books-bulk-import/new");
	const site = page.locator('input[data-fieldname="hosted_site"]');
	await site.click();
	await site.pressSequentially(data.subdomain, { delay: 30 });
	await page.getByText(data.site, { exact: true }).first().click();
	await expect.poll(() => page.evaluate(() => window.cur_frm && cur_frm.doc.hosted_site)).toBe(data.site);

	const type = page.locator('input[data-fieldname="import_type"]');
	await type.click();
	await type.pressSequentially("Ite", { delay: 50 });
	const option = page.locator('[role="listbox"] li:visible', { hasText: /^Item$/ });
	await expect(option).toBeVisible();
	await option.click();

	await expect.poll(() => page.evaluate(() => window.cur_frm && cur_frm.doc.import_type)).toBe("Books Item");
	await expect(page.locator(".bulk-import-grid thead")).toContainText("Item Name *");
});

test("Check marks each wrong cell with the site's message (AC-5)", async ({ page }) => {
	// covers: AC-5
	const message = '"Bogus" is not an option of Purpose';
	await page.route("**/api/method/rarebooks_control.bulk_import.check", (route) =>
		route.fulfill({
			json: {
				message: {
					status: "Draft",
					warnings: [{ row_ids: [data.bad_row], column_key: "item_usage", message, blocking: true }],
				},
			},
		})
	);
	await page.goto(`/app/books-bulk-import/${data.draft}`);
	await expect(page.locator(".bulk-import-grid")).toBeVisible();
	await page.getByRole("button", { name: "Check", exact: true }).click();

	const bad = page.locator(".bulk-import-grid tbody td", { hasText: /^Bogus$/ });
	await expect(bad).toHaveCSS("background-color", MARKED);
	await expect(bad).toHaveAttribute("title", message);
	const good = page.locator(".bulk-import-grid tbody td", { hasText: /^Sales$/ });
	await expect(good).not.toHaveCSS("background-color", MARKED);
});

test("an Importing import polls once every 5 seconds, even after reloads (AC-9)", async ({ page }) => {
	// covers: AC-9
	let refreshes = 0;
	await page.route("**/api/method/rarebooks_control.bulk_import.refresh", (route) => {
		refreshes += 1;
		return route.fulfill({
			json: { message: { status: "Importing", success_count: 0, failed_count: 0, results: {}, last_error: null } },
		});
	});
	await page.goto(`/app/books-bulk-import/${data.importing}`);
	await expect.poll(() => page.evaluate(() => window.cur_frm && cur_frm.doc.status)).toBe("Importing");
	await expect(page.getByRole("button", { name: "Check", exact: true })).toHaveCount(0);

	// Realtime updates reload an open form; each reload used to leave one more 5 second timer running.
	for (let reload = 0; reload < 2; reload++) {
		await page.evaluate(() => cur_frm.reload_doc());
		await page.waitForTimeout(1000);
	}
	refreshes = 0;
	await page.waitForTimeout(POLL_MS * 2 + 500);
	// One timer gives 2 refreshes in this window; three timers would give 6.
	expect(refreshes).toBeGreaterThanOrEqual(1);
	expect(refreshes).toBeLessThanOrEqual(3);
});

function prepare(site) {
	// Frappe passes NODE_PATH as <bench>/apps/frappe/node_modules; BENCH_PATH overrides it.
	const bench = process.env.BENCH_PATH || path.resolve(process.env.NODE_PATH || "", "../../..");
	const output = execFileSync(
		path.join(bench, "env", "bin", "python"),
		[
			"-m",
			"frappe.utils.bench_helper",
			"frappe",
			"--site",
			site,
			"execute",
			"rarebooks_control.tests.ui_setup.prepare",
		],
		{ cwd: path.join(bench, "sites"), encoding: "utf8" }
	);
	const last = output.trim().split("\n").pop();
	return JSON.parse(last);
}

async function login(page) {
	// Two factor auth is on for every role, so the password step answers a tmp_id and the TOTP code finishes it.
	// The calls go from the page, since Node's resolver doesn't know `*.localhost` the way Chromium does.
	await page.goto("/login");
	for (let attempt = 0; attempt < 2; attempt++) {
		const first = await post(page, { usr: data.user, pwd: data.password });
		expect(first.status, JSON.stringify(first.body)).toBe(200);
		const second = await post(page, { otp: totp(data.otp_secret), tmp_id: first.body.tmp_id });
		if (second.status === 200) {
			return;
		}
		// A code made at the end of its 30 second step can be stale on arrival; the next one is fresh.
		await page.waitForTimeout(2000);
	}
	throw new Error("login with the TOTP code failed twice");
}

function post(page, body) {
	return page.evaluate(async (data) => {
		const response = await fetch("/api/method/login", {
			method: "POST",
			headers: { "Content-Type": "application/json", Accept: "application/json" },
			body: JSON.stringify(data),
		});
		return { status: response.status, body: await response.json().catch(() => null) };
	}, body);
}

function totp(secret) {
	// RFC 6238 with SHA-1, 6 digits and 30 second steps, as pyotp's TOTP checks it.
	const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
	const bits = [...secret.replace(/=+$/, "").toUpperCase()]
		.map((char) => alphabet.indexOf(char).toString(2).padStart(5, "0"))
		.join("");
	const key = Buffer.from(bits.match(/.{8}/g).map((byte) => parseInt(byte, 2)));
	const counter = Buffer.alloc(8);
	counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30000)));
	const hash = crypto.createHmac("sha1", key).update(counter).digest();
	const offset = hash[hash.length - 1] & 15;
	return String((hash.readUInt32BE(offset) & 0x7fffffff) % 1000000).padStart(6, "0");
}
