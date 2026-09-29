# Entergy Usage (Unofficial Hardened Fork)

![Generic electricity mark](assets/entergy-icon.svg)

This independent Home Assistant integration imports delayed whole-home Entergy utility intervals. It is unofficial and is not affiliated with, endorsed by, or supported by Entergy. It is not a live, real-time, or revenue-grade meter and is not bill-grade. The utility source can remain delayed even when polling more frequently; use the newest interval and last successful fetch timestamps to assess freshness.

This unreleased `1.0.0-rc.1` candidate supports Home Assistant **2026.9.3 and 2026.9.4** on **Python 3.14**. Only one installed integration can own the `entergy_mobile` domain. Do not install this candidate on a production system until an exact reviewed release has been published and verified.

The default poll is four hours; options allow one through 24 hours. Normal reconciliation refetches 45 days. Historical backfill is bounded to 370 days and runs one weekly page every 30 minutes, at most two per hour and 48 per day. Its 53 pages normally take about 27 hours. The private canonical ledger retains at least 400 days of intervals plus baselines. Corrections can revise previously imported external Recorder statistics.

Imported and returned energy are separate external Recorder statistics for the Energy dashboard. Cost and compensation are separate, non-negative series exposed only if the source contract and Home Assistant currency are both USD. No currency conversion or inferred tariff is performed.

Credentials are entered only through the trusted local Home Assistant config flow. The username and password remain in a permission-protected Home Assistant config entry, which is not an encrypted password vault; protect backups that may contain them. The access token remains in memory only. MFA, CAPTCHA, consent, and unknown login challenges fail closed. Never weaken utility-account security to make this integration work.

See [installation and update procedures](docs/INSTALL.md), [privacy and retention](docs/PRIVACY.md), [rollback](docs/ROLLBACK.md), [security reporting](SECURITY.md), [contributing](CONTRIBUTING.md), [attribution](NOTICE.md), and the [changelog](CHANGELOG.md).
