"""Base entities for Entergy."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EntergyDataUpdateCoordinator


class EntergyEntity(CoordinatorEntity[EntergyDataUpdateCoordinator]):
    """Base Entergy entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EntergyDataUpdateCoordinator,
        public_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, public_id)},
            entry_type=DeviceEntryType.SERVICE,
            name="Entergy Usage",
            manufacturer="Entergy",
            model="Cloud utility usage",
        )
