"""The Entergy integration."""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from .api import EntergyApiClient, EntergyApiError, EntergyAuthError, EntergyMfaRequired
from .const import (
    CONF_ACCOUNT_ID,
    CONF_LANGUAGE,
    CONF_SCAN_INTERVAL_SECONDS,
    DEFAULT_LANGUAGE,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
)
from .coordinator import EntergyDataUpdateCoordinator
from .ledger import EntergyLedger, LedgerRepairError, async_import_legacy_v1_store
from .models import LedgerMutation
from .statistics import statistic_ids

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


def get_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> EntergyDataUpdateCoordinator:
    """Return coordinator for an entry."""
    return hass.data[DOMAIN][entry.entry_id]["coordinator"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Entergy from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)
    client = EntergyApiClient(
        session=session,
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        language=entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
    )

    try:
        await client.async_initialize()
        await client.async_login()
    except EntergyMfaRequired as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except EntergyAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except EntergyApiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    scan_interval = int(
        entry.options.get(CONF_SCAN_INTERVAL_SECONDS, DEFAULT_SCAN_INTERVAL_SECONDS)
    )

    coordinator = EntergyDataUpdateCoordinator(
        hass=hass,
        client=client,
        account_id=entry.data[CONF_ACCOUNT_ID],
        entry_id=entry.entry_id,
        scan_interval_seconds=scan_interval,
    )
    await coordinator.async_initialize()
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {"client": client, "coordinator": coordinator}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Entergy config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if entry_data:
            client: EntergyApiClient = entry_data["client"]
            await client.async_logout()
    return unload_ok


def _migration_issue(hass: HomeAssistant, public_id: str, key: str) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{key}_{public_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=key,
    )


def _migrate_registries(hass: HomeAssistant, entry: ConfigEntry, public_id: str) -> None:
    """Use stable registry UUIDs for resumable, privacy-first updates."""
    devices, entities = dr.async_get(hass), er.async_get(hass)
    tokens = {str(entry.data.get(key, "")).casefold() for key in (CONF_USERNAME, CONF_ACCOUNT_ID)}
    tokens |= {slugify(token) for token in tokens}

    def unsafe(value: str) -> bool:
        return not all(tokens) or any(token in value.casefold() for token in tokens)

    owned_devices = dr.async_entries_for_config_entry(devices, entry.entry_id)
    device_ids = {device.id for device in owned_devices}
    attached = [
        entity
        for entity in entities.entities.values()
        if entity.device_id in device_ids or entity.config_entry_id == entry.entry_id
    ]
    # Persist the decision before the first rename; subsequent retries must still
    # rename safe-looking peers after the original offending ID has disappeared.
    rename_all = entry.data.get(
        "migration_rename_all",
        any(
            unsafe(entity.entity_id)
            or entity.entity_id.startswith(f"{entity.domain}.entergy_{public_id}_")
            for entity in attached
        ),
    )
    if "migration_rename_all" not in entry.data:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "migration_rename_all": rename_all}
        )
    if rename_all:
        _migration_issue(hass, public_id, "legacy_entity_id")
    prefix = f"{DOMAIN}_{entry.entry_id}_{entry.data.get(CONF_ACCOUNT_ID, '')}_"
    for entity in attached:
        changes: dict[str, object] = {}
        if rename_all:
            target = f"{entity.domain}.entergy_{public_id}_{entity.id}"
            if entity.entity_id != target:
                changes["new_entity_id"] = entities.async_get_available_entity_id(
                    entity.domain,
                    f"entergy_{public_id}_{entity.id}",
                    current_entity_id=entity.entity_id,
                )
        if entity.platform == DOMAIN:
            # Only known legacy keys are retained. Arbitrary suffixes could be PII.
            known = {
                "total_import_kwh",
                "total_export_kwh",
                "latest_hour_net_kwh",
                "latest_hour_import_kwh",
                "latest_hour_export_kwh",
                "latest_hour_cost",
                "latest_day_net_kwh",
                "latest_day_import_kwh",
                "latest_day_export_kwh",
                "latest_day_cost",
                "last_7_days_net_kwh",
                "last_7_days_cost",
                "month_to_date_net_kwh",
                "month_to_date_cost",
                "tracked_interval_count",
                "latest_hour_timestamp",
            }
            key = entity.unique_id.removeprefix(prefix)
            if entity.unique_id.startswith(f"{public_id}_"):
                key = entity.unique_id.removeprefix(f"{public_id}_")
            if key not in known:
                key = f"legacy_{entity.id}"
            if entity.unique_id != f"{public_id}_{key}":
                changes["new_unique_id"] = f"{public_id}_{key}"
            if key in {"total_import_kwh", "total_export_kwh"}:
                changes["disabled_by"] = er.RegistryEntryDisabler.INTEGRATION
            changes.update(name=None, original_name="Entergy", aliases=set())
        elif rename_all:
            changes.update(name=None, aliases=set())
        changes = {
            key: value
            for key, value in changes.items()
            if key.startswith("new_") or getattr(entity, key) != value
        }
        if changes:
            entities.async_update_entity(entity.entity_id, **changes)
    for device in owned_devices:
        devices.async_update_device(
            device.id,
            new_identifiers={(DOMAIN, public_id)},
            name="Entergy",
            name_by_user=None,
            serial_number=None,
        )
    _migration_issue(hass, public_id, "legacy_energy_source")


