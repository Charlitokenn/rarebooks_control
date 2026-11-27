// Desk UI tests, run by Frappe with `bench --site <site> run-ui-tests rarebooks_control --headless`. Frappe
// installs @playwright/test in its own node_modules and passes BASE_URL and NODE_PATH. The site must be served
// (`bench start`) and allow tests.
const { defineConfig } = require("@playwright/test");

const ci = Boolean(process.env.CI);

module.exports = defineConfig({
	testDir: "./playwright/tests",
	fullyParallel: false,
	workers: 1,
	forbidOnly: ci,
	retries: ci ? 1 : 0,
	timeout: 120000,
	expect: { timeout: 20000 },
	reporter: ci ? [["list"], ["github"]] : [["list"]],
	use: {
		baseURL: process.env.BASE_URL || "http://localhost:8000",
		viewport: { width: 1400, height: 960 },
		actionTimeout: 20000,
		navigationTimeout: 30000,
		trace: "on-first-retry",
		screenshot: "only-on-failure",
	},
});
