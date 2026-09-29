# Hardened Entergy Usage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a public, production-quality Home Assistant custom integration that imports delayed Entergy whole-home electricity data without leaking account information, weakening authentication, or corrupting Energy dashboard statistics.

**Architecture:** Keep the `entergy_mobile` domain and upstream history, but replace the permissive client and synthetic meter with isolated typed layers: fixed-origin transport, strict parsing, a correction-aware private ledger, external Recorder statistics, and a PII-free Home Assistant surface. Persist canonical state before queuing at-least-once statistics, verify Recorder on the next run, and keep normal polling, low-rate backfill, reauthentication, repairs, and rollback independently testable.

**Tech Stack:** Python 3.14; Home Assistant 2026.9.3 and 2026.9.4; aiohttp through Home Assistant's shared session; Home Assistant `Store`, Recorder statistics, config-flow, entity, and repairs APIs; pytest-homeassistant-custom-component; pytest-cov; Ruff; mypy; uv; GitHub Actions; HACS and hassfest validation.

**Spec:** `docs/superpowers/specs/2026-09-28-entergy-integration-design.md`

## Global Constraints

- Keep domain and install path exactly `entergy_mobile` and `custom_components/entergy_mobile`; only one provider of that domain may be installed.
- Support Home Assistant `2026.9.3` through current stable `2026.9.4` on Python 3.14. Use `voluptuous` schemas for that compatibility range.
- Add no runtime Python dependency. Pin all development dependencies and every GitHub Action to immutable versions/commits.
- Use only synthetic fixtures. Never commit credentials, tokens, account numbers, service addresses, production responses, Home Assistant runtime data, or private homelab configuration.
- Treat `https://prod.entergy.mindgrb.io:443` as the only origin. Keep TLS verification enabled, disable redirects, and expose no user-configurable base URL.
- Accept account path segments only when they match 1–128 ASCII unreserved characters and contain no `..`; show nicknames only under the spec's 1–40-character, no-digit/address-marker rule.
- Enforce 10-second connect, 20-second socket-read, and 30-second total timeouts; 2 MiB decompressed response, 512 intervals per page, seven normal weekly pages, and 12 requests per chain.
- Poll every four hours by default, configurable from one to 24 hours, with 0–10 percent jitter. Backfill at most one weekly page every 30 minutes, two per hour, 48 per day, and 370 days total.
- Import/return are separate non-negative kWh series. Monetary statistics are enabled only when source and Home Assistant currency are USD; negative source amounts become non-negative compensation.
- Fingerprints include only stable source fields and exclude receipt/fetch time, account data, and runtime metadata.
- Quarantine apparent deletion markers and mass changes only when all thresholds hold: at least 24 known overlaps, more than 75 percent changed fingerprints, and aggregate energy delta over 25 percent.
- Use random `uuid4().hex` public IDs. Raw account IDs remain only in private config-entry data used for API calls.
- Use Home Assistant `Store(private=True, atomic_writes=True)` and verify every save by public `async_load()` readback before changing in-memory state.
- Treat Recorder writes as at-least-once queued work. Persist a pending boundary before queuing, verify Recorder on the next update, and requeue the idempotent suffix until verified.
- Never edit Recorder SQLite, `.storage/energy`, SSH, networking, the completed dashboard, or any live Home Assistant service in this implementation branch.
- Target 100 percent coverage in transport, authentication, parser, ledger, statistics, and redaction modules, and at least 95 percent overall.
- Do not tag a release, merge the pull request, or install on VM202 during implementation. Finish with a reviewable PR and reproducible release-candidate artifacts.

## Target File Map

### Component

- `custom_components/entergy_mobile/models.py`: immutable credentials, account, interval, ledger, statistics, and PII-free snapshot types.
- `custom_components/entergy_mobile/errors.py`: typed errors whose string and repr never contain remote or secret values.
- `custom_components/entergy_mobile/parser.py`: strict JSON normalization and local-calendar summaries; no Home Assistant imports.
- `custom_components/entergy_mobile/api.py`: operation allowlist, fixed-origin transport, bounded reads, request budget, login state, and token lifecycle.
- `custom_components/entergy_mobile/ledger.py`: pure reconciliation plus versioned private `Store` adapter and v1 bootstrap.
- `custom_components/entergy_mobile/statistics.py`: hourly buckets, IDs/metadata, Recorder queueing, and next-run verification.
- `custom_components/entergy_mobile/coordinator.py`: 45-day reconciliation, priority lock, jitter/backoff, and bounded backfill worker.
- `custom_components/entergy_mobile/issues.py`: stable localized repair creation/deletion with public IDs only.
- `custom_components/entergy_mobile/config_flow.py`: disclosure, account choice, duplicate prevention, reauth, timezone, and reload-safe options.
- `custom_components/entergy_mobile/__init__.py`: typed runtime data, setup/unload, config-entry and registry migration.
- `custom_components/entergy_mobile/entity.py`: generic device identity using only the public ID.
- `custom_components/entergy_mobile/sensor.py`: rolling summaries, freshness, latest fetch/data, and backfill status only.
- `custom_components/entergy_mobile/diagnostics.py`: construction from a strict allowlist.
- `custom_components/entergy_mobile/const.py`, `manifest.json`, `strings.json`, and `translations/en.json`: fixed policy values and localized product copy.

