# Hardened Entergy Usage integration design

**Status:** Approved design direction; implementation has not started

**Fork point:** `daviddelahoz/ha-entergy@eb5bd5f928fc221a2262f291792eeed4f1f21c50`

**Target repository:** `BeauDevCode/ha-entergy`

**Component domain:** `entergy_mobile`

## Objective

Build a public, reproducible, security-focused fork of the existing unofficial
Entergy integration. It will import Entergy's delayed whole-home electricity
intervals and any source-provided cost into Home Assistant without exposing
account data, weakening authentication, or presenting utility data as a live or
revenue-grade meter.

The work succeeds when the integration provides:

- safe local sign-in and reauthentication;
- explicit, fail-closed handling of MFA and unknown challenges;
- Central-time and daylight-saving-time-correct interval processing;
- bounded historical backfill and correction-aware statistics;
- Home Assistant Energy dashboard-compatible external statistics;
- deterministic security and data tests;
- pinned, immutable releases; and
- a documented, reversible deployment path.

## Scope

The first hardened release covers:

- config-flow sign-in, account selection, reauthentication, options, and unload;
- whole-home imported energy, returned energy, and optional cost intervals;
- delayed summary and freshness entities;
- long-term external statistics for the Energy dashboard;
- bounded historical backfill and rolling correction reconciliation;
- privacy-safe logs and diagnostics;
- automated tests, CI, releases, installation, upgrades, and rollback.

It does not cover real-time watts, a homelab-only subtotal, payment or billing
actions, MFA bypass, public ingress, credential syncing outside Home Assistant,
direct Recorder database edits, or electrical-panel hardware. A physical meter
or submeter is still required for an accurate homelab-only subtotal.

## Data-source limitations

This is an unofficial integration and is not affiliated with, endorsed by, or
supported by Entergy. It depends on service behavior that can change without
notice. Entergy's customer tools describe interval data that is published
several times per day; faster polling cannot turn it into real-time telemetry.
The UI and documentation must display both the last successful fetch time and
the newest utility interval time so users can see the actual delay.

The integration must never describe its values as bill-grade measurements or
replace the utility bill. Cost is imported only when Entergy supplies it. Any
future calculated cost must be clearly labeled as an estimate and designed
separately.

## Repository identity and attribution

The project remains a GitHub fork so the upstream history and MIT license stay
visible. The hardened fork will:

- retain the upstream `LICENSE` and copyright notices;
- add a `NOTICE.md` naming the original project and exact fork point;
- add `SECURITY.md` and contribution guidance;
- show an explicit unofficial-project notice in user-facing documentation;
- remove promotional use of Entergy's wordmark unless trademark permission is
  established; and
- use a generic electricity icon for fork-owned presentation.

The component keeps the `entergy_mobile` domain for migration compatibility and
possible upstream comparison. Its display name becomes **Entergy Usage
(Unofficial Hardened Fork)**. Documentation must warn that only one repository
providing this domain can be installed at a time.

The public repository must never contain credentials, tokens, account numbers,
addresses, captured production responses, Home Assistant runtime files, or
private homelab configuration.

## Threat model and trust boundaries

Protected information includes the Entergy username and password, access token,
account number, service address, interval history, and cost. It must be kept out
of logs, diagnostics, entity attributes, titles, identifiers, CI artifacts, and
Git history.

All remote JSON, headers, redirects, exception text, timestamps, and account
identifiers are untrusted. Relevant threats include:

- malicious or unexpectedly large response data;
- service endpoint or schema drift;
- unexpected authentication challenges;
- credential retry loops;
- duplicate, missing, reordered, estimated, or corrected intervals;
- rate limiting and transient service failures;
- compromised dependencies, CI actions, or release assets; and
- operator mistakes during install, upgrade, or rollback.

Home Assistant, the host operating system, TLS validation, and the system trust
store are trusted boundaries. Home Assistant config entries are
permission-protected but are not an encrypted password vault. If the Home
Assistant host or its backups are compromised, this integration cannot keep a
stored login password secret. That residual risk must be stated plainly.

