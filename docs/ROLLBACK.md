# Rollback

Keep the exact prior reviewed archive, its recorded commit and SHA-256, and a protected pre-change supported Home Assistant backup. Never use a moving branch for rollback. Backups containing the utility password require an approved protection and recovery design.

Disable or unload the integration through supported Home Assistant controls. Restore the exact prior `custom_components/entergy_mobile/` tree, or restore the supported backup when the prior code cannot understand migrated state. Run `ha core check`, schedule one restart, then make bounded checks of authentication, freshness, external statistics, repairs, and sanitized logs.

Replacing Python files alone does not reverse config-entry or registry migration, external Recorder statistics, or ledger schema changes. Preserve Recorder history by default. Never delete statistics, the private ledger, `.storage` entries, or SQLite rows as a routine rollback step. Any historical-statistics removal is a separate deliberate Home Assistant action. See [installation](INSTALL.md) and [privacy](PRIVACY.md).
