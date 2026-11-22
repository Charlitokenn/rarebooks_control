# Billing DocTypes

## Overview

The data model of the control site (spec 0008, plus the removal DocTypes from specs 0007 and 0010). Each DocType is a folder with its schema JSON, Python controller, an empty desk JS stub and, where it has rules, an integration test.

## Key files

| File | Owns |
|---|---|
| `books_hosted_site/` | The registry, one row per hosted site (named by `site`): billing state, push state, control secrets |
| `books_plan/` | Prices per provider and period: ClickPesa yearly in TZS, PayPal in USD with its PayPal plan id |
| `books_checkout/` | One checkout attempt, found by its hidden `token_hash` |
| `books_payment_event/` | Provider events, unique by `event_id` so each is handled once |
| `books_removal_settings/` | Single: removal `mode`, pause, approval, the daily drop cap, and `books_bench_group`, the Frappe Cloud bench group that holds the customer sites (spec 0010) |
| `books_removal_case/` | One removal of one hosted site (spec 0007): status, export, hold, approvals. `books_removal_case.js` adds the Approve, Pause, Resume and Retry buttons |
| `books_removal_warning/` | Child table of a case: one row per warning email sent |
| `books_signup/` | One self serve sign up from the marketing site (spec 0014): status Queued, Claiming, Preparing, Ready or Failed, its step, and the claimed `hosted_site`. Unique on `email_key`, `clerk_user_id` and `subdomain` |
| `books_onboarding_settings/` | Single: sign up pause, `pool_size`, the daily claim cap, `discard_mode`, the pool build pause, the Frappe Cloud server, plan and domain for new sites, and the hidden `marketing_secret` with `marketing_notify_url` (spec 0014) |
| `../../tests/data_model.py` | Test helpers: one user per role, `make_site` |

## Conventions

- Edit DocTypes in desk with developer mode on and let Frappe export the JSON. Never hand edit the `# begin: auto-generated types` block in a controller; Frappe rewrites it on save.
- Set every permission right explicitly in the schema, since Frappe otherwise grants create, write, delete and share to a new row. No role can delete any billing record.
- Roles: Billing Manager writes billing data and plans; System Manager and Removal Manager only read billing data. Removal Manager edits Books Removal Settings; System Manager only reads it.
- Books Hosted Site, Books Checkout and Books Payment Event are created only by the control site's own code, never from desk.
- Books Removal Case is read only in desk for every role, keeps track changes, and no role deletes it. Its status changes only through `removal.py`: the signed endpoints the customer site calls (`export_ready`, `step_failed`) and the Removal Manager actions (spec 0007 AC-13, AC-17).
- The domain fields on Books Hosted Site (`DOMAIN_FIELDS` in its controller) are read only for every role, the API included, and written only by `registry.py` and `removal.mark_removed`. `subdomain` is unique and never changes once set, and a blank one is saved as NULL, since MariaDB's unique index allows many NULLs but only one empty string (spec 0012 AC-2, AC-12).
- `site_url` changes only at the last domain step (`Primary` in `registry.move_domain`), in the same save that sets `domain_status` to Active (spec 0012 AC-8).
- Billing fields on Books Hosted Site sit at permlevel 1, so only a Billing Manager writes them. A new billing field needs permlevel 1 and an entry in `BILLING_FIELDS` in the test.
- Secrets live in hidden `Password` fields (stored encrypted, kept out of Version diffs). Read them with `doc.get_password(...)`.
- Tests subclass `frappe.tests.IntegrationTestCase` and build rows through the helpers in `tests/data_model.py`.

## Gotchas

- A hidden field cannot be marked mandatory, so required hidden values (`control_secret`, `token_hash`) are checked in `validate()` instead.
- Frappe skips Version records in tests; call `save(ignore_version=False)` when a test checks track changes.
- Switch back with `frappe.set_user("Administrator")` in `tearDown` after a test acts as a role user.
- `site_url` must start with `https://`.
- `owner_email` on Books Hosted Site may be blank only while `pool_status` is Building or Ready (a warm pool site no one has claimed yet, spec 0014). Pushes, the removal round and `get_state` skip those rows.

## Related specs

Spec 0007 (trial end removal), spec 0008 (billing data model and roles), spec 0010 (Frappe Cloud hosting), spec 0011 (archive call mark), spec 0012 (custom domains), spec 0013 (archived site proof) and spec 0014 (self serve onboarding), kept in the `rarebooks-web` repo.

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