async def _migration_identity(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Keep a private durable identity checkpoint across delayed entry saves.

    Retained after success for rollback/recovery: HA has no public synchronous
    config-entry flush with which to order safe checkpoint removal.
    """
    key = f"{DOMAIN}.migration_{entry.entry_id}"

    def store(*, read_only: bool = False) -> Store[dict[str, str]]:
        return Store(hass, 1, key, private=True, atomic_writes=True, read_only=read_only)

    def validate(value: object) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
            raise LedgerRepairError
        return value

    if hass.state in (CoreState.stopping, CoreState.final_write):
        raise LedgerRepairError
    checkpoint = await store(read_only=True).async_load()
    entry_id = entry.data.get("public_id")
    if entry_id is not None:
        validate(entry_id)
    if checkpoint is not None:
        if not isinstance(checkpoint, dict) or set(checkpoint) != {"public_id"}:
            raise LedgerRepairError
        public_id = validate(checkpoint["public_id"])
        if entry_id is not None and entry_id != public_id:
            raise LedgerRepairError
    else:
        public_id = validate(entry_id) if entry_id is not None else uuid4().hex
        await store().async_save({"public_id": public_id})
    verified = await store(read_only=True).async_load()
    if verified != {"public_id": public_id} or hass.state in (
        CoreState.stopping,
        CoreState.final_write,
    ):
        raise LedgerRepairError
    return public_id


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Persist identity first, verified ledger next, and version last."""
    if entry.version > 2:
        return False
    if entry.version == 2:
        return True
    public_id = entry.entry_id  # Value-free repair identity until checkpoint validation.
    try:
        public_id = await _migration_identity(hass, entry)
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                "public_id": public_id,
                "time_zone": entry.data.get("time_zone") or hass.config.time_zone,
                "ledger_initialized": entry.data.get("ledger_initialized", False),
            },
        )
        statistic_ids(public_id)
        ledger = EntergyLedger(hass, public_id)
        state = await ledger.async_load(initialized=entry.data["ledger_initialized"])
        if state.revision == 0:
            candidate = await async_import_legacy_v1_store(
                hass, entry_id=entry.entry_id, public_id=public_id
            )
            result = await ledger.async_ingest(
                LedgerMutation(replace(candidate or state, revision=state.revision + 1))
            )
            if result.deferred or result.repair:
                raise LedgerRepairError
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "ledger_initialized": True}
        )
        _migrate_registries(hass, entry, public_id)
        interval = max(
            3600,
            min(
                86400,
                int(entry.options.get(CONF_SCAN_INTERVAL_SECONDS, DEFAULT_SCAN_INTERVAL_SECONDS)),
            ),
        )
        hass.config_entries.async_update_entry(
            entry,
            unique_id=public_id,
            title="Entergy",
            options={**entry.options, CONF_SCAN_INTERVAL_SECONDS: interval},
            version=2,
            minor_version=1,
        )
    except Exception:
        _migration_issue(hass, public_id, "ledger_repair")
        return False
    ir.async_delete_issue(hass, DOMAIN, f"ledger_repair_{public_id}")
    ir.async_delete_issue(hass, DOMAIN, f"ledger_repair_{entry.entry_id}")
    return True