### Tests and tooling

- `tests/conftest.py` and `tests/fixtures/*.json`: Home Assistant fixtures and fabricated payloads only.
- `tests/test_models.py`, `test_parser.py`, `test_api.py`, `test_ledger.py`, `test_statistics.py`, `test_config_flow.py`, `test_migration.py`, `test_coordinator.py`, `test_init.py`, `test_sensor.py`, `test_diagnostics.py`, and `test_repository_policy.py`.
- `pyproject.toml`, `uv.lock`, and `requirements/ha-2026.9.3.txt`: pinned local/current and minimum-version environments.
- `scripts/check_coverage.py`: enforces 100 percent coverage in the six security/data modules in addition to the overall gate.
- `.github/workflows/ci.yml`, `codeql.yml`, and `release.yml`; `.github/dependabot.yml`.

### Public project surface

- Rewrite `README.md`; add `NOTICE.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `docs/INSTALL.md`, `docs/PRIVACY.md`, and `docs/ROLLBACK.md`.
- Add `.github/CODEOWNERS`, a pull-request template, and structured bug/feature issue forms owned by the real `BeauDevCode` maintainer.
- Replace promotional wordmark artwork with original generic electricity artwork while keeping the required in-component icon.

## Review Focus

1. A chunked gzip response expands to 2 MiB + 1 byte and embeds a secret sentinel: reject before parsing, preserve last-good data, and leak no sentinel to logs, exceptions, or diagnostics. Covered in Task 3.
2. Login returns a plausible token and an unknown nested challenge with an attacker URL: fail closed, retain no token, follow no URL, and make no account request. Covered in Task 4.
3. Reauthentication exposes a different raw account with the same masked last four digits: reject it and retain the original credentials, public ID, statistics, and runtime. Covered in Task 8.
4. The process restarts after a corrected ledger is verified on disk but before Recorder commits queued statistics: reload, verify or requeue the suffix exactly, preserve corrected cumulative sums, and advance the pending marker only after next-run Recorder verification. Covered in Task 7.
5. A 401 login refresh plus retryable failures consumes the shared request budget: make no thirteenth request, permit only one relogin, keep errors sanitized, and preserve last-good state. Covered in Tasks 3 and 4.

---

### Task 1: Reproducible test harness and public project identity

**Files:**
- Create: `pyproject.toml`
- Create: `uv.lock`
- Create: `requirements/ha-2026.9.3.txt`
- Create: `tests/conftest.py`
- Create: `tests/test_repository_policy.py`
- Modify: `.gitignore`
- Modify: `custom_components/entergy_mobile/manifest.json`
- Modify: `hacs.json`

**Interfaces:**
- Produces the shared pytest environment, `enable_custom_integrations` fixture, `recorder_mock` support, and project metadata all later tasks consume.
- Pins `uv==0.12.19`, `ruff==0.16.9`, `mypy==2.3.1`, `pytest-cov==7.1.0`, `pip-audit==2.10.1`, PHACC `0.13.366` for HA 2026.9.3, and PHACC `0.13.367` for HA 2026.9.4.

- [ ] **Step 1: Create the Python 3.14 development definition and lock it**

  Set `requires-python = ">=3.14,<3.15"`, `asyncio_mode = "auto"`, strict Ruff/mypy settings, branch coverage, and source/test paths. Generate `uv.lock` with PHACC 0.13.367 as the default environment and an exact minimum-version requirements file with PHACC 0.13.366. Apply temporary file-specific mypy `ignore_errors` overrides only to untouched v0.1.1 lifecycle/config/coordinator/entity/sensor/diagnostics modules; each owning task removes its override, and Task 10 removes the final one.

- [ ] **Step 2: Write repository-policy tests that initially fail**

  Add tests asserting the manifest display name is `Entergy Usage (Unofficial Hardened Fork)`, its existing version remains valid SemVer until the release task, codeowner/docs/issues point to `BeauDevCode`, HACS requires HA 2026.9.3, no forbidden production-artifact extensions exist, and no tracked file matches representative credential/private-key patterns.

- [ ] **Step 3: Run the policy test and observe RED**

  Run: `uv run pytest tests/test_repository_policy.py -q`

  Expected: failures on the upstream name, owner, URLs, and HA floor.

- [ ] **Step 4: Update only metadata and ignore rules**

  Preserve `domain`, `config_flow`, `iot_class`, and empty runtime requirements. Add `.venv`, test caches, coverage, build, packet-capture, database, log, media, and credential artifact exclusions.

- [ ] **Step 5: Run baseline validation**

  Run: `uv run pytest tests/test_repository_policy.py -q && uv run ruff check . && uv run mypy custom_components/entergy_mobile`

  Expected: all commands exit 0.

- [ ] **Step 6: Commit**

  Commit: `build: add reproducible Home Assistant test harness`

### Task 2: Safe immutable models and strict usage parser

**Files:**
- Create: `custom_components/entergy_mobile/models.py`
- Create: `custom_components/entergy_mobile/errors.py`
- Create: `custom_components/entergy_mobile/parser.py`
- Create: `tests/test_models.py`
- Create: `tests/test_parser.py`
- Create: `tests/fixtures/app.json`
- Create: `tests/fixtures/login.json`
- Create: `tests/fixtures/accounts.json`
- Create: `tests/fixtures/weekly_usage.json`

**Interfaces:**
- Produces `Credentials`, `ClientMetadata`, `LoginResult`, `Account`, `EnergyInterval`, `LedgerTotals`, `LedgerState`, `LedgerMutation`, `Freshness`, and `UsageSnapshot` as frozen slot dataclasses; credential/token/account-bearing types use `repr=False` or redacted reprs.
- Produces `EntergyError(category, status=None, retry_after=None)`, `AuthError`, `ChallengeError`, `RateLimitError`, `PayloadError`, `PolicyError`, `LedgerError`, and `ErrorCategory` with stable secret-free `str`/`repr`.
- Produces `parse_client_metadata(payload) -> ClientMetadata`, `parse_login(payload) -> LoginResult`, `parse_accounts(payload) -> tuple[Account, ...]`, `parse_account(payload, expected_account_id) -> Account`, `parse_usage(payload, *, source_time_zone, received_at) -> tuple[EnergyInterval, ...]`, and `summarize_usage(state, *, time_zone, now) -> UsageSnapshot`.

- [ ] **Step 1: Write failing model/parser tests**

  Cover the approved aliases; fabricated account masking inputs; finite `Decimal(str(value))`; positive usage to import and negative signed usage to return; one-hour UTC intervals; 512-record maximum; 10,000 kWh and USD 1,000,000 caps; duplicate/conflicting overlap behavior; missing data; naive timestamps; `NaN`/infinity/bool rejection; and canonical fingerprints that contain no account data.

- [ ] **Step 2: Add DST and freshness regressions**

  Assert America/Chicago spring-forward creates no synthetic hour, both fall-back 01:00 folds remain distinct UTC intervals, local-day summaries use Central time, and freshness is `fresh <=36h`, `delayed >36h..72h`, `stale >72h`, or `unknown` with no interval.

- [ ] **Step 3: Run the targeted tests and observe RED**

  Run: `uv run pytest tests/test_models.py tests/test_parser.py -q`

  Expected: import errors for the new modules.

- [ ] **Step 4: Implement the immutable types and parser**

  Accept only the compatibility schema enumerated in the spec. Store interval identity in aware UTC, derive `end = start + 1 hour`, and SHA-256 only start, end, import, return, amount, currency, estimated status, and optional source revision. Exclude `received_at`, account data, fetch time, and runtime metadata; never embed rejected values in exceptions.

- [ ] **Step 5: Run GREEN and static checks**

  Run: `uv run pytest tests/test_models.py tests/test_parser.py -q && uv run ruff check custom_components/entergy_mobile tests && uv run mypy custom_components/entergy_mobile`

  Expected: all commands exit 0.

- [ ] **Step 6: Commit**

  Commit: `feat: add strict privacy-safe utility data model`

### Task 3: Fixed-origin bounded HTTP transport

**Files:**
- Rewrite: `custom_components/entergy_mobile/api.py`
- Modify: `custom_components/entergy_mobile/const.py`
- Create: `tests/test_api.py`

**Interfaces:**
- Produces `RequestBudget(limit: int = 12, used: int = 0)` with `consume() -> None`.
- Produces `EntergyApiClient(session, credentials, *, language="en", app_version="3.59.0")` with `async_initialize(budget)`, `async_get_accounts(budget)`, `async_get_account(account_id, budget)`, and `async_get_weekly_usage(account_id, start_date, budget)`.
- The private request method accepts an internal `ApiOperation` enum, never an arbitrary URL.
- Until Tasks 8–10 migrate every caller, the client also exposes a clearly marked internal compatibility constructor for the old `username=`/`password=` keywords, optional budgets that create one bounded chain, old exception-name aliases, and `async_fetch_current_usage(account_id, budget=None)` as a six-day `async_get_weekly_usage` wrapper. `tests/test_repository_policy.py` requires that entire bridge to be gone in Task 10.

- [ ] **Step 1: Write failing allowlist and account-segment tests**

  Assert only the six method/path templates and approved query keys work. Reject alternate scheme/host/port, userinfo, fragments, redirects, caller URLs, slash, backslash, control characters, traversal, and overlength account segments before the session is called.

- [ ] **Step 2: Write the decompression and budget Review Focus tests**

  Add `test_chunked_gzip_body_exceeding_decompressed_limit_fails_without_leak` and a budget test that proves request 13 is never attempted. Also cover malformed content type/JSON, 512+ normalized records, timeout conversion, and HTTP-date/delta-seconds `Retry-After` clamped to 24 hours.

- [ ] **Step 3: Run the transport tests and observe RED**

  Run: `uv run pytest tests/test_api.py -k 'origin or redirect or budget or decompressed or retry_after' -q`

  Expected: failures because the upstream client accepts URLs, follows redirects, and reads unbounded bodies.

- [ ] **Step 4: Implement the bounded operation transport**

  Use the shared HA session, `allow_redirects=False`, normal TLS, explicit `aiohttp.ClientTimeout(connect=10, sock_read=20, total=30)`, streaming decompressed reads capped at 2 MiB, JSON media-type checks, and exception conversion `from None`. Consume the same budget through initialization and nested calls.

- [ ] **Step 5: Run all API tests and leak assertions**

  Run: `uv run pytest tests/test_api.py -q`

  Expected: all pass and every sentinel is absent from `caplog`, exception strings, and reprs.

- [ ] **Step 6: Commit**

  Commit: `feat: enforce fixed-origin bounded Entergy transport`

### Task 4: Fail-closed authentication and token lifecycle

**Files:**
- Modify: `custom_components/entergy_mobile/api.py`
- Modify: `custom_components/entergy_mobile/parser.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_parser.py`

**Interfaces:**
- Adds `EntergyApiClient.async_login(budget) -> None`, `async_logout(budget=None) -> None`, `clear_token() -> None`, and a read-only `authenticated: bool` property.
- `async_logout` clears memory in `finally`; 401/403 permits exactly one login in the same shared budget.

- [ ] **Step 1: Write failing authentication tests**

  Cover client metadata/login schemas, invalid credentials, token absence, logout failure, token clearing, one 401 refresh, second 401/403 auth failure, and no invalid-credential retry loop.

- [ ] **Step 2: Add the challenge and recursive-budget Review Focus tests**

  Add `test_token_plus_unknown_challenge_never_authenticates` with a nested attacker URL and `test_request_budget_counts_recursive_auth_and_retries`; assert no URL follow, no account call, one login maximum, no thirteenth request, and no token retained.

- [ ] **Step 3: Run and observe RED**

  Run: `uv run pytest tests/test_api.py tests/test_parser.py -k 'login or auth or challenge or token or budget' -q`

  Expected: challenge-plus-token and token-clearing assertions fail against upstream behavior.

- [ ] **Step 4: Implement the auth state machine**

  Inspect every `nextAction` before token acceptance. Map MFA, CAPTCHA, consent, and unknown challenge to typed safe errors; never submit a challenge, log its payload, or follow its URL.

- [ ] **Step 5: Run authentication and full parser/API suites**

  Run: `uv run pytest tests/test_api.py tests/test_parser.py -q`

  Expected: all pass.

- [ ] **Step 6: Commit**

  Commit: `feat: fail closed on Entergy authentication challenges`

### Task 5: Versioned private ledger and verified persistence

**Files:**
- Create: `custom_components/entergy_mobile/ledger.py`
- Create: `tests/test_ledger.py`

**Interfaces:**
- Produces `EntergyLedger(hass, public_id)` using key `entergy_mobile.ledger_<public_id>`, Store major version 2, `private=True`, and `atomic_writes=True`.
- Produces `state`, `async_load(*, initialized: bool)`, `async_ingest(...) -> LedgerMutation`, `async_mark_statistics_pending(*, from_hour: datetime, fingerprint: str)`, and `async_mark_statistics_verified(*, through: datetime, fingerprint: str)`.
- Produces `async_import_legacy_v1_store(hass, *, entry_id, public_id) -> LedgerState | None`; it never removes or mutates the legacy key.

- [ ] **Step 1: Write failing serialization and version tests**

  Assert Decimal is encoded as strings, datetimes as UTC ISO, schema/revision fingerprints round-trip, a genuinely missing uninitialized store creates state, and future versions block setup.

- [ ] **Step 2: Write persistence-failure tests**

  Simulate `async_save` logging/swallowing a failure, readback returning the old revision, corrupt Store returning `None`, missing-after-`ledger_initialized=true`, and Core changing to `CoreState.stopping`/`final_write` during save. Assert memory stays byte-equivalent to the prior state, no statistics queue occurs, and the mutation is deferred or returns a ledger repair result as appropriate.

- [ ] **Step 3: Run and observe RED**

  Run: `uv run pytest tests/test_ledger.py -k 'store or save or corrupt or version or round_trip' -q`

  Expected: module import failure.

- [ ] **Step 4: Implement the Store adapter and public readback verification**

  Build and validate a candidate snapshot, require Core not be `stopping` or `final_write`, save it, reload with `Store.async_load`, recheck Core state, compare the candidate revision/fingerprint, then swap memory. Treat `None` as new only when the config-entry initialization marker is false. A shutdown race defers the candidate and never queues Recorder work.

- [ ] **Step 5: Implement and test v1 bootstrap without destroying rollback data**

  Parse valid legacy signed usage, recompute retained import/return, derive only defensible pre-window baselines, omit unrecoverable historic cost, reject unsafe legacy records, and keep `entergy_mobile_usage_<entry_id>` untouched.

- [ ] **Step 6: Run GREEN**

  Run: `uv run pytest tests/test_ledger.py -q`

  Expected: all pass.

- [ ] **Step 7: Commit**

  Commit: `feat: add verified private energy ledger`

### Task 6: Correction-aware reconciliation and retention

**Files:**
- Modify: `custom_components/entergy_mobile/ledger.py`
- Modify: `tests/test_ledger.py`

**Interfaces:**
- Produces pure `reconcile(state, incoming, *, received_at, backfill_cursor=UNCHANGED, backfill_complete=UNCHANGED) -> LedgerMutation`.
- The result reports inserted, corrected, estimated, and earliest statistics hour without mutating the input.

- [ ] **Step 1: Write failing reconciliation tests**

  Cover first insert, duplicate idempotency, reordered input, estimated-to-actual, upward and downward corrections, correction propagation through every later cumulative sum, and 400-day retention with import/return/cost/compensation pre-window baselines.

- [ ] **Step 2: Write quarantine tests**

  Prove missing records are not deletions, explicit retractions and suspicious mass changes reject the whole candidate, and last-good serialized state/sums remain exactly unchanged.

- [ ] **Step 3: Run and observe RED**

  Run: `uv run pytest tests/test_ledger.py -k 'reconcile or correction or quarantine or retention' -q`

  Expected: failures because v0.1.1 only accepts upward deltas.

- [ ] **Step 4: Implement pure reconciliation and pruning**

  Key by UTC start, replace only explicit same-start revisions, rebuild the suffix from the earliest changed hour, move pruned totals into baselines, and never infer deletion from absence.

- [ ] **Step 5: Run ledger suite and property-style order/idempotency cases**

  Run: `uv run pytest tests/test_ledger.py -q`

  Expected: all pass.

- [ ] **Step 6: Commit**

  Commit: `feat: reconcile utility corrections without total drift`

### Task 7: Recorder external statistics and next-run verification

**Files:**
- Create: `custom_components/entergy_mobile/statistics.py`
- Create: `tests/test_statistics.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces `StatisticIds`, `StatisticsQueueResult`, `statistic_ids(public_id: str) -> StatisticIds`, `build_hourly_statistics(state: LedgerState, *, public_id: str, currency: str, start: datetime | None) -> tuple[tuple[StatisticMetaData, tuple[StatisticData, ...]], ...]`, `async_queue_external_statistics(hass: HomeAssistant, batches: Sequence[tuple[StatisticMetaData, Sequence[StatisticData]]]) -> StatisticsQueueResult`, and `async_verify_queued_statistics(hass: HomeAssistant, *, ids: StatisticIds, expected_batches: Sequence[tuple[StatisticMetaData, Sequence[StatisticData]]], through: datetime) -> bool`.
- IDs are exactly `entergy_mobile:<public_id>_{consumption,return,cost,compensation}` and pass `valid_statistic_id`.

