"""The temporary upstream exception is exact, expiring, and fail closed."""

import runpy
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 29, tzinfo=UTC)
PINS = ["cryptography==48.0.1", "PyJWT==2.13.0"]
INVENTORY = {"homeassistant": "2026.9.4", "cryptography": "48.0.1", "pyjwt": "2.13.0"}


def report() -> dict:
    return {
        "dependencies": [
            {"name": "homeassistant", "version": "2026.9.4", "vulns": []},
            {
                "name": "cryptography",
                "version": "48.0.1",
                "vulns": [{"id": f"PYSEC-2026-{n}"} for n in (3552, 3553, 3554, 3552)],
            },
            {"name": "PyJWT", "version": "2.13.0", "vulns": [{"id": "CVE-2026-102274"}]},
        ],
        "fixes": [],
    }


@pytest.fixture
def gate():
    return runpy.run_path(str(ROOT / "scripts/check_dependency_audit.py"))["validate"]


def check(gate, payload=None, **kwargs):
    args = dict(
        exit_code=1,
        expected_ha="2026.9.4",
        installed=INVENTORY,
        requirements=PINS,
        now=NOW,
        started=NOW - timedelta(seconds=2),
        modified=NOW - timedelta(seconds=1),
        manifest={"requirements": []},
    )
    args.update(kwargs)
    return gate(report() if payload is None else payload, **args)


def test_exact_exception_normalizes_duplicates_and_names(gate) -> None:
    assert check(gate) == 4


@pytest.mark.parametrize("version", ["2026.9.3", "2026.9.4"])
def test_both_exact_environments(gate, version) -> None:
    data = report()
    data["dependencies"][0]["version"] = version
    assert (
        check(gate, data, expected_ha=version, installed={**INVENTORY, "homeassistant": version})
        == 4
    )


@pytest.mark.parametrize("code", [0, 2, -1, True])
def test_audit_clean_error_or_signal_requires_review(gate, code) -> None:
    with pytest.raises(ValueError):
        check(gate, exit_code=code)


@pytest.mark.parametrize(
    "change",
    [
        "new",
        "missing",
        "version",
        "skipped",
        "no_vulns",
        "duplicate_package",
        "inventory",
        "bad_id",
        "fixes",
        "empty",
        "shape",
    ],
)
def test_changed_or_malformed_report_is_rejected(gate, change) -> None:
    data = deepcopy(report())
    if change == "new":
        data["dependencies"][0]["vulns"] = [{"id": "CVE-NEW"}]
    elif change == "missing":
        data["dependencies"][2]["vulns"] = []
    elif change == "version":
        data["dependencies"][1]["version"] = "49.0.0"
    elif change == "skipped":
        data["dependencies"][0]["skip_reason"] = "network error"
    elif change == "no_vulns":
        del data["dependencies"][0]["vulns"]
    elif change == "duplicate_package":
        data["dependencies"].append(data["dependencies"][0])
    elif change == "inventory":
        data["dependencies"].pop(0)
    elif change == "bad_id":
        data["dependencies"][1]["vulns"][0]["id"] = 17
    elif change == "fixes":
        data["fixes"] = [{"unexpected": "fix"}]
    elif change == "empty":
        data["dependencies"] = []
    else:
        data = []
    with pytest.raises(ValueError):
        check(gate, data)


@pytest.mark.parametrize(
    "requirements",
    [
        [],
        ["cryptography>=48.0.1", "PyJWT==2.13.0"],
        ["cryptography==48.0.1; extra == 'test'", "PyJWT==2.13.0"],
        [*PINS, "cryptography>=49"],
    ],
)
def test_exception_requires_exact_unconditional_host_pins(gate, requirements) -> None:
    with pytest.raises(ValueError):
        check(gate, requirements=requirements)


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_ha": "2026.9.5"},
        {"now": datetime(2026, 10, 29, tzinfo=UTC)},
        {"modified": NOW - timedelta(seconds=3)},
        {"modified": NOW + timedelta(seconds=1)},
        {"started": NOW - timedelta(minutes=21)},
        {"manifest": {"requirements": ["requests==1"]}},
        {"now": NOW.replace(tzinfo=None)},
    ],
)
def test_expiry_staleness_environment_and_owned_dependencies_fail(gate, changes) -> None:
    with pytest.raises(ValueError):
        check(gate, **changes)


@pytest.mark.parametrize(
    "failure", [None, "invalid_json", "missing_output", "tool_error", "timeout", "stale"]
)
def test_runner_owns_fresh_json_and_preserves_auditor_failures(
    monkeypatch, capsys, failure
) -> None:
    import json
    import os
    import subprocess
    import sys
    from types import SimpleNamespace

    module = runpy.run_path(str(ROOT / "scripts/check_dependency_audit.py"))
    monkeypatch.setattr(
        module["metadata"],
        "distributions",
        lambda: [
            SimpleNamespace(metadata={"Name": name}, version=version)
            for name, version in INVENTORY.items()
        ],
    )
    monkeypatch.setattr(module["metadata"], "requires", lambda name: PINS)
    module["run"].__globals__["datetime"] = SimpleNamespace(
        now=lambda zone: NOW, fromtimestamp=datetime.fromtimestamp
    )
    paths = []

    def audit(command, *, check, timeout):
        assert command[:4] == [sys.executable, "-m", "pip_audit", "--format=json"]
        assert command[4] == "--output" and check is False and timeout == 1100
        path = Path(command[5])
        assert not path.exists()
        paths.append(path)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, timeout)
        if failure != "missing_output":
            path.write_text("{invalid" if failure == "invalid_json" else json.dumps(report()))
        if path.exists():
            os.utime(path, (NOW.timestamp(), NOW.timestamp()))
        if failure == "stale":
            os.utime(path, (1, 1))
        return SimpleNamespace(returncode=2 if failure == "tool_error" else 1)

    monkeypatch.setattr(module["subprocess"], "run", audit)
    if failure:
        with pytest.raises((OSError, ValueError, subprocess.TimeoutExpired)):
            module["run"]("2026.9.4")
    else:
        module["run"]("2026.9.4")
        module["run"]("2026.9.4")
        assert paths[0] != paths[1]
        assert "NOT a clean audit" in capsys.readouterr().out
    assert all(not path.exists() for path in paths)
