"""Strict allowlist diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from custom_components.entergy_mobile import EntergyRuntimeData
from custom_components.entergy_mobile.diagnostics import async_get_config_entry_diagnostics
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component import common  # type: ignore[import-untyped]

PUBLIC_ID = "a" * 32
EXPECTED = {
    "integration_version",
    "home_assistant_version",
    "configured_poll_seconds",
    "next_poll_seconds",
    "last_successful_fetch",
    "newest_interval",
    "error_category",
    "retained_interval_count",
    "estimated_interval_count",
    "last_inserted_count",
    "last_corrected_count",
    "freshness",
    "backfill_pages_completed",
    "backfill_pages_total",
    "backfill_complete",
}


async def test_diagnostics_exact_allowlist_is_fresh_json_and_excludes_canaries(
    hass: HomeAssistant,
) -> None:
    canary = "private-canary-value"
    entry = common.MockConfigEntry(
        domain="entergy_mobile",
        title=canary,
        unique_id=PUBLIC_ID,
        version=99,
        data={"username": canary, "password": canary, "account_id": canary},
        options={"private_option": canary},
    )
    entry.add_to_hass(hass)
    safe = {
        "configured_poll_seconds": 14_400,
        "next_poll_seconds": 3600,
        "last_successful_fetch": "2026-09-28T12:00:00+00:00",
        "newest_interval": "2026-09-28T10:00:00+00:00",
        "error_category": "transient",
        "retained_interval_count": 12,
        "estimated_interval_count": 2,
        "last_inserted_count": 1,
        "last_corrected_count": 3,
        "freshness": "delayed",
        "backfill_pages_completed": 4,
        "backfill_pages_total": 53,
        "backfill_complete": False,
        "backfill_progress_percent": 7,
        "repair_conditions": ["schema_drift"],
        "unapproved": canary,
    }
    coordinator = SimpleNamespace(
        diagnostics=lambda: dict(safe),
        data={"payload": canary},
        last_exception=RuntimeError(canary),
    )
    entry.runtime_data = EntergyRuntimeData(
        cast(Any, SimpleNamespace(access_token=canary, url=canary, headers={canary: canary})),
        cast(Any, SimpleNamespace(raw=canary)),
        cast(Any, coordinator),
    )
    integration = SimpleNamespace(version="0.1.1")
    with patch(
        "custom_components.entergy_mobile.diagnostics.async_get_integration",
        AsyncMock(return_value=integration),
    ):
        result = await async_get_config_entry_diagnostics(hass, entry)
    assert set(result) == EXPECTED
    assert result["integration_version"] == "0.1.1"
    assert result["home_assistant_version"] != "99"
    assert result["freshness"] == "delayed"
    encoded = json.dumps(result, sort_keys=True)
    assert canary not in encoded and PUBLIC_ID not in encoded
    result["retained_interval_count"] = 999
    assert coordinator.diagnostics()["retained_interval_count"] == 12


async def test_diagnostics_resolves_manifest_and_home_assistant_versions(
    hass: HomeAssistant,
) -> None:
    from homeassistant.const import __version__ as home_assistant_version

    entry = common.MockConfigEntry(domain="entergy_mobile", version=99, data={})
    entry.add_to_hass(hass)
    status = {
        key: None
        for key in EXPECTED
        if key not in {"integration_version", "home_assistant_version"}
    }
    coordinator = SimpleNamespace(diagnostics=lambda: status)
    entry.runtime_data = EntergyRuntimeData(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, coordinator),
    )
    result = await async_get_config_entry_diagnostics(hass, entry)
    manifest = json.loads(Path("custom_components/entergy_mobile/manifest.json").read_text())
    assert result["integration_version"] == manifest["version"]
    assert result["home_assistant_version"] == home_assistant_version
    assert result["integration_version"] != str(entry.version)