- [ ] **Step 1: Write failing pure statistics tests**

  Cover top-of-hour aware UTC starts, split/combine behavior, `state` hourly deltas, correction-aware `sum`, non-netted energy, valid IDs, energy metadata, Opower-compatible monetary metadata, USD mismatch suppression, and negative amount to compensation.

- [ ] **Step 2: Write Recorder integration tests**

  Load `recorder_mock`, queue insert/update data, call `pytest_homeassistant_custom_component.components.recorder.common.async_wait_recording_done`, and assert rows via supported Recorder query functions. Verify same timestamps update rather than duplicate.

- [ ] **Step 3: Add the crash/restart Review Focus test**

  Persist a downward correction with an earliest-pending hour, make queueing occur without commit confirmation, recreate runtime state, and verify every non-empty expected series at the boundary timestamp by both `state` and `sum` on the next run. Empty optional cost/compensation batches do not block success. Either requeue once or advance the marker; assert corrected lower sums and one row per timestamp.

- [ ] **Step 4: Run and observe RED**

  Run: `uv run pytest tests/test_statistics.py -q`

  Expected: module import failure.

- [ ] **Step 5: Implement at-least-once queue and verification**

  Use `StatisticData`, `StatisticMetaData`, `StatisticMeanType.NONE`, `EnergyConverter.UNIT_CLASS`, kWh, and `async_add_external_statistics`. Query Recorder in its executor on the next coordinator run; never call the test wait helper in production.

