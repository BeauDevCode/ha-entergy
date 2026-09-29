# Security reporting

Security fixes are considered for the latest reviewed release candidate or release; older builds are unsupported. Reports about authentication, privacy, transport, ledger/Recorder integrity, dependency or workflow supply chain, and unsafe installation behavior are in scope.

Use [GitHub private vulnerability reporting](https://github.com/BeauDevCode/ha-entergy/security/advisories/new) for sensitive reports. If that form is unavailable, contact [@BeauDevCode](https://github.com/BeauDevCode) through GitHub to arrange a private channel; do not post sensitive details in a public issue. We do not promise a bounty, acknowledgement deadline, remediation SLA, or a particular confidentiality outcome.

Send a minimal synthetic reproduction. Omit credentials, tokens, account numbers, service addresses, production payloads, and Home Assistant runtime files. See [privacy guidance](docs/PRIVACY.md).

## Temporary upstream dependency exception

The full-environment audits for Home Assistant **2026.9.3 and 2026.9.4 are not clean**. Both versions require `cryptography==48.0.1` (advisories `PYSEC-2026-3552`, `PYSEC-2026-3553`, `PYSEC-2026-3554`) and `PyJWT==2.13.0` (`CVE-2026-102274`). Seven audit rows normalize to these four findings. The installed `pyOpenSSL==26.2.0` also constrains cryptography to `<49`. These are upstream host dependencies; this integration's manifest still adds no runtime dependencies. No incompatible pin override or advisory ignore is used.

CI audits each full pinned environment afresh and accepts only that exact finding set while installed Home Assistant metadata still requires both exact versions. The dependency-free gate rejects tool errors, skipped packages, malformed or stale evidence, inventory differences, new or disappeared findings, and changed host pins. The exception expires at **2026-10-29 00:00:00 UTC**; this is the latest review date, not a claim that the risk is fixed or harmless. Review sooner when a compatible patched Home Assistant version becomes available, regenerate both reviewed environments, rerun every gate, and remove or explicitly re-review the exception. The candidate remains unpublished pending hosted verification and final review.