## Component boundaries

The implementation separates network, parsing, storage, and Home Assistant
concerns so security and arithmetic can be tested without a running instance:

- `api.py`: fixed-host transport, authentication state machine, and typed safe
  errors;
- `models.py` and `parser.py`: strict schema validation and immutable normalized
  intervals, with no Home Assistant imports;
- `ledger.py`: durable canonical intervals, deduplication, corrections, pruning,
  and cumulative-series calculation;
- `statistics.py`: hourly aggregation and Recorder external-statistics import;
- `coordinator.py`: bounded scheduling, backoff, and orchestration;
- `config_flow.py`: setup, account selection, reauthentication, and options;
- `sensor.py`: delayed summaries and freshness only; and
- `diagnostics.py`: explicitly allowlisted operational fields only.

## Authentication and reauthentication

Credentials are entered only in Home Assistant's local config flow over the
user's already trusted Home Assistant connection. They must never be requested
through chat, passed in command-line arguments, committed, or written to
workspace documents.

The observed service uses username/password login and an ephemeral access token
rather than documented OAuth. Automated recovery after a Home Assistant restart
therefore requires the password in `ConfigEntry.data`. The UI and documentation
must disclose that storage choice before sign-in. The access token remains only
in memory and is cleared on unload and authentication failure.

Every `nextAction` or challenge is checked before accepting login as successful.
The first hardened release does not submit one-time codes or bypass MFA. An MFA,
CAPTCHA, consent, or unknown challenge stops authentication with a localized
`mfa_required` or `unsupported_challenge` result. It must never recommend
disabling account security.

At runtime, a 401 or 403 permits one fresh login. Another failure raises
`ConfigEntryAuthFailed` and starts Home Assistant reauthentication. Invalid
credentials are never retried in a loop. Reauthentication replaces only the
credentials and must prove that the selected account matches the existing
entry before saving.

## Network policy

The client permits HTTPS on port 443 to the exact host
`prod.entergy.mindgrb.io` and only the reviewed method/path combinations needed
for metadata, login, logout, account discovery, and interval usage. The base URL
is not user-configurable.

Automatic redirects are disabled. If a future protocol requires a redirect,
each hop must be explicitly reviewed and allowlisted, with HTTPS, the exact
approved host, port 443, no user information, no fragment, no alternate host or
IP, no downgrade, and at most one hop. URLs returned in JSON are never followed.

The selected account identifier must be 1–128 ASCII unreserved characters
(`[A-Za-z0-9._~-]`), must not contain `..`, and is encoded as one path segment.
Control characters and path traversal are rejected. Normal certificate and
hostname validation remain enabled. The integration offers no insecure TLS
switch and does not add brittle certificate pinning.

The complete V1 allowlist is:

| Method | Path template | Purpose |
| --- | --- | --- |
| `GET` | `/api/app` | Obtain reviewed client metadata |
| `POST` | `/api/login` | Authenticate |
| `POST` | `/api/logout` | End a session, best effort |
| `GET` | `/api/accounts` | List eligible accounts |
| `GET` | `/api/accounts/{account_id}` | Confirm the selected account |
| `GET` | `/api/accounts/{account_id}/weeklyusage` | Fetch one weekly usage page |

Only `appVersion` and `language` query parameters are allowed globally;
`weeklyusage` also permits `view=day` and an ISO `startDate`. The transport has a
10-second connect timeout, 20-second socket-read timeout, and 30-second total
timeout. Each response is limited to 2 MiB after decompression and each weekly
page to 512 normalized intervals. A normal reconciliation may fetch at most
seven weekly pages and one request chain may make at most 12 HTTP requests,
including initialization, login, and account verification. `Retry-After` is
accepted in either HTTP delta-seconds or HTTP-date form, converted to a
non-negative delay, and clamped to 24 hours. Invalid values fall back to the
normal exponential schedule. Exceeding any limit preserves the last known-good
state and raises a sanitized error.

Responses require an accepted JSON content type and a reviewed schema. Retry
behavior applies only to retryable operations and never changes the
authentication policy.

