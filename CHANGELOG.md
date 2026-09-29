# Changelog

## [1.0.0-rc.1] — Unreleased

- Hardened username/password authentication and fail-closed challenge handling; restricted transport to the reviewed fixed origin.
- Added a private canonical ledger, correction-safe external Recorder energy and USD-only money statistics, bounded 370-day backfill, and 400-day retention plus baselines.
- Added privacy-preserving config-entry and registry migration, local diagnostics and persistent repairs, and the final Home Assistant sensor/lifecycle surface.
- Supported on Home Assistant 2026.9.3 and 2026.9.4 with Python 3.14. Upgrades need a protected supported backup because migration and statistics changes are not reversed by replacing code alone.
- Added adversarial boundary and shutdown regressions, both pinned coverage gates, and optional Recorder initialization ordering.
- Dependency audits retain a disclosed temporary upstream exception for four cryptography/PyJWT advisories, expiring 2026-10-29 00:00 UTC. Both full environments are audited without ignores or pin overrides; see [SECURITY.md](SECURITY.md). This is not a clean audit.
- Bounded decimal representations and exact aggregate arithmetic prevent payload-driven precision expansion; full pending-suffix Recorder verification preserves offsetting historical corrections after a crash. Unsafe existing ledger values fail closed without a lossy migration.
- Utility data may be delayed and is not bill-grade. MFA/CAPTCHA/consent challenges fail closed.

The candidate is not published. Installation procedures apply only after a reviewed release, checksum, and attestation exist.
