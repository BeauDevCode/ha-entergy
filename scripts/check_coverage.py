"""Fail closed on branch and critical-module coverage regressions."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRITICAL = tuple(
    f"custom_components/entergy_mobile/{name}.py"
    for name in ("api", "errors", "parser", "ledger", "statistics", "diagnostics")
)


def fail(reason: str) -> None:
    print(f"coverage policy: {reason}", file=sys.stderr)
    raise SystemExit(1)


def number(value: object, label: str, *, integer: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (integer and not isinstance(value, int))
    ):
        fail(f"invalid {label}")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (integer and not result.is_integer()):
        fail(f"invalid {label}")
    return result


def main() -> None:
    if len(sys.argv) != 2:
        fail("expected one coverage JSON path")
    try:
        report = json.loads(Path(sys.argv[1]).read_text())
    except (OSError, ValueError) as exc:
        fail(f"cannot read coverage JSON: {type(exc).__name__}")
    if not isinstance(report, dict):
        fail("malformed report")
    meta, totals, files = report.get("meta"), report.get("totals"), report.get("files")
    if not isinstance(meta, dict) or meta.get("branch_coverage") is not True:
        fail("branch coverage is required")
    if not isinstance(totals, dict):
        fail("missing totals")
    overall = number(totals.get("percent_covered"), "overall percent")
    if overall > 100:
        fail("invalid overall percent")
    if overall < 95:
        fail("overall coverage below 95%")
    if not isinstance(files, dict):
        fail("missing file results")
    normalized: dict[str, dict[str, object]] = {}
    for raw, result in files.items():
        if not isinstance(raw, str) or not isinstance(result, dict):
            fail("malformed file result")
        key = raw.replace("\\", "/")
        if key.startswith("./"):
            key = key[2:]
        root = ROOT.as_posix() + "/"
        if key.startswith(root):
            key = key[len(root) :]
        if key in normalized:
            fail(f"ambiguous file: {key}")
        normalized[key] = result
    for key in CRITICAL:
        result = normalized.get(key)
        if result is None:
            fail(f"missing critical file: {key}")
        summary = result.get("summary")
        if not isinstance(summary, dict):
            fail(f"missing summary: {key}")
        values = {
            name: number(summary.get(name), f"{key} {name}", integer=True)
            for name in (
                "num_statements",
                "covered_lines",
                "missing_lines",
                "num_branches",
                "covered_branches",
                "missing_branches",
            )
        }
        if (
            values["covered_lines"] + values["missing_lines"] != values["num_statements"]
            or values["covered_branches"] + values["missing_branches"] != values["num_branches"]
        ):
            fail(f"inconsistent counts: {key}")
        missing_lines, missing_branches = (
            result.get("missing_lines"),
            result.get("missing_branches"),
        )
        if not isinstance(missing_lines, list) or not isinstance(missing_branches, list):
            fail(f"missing detail: {key}")
        if (
            values["missing_lines"]
            or values["missing_branches"]
            or missing_lines
            or missing_branches
        ):
            fail(f"critical coverage below 100%: {key}")
    names = ", ".join(Path(key).name for key in CRITICAL)
    print(f"coverage policy: {overall:.2f}% overall; 100% lines/branches: {names}")


if __name__ == "__main__":
    main()