- [ ] **Step 6: Run GREEN on both supported HA environments**

  Run current: `uv run pytest tests/test_statistics.py -q`

  Prepare minimum: `uv venv .venv-ha-2026.9.3 --python 3.14 && uv pip sync --python .venv-ha-2026.9.3/bin/python requirements/ha-2026.9.3.txt`

  Run minimum in its matrix environment: `.venv-ha-2026.9.3/bin/python -m pytest tests/test_statistics.py -q`

  Expected: both pass.

- [ ] **Step 7: Commit**

  Commit: `feat: publish correction-safe external energy statistics`

### Task 8: Private config flow, reauthentication, and v1 migration

**Files:**
- Rewrite: `custom_components/entergy_mobile/config_flow.py`
- Modify: `custom_components/entergy_mobile/__init__.py`
- Modify: `custom_components/entergy_mobile/const.py`
- Modify: `custom_components/entergy_mobile/strings.json`
- Modify: `custom_components/entergy_mobile/translations/en.json`
- Create: `tests/test_config_flow.py`
- Create: `tests/test_migration.py`

**Interfaces:**
- `EntergyMobileConfigFlow.VERSION = 2`, `MINOR_VERSION = 1`; implements user, account, reauth, and reauth-confirm steps returning `ConfigFlowResult`.
- `EntergyOptionsFlow(OptionsFlowWithReload)` is constructed with no custom arguments and exposes the one-to-24-hour option.
- `async_migrate_entry(hass, entry) -> bool` adds/reuses `public_id`, timezone, and `ledger_initialized`; migrates registries and bootstraps the new ledger before setting version 2.