## Secret and personal-data handling

API exceptions expose a stable category and HTTP status only. They must not
contain response bodies, headers, token fragments, query strings, usernames,
account identifiers, or addresses. Debug logs may include an operation name,
status class, elapsed time, and record count, but never raw payloads.

Diagnostics are built from an allowlist:

- integration and compatible Home Assistant version;
- configured polling interval;
- last successful fetch and newest data timestamps;
- sanitized error category;
- interval, estimate, and correction counts; and
- freshness status.

Diagnostics never serialize config-entry data, raw coordinator or API data,
exception strings, headers, or URLs.

Account choices display only `Account ending ••••1234` and an optional safe
nickname. A nickname is shown only from the reviewed `nickname`/`name` fields,
after trimming, when it is 1–40 characters and contains no digit, line break,
comma, or `@`; otherwise it is omitted as potentially identifying/address-like.
The full account identifier is retained only in the private config entry for
API calls.

## Pseudonymous identifiers

Setup generates a random `public_id` using `uuid4().hex`, yielding 32 lowercase
hexadecimal characters with no hyphens. This value identifies the config entry,
device, entities, storage, and statistics and must pass Home Assistant's
statistic-ID validator before setup completes. Usernames, addresses, and account
numbers must not appear in identifiers, titles, names, state attributes, or
diagnostics. An unsalted hash of an account number is not an acceptable
substitute.

Duplicate setup is prevented by comparing the selected raw account identifier
to existing private entries. Migrations create the `public_id` before registry
or statistic changes. Existing entity IDs are preserved only when they do not
contain the raw or Home Assistant-slugified username/account identifier;
privacy wins over identifier stability. When PII-freedom cannot be proved, the
integration renames every entity attached to the legacy PII-bearing device. A
supported entity-registry rename and a repair warning document any automation
impact when an entity ID must change.

## Canonical interval model

Each normalized record contains:

- timezone-aware UTC start and end;
- non-negative imported kWh;
- non-negative returned kWh;
- optional source-provided monetary amount and validated ISO 4217 currency;
- estimated/actual status;
- receipt time; and
- source revision or a deterministic fingerprint.

The source fingerprint includes only stable source-derived fields: start, end,
import, return, amount, currency, estimated status, and optional source
revision. It explicitly excludes receipt time, account data, fetch time, and
all runtime metadata so a refetch of unchanged data remains idempotent.

The reviewed compatibility schema accepts client metadata from `clientId` at
the root or in `data`; login tokens from `accessToken`, `access_token`, or
`token` at the root or in `data`; and challenges from `nextAction` at either
location. Account lists may be a root list or one of `accounts`, `data`,
`items`, or `results`; identifiers may use the known upstream aliases, while
only `nickname` or `name` can supply an optional display nickname. Service
addresses are never displayed. Usage must be found at
`data.daily.electric[].hourly[]` with `date`, finite `usage`, optional finite
`cost`, and boolean `isEstimated` fields.

The parser rejects naive or invalid timestamps, non-finite numbers, negative
import/export values, `end <= start`, conflicting overlapping records, and
implausible durations or energy. Missing intervals remain missing; the
integration does not invent usage.

V1 usage records represent exactly one absolute UTC hour. The parser rejects
more than 10,000 kWh of import or return in one interval and monetary amounts
whose absolute value exceeds USD 1,000,000. These are defensive payload limits,
not claims about a customer's service capacity.

The account or service IANA timezone is used when supplied. Otherwise setup
requires a timezone option defaulting to Home Assistant's timezone. Calendar
days, weeks, and months are grouped in that zone while interval identity and
storage use UTC. Repeated fall-back hours remain distinct UTC intervals;
spring-forward gaps do not create synthetic hours.

Imported and returned energy remain separate non-negative series and are never
netted for Energy dashboard statistics. Source intervals are split or combined
into UTC hourly buckets as needed.

