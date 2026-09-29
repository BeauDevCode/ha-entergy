"""Guard the public integration metadata and repository hygiene."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "custom_components/entergy_mobile/manifest.json"
HACS = ROOT / "hacs.json"


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / path.decode() for path in result.stdout.split(b"\0") if path]


def test_manifest_display_name() -> None:
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["name"] == "Entergy Usage (Unofficial Hardened Fork)"


def test_manifest_codeowner() -> None:
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["codeowners"] == ["@BeauDevCode"]


def test_manifest_documentation_url() -> None:
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["documentation"] == "https://github.com/BeauDevCode/ha-entergy"


def test_manifest_issue_tracker_url() -> None:
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["issue_tracker"] == "https://github.com/BeauDevCode/ha-entergy/issues"


def test_manifest_runtime_contract_is_preserved() -> None:
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["domain"] == "entergy_mobile"
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "cloud_polling"
    assert manifest["requirements"] == []


def test_manifest_retains_valid_semver_until_release() -> None:
    version = json.loads(MANIFEST.read_text())["version"]
    assert re.fullmatch(
        r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
        r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
        version,
    )


def test_hacs_requires_supported_home_assistant_floor() -> None:
    hacs = json.loads(HACS.read_text())
    assert hacs["homeassistant"] == "2026.9.3"


def test_no_production_artifacts_are_tracked() -> None:
    forbidden_suffixes = {
        ".db",
        ".sqlite",
        ".sqlite3",
        ".pcap",
        ".pcapng",
        ".har",
        ".log",
        ".mp4",
        ".mov",
        ".m4a",
        ".wav",
        ".heic",
        ".jpg",
        ".jpeg",
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".zip",
    }
    violations = [
        str(path.relative_to(ROOT))
        for path in _tracked_files()
        if path.suffix.lower() in forbidden_suffixes
    ]
    assert violations == []


def test_no_representative_credentials_or_private_keys_are_tracked() -> None:
    patterns = (
        re.compile("-----BEGIN " + "(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile("ghp_" + r"[A-Za-z0-9]{36}"),
        re.compile("AKIA" + r"[A-Z0-9]{16}"),
        re.compile(
            r"(?i)(?:password|access_token|client_secret)\s*[:=]\s*"
            r"['\"][A-Za-z0-9+/._-]{12,}['\"]"
        ),
    )
    violations: list[str] = []
    for path in _tracked_files():
        content = path.read_text(errors="ignore")
        if any(pattern.search(content) for pattern in patterns):
            violations.append(str(path.relative_to(ROOT)))
    assert violations == []


def test_temporary_runtime_compatibility_paths_are_absent() -> None:
    production = "\n".join(
        path.read_text() for path in (ROOT / "custom_components/entergy_mobile").glob("*.py")
    )
    for forbidden in (
        "EntergyApiError",
        "EntergyAuthError",
        "EntergyMfaRequired",
        "EntergyLoginResult",
        "_legacy_raw",
        "async_fetch_current_usage",
        "def get_coordinator",
        "hass.data[DOMAIN]",
        "hass.data.setdefault(DOMAIN",
    ):
        assert forbidden not in production
    api = (ROOT / "custom_components/entergy_mobile/api.py").read_text()
    assert "username: str | None" not in api
    assert "password: str | None" not in api
    assert "RequestBudget | None" not in api


def test_diagnostics_uses_allowlist_instead_of_serialization_or_redaction() -> None:
    diagnostics = (ROOT / "custom_components/entergy_mobile/diagnostics.py").read_text()
    for forbidden in (
        "entry.as_dict",
        "async_redact_data",
        "coordinator.data",
        "last_exception",
    ):
        assert forbidden not in diagnostics


def test_no_temporary_mypy_ignore_errors_and_translations_match() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert "ignore_errors = true" not in pyproject
    strings = json.loads((ROOT / "custom_components/entergy_mobile/strings.json").read_text())
    translations = json.loads(
        (ROOT / "custom_components/entergy_mobile/translations/en.json").read_text()
    )
    assert strings == translations