- [ ] **Step 1: Write failing setup-flow tests**

  Assert credential-at-rest disclosure appears before password input; password uses `TextSelectorType.PASSWORD` with `current-password`; account labels reveal only last four plus a safe non-address nickname; address-like nicknames are suppressed; `uuid4().hex` is valid; duplicates compare raw private IDs; clients logout in `finally`; and options clamp legacy values below 3600 to 3600 while defaulting missing options to 14400.

- [ ] **Step 2: Write failing reauth tests**

  Cover invalid auth, every challenge type, same-account success, different-account rejection, and `test_reauth_same_masked_suffix_but_different_raw_account_is_rejected`. Assert only username/password/language change, entry count stays constant, password is never prefilled, and public ID/statistics remain unchanged.

- [ ] **Step 3: Write v1 migration tests**

  Start from raw `username_account` unique ID, raw-account title/device identifiers/entity unique IDs, old totals, and old options. Assert migration persists public ID before later work and retries reuse it. Preserve only entity IDs proven free of both raw and Home Assistant-slugified username/account tokens; when proof is not possible, rename every entity attached to the legacy PII-bearing device through the supported registry API. Create `legacy_entity_id_<public_id>` with automation-impact guidance, migrate unique/device IDs, disable old total sensors rather than repurposing them, and create the legacy Energy-source repair. Include a regression where account `12-34` appears as `12_34`.

