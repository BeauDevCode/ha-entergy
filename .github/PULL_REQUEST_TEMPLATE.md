## Change

Describe behavior, migration, Recorder, network, dependency, or action-pin impact. Link an issue if useful.

## Validation

List synthetic tests and exact Home Assistant versions run. Explain any skipped check.

## Privacy

Do not post passwords, credentials, tokens, account numbers, addresses, payloads, captures, databases, Home Assistant runtime files, or homelab details. Use only sanitized diagnostics and synthetic examples.

Pinned HACS and hassfest wrappers still invoke upstream-maintained mutable containers; review upstream changes manually alongside Dependabot action-pin updates.

The RC audit gate temporarily accepts only the four upstream cryptography/PyJWT advisories documented in SECURITY.md, until 2026-10-29 00:00 UTC. Report each exact HA audit result as an accepted upstream exception, never as a clean audit. Confirm that changed or disappeared findings still fail closed.
