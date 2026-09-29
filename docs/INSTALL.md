# Installation and updates

**Release procedure only:** `1.0.0-rc.1` is currently an unreleased candidate. The live-install gate is closed until an exact reviewed release, checksum, attestation, and deployment approval exist. Never install a moving `main` or feature branch.

Use Home Assistant 2026.9.3 or 2026.9.4 on Python 3.14. Before installation, verify that no other installed integration owns the `entergy_mobile` domain. Take a protected supported Home Assistant backup; it may contain the utility password. Review the exact release diff and archive checksum and record the release tag, commit, and SHA-256. Do not edit `.storage` or Recorder manually.

For HACS, add only the fork repository `https://github.com/BeauDevCode/ha-entergy` and select the exact reviewed release tag if HACS preserves that exact tag. If it cannot, use the manual archive procedure. Do not select a branch or an unreviewed commit.

For a manual install, after publication download the [exact archive](https://github.com/BeauDevCode/ha-entergy/releases/download/v1.0.0-rc.1/ha-entergy-1.0.0-rc.1.zip) and [matching checksum](https://github.com/BeauDevCode/ha-entergy/releases/download/v1.0.0-rc.1/ha-entergy-1.0.0-rc.1.zip.sha256) from the **same reviewed release**. Verify with `sha256sum --check ha-entergy-1.0.0-rc.1.zip.sha256` (or a trusted equivalent). Inspect the archive: its only top-level tree must be `custom_components/entergy_mobile/`. Extract it under the Home Assistant config directory so the final path is `/config/custom_components/entergy_mobile/`.

Run `ha core check`, schedule one restart, then use Settings → Devices & services to add or verify the integration through the local config flow. Check authentication, newest utility interval, last successful fetch, external statistics, repair issues, and a bounded window of sanitized logs. MFA and other unknown challenges require operator action; never disable account protection. The source is delayed and is not a live or bill-grade meter.

In the Energy dashboard, select the imported external statistic for grid consumption and the returned external statistic for energy returned to grid after a verified import. Cost and compensation remain absent when source or Home Assistant currency is not USD; no conversion occurs.

For updates, repeat the exact-release, backup, diff, checksum, `ha core check`, and one-restart sequence. Migration can change config-entry/registry identity, private ledger state, and external statistics; keep the pre-change supported backup and follow [rollback guidance](ROLLBACK.md) if needed. See [privacy details](PRIVACY.md).