- [ ] **Step 4: Run and observe RED**

  Run: `uv run pytest tests/test_config_flow.py tests/test_migration.py -q`

  Expected: failures for missing disclosure/reauth/migration and raw account exposure.

- [ ] **Step 5: Implement flow and migration using HA 2026.9-compatible APIs**

  Keep `voluptuous`; use `_get_reauth_entry`, `_abort_if_unique_id_mismatch`, and `async_update_reload_and_abort`. Remove the config-entry update listener and use `OptionsFlowWithReload` to prevent double reloads.

- [ ] **Step 6: Run GREEN and migration idempotency twice**

  Run: `uv run pytest tests/test_config_flow.py tests/test_migration.py -q`

  Expected: all pass, including a second migration invocation with no changes.

- [ ] **Step 7: Commit**

  Commit: `feat: add private setup reauth and safe migration`

### Task 9: Priority polling, bounded backfill, and backoff

**Files:**
- Rewrite: `custom_components/entergy_mobile/coordinator.py`
- Create: `tests/test_coordinator.py`

**Interfaces:**
- Produces `EntergyDataUpdateCoordinator(hass, entry, client, ledger, *, time_zone)` with `async_initialize`, `_async_update_data`, `async_run_backfill_once`, `async_start_backfill`, `async_shutdown`, and `diagnostics`.
- Constructor accepts deterministic clock/jitter hooks only under private keyword parameters used by tests.

- [ ] **Step 1: Write failing normal-sync tests**

  Assert a 45-day window uses at most seven pages and one shared 12-call budget, normal sync wins the single-flight priority flag, pending Recorder data is verified/requeued first, and schema/currency/retraction errors preserve the prior snapshot.

- [ ] **Step 2: Write failing scheduler/backoff tests**

  Use a fake clock/RNG to pin 0 and 10 percent jitter, 1/2/4/8/24-hour transient backoff, HTTP `Retry-After`, reset on success, and no requests while disabled/unloaded.

- [ ] **Step 3: Write backfill tests**

  Assert one historical page per 30 minutes, two per hour, 48 per day, checkpoint after each verified page, normal polling is never starved, pause on failure/rate limit, restart resume, 370-day bound, and 53 pages take approximately 27 hours without presenting completion early.

- [ ] **Step 4: Run and observe RED**

  Run: `uv run pytest tests/test_coordinator.py -q`

  Expected: upstream coordinator lacks these controls.

- [ ] **Step 5: Implement orchestration only**

  Keep parsing, ledger math, and statistics outside the coordinator. Convert auth failures to `ConfigEntryAuthFailed`, initial transient failures to `ConfigEntryNotReady`, and ordinary later transient failures to `UpdateFailed` with safe categories only.

- [ ] **Step 6: Run GREEN**

  Run: `uv run pytest tests/test_coordinator.py -q`

  Expected: all pass with no real waits or network.

- [ ] **Step 7: Commit**

  Commit: `feat: coordinate bounded polling and historical backfill`

### Task 10: Home Assistant lifecycle, repairs, entities, and diagnostics

**Files:**
- Rewrite: `custom_components/entergy_mobile/__init__.py`
- Create: `custom_components/entergy_mobile/issues.py`
- Modify: `custom_components/entergy_mobile/entity.py`
- Rewrite: `custom_components/entergy_mobile/sensor.py`
- Rewrite: `custom_components/entergy_mobile/diagnostics.py`
- Modify: `custom_components/entergy_mobile/strings.json`
- Modify: `custom_components/entergy_mobile/translations/en.json`
- Create: `tests/test_init.py`
- Create: `tests/test_sensor.py`
- Create: `tests/test_diagnostics.py`