The reviewed Entergy source contract is USD. Monetary statistics are enabled
only when the normalized source currency is USD (or the reviewed response omits
currency) and `hass.config.currency` is also `USD`. Any other or mixed currency
preserves the energy data, skips monetary import, and raises a repair issue.
Non-negative amounts feed the cost series. A negative amount is represented as
a separate, non-negative compensation series; it is never silently folded into
cost or mislabeled as another currency.

## Durable ledger

The canonical ledger uses Home Assistant's `Store` helper with a versioned JSON
schema and no direct `.storage` file access. Schema version 2 stores:

- normalized intervals keyed by UTC start time;
- the cumulative import, return, cost, and compensation baselines immediately
  before the retained window;
- the historical backfill cursor;
- the earliest hour awaiting statistics reconciliation; and
- the last Recorder-verified statistics fingerprint.

The `Store` is created with `private=True` and `atomic_writes=True`. All changes
are copy-on-write. Because `Store.async_save()` logs some write failures instead
of returning a success value, the integration saves the complete candidate,
reloads it through the public `Store.async_load()` API, compares its canonical
fingerprint, and swaps it into memory only after that verification. Statistics
are queued after the verified ledger snapshot; the persisted pending-hour
marker and rolling overlap make retries idempotent. Home Assistant owns atomic
replacement and file permissions. The integration does not change permissions
or write temporary files itself.

Home Assistant defers Store writes while Core is stopping and may return the
pending in-memory candidate from an immediate load. The ledger therefore checks
Core state before saving and again after readback; if Core is stopping or enters
final-write shutdown, it defers the mutation, keeps the prior in-memory state,
and queues no Recorder work.

Every schema change has an explicit, tested migration. The config entry records
`ledger_initialized=true` only after the first verified save. A `None` load is
new state only while that marker is absent; if the marker exists, setup blocks
instead of rebuilding empty. This supported marker is necessary because Home
Assistant may rename corrupt Store JSON and return the same `None` value as a
missing file. An unreadable, corrupt, missing-after-initialization, or
future-version store is never overwritten: setup stops data import, preserves
Home Assistant's recovered file where available, and creates a repair issue
with secret-free recovery instructions. Tests cover a logged/hidden save
failure, failed read-back verification, a failed statistics queue after save,
corrupt JSON, missing initialized storage, and unsupported schema versions.

## Backfill, reconciliation, and retention

Initial sync backfills at most 370 days or the service's available history. A
separate low-rate worker processes one historical weekly page every 30 minutes,
at most two pages per hour and 48 per day. It stores a checkpoint after every
page, pauses on errors or rate limits, and shares a single-flight lock with
normal polling. The normal 45-day reconciliation always has priority. With 53
weekly pages available and no service backoff, initial history takes about 27
hours; the UI shows progress and never presents partial backfill as complete.

Normal sync refetches a rolling 45-day window to catch revised estimates and
late corrections. The durable ledger retains at least 400 days plus cumulative
baselines before the prune boundary.

When the same interval start is returned with a changed value, the canonical
record is replaced and hourly values and cumulative sums are rebuilt from the
earliest changed hour through the newest retained hour. Reimporting that suffix
lets a downward correction reduce later sums correctly.

Absence from a response is never interpreted as deletion because Recorder does
not delete an existing external-statistics row merely because it is omitted
from a later import. An explicit source deletion/retraction marker or a
suspicious mass change is quarantined: the integration preserves the
last-known-good ledger and statistics and raises a repair issue. A future
deletion implementation requires a separate design using a supported,
statistic-scoped Recorder operation. Regression tests prove that a missing or
retracted interval neither leaves a silently altered cumulative series nor
invents zero usage.

V1 recognizes no deletion field in the reviewed source schema; any apparent
deletion/retraction marker is therefore schema drift and quarantined. A mass
change is suspicious when at least 24 known intervals overlap, more than 75
percent change fingerprint in one response, and the absolute aggregate energy
delta exceeds 25 percent of the prior overlap. All three conditions are
required so a normal estimated-to-actual update does not trigger on count alone.

## Home Assistant Recorder and Energy dashboard

The primary measurements are Recorder external statistics rather than
synthetic `total_increasing` sensor state:

