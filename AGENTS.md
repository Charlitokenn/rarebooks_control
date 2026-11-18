# Rarebooks Control

The RareBooks billing control site: a Frappe app holding the hosted site registry, plans, checkouts, payment events and the pushes to hosted sites.

## Stack

- **Language / Runtime**: Python 3.14 (`requires-python = ">=3.14"`)
- **Framework**: Frappe version 16 (`frappe >=16.36.0,<17.0.0`), installed and managed by bench, not pip
- **Key dependencies**: MariaDB 11.8, Redis (cache and queue), `requests` (comes with Frappe); payment providers ClickPesa (TZS) and PayPal (USD)
- **Package manager**: bench (`frappe-bench` via `uv tool install`); build backend flit_core

## Build approach

Tracer Bullet: each slice is built end to end and works before the next starts. Source: the scope file in the sibling `rarebooks-web` repo (`docs/scope/scope.md`, feature 8).

## Commands

```bash
# Install (from your bench folder)
bench get-app --soft-link <path-to-this-repo>
bench --site <site> install-app rarebooks_control
bench --site <site> migrate

# Lint and format check (same version as CI and pre-commit)
uvx ruff@0.14.10 check rarebooks_control
uvx ruff@0.14.10 format --check rarebooks_control

# Test (the site needs allow_tests set to 1)
bench --site <site> run-tests --app rarebooks_control
```

There is no build step and no `package.json`; run everything through bench inside a bench folder.

## Specs

Specs are referenced by number in commits, comments and tests (for example spec 0008, AC-14), but they are not stored in this repo. The pre-commit config points to the sibling `rarebooks-web` repo, which likely holds them.

## Rules

- Tabs for Python and JS indentation, double quotes, line length 110 (ruff config in `pyproject.toml`). Keep the ruff version (`0.14.10`) the same in `.github/workflows/ci.yml`, `.pre-commit-config.yaml` and the `rarebooks-web` repo.
- Every whitelisted method must carry type annotations; `hooks.py` sets `require_type_annotated_api_methods = True`, so Frappe refuses an unannotated one.
- Wrap every user facing message in `_()` for translation; raise errors with `frappe.throw`.
- Comments explain why and cite the spec and acceptance criterion they come from (`spec 0008 AC-1`). Tests mark what they prove with `# covers: AC-n`.
- Money is a `Decimal` rounded with `ROUND_HALF_UP` to the currency's smallest unit (whole TZS, USD cents), never a float.
- Schema changes go through `bench migrate`; data fixes go in `rarebooks_control/patches/` and are listed in `patches.txt`.
- Signed endpoints (`api.py`, `webhooks.py`) answer through `signing.signed_response`, never `frappe.throw` or Frappe's `message` wrapper, so the bytes signed are the bytes sent (spec 0008 AC-5).
- `tests/signing_vectors.json` must stay byte for byte identical to `frappe_books/tests/signing_vectors.json` in `rarebooks-web`. Change both together.
- Every payment change goes through `billing.confirm_*`: lock the site row, ask the provider, then write. A webhook body alone never changes billing (spec 0008 AC-7).
- Only `providers/clickpesa.py` talks to ClickPesa; tests patch its functions. Its credentials come from site config (`clickpesa_client_id`, `clickpesa_api_key`, `clickpesa_checksum_key`) and are never logged.

## Agent skills

No Agent Skills found for this stack yet (searched November 2026).

MCP servers: paypal/paypal-mcp-server (recommended), Sena-IT/frappe-mcp-server (recommended, community)

## Context files

- [rarebooks_control/rarebooks_control/doctype/AGENTS.md](rarebooks_control/rarebooks_control/doctype/AGENTS.md): the billing DocTypes, their permissions, secrets and test helpers

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