**Interfaces:**
- Produces typed `EntergyRuntimeData(client, ledger, coordinator)` stored in `entry.runtime_data` and `type EntergyConfigEntry = ConfigEntry[EntergyRuntimeData]`.
- `issues.py` produces `create_issue(hass, public_id, kind, placeholders=None)` and `delete_issue(...)` for the eight stable issue IDs in the spec.
- Sensors consume only `UsageSnapshot`; diagnostics consume only `coordinator.diagnostics()`.

- [ ] **Step 1: Write lifecycle tests**

  Assert setup loads/validates ledger before fetching, marks first verified initialization, forwards sensors, starts backfill after a good first refresh, maps failures correctly, and unload cancels workers before best-effort logout and unconditional token clearing.

- [ ] **Step 2: Write entity and device privacy tests**

  Assert generic device name/model, public-ID identifiers, no raw account state attributes, no monotonic total sensors, and separate entities for newest interval, last successful fetch, freshness, backfill progress, latest/today/seven-day/month import and return, and validated cost summaries.

- [ ] **Step 3: Write diagnostics and repair tests**

  Assert the diagnostics object contains only version, poll interval, last fetch/data timestamps, safe error category, counts, and freshness. Reject entry data/raw coordinator/exception/headers/URLs. Assert persistent issues are created only for actionable ledger/schema/currency/retraction/legacy/backfill conditions and deleted when resolved.

- [ ] **Step 4: Run and observe RED**

  Run: `uv run pytest tests/test_init.py tests/test_sensor.py tests/test_diagnostics.py -q`

  Expected: raw account and config-entry fields are exposed by upstream entities/diagnostics.

- [ ] **Step 5: Implement typed lifecycle and PII-free UI**

  Remove `hass.data` runtime storage and the update listener. Remove every temporary API compatibility constructor, optional-budget path, legacy exception alias, and `async_fetch_current_usage` wrapper plus every temporary mypy override, then make `tests/test_repository_policy.py` enforce their absence. Use translation keys, consistent electricity/freshness icons, and Home Assistant entity categories: user-facing usage/freshness stays enabled, while tracked-interval, correction, last-fetch, and backfill internals are diagnostic and disabled by default. Keep `strings.json` and `translations/en.json` synchronized, with the installed custom integration reading translations from `translations/en.json`.

- [ ] **Step 6: Run GREEN and repository-wide sentinel scan tests**

  Run: `uv run pytest tests/test_init.py tests/test_sensor.py tests/test_diagnostics.py tests/test_repository_policy.py -q`

  Expected: all pass.

- [ ] **Step 7: Commit**

  Commit: `feat: expose private utility health without account data`

### Task 11: Company-quality documentation, original branding, and secure CI

