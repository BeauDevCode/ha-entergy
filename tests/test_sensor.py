"""PII-free rolling summary and utility health sensors."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from custom_components import entergy_mobile as integration
from custom_components.entergy_mobile.const import DOMAIN
from custom_components.entergy_mobile.models import Freshness, UsageSnapshot
from custom_components.entergy_mobile.sensor import SENSORS, EntergySensor
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pytest_homeassistant_custom_component import common  # type: ignore[import-untyped]

PUBLIC_ID = "a" * 32


def snapshot(*, money: bool = True, freshness: Freshness = Freshness.FRESH) -> UsageSnapshot:
    values = [Decimal(str(index)) if money else None for index in range(9, 17)]
    return UsageSnapshot(
        datetime(2026, 9, 28, 10, tzinfo=UTC),
        freshness,
        Decimal("1"),
        Decimal("2"),
        Decimal("3"),
        Decimal("4"),
        Decimal("5"),
        Decimal("6"),
        Decimal("7"),
        Decimal("8"),
        *values,
    )


def status() -> dict[str, Any]:
    return {
        "last_successful_fetch": "2026-09-28T12:00:00+00:00",
        "retained_interval_count": 12,
        "estimated_interval_count": 2,
        "last_corrected_count": 3,
        "backfill_progress_percent": 100,
        "backfill_complete": False,
    }


def sensor(key: str, *, snap: UsageSnapshot | None = None) -> EntergySensor:
    coordinator = SimpleNamespace(data=snap or snapshot(), diagnostics=status)
    description = next(item for item in SENSORS if item.key == key)
    return EntergySensor(coordinator, PUBLIC_ID, description)  # type: ignore[arg-type]


def test_exact_sensor_inventory_and_no_monotonic_lifetime_totals() -> None:
    keys = {item.key for item in SENSORS}
    assert keys == {
        "newest_interval",
        "freshness",
        "latest_import",
        "today_import",
        "seven_day_import",
        "month_import",
        "latest_return",
        "today_return",
        "seven_day_return",
        "month_return",
        "latest_cost",
        "today_cost",
        "seven_day_cost",
        "month_cost",
        "latest_compensation",
        "today_compensation",
        "seven_day_compensation",
        "month_compensation",
        "last_successful_fetch",
        "retained_interval_count",
        "estimated_interval_count",
        "last_corrected_count",
        "backfill_progress",
    }
    assert all(item.state_class != SensorStateClass.TOTAL_INCREASING for item in SENSORS)
    assert all("lifetime" not in item.key and not item.key.startswith("total_") for item in SENSORS)


def test_rolling_values_map_exactly_and_money_can_be_absent() -> None:
    assert sensor("latest_import").native_value == Decimal("1")
    assert sensor("month_return").native_value == Decimal("8")
    assert sensor("latest_cost").native_value == Decimal("9")
    assert sensor("month_compensation").native_value == Decimal("16")
    assert sensor("latest_cost", snap=snapshot(money=False)).native_value is None
    assert sensor("latest_import", snap=snapshot(money=False)).native_value == Decimal("1")


@pytest.mark.parametrize("freshness", list(Freshness))
def test_freshness_and_timestamp_values(freshness: Freshness) -> None:
    assert sensor("freshness", snap=snapshot(freshness=freshness)).native_value == freshness.value
    newest = sensor("newest_interval").native_value
    fetched = sensor("last_successful_fetch").native_value
    assert newest == datetime(2026, 9, 28, 10, tzinfo=UTC)
    assert fetched == datetime(2026, 9, 28, 12, tzinfo=UTC)


def test_diagnostic_defaults_and_backfill_never_claims_early_completion() -> None:
    disabled = {
        item.key
        for item in SENSORS
        if item.entity_category == EntityCategory.DIAGNOSTIC
        and item.entity_registry_enabled_default is False
    }
    assert disabled == {
        "last_successful_fetch",
        "retained_interval_count",
        "estimated_interval_count",
        "last_corrected_count",
        "backfill_progress",
    }
    assert sensor("backfill_progress").native_value == 99


def test_generic_device_and_public_unique_ids_have_no_attributes() -> None:
    subject = sensor("today_import")
    assert subject.unique_id == f"{PUBLIC_ID}_today_import"
    rendered = repr(subject.device_info)
    for sentinel in ("private-user", "private-account", "private-address", "entry-id"):
        assert sentinel not in rendered
    device = subject.device_info
    assert device is not None
    assert device["identifiers"] == {("entergy_mobile", PUBLIC_ID)}
    assert subject.extra_state_attributes is None


def test_sensor_metadata_uses_translations_and_rolling_total_state() -> None:
    energy = next(item for item in SENSORS if item.key == "today_import")
    freshness = next(item for item in SENSORS if item.key == "freshness")
    assert not isinstance(energy.name, str) and energy.translation_key == "today_import"
    assert energy.state_class == SensorStateClass.TOTAL
    assert freshness.device_class == SensorDeviceClass.ENUM
    assert freshness.options == [item.value for item in Freshness]


def test_translation_files_are_identical_and_cover_every_sensor() -> None:
    strings = json.loads(Path("custom_components/entergy_mobile/strings.json").read_text())
    translations = json.loads(
        Path("custom_components/entergy_mobile/translations/en.json").read_text()
    )
    assert strings == translations
    entities = strings["entity"]["sensor"]
    assert set(entities) == {item.key for item in SENSORS}
    assert set(entities["freshness"]["state"]) == {item.value for item in Freshness}


class PlatformCoordinator(DataUpdateCoordinator[UsageSnapshot]):
    """Real HA coordinator boundary with deterministic validated data."""

    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry[Any], data: UsageSnapshot
    ) -> None:
        super().__init__(
            hass,
            logging.getLogger(__name__),
            name="entergy-platform-test",
            config_entry=config_entry,
        )
        self.data = data
        self._status = status()

    async def _async_update_data(self) -> UsageSnapshot:
        return self.data

    async def async_initialize(self) -> None:
        """No-op local fixture initialization."""

    async def async_start_backfill(self) -> None:
        """No-op fixture backfill."""

    def diagnostics(self) -> dict[str, Any]:
        return dict(self._status)


async def test_real_sensor_platform_state_registry_and_coordinator_availability(
    hass: HomeAssistant,
) -> None:
    item = common.MockConfigEntry(
        domain=DOMAIN,
        unique_id=PUBLIC_ID,
        version=2,
        minor_version=1,
        data={
            "username": "private-user",
            "password": "private-password",
            "account_id": "private-account",
            "public_id": PUBLIC_ID,
            "time_zone": "America/Chicago",
            "ledger_initialized": True,
        },
        options={"scan_interval_seconds": 14_400},
    )
    item.add_to_hass(hass)
    coordinator = PlatformCoordinator(hass, item, snapshot())
    ledger = Mock()
    ledger.state.revision = 1
    client = Mock(authenticated=False)
    client.async_logout = AsyncMock()
    client.clear_token = Mock()
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
    ):
        assert await hass.config_entries.async_setup(item.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)

    def entity_id(key: str) -> str:
        value = registry.async_get_entity_id("sensor", DOMAIN, f"{PUBLIC_ID}_{key}")
        assert value is not None
        return value

    assert hass.states.get(entity_id("today_import")).state == "3"  # type: ignore[union-attr]
    assert (
        hass.states.get(entity_id("newest_interval")).state  # type: ignore[union-attr]
        == "2026-09-28T10:00:00+00:00"
    )
    freshness_id = entity_id("freshness")
    assert hass.states.get(freshness_id).state == "fresh"  # type: ignore[union-attr]
    for value in Freshness:
        coordinator.async_set_updated_data(replace(coordinator.data, freshness=value))
        await hass.async_block_till_done()
        assert hass.states.get(freshness_id).state == value.value  # type: ignore[union-attr]

    diagnostic_id = entity_id("last_successful_fetch")
    diagnostic = registry.async_get(diagnostic_id)
    assert diagnostic is not None and diagnostic.disabled_by == er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(diagnostic_id) is None

    retained = coordinator.data
    coordinator.async_set_update_error(UpdateFailed("transient"))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id("today_import")).state == "unavailable"  # type: ignore[union-attr]
    assert coordinator.data is retained
    coordinator.async_set_updated_data(retained)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id("today_import")).state == "3"  # type: ignore[union-attr]

    assert await hass.config_entries.async_unload(item.entry_id)