- `entergy_mobile:<public_id>_consumption`;
- `entergy_mobile:<public_id>_return`;
- `entergy_mobile:<public_id>_cost` when validated charges are present; and
- `entergy_mobile:<public_id>_compensation` when validated credits are present.

The implementation uses Home Assistant's supported
`async_add_external_statistics` API, whose same-timestamp behavior updates an
existing hour. The API queues Recorder work and returns no commit confirmation,
so successful return means **queued**, not durably committed. The ledger keeps
a rolling, idempotent overlap and a pending marker so a restart safely queues
the same correction suffix again.

Metadata uses the component domain as its source,
`StatisticMeanType.NONE`, `has_sum=True`, and `EnergyConverter.UNIT_CLASS` with
kWh for energy. Following Home Assistant's current Opower implementation,
monetary metadata uses `unit_class=None` and `unit_of_measurement=None`; the
separate source/Home Assistant USD validation still controls whether it is
queued. Every hourly item has `state` equal to that hour's value and `sum` equal
to the correction-aware cumulative value. Cost and compensation are separate,
non-negative cumulative series.

Normal entities provide freshness and rolling summaries without pretending to
be monotonic meters. The integration never edits `.storage/energy` or the
Recorder database. After a verified first import, documentation guides the
user to select consumption and return statistics in the Energy dashboard.

## Polling and failure behavior

The default poll interval is four hours, configurable from one to 24 hours,
with 0–10 percent jitter. The client respects `Retry-After` and uses transient
backoff of one, two, four, eight, then at most 24 hours. Success resets backoff.
Only one request chain runs at a time.

Data freshness is based on the newest utility interval, not the fetch time:
`fresh` is at most 36 hours old, `delayed` is over 36 through 72 hours, `stale`
is over 72 hours, and `unknown` means no valid interval exists. Last successful
fetch is shown separately.

Failure handling is explicit:

- 401/403: one login attempt, then reauthentication;
- 429, 5xx, and timeouts: keep last good data and back off;
- schema drift or suspicious corrections: keep last good data and create a
  repair issue;
- invalid credentials or authentication challenge: stop and require local user
  action; and
- disabled or unloaded entry: make no requests.

Stable repair issue IDs contain only the random public ID:
`ledger_corrupt_<public_id>`, `ledger_future_<public_id>`,
`schema_drift_<public_id>`, `currency_mismatch_<public_id>`,
`data_retraction_<public_id>`, `legacy_energy_source_<public_id>`,
`legacy_entity_id_<public_id>`, and `backfill_stalled_<public_id>`. Issue titles
and placeholders are localized and never include remote payload or account
data.

Ledger corruption/future versions, schema drift, and data retraction use
`ERROR`; currency mismatch, legacy Energy-source/entity-ID replacement, and a
backfill that makes no cursor progress for 24 hours while
authentication/network health remain good use `WARNING`. No integration
condition uses `CRITICAL`.

## Test strategy

All fixtures are synthetic. Production responses, account details, credentials,
and tokens are forbidden in tests and artifacts.

Pure unit tests cover:

- accepted and hostile parser variants;
- URL and redirect rejection;
- error, body, header, and log redaction;
- setup, invalid login, MFA, reauthentication, and duplicate setup;
- import/return splitting and missing, duplicate, or reordered intervals;
- statistic-ID validation for generated public IDs;
- idempotent ingestion and restart recovery;
- estimated-to-actual, upward, and downward corrections;
- missing and explicitly retracted interval quarantine behavior;
- USD validation, currency mismatch, and cost/compensation splitting;
- prune-baseline behavior;
- local calendar grouping and both daylight-saving transitions;
- backfill checkpoints; and
- request/path/response limits, rate limits, backoff, jitter, unload, and token
  clearing.

Home Assistant tests cover setup, migrations, unload, PII-free registries,
diagnostic allowlisting, external-statistics insert/update behavior, metadata,
restart persistence, and Energy-source discovery. A repository-wide canary test
checks that representative secret and personal-data markers cannot appear in
logs or diagnostics.

## CI, dependencies, and releases

