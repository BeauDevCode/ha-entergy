"""Guard the public integration metadata and repository hygiene."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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


def test_release_identity_attribution_and_license() -> None:
    assert json.loads(MANIFEST.read_text())["version"] == "1.0.0-rc.1"
    assert hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest() == (
        "855048541ef51ff34cc621315f75f336d280cc00e9e5e7013c4f1e337a63d8bc"
    )
    notice = (ROOT / "NOTICE.md").read_text()
    for required in (
        "David Delahoz",
        "daviddelahoz/ha-entergy",
        "eb5bd5f928fc221a2262f291792eeed4f1f21c50",
        "LICENSE",
        "unofficial",
        "original",
        "not affiliated",
    ):
        assert required.lower() in notice.lower()


def test_public_documents_disclose_operational_limits() -> None:
    required = {
        "README.md": (
            "Entergy Usage (Unofficial Hardened Fork)",
            "2026.9.3",
            "2026.9.4",
            "Python 3.14",
            "four hours",
            "45 days",
            "370 days",
            "USD",
            "MFA",
            "one",
            "not a live",
            "not bill-grade",
        ),
        "SECURITY.md": (
            "@BeauDevCode",
            "private vulnerability reporting",
            "not enabled",
            "credentials",
        ),
        "CONTRIBUTING.md": ("Python 3.14", "0.12.19", "synthetic", "mypy", "pip-audit"),
        "CHANGELOG.md": ("1.0.0-rc.1", "Unreleased", "migration", "backfill"),
        "docs/INSTALL.md": (
            "exact",
            "sha256",
            "ha core check",
            "one restart",
            "2026.9.3",
            "2026.9.4",
            "migration",
        ),
        "docs/PRIVACY.md": (
            "password",
            "config entry",
            "memory",
            "400",
            "Recorder",
            "backup",
            "USD",
        ),
        "docs/ROLLBACK.md": ("exact prior", "backup", "ha core check", "migration", "Recorder"),
    }
    for filename, phrases in required.items():
        content = (ROOT / filename).read_text().lower()
        for phrase in phrases:
            assert phrase.lower() in content, (filename, phrase)
    readme = (ROOT / "README.md").read_text()
    for linked in (
        "NOTICE.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "docs/INSTALL.md",
        "docs/PRIVACY.md",
        "docs/ROLLBACK.md",
    ):
        assert f"]({linked})" in readme
    for path in [
        ROOT / "README.md",
        ROOT / "NOTICE.md",
        ROOT / "SECURITY.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "CHANGELOG.md",
        *(ROOT / "docs").glob("*.md"),
    ]:
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", path.read_text()):
            if "://" not in target and not target.startswith("mailto:"):
                assert (path.parent / target).exists(), (path, target)
    public = "\n".join((ROOT / name).read_text() for name in required)
    assert "https://github.com/daviddelahoz/ha-entergy" not in public
    assert "buymeacoffee" not in public.lower()


def test_original_artwork_replaces_upstream_assets() -> None:
    assert not (ROOT / "assets/entergy-wordmark.svg").exists()
    svg = ROOT / "assets/entergy-icon.svg"
    png = ROOT / "custom_components/entergy_mobile/brand/icon.png"
    assert (
        hashlib.sha256(svg.read_bytes()).hexdigest()
        != "fef4f227871eaeb09d1a864d5b9fdc339c1b0d16353231e9bbded0dae1305990"
    )
    assert (
        hashlib.sha256(png.read_bytes()).hexdigest()
        != "9b269f23997ff3c3952937ad0d70b3fd005afb08ec6f7449b879b51b95eb702e"
    )
    content = svg.read_text()
    assert "viewBox=" in content and "<title>" in content and "<desc>" in content
    for forbidden in (
        "<script",
        "<foreignObject",
        "<image",
        "data:",
        "href=",
        "<text",
        "linearGradient",
    ):
        assert forbidden not in content
    assert re.search(r"<svg[^>]*viewBox=\"0 0 \d+ \d+\"", content)
    from PIL import Image

    with Image.open(png) as im:
        assert im.size == (256, 256) and im.mode == "RGBA"
    assert "entergy-wordmark" not in (ROOT / "README.md").read_text()


def test_workflows_have_pinned_actions_and_least_privilege() -> None:
    pins = {
        "actions/checkout": "d23441a48e516b6c34aea4fa41551a30e30af803",
        "actions/setup-python": "ece7cb06caefa5fff74198d8649806c4678c61a1",
        "astral-sh/setup-uv": "37802adc94f370d6bfd71619e3f0bf239e1f3b78",
        "github/codeql-action/init": "1190a975f95ce23525efb6a3fc21ea29567c1b52",
        "github/codeql-action/analyze": "1190a975f95ce23525efb6a3fc21ea29567c1b52",
        "gitleaks/gitleaks-action": "e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e",
        "hacs/action": "d556e736723344f83838d08488c983a15381059a",
        "home-assistant/actions/hassfest": "06749dd8c0b54f350bc69c8752456cee498808a3",
        "actions/attest": "1e69f48acb82d1966a394da916b4c1698aa569d6",
        "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    }
    for name in ("ci", "validate", "codeql", "release"):
        content = (ROOT / f".github/workflows/{name}.yml").read_text()
        data = yaml.safe_load(content)
        assert "permissions" in data and data["permissions"] == {}
        assert "concurrency" in data
        assert "pull_request_target" not in content
        assert "curl" not in content
        for job in data["jobs"].values():
            assert job["timeout-minutes"] > 0
            assert "permissions" in job
            assert all(
                level == "read"
                or (name == "codeql" and scope == "security-events" and level == "write")
                or (
                    name == "release"
                    and scope in {"id-token", "attestations", "artifact-metadata"}
                    and level == "write"
                )
                for scope, level in job["permissions"].items()
            )
        for action, sha in re.findall(
            r"^\s*uses:\s*([^@\s]+)@([a-f0-9]{40})(?:\s*#\s*\S+)?\s*$", content, re.M
        ):
            assert pins[action] == sha
        uses_lines = re.findall(r"^\s*uses:.*$", content, re.M)
        assert len(uses_lines) == len(
            re.findall(r"^\s*uses:\s*[^@\s]+@[a-f0-9]{40}\s+#\s*\S+.*$", content, re.M)
        )
    release = (ROOT / ".github/workflows/release.yml").read_text()
    for required in (
        "ha-entergy-1.0.0-rc.1.zip",
        ".zip.sha256",
        "git archive",
        "sha256sum --check",
        "manifest.json",
        "actions/attest",
    ):
        assert required in release
    for forbidden in (
        "gh release",
        "git tag",
        "contents: write",
        "create-release",
        "upload-release-asset",
    ):
        assert forbidden not in release
    validation = (ROOT / ".github/workflows/validate.yml").read_text()
    assert "GITLEAKS_VERSION: '8.24.3'" in validation
    assert "GITLEAKS_ENABLE_COMMENTS: 'false'" in validation
    assert "GITLEAKS_ENABLE_UPLOAD_ARTIFACT: 'false'" in validation


def test_repository_governance_templates() -> None:
    assert "@BeauDevCode" in (ROOT / ".github/CODEOWNERS").read_text()
    for name in (
        "PULL_REQUEST_TEMPLATE.md",
        "ISSUE_TEMPLATE/bug.yml",
        "ISSUE_TEMPLATE/feature.yml",
        "ISSUE_TEMPLATE/config.yml",
    ):
        content = (ROOT / ".github" / name).read_text().lower()
        assert "password" in content or "credential" in content
        assert "account" in content
    dependabot = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    assert {x["package-ecosystem"] for x in dependabot["updates"]} == {"github-actions", "pip"}


def _coverage_report() -> dict[str, object]:
    files = {}
    for module in ("api", "errors", "parser", "ledger", "statistics", "diagnostics"):
        files[f"custom_components/entergy_mobile/{module}.py"] = {
            "summary": {
                "num_statements": 10,
                "covered_lines": 10,
                "missing_lines": 0,
                "num_branches": 2,
                "covered_branches": 2,
                "missing_branches": 0,
            },
            "missing_lines": [],
            "missing_branches": [],
        }
    return {"meta": {"branch_coverage": True}, "totals": {"percent_covered": 95.0}, "files": files}


@pytest.mark.parametrize(
    "mutation",
    [
        "valid",
        "no_branch",
        "low_total",
        "missing_file",
        "missing_line",
        "missing_branch",
        "ambiguous",
        "wrong_path",
        "malformed",
    ],
)
def test_coverage_checker_policy(tmp_path: Path, mutation: str) -> None:
    report = _coverage_report()
    files = report["files"]
    assert isinstance(files, dict)
    key = "custom_components/entergy_mobile/api.py"
    if mutation == "no_branch":
        report["meta"]["branch_coverage"] = False
    if mutation == "low_total":
        report["totals"]["percent_covered"] = 94.99
    if mutation == "missing_file":
        del files[key]
    if mutation == "missing_line":
        files[key]["summary"]["missing_lines"] = 1
    if mutation == "missing_branch":
        files[key]["missing_branches"] = [[1, 2]]
    if mutation == "ambiguous":
        files["./" + key] = files[key]
    if mutation == "wrong_path":
        files["other/" + key] = files.pop(key)
    path = tmp_path / "coverage.json"
    path.write_text("{" if mutation == "malformed" else json.dumps(report))
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_coverage.py"), str(path)],
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is (mutation == "valid"), result.stderr
