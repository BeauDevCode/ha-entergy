# Changelog

## [1.0.0-rc.1] — Unreleased

- Hardened username/password authentication and fail-closed challenge handling; restricted transport to the reviewed fixed origin.
- Added a private canonical ledger, correction-safe external Recorder energy and USD-only money statistics, bounded 370-day backfill, and 400-day retention plus baselines.
- Added privacy-preserving config-entry and registry migration, local diagnostics and persistent repairs, and the final Home Assistant sensor/lifecycle surface.
- Supported on Home Assistant 2026.9.3 and 2026.9.4 with Python 3.14. Upgrades need a protected supported backup because migration and statistics changes are not reversed by replacing code alone.
- Utility data may be delayed and is not bill-grade. MFA/CAPTCHA/consent challenges fail closed.

The candidate is not published. Installation procedures apply only after a reviewed release, checksum, and attestation exist.