CI uses least-privilege permissions and pins third-party actions to commit SHAs.
Required jobs include Ruff/format, mypy, pytest on the supported minimum and
current stable Home Assistant versions, hassfest, HACS validation, dependency
audit, CodeQL, and secret scanning. Coverage targets are 100 percent for the
transport, authentication, parsing, ledger, statistics, and redaction modules
and at least 95 percent overall. Dependency bots may open reviewable pull
requests but never auto-merge them.

The supported floor is Home Assistant 2026.9.3 on Python 3.14. CI also tests
Home Assistant 2026.9.4, the current stable version at this design checkpoint.
The manifest has no added runtime dependencies unless a later reviewed change
proves one necessary.

Releases use semantic versions and protected tags only after required review
and CI. The first hardened candidate is `1.0.0-rc.1`. Release notes identify
supported Home Assistant versions, the upstream base, migrations, and known
source limitations. The release asset is
`ha-entergy-1.0.0-rc.1.zip`, containing `custom_components/entergy_mobile/` at
its root, with `ha-entergy-1.0.0-rc.1.zip.sha256` in standard
`<hash><two spaces><filename>` form. GitHub artifact attestation is published;
an SPDX JSON SBOM is also published if runtime dependencies are introduced.
Install instructions never point production systems at a moving branch.

## Installation, updates, and rollback

Beaulab will install an exact reviewed release and record its commit and archive
SHA-256 in the private homelab repository. Credentials and live Home Assistant
configuration remain outside Git. HACS is used only if it preserves the exact
release; otherwise a verified archive is extracted into
`/config/custom_components/entergy_mobile`.

Each update requires a reviewed diff, verified checksum, `ha core check`, one
scheduled restart, and bounded post-start checks for authentication, statistics,
and logs. Custom-component updates are not unattended.

Rollback restores the exact prior archive instead of `main`. It unloads or
removes the new entry through supported Home Assistant controls and preserves
historical statistics unless the user separately approves their removal. It
never edits live SQLite or `.storage` files by hand.

## Live deployment gate

Publishing this repository does not authorize installation. Before VM202 is
changed:

1. CI, review, release, and checksum verification must pass.
2. Confirm that another component with the same domain is not installed.
3. Create a timestamped, root-only backup of any prior component and the
   relevant supported Home Assistant state; keep its manifest and hashes off
   GitHub.
4. Ensure any backup that will contain the Entergy password has an approved
   protection and recovery design.
5. Stage only the expected component files and verify checksums and ownership.
6. Run `ha core check`.
7. Obtain explicit approval for one Home Assistant Core restart and its
   expected downtime.
8. Restart once, inspect bounded logs, and have the user enter credentials in
   the local Home Assistant UI.
9. Verify challenge handling, statistic IDs, backfill, idempotency, corrections,
   freshness, and Energy dashboard discovery without displaying secrets.

If startup or validation fails, stop polling, unload or remove the new entry
through supported controls, restore the protected prior component/state, run
`ha core check`, restart once, and confirm prior Home Assistant health.

## Implementation sequence

1. Establish project identity, attribution, security policy, test harness, and
   pinned CI.
2. Isolate and harden transport, authentication, parsing, and redaction.
3. Add the canonical ledger, timezone logic, reconciliation, and persistence.
4. Add external statistics, freshness entities, and Home Assistant lifecycle.
5. Add migration, install, release, validation, and rollback documentation.
6. Complete review, CI, release pinning, and checksum verification before any
   live deployment is proposed.

## References

- [Entergy meter and myAdvisor information](https://www.entergy.com/meter)
- [Home Assistant config flow documentation](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/)
- [Home Assistant reauthentication documentation](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/#reauthentication)
- [Home Assistant diagnostics documentation](https://developers.home-assistant.io/docs/core/integration_diagnostics/)
- [Home Assistant external statistics documentation](https://developers.home-assistant.io/docs/core/entity/sensor/#long-term-statistics)
- [Recorder statistics API changes](https://developers.home-assistant.io/blog/2025/10/16/recorder-statistics-api-changes/)
