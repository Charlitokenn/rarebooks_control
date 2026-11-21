# Review, v16-dashboard-features, 2026-11-18

**Reviewed by**: Sonnet 5.5 (author on a different model)
**Scope**: 44 files, branch vs main (merge base 67472af)
**Verdict**: Approve with nits

## Summary
This change builds the first thread of spec 0008: the data model, signing, `register_site`, `get_state`, `create_checkout`, the `go` link, the ClickPesa webhook, the one confirm step, the AC-8 date rule and the push to a customer site. The money and security design is solid. The webhook body is never trusted, ClickPesa is asked for the truth while the site row is locked, events are deduplicated, money is Decimal, and secrets stay in Password fields. I found no blockers and no majors. The minors below are mostly robustness gaps and tests that would not catch a real mistake. I could not run the tests (no bench here). Ruff 0.14.10 check and format both pass. The signing vectors file is identical in both repos, and the push and answer shapes match `frappe_books` (`receive_state`, `call_control`, `register`).

## Minor
### 🟡 A non ASCII signature or checksum gives a 500 instead of a 401, `rarebooks_control/signing.py:35` and `rarebooks_control/providers/clickpesa.py:120`
**Problem**: `hmac.compare_digest` raises `TypeError` when one of the two strings has a non ASCII character. The `X-Books-Signature` header (Werkzeug decodes it as latin 1) and the JSON `checksum` value are both attacker controlled strings. I confirmed the `TypeError` in Python.
**Why it matters**: The spec says a bad signature gets 401 with no detail (AC-1, AC-2, AC-5, AC-7). Today anyone can get a 500 from the webhook, and from any signed endpoint for a site name they know. Each 500 writes an Error Log row. Nothing is written to billing data, so this is noise and a contract slip, not a breach.
**Suggested fix**: Compare bytes (encode both sides, or encode with `errors="replace"` first), or return False when the received value is not ASCII. Add a test with a non ASCII header and a non ASCII checksum.

### 🟡 The ClickPesa checksum tests prove nothing about ClickPesa's real rule, `rarebooks_control/tests/control.py:webhook_body`
**Problem**: `webhook_body` signs with `clickpesa.checksum`, and the webhook verifies with the same function, so every test passes whatever the canonical form is. There is no fixed example from ClickPesa's docs or sandbox. The canonical form also depends on how Python prints numbers (`1000.0` stays `1000.0`, JavaScript prints `1000`), and the spec lists ClickPesa's exact rules as still open.
**Why it matters**: If the rule differs even slightly, every real webhook gets 401 and no payment unlocks anything. It fails closed, so it is safe, but it will only show up in the sandbox.
**Suggested fix**: Add one known answer vector taken from a real sandbox webhook (nested data, a number, a non ASCII name) as a test, and note where it came from.

### 🟡 `providers/clickpesa.py` has no direct tests, `rarebooks_control/providers/clickpesa.py:154`
**Problem**: Token caching, the 401 token drop, the 404 as "no payments", a dict answer versus a list answer, the `https://` check on the link and the missing key branch are all untested. Every test patches `create_checkout_link` or `payments`.
**Why it matters**: This is the only code that talks to the money provider, and it has real branching.
**Suggested fix**: Add a small test file that patches `requests.request` and `requests.post`, covering each branch above.

### 🟡 Row locks are held across slow network calls, `rarebooks_control/billing.py:369` and `rarebooks_control/api.py:96`
**Problem**: `confirm_clickpesa` holds the site and checkout locks while calling ClickPesa (token plus query, 10 seconds each). `create_checkout` holds the site lock and the Books Checkout naming series row (taken by `set_new_name`) while it calls ClickPesa. `alert_ops` also sends mail with `now=True` inside the same transaction.
**Why it matters**: The spec accepts the 10 second hold on the site row. The naming series row is shared by every site, so two customers creating a checkout at once queue behind each other's ClickPesa call, and a slow ClickPesa can hit the lock wait timeout. Volume is low today.
**Suggested fix**: Leave as is for now, but record it. Later, insert the checkout (and commit) before calling ClickPesa, and queue ops mail instead of sending it inside the lock.

### 🟡 The event row is read before the lock, so a concurrent duplicate can misbehave, `rarebooks_control/billing.py:373`
**Problem**: The webhook reads the event and the checkout with plain reads before `confirm_clickpesa` takes its locks. MariaDB's default isolation level keeps that earlier snapshot, so `frappe.get_doc(EVENT, event_name)` can show an old row. Two deliveries of one event at the same time can both run. The second one then finds the checkout `Paid` and tries to save its stale event, which will most likely fail with a timestamp mismatch (a 500, then ClickPesa retries and gets `duplicate`) or overwrite `Applied` with `Ignored`.
**Why it matters**: No money is wrongly given, because the checkout is read under lock, but the stored outcome can be wrong or a retry is forced. The spec build plan lists a two webhooks at once test, but it is not in this thread.
**Suggested fix**: Read the event with `for_update=True` after the site lock is taken, and skip work if it already has an outcome. Add a concurrency test when the ordering tests land.

### 🟡 An `Ignored` outcome closes the event for good, `rarebooks_control/billing.py:376`
**Problem**: If ClickPesa's status query has no paid item yet when the webhook arrives (a delay, or `PROCESSING`), the event is saved as `Ignored`, and a redelivery is answered `duplicate`. This matches AC-7 as written.
**Why it matters**: A real payment is then only picked up by the return page (AC-12) or the hourly reconcile (AC-16), and neither exists yet (`billing.returned` is referenced but not defined). Until they land, a paid customer can stay locked.
**Suggested fix**: Do not turn on ClickPesa for real customers until AC-12 and AC-16 are in. Say so in the build plan notes.

