"""Strictly allowlisted diagnostics for Entergy Usage."""

from __future__ import annotations

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from . import EntergyConfigEntry
from .const import DOMAIN

type _DiagnosticValue = str | int | bool | None

_APPROVED = (
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
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: EntergyConfigEntry,
) -> dict[str, _DiagnosticValue]:
    """Build a new object from the fixed public diagnostics contract."""
    status = entry.runtime_data.coordinator.diagnostics()
    integration = await async_get_integration(hass, DOMAIN)
    result: dict[str, _DiagnosticValue] = {
        "integration_version": str(integration.version),
        "home_assistant_version": HA_VERSION,
    }
    for key in _APPROVED:
        value = status.get(key)
        result[key] = value if isinstance(value, (str, int, bool)) or value is None else None
    return result