**Files:**
- Rewrite: `README.md`
- Create: `NOTICE.md`
- Create: `SECURITY.md`
- Create: `CONTRIBUTING.md`
- Create: `CHANGELOG.md`
- Create: `docs/INSTALL.md`
- Create: `docs/PRIVACY.md`
- Create: `docs/ROLLBACK.md`
- Replace: `assets/entergy-icon.svg`
- Delete: `assets/entergy-wordmark.svg`
- Replace: `custom_components/entergy_mobile/brand/icon.png`
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/codeql.yml`
- Create: `.github/workflows/release.yml`
- Create: `.github/dependabot.yml`
- Create: `.github/CODEOWNERS`
- Create: `.github/PULL_REQUEST_TEMPLATE.md`
- Create: `.github/ISSUE_TEMPLATE/bug.yml`
- Create: `.github/ISSUE_TEMPLATE/feature.yml`
- Create: `.github/ISSUE_TEMPLATE/config.yml`
- Create: `scripts/check_coverage.py`
- Modify: `.github/workflows/validate.yml`
- Modify: `tests/test_repository_policy.py`

**Interfaces:**
- Produces public installation/privacy/security/rollback contracts and CI gates; no runtime interface.
- Pins checkout v6 to `d23441a48e516b6c34aea4fa41551a30e30af803`, setup-python v6 to `ece7cb06caefa5fff74198d8649806c4678c61a1`, setup-uv v7 to `94527f2e458b27549849d47d273a16bec83a01e9`, CodeQL v3 to `87ef0dc97def48aa960fbf026a2563ee9dbdb470`, gitleaks v2 to `dcedce43c6f43de0b836d1fe38946645c9c638dc`, HACS validation to `d556e736723344f83838d08488c983a15381059a`, and hassfest to `06749dd8c0b54f350bc69c8752456cee498808a3`.

- [ ] **Step 1: Extend policy tests and observe RED**

  Assert upstream donation/wordmark/URLs are absent; attribution/license/unofficial notice are present; docs disclose delayed data, plaintext config-entry credential risk, MFA fail-closed behavior, USD policy, one-domain rule, exact-release install, migration, rollback, and no live/bill-grade claim; workflows have explicit permissions/timeouts/concurrency and no mutable `uses:` refs.

- [ ] **Step 2: Run the policy tests**

  Run: `uv run pytest tests/test_repository_policy.py -q`

  Expected: documentation, asset, and workflow failures.

- [ ] **Step 3: Create the public project surface**

  Preserve MIT `LICENSE` unchanged, credit David Delahoz and fork point in `NOTICE.md`, publish responsible disclosure instructions without promising a bounty/SLA, add real maintainer ownership/templates, and use an original generic electricity mark. Do not imply Entergy affiliation or fabricate a company/team. Set the manifest version to `1.0.0-rc.1` only here, after the complete behavior and migration exist.

- [ ] **Step 4: Build least-privilege CI and release-candidate packaging**

  Test exact HA 2026.9.3 and 2026.9.4 pairs, 100/95 coverage gates, Ruff, mypy, pip-audit, HACS, hassfest, CodeQL, and secret scanning. Have `scripts/check_coverage.py` read coverage JSON and require 100 percent for `api.py`, `errors.py`, `parser.py`, `ledger.py`, `statistics.py`, and `diagnostics.py`. Build `ha-entergy-1.0.0-rc.1.zip` with `custom_components/entergy_mobile/` at root, its standard `.sha256`, and GitHub attestation; do not publish or tag it in this task.

- [ ] **Step 5: Validate workflows and docs**

  Run: `uv run pytest tests/test_repository_policy.py -q && uv run ruff check . && uv run mypy custom_components/entergy_mobile`

  Expected: all exit 0. Record in the PR that pinned HACS/hassfest actions still invoke upstream-maintained containers, so Dependabot/manual review remains required.

- [ ] **Step 6: Commit**

  Commit: `docs: publish secure integration operations and release policy`

### Task 12: Full contract verification and reviewable release candidate

**Files:**
- Modify as required by verified failures only.
- Create generated local artifacts under ignored `dist/`; do not commit binaries.
- Update: `CHANGELOG.md`

**Interfaces:**
- Produces a clean branch, green CI-equivalent evidence, a reproducible local RC archive/checksum, and a PR-ready review summary.

- [ ] **Step 1: Run the exact minimum and current test matrices**

  Run current: `uv run pytest --cov=custom_components.entergy_mobile --cov-branch --cov-report=term-missing --cov-report=json:coverage.json --cov-fail-under=95`

  Prepare minimum once: `uv venv .venv-ha-2026.9.3 --python 3.14 && uv pip sync --python .venv-ha-2026.9.3/bin/python requirements/ha-2026.9.3.txt`

  Run minimum: `.venv-ha-2026.9.3/bin/python -m pytest --cov=custom_components.entergy_mobile --cov-branch --cov-report=term-missing --cov-report=json:coverage-min.json --cov-fail-under=95`

  Run critical gates: `uv run python scripts/check_coverage.py coverage.json && .venv-ha-2026.9.3/bin/python scripts/check_coverage.py coverage-min.json`

  Expected: zero failures; 100 percent in critical modules and at least 95 percent overall.

- [ ] **Step 2: Run static, metadata, dependency, and secret checks**

  Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy custom_components/entergy_mobile && uv run pip-audit`

  Run the pinned HACS/hassfest validation commands from CI and a repository-wide gitleaks scan.

  Expected: all exit 0 with no ignored validation or secret finding.

- [ ] **Step 3: Build and verify the release-candidate archive locally**

  Produce `dist/ha-entergy-1.0.0-rc.1.zip` with only the component tree, produce `dist/ha-entergy-1.0.0-rc.1.zip.sha256`, verify the hash, extract into a temporary Home Assistant config layout, and rerun manifest/import checks there.

- [ ] **Step 4: Perform independent whole-branch review**

  Compare every spec section and plan checkbox to the final diff. Review auth, redirects, PII, Store readback, Recorder at-least-once behavior, DST, corrections, migration, backfill, workflow permissions, and rollback. Fix findings through their owning targeted tests, then rerun Steps 1–3.

- [ ] **Step 5: Commit final verified adjustments and push**

  Commit only if verification caused tracked changes: `chore: prepare hardened Entergy release candidate`

  Push `codex/harden-entergy` and update PR #1 with final behavior, test counts, coverage, artifact hash, compatibility, known limitations, and explicit confirmation that no live Home Assistant install occurred.

- [ ] **Step 6: Verify hosted CI and security gates**

  Run: `gh pr checks 1 --repo BeauDevCode/ha-entergy --watch`

  Expected: minimum/current tests, lint/type, coverage, HACS, hassfest, audit, CodeQL, and secret-scan checks all pass. Fix any hosted-only failure through its owning test/task and rerun the full local gates before pushing again.

- [ ] **Step 7: Apply public repository governance after check names exist**

  Enable vulnerability alerts and Dependabot security updates. Create a `main` ruleset that blocks deletion and force-push, requires a pull request, and requires the exact passing CI jobs from Step 6; use zero mandatory human approvals because `BeauDevCode` is currently the sole real maintainer, and retain the repository-admin recovery bypass. Read the ruleset back through the GitHub API and add its settings to the PR evidence.

- [ ] **Step 8: Stop at the release/deployment gate**

  Leave PR #1 reviewable. Do not merge, tag, publish a release, enter Entergy credentials, alter backups, or restart Home Assistant until those concrete artifacts receive their separate final review.
