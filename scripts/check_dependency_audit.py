"""Run a fresh full-environment audit with one expiring upstream-only exception.

No advisory ignores or dependency overrides are used. Acceptance is not a clean
security audit. Every changed finding or host pin requires review of this policy.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
EXPIRY = datetime(2026, 10, 29, tzinfo=UTC)
EXPECTED = {("cryptography", "48.0.1", f"PYSEC-2026-{number}") for number in (3552, 3553, 3554)} | {
    ("pyjwt", "2.13.0", "CVE-2026-102274")
}
PINS = {"cryptography": "48.0.1", "pyjwt": "2.13.0"}


def normalize(name: str) -> str:
    """Normalize Python distribution names without a third-party dependency."""
    return re.sub(r"[-_.]+", "-", name).lower()


def validate(
    report: object,
    *,
    exit_code: int,
    expected_ha: str,
    installed: dict[str, str],
    requirements: list[str],
    now: datetime,
    started: datetime,
    modified: datetime,
    manifest: object,
) -> int:
    """Accept only the exact, fresh findings and reviewed environment metadata."""

    def require(condition: bool, reason: str) -> None:
        if not condition:
            raise ValueError(reason)

    require(all(value.tzinfo is UTC for value in (now, started, modified)), "UTC evidence required")
    require(now < EXPIRY, "upstream exception expired on 2026-10-29 UTC")
    require(
        started <= modified <= now and timedelta(0) <= now - started <= timedelta(minutes=20),
        "stale audit evidence",
    )
    require(
        type(exit_code) is int and exit_code == 1,
        "audit must exit exactly 1 with reviewed findings",
    )
    require(expected_ha in ("2026.9.3", "2026.9.4"), "unreviewed Home Assistant version")
    require(
        installed.get("homeassistant") == expected_ha,
        "installed Home Assistant does not match matrix",
    )
    require(
        isinstance(manifest, dict) and manifest.get("requirements") == [],
        "integration-owned dependencies require review",
    )
    for name, version in PINS.items():
        require(installed.get(name) == version, "installed upstream pin changed")
        declarations = [
            item
            for item in requirements
            if normalize(re.split(r"[\s\[<>=!~;(]", item, maxsplit=1)[0]) == name
        ]
        require(
            len(declarations) == 1
            and re.fullmatch(
                re.escape(name) + r"\s*==\s*" + re.escape(version), declarations[0], re.IGNORECASE
            )
            is not None,
            "Home Assistant no longer declares exact unconditional pins",
        )
    require(isinstance(report, dict), "malformed audit report")
    require(report.get("fixes") == [], "unexpected audit fixes")
    dependencies = report.get("dependencies")
    require(isinstance(dependencies, list) and bool(dependencies), "missing dependency inventory")
    inventory: dict[str, str] = {}
    findings: set[tuple[str, str, str]] = set()
    for item in dependencies:
        require(isinstance(item, dict), "malformed dependency")
        require("skip_reason" not in item, "audit skipped a dependency")
        name, version, vulns = item.get("name"), item.get("version"), item.get("vulns")
        require(
            isinstance(name, str) and bool(name) and isinstance(version, str) and bool(version),
            "missing dependency identity",
        )
        name = normalize(name)
        require(name not in inventory, "duplicate dependency inventory")
        inventory[name] = version
        require(isinstance(vulns, list), "malformed vulnerability list")
        for vuln in vulns:
            require(
                isinstance(vuln, dict) and isinstance(vuln.get("id"), str) and bool(vuln["id"]),
                "malformed advisory",
            )
            findings.add((name, version, vuln["id"]))
    require(inventory == installed, "audit inventory differs from installed environment")
    require(findings == EXPECTED, "new or disappeared findings require policy review")
    return len(findings)


def run(expected_ha: str) -> None:
    """Own the auditor invocation and fresh output file; never accept cached JSON."""
    installed = {}
    for distribution in metadata.distributions():
        name = normalize(distribution.metadata["Name"])
        if name in installed:
            raise ValueError("duplicate installed distribution")
        installed[name] = distribution.version
    requirements = metadata.requires("homeassistant") or []
    manifest = json.loads((ROOT / "custom_components/entergy_mobile/manifest.json").read_text())
    with TemporaryDirectory(prefix="entergy-audit-") as directory:
        output = Path(directory) / "audit.json"
        started = datetime.now(UTC)
        result = subprocess.run(
            [sys.executable, "-m", "pip_audit", "--format=json", "--output", str(output)],
            check=False,
            timeout=1100,
        )
        report = json.loads(output.read_text())
        count = validate(
            report,
            exit_code=result.returncode,
            expected_ha=expected_ha,
            installed=installed,
            requirements=requirements,
            now=datetime.now(UTC),
            started=started,
            modified=datetime.fromtimestamp(output.stat().st_mtime, UTC),
            manifest=manifest,
        )
    print(
        f"dependency audit: accepted TEMPORARY UPSTREAM EXCEPTION for {count} unique advisories; HA {expected_ha}; expires 2026-10-29 UTC; NOT a clean audit"
    )


def main() -> None:
    try:
        if len(sys.argv) != 2:
            raise ValueError("expected exact Home Assistant matrix version")
        run(sys.argv[1])
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        metadata.PackageNotFoundError,
        subprocess.SubprocessError,
    ) as error:
        print(f"dependency audit: FAILED ({type(error).__name__}): {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
