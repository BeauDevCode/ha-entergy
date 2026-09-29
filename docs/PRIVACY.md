# Privacy and data retention

The trusted local Home Assistant config flow collects the utility username and password. They remain in the permission-protected config entry, which is not an encrypted password vault. Home Assistant backups may contain the password; protect and test recovery for those backups. The access token lives only in process memory and is cleared on unload/authentication failure.

The integration processes a raw account ID and utility intervals, including usage and optional costs, only inside private runtime/storage boundaries. Public entities, identifiers, and diagnostics use a random public ID or fixed safe values; downloadable diagnostics use an exact allowlist and do not include credentials, account ID, address, token, payload, or exception text. Logs should contain safe operational messages only; still sanitize them before sharing.

Requests go only to the reviewed Entergy HTTPS origin. There is no integration telemetry. CI uses synthetic fixtures and no live utility calls. The private ledger retains at least 400 days of canonical intervals plus baselines. Home Assistant Recorder stores external statistics and may retain household energy history separately; Recorder and backup retention are operator-controlled.

Removing the integration does not silently erase Recorder history. Deleting historical statistics is a separate, deliberate Home Assistant action. Do not edit SQLite or `.storage` directly. The source can be delayed; values are not live or bill-grade. Monetary statistics are USD-only when both the source contract and Home Assistant currency are USD, with no conversion. See [installation](INSTALL.md) and [rollback](ROLLBACK.md).
