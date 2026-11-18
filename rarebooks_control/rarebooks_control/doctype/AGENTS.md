# Billing DocTypes

## Overview

The data model of the control site (spec 0008, plus Books Hosting Bench from spec 0007). Each DocType is a folder with its schema JSON, Python controller, an empty desk JS stub and, where it has rules, an integration test.

## Key files

| File | Owns |
|---|---|
| `books_hosted_site/` | The registry, one row per hosted site (named by `site`): billing state, push state, control secrets |
| `books_plan/` | Prices per provider and period: ClickPesa yearly in TZS, PayPal in USD with its PayPal plan id |
| `books_checkout/` | One checkout attempt, found by its hidden `token_hash` |
| `books_payment_event/` | Provider events, unique by `event_id` so each is handled once |
| `books_hosting_bench/` | The benches that host sites |
| `../../tests/data_model.py` | Test helpers: one user per role, `make_bench`, `make_site` |

## Conventions

- Edit DocTypes in desk with developer mode on and let Frappe export the JSON. Never hand edit the `# begin: auto-generated types` block in a controller; Frappe rewrites it on save.
- Set every permission right explicitly in the schema, since Frappe otherwise grants create, write, delete and share to a new row. No role can delete any billing record.
- Roles: Billing Manager writes billing data and plans; System Manager and Removal Manager only read billing data. System Manager creates benches, Removal Manager edits them.
- Books Hosted Site, Books Checkout and Books Payment Event are created only by the control site's own code, never from desk.
- Billing fields on Books Hosted Site sit at permlevel 1, so only a Billing Manager writes them. A new billing field needs permlevel 1 and an entry in `BILLING_FIELDS` in the test.
- Secrets live in hidden `Password` fields (stored encrypted, kept out of Version diffs). Read them with `doc.get_password(...)`.
- Tests subclass `frappe.tests.IntegrationTestCase` and build rows through the helpers in `tests/data_model.py`.

## Gotchas

- A hidden field cannot be marked mandatory, so required hidden values (`control_secret`, `token_hash`) are checked in `validate()` instead.
- Frappe skips Version records in tests; call `save(ignore_version=False)` when a test checks track changes.
- Switch back with `frappe.set_user("Administrator")` in `tearDown` after a test acts as a role user.
- `site_url` must start with `https://`.

## Related specs

Spec 0007 (hosting benches) and spec 0008 (billing data model and roles), kept outside this repo.

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