### 🟡 A site can set a trial end date that breaks its own payment, `rarebooks_control/api.py:241` and `rarebooks_control/state.py:195`
**Problem**: `_date_or_none` accepts any date. A signed call with `trial_ends_on` of `9999-12-31` is stored, and then `getdate(trial_ends_on) + ONE_DAY` raises `OverflowError` (checked in Python) inside the confirm step.
**Why it matters**: Only that customer is hurt, but their payment then fails with a 500 on every redelivery and ops has to fix it by hand.
**Suggested fix**: Reject or ignore dates outside a sane range (for example more than a few years from today), and catch the error in the date rule.

### 🟡 Status is not checked again after the lock in `create_checkout`, `rarebooks_control/api.py:96`
**Problem**: `_verify_site` checks `Removed` on an unlocked read. After `for_update` the status is not looked at again.
**Why it matters**: A site removed in the gap can still get a checkout (tiny window, and AC-2 and AC-5 say a removed site gets 401).
**Suggested fix**: After taking the lock, return the 401 answer if the status is `Removed`.

### 🟡 One failure in `send` can leave a push stuck without a status, `rarebooks_control/push.py:264`
**Problem**: `row.get_password("control_secret")` is outside the `try`. If it raises, the job dies before any status is written. The row stays `Pending`, and the sweep retries it every minute with nothing recorded. Also `requests.post` follows redirects by default, so a redirecting customer URL would get the signed body resent elsewhere.
**Why it matters**: A silent retry loop is hard to see, and the redirect resends a signed state body.
**Suggested fix**: Catch the password error and record it as a failure. Pass `allow_redirects=False`.

### 🟡 A test checks a case the lock makes impossible, `rarebooks_control/push.py:299` and `rarebooks_control/tests/test_clickpesa_payment.py:242`
**Problem**: `push_site` holds the row lock until commit, so `state_version` cannot change between the read and the "read again" in `success_values`. `test_a_change_during_a_push_sends_again` fakes the change inside the HTTP stub on one connection, which cannot happen with the real lock. The Pending path is real (a job that is still finishing skips a new request, and the sweep catches it), but this branch is not how it happens.
**Why it matters**: The test gives false comfort. The real race (a payment waiting on the lock, then committing while the old job is still marked running) is untested.
**Suggested fix**: Keep the re-read as a cheap guard, but test the real race: change the row after the push commits and check that the sweep requeues it.

### 🟡 A second payment alerts again on every later event, `rarebooks_control/billing.py:391`
**Problem**: When a checkout is `Paid` and ClickPesa still lists extra paid items, every later webhook for that order (a different event type or payment id) sends another ops email with outcome `Unknown`.
**Why it matters**: Alert spam hides the one alert that matters. It stays correct on money, since no extra year is given.
**Suggested fix**: Alert only when the extra payment id has not been seen before (for example, look for an existing event for it).

## Nits
- ⚪ `rarebooks_control/billing.py:281`, the cookie path is `.../billing.returned`, but spec AC-6 says `.../billing`. The code is right (browsers match a path only up to a `/`), so update the spec text.
- ⚪ `rarebooks_control/billing.py:278`, `RETURNED` points to `billing.returned`, which does not exist yet (AC-12). Fine for this thread, but the ClickPesa return URL must not be set until it does.
- ⚪ `rarebooks_control/api.py:174`, an unknown site gets an unsigned 401 and a known site with a bad signature gets a signed one, so a caller can tell which site names exist. Low risk, but worth knowing.
- ⚪ `rarebooks_control/providers/clickpesa.py:116`, a missing checksum key writes an Error Log row on every webhook. Log once per hour instead.
- ⚪ `rarebooks_control/api.py:142`, `json_amount` returns a float for USD, and amounts reach the code as floats before `Decimal(str(...))`. It is correct for 2 decimals, but the rule says never float, so say in a comment that `str()` is what keeps it exact.
- ⚪ `rarebooks_control/billing.py:510`, `Decimal("NaN")` from a bad `collectedAmount` passes `_amount` and then makes the `>=` raise. Treat non finite values as None.
- ⚪ `rarebooks_control/billing.py:480`, alert text shows `120000.0 TZS`. Format TZS as a whole number.
- ⚪ `AGENTS.md`, "Build approach" is still `<TBD, set by /scope>`.

## Strengths
- The webhook is a "go and look" signal only. Nothing changes from its body, ClickPesa is asked under the site lock, and a provider error leaves the event open so a redelivery works (tested).
- Money uses Decimal with `ROUND_HALF_UP` to the currency unit, and the amount check compares Decimals, never floats. The AC-8 date rule is a small pure function with tests for trial, early renewal, lapse, 29 February and Dar es Salaam near midnight.
- Signed answers go through one helper that signs the exact bytes sent, and the secrets stay in hidden Password fields with no desk delete or create rights. Rate limits are per site, not per IP, which fits the shared hosting IP.
- The `go` link stores only a token hash, rotates it on reuse, expires it, and sets an HttpOnly Secure cookie. `return_url` is limited to the site's own host.

## Test coverage
Good for the happy path and the main failures: bad checksum, repeated webhook, ClickPesa down then redelivery, pending status, underpay and wrong currency, second payment, removed site, unknown order, push failure, removed site never pushed, role permissions and the signing vectors. Gaps: no known answer for ClickPesa's checksum, no direct tests of the ClickPesa client, no non ASCII signature test, no concurrency or ordering tests (planned later), no tests for push answers that are not 200 or not valid JSON, and the extreme date case. Tests carry `# covers:` tags as the project asks.
