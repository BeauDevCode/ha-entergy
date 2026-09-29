"""Privacy-safe rolling usage and integration-health sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import EntergyConfigEntry
from .coordinator import EntergyDataUpdateCoordinator
from .entity import EntergyEntity
from .models import Freshness, UsageSnapshot

type _SnapshotValue = Decimal | datetime | str | None
type _StatusValue = str | int | bool | None | list[str]


@dataclass(frozen=True, kw_only=True)
class EntergySensorEntityDescription(SensorEntityDescription):
    """Describe one explicitly approved public sensor."""

    snapshot_value: Callable[[UsageSnapshot], _SnapshotValue] | None = None
    status_key: str | None = None


def _usage(
    key: str,
    field: str,
    *,
    monetary: bool = False,
) -> EntergySensorEntityDescription:
    return EntergySensorEntityDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement="USD" if monetary else UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.MONETARY if monetary else SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:currency-usd" if monetary else "mdi:transmission-tower-import",
        suggested_display_precision=2 if monetary else 3,
        snapshot_value=lambda snapshot: getattr(snapshot, field),
    )


SENSORS: tuple[EntergySensorEntityDescription, ...] = (
    EntergySensorEntityDescription(
        key="newest_interval",
        translation_key="newest_interval",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-check-outline",
        snapshot_value=lambda snapshot: snapshot.newest_interval_start,
    ),
    EntergySensorEntityDescription(
        key="freshness",
        translation_key="freshness",
        device_class=SensorDeviceClass.ENUM,
        options=[value.value for value in Freshness],
        icon="mdi:database-clock-outline",
        snapshot_value=lambda snapshot: snapshot.freshness.value,
    ),
    _usage("latest_import", "latest_import_kwh"),
    _usage("today_import", "today_import_kwh"),
    _usage("seven_day_import", "seven_day_import_kwh"),
    _usage("month_import", "month_import_kwh"),
    _usage("latest_return", "latest_return_kwh"),
    _usage("today_return", "today_return_kwh"),
    _usage("seven_day_return", "seven_day_return_kwh"),
    _usage("month_return", "month_return_kwh"),
    _usage("latest_cost", "latest_cost", monetary=True),
    _usage("today_cost", "today_cost", monetary=True),
    _usage("seven_day_cost", "seven_day_cost", monetary=True),
    _usage("month_cost", "month_cost", monetary=True),
    _usage("latest_compensation", "latest_compensation", monetary=True),
    _usage("today_compensation", "today_compensation", monetary=True),
    _usage("seven_day_compensation", "seven_day_compensation", monetary=True),
    _usage("month_compensation", "month_compensation", monetary=True),
    EntergySensorEntityDescription(
        key="last_successful_fetch",
        translation_key="last_successful_fetch",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:cloud-check-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        status_key="last_successful_fetch",
    ),
    EntergySensorEntityDescription(
        key="retained_interval_count",
        translation_key="retained_interval_count",
        icon="mdi:counter",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        status_key="retained_interval_count",
    ),
    EntergySensorEntityDescription(
        key="estimated_interval_count",
        translation_key="estimated_interval_count",
        icon="mdi:counter",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        status_key="estimated_interval_count",
    ),
    EntergySensorEntityDescription(
        key="last_corrected_count",
        translation_key="last_corrected_count",
        icon="mdi:database-edit-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        status_key="last_corrected_count",
    ),
    EntergySensorEntityDescription(
        key="backfill_progress",
        translation_key="backfill_progress",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:history",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        status_key="backfill_progress_percent",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EntergyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors from the typed runtime container."""
    del hass
    runtime = entry.runtime_data
    public_id = str(entry.data["public_id"])
    async_add_entities(
        EntergySensor(runtime.coordinator, public_id, description) for description in SENSORS
    )


class EntergySensor(EntergyEntity, SensorEntity):
    """One explicitly allowlisted Entergy sensor."""

    entity_description: EntergySensorEntityDescription

    def __init__(
        self,
        coordinator: EntergyDataUpdateCoordinator,
        public_id: str,
        description: EntergySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, public_id)
        self.entity_description = description
        self._attr_unique_id = f"{public_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return only a reviewed snapshot or safe-status value."""
        value_fn = self.entity_description.snapshot_value
        if value_fn is not None:
            return value_fn(self.coordinator.data)
        key = self.entity_description.status_key
        assert key is not None
        status: dict[str, _StatusValue] = self.coordinator.diagnostics()
        value = status.get(key)
        if self.entity_description.device_class == SensorDeviceClass.TIMESTAMP:
            if not isinstance(value, str):
                return None
            parsed = dt_util.parse_datetime(value)
            return parsed if parsed is not None and parsed.tzinfo is not None else None
        if key == "backfill_progress_percent":
            if status.get("backfill_complete") is True:
                return 100
            return min(99, value) if type(value) is int else None
        return value
