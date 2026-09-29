"""The Entergy Usage integration lifecycle and migration."""

from __future__ import annotations

import asyncio
import os.path
import re
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, cast
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from .api import EntergyApiClient, RequestBudget
from .const import (
    CONF_ACCOUNT_ID,
    CONF_LANGUAGE,
    CONF_SCAN_INTERVAL_SECONDS,
    DEFAULT_LANGUAGE,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
)
from .coordinator import EntergyDataUpdateCoordinator
from .issues import RepairKind, create_issue, delete_issue
from .ledger import (
    EntergyLedger,
    LedgerRepairError,
    async_import_legacy_v1_store,
)
from .models import Credentials, LedgerMutation
from .statistics import statistic_ids

PLATFORMS: list[Platform] = [Platform.SENSOR]


@dataclass(slots=True)
class EntergyRuntimeData:
    """Typed runtime objects owned by one config entry."""

    client: EntergyApiClient
    ledger: EntergyLedger
    coordinator: EntergyDataUpdateCoordinator


type EntergyConfigEntry = ConfigEntry[EntergyRuntimeData]

_RUNTIME_REPAIRS = {
    RepairKind.LEDGER_CORRUPT,
    RepairKind.LEDGER_FUTURE,
    RepairKind.SCHEMA_DRIFT,
    RepairKind.CURRENCY_MISMATCH,
    RepairKind.DATA_RETRACTION,
    RepairKind.BACKFILL_STALLED,
}
_CONDITION_TO_REPAIR = {
    "ledger_repair": RepairKind.LEDGER_CORRUPT,
    **{kind.value: kind for kind in _RUNTIME_REPAIRS},
}


def _active_runtime_repairs(
    coordinator: EntergyDataUpdateCoordinator,
) -> tuple[set[RepairKind], bool]:
    """Read the coordinator's closed, value-free repair condition set."""
    diagnostics = coordinator.diagnostics()
    conditions = diagnostics.get("repair_conditions")
    active: set[RepairKind] = set()
    if isinstance(conditions, list):
        for token in conditions:
            if isinstance(token, str) and (kind := _CONDITION_TO_REPAIR.get(token)) is not None:
                active.add(kind)
    return active, isinstance(diagnostics.get("last_successful_fetch"), str)


def _create_active_runtime_issues(
    hass: HomeAssistant,
    public_id: str,
    coordinator: EntergyDataUpdateCoordinator,
) -> tuple[set[RepairKind], bool]:
    """Create observed issues without treating a failed poll as proof of repair."""
    active, has_verified_fetch = _active_runtime_repairs(coordinator)
    for kind in active:
        create_issue(hass, public_id, kind)
    return active, has_verified_fetch


def _reconcile_runtime_issues(
    hass: HomeAssistant,
    public_id: str,
    coordinator: EntergyDataUpdateCoordinator,
) -> None:
    """Mirror conditions only after the coordinator has verified usable state."""
    active, has_verified_fetch = _create_active_runtime_issues(hass, public_id, coordinator)
    if not has_verified_fetch:
        return
    for kind in _RUNTIME_REPAIRS:
        if kind not in active:
            delete_issue(hass, public_id, kind)


def _resolve_verified_ledger_issues(
    hass: HomeAssistant,
    public_id: str,
    ledger: EntergyLedger,
) -> None:
    """Clear local ledger repairs only after a verified persisted revision loads."""
    if ledger.state.revision <= 0:
        return
    delete_issue(hass, public_id, RepairKind.LEDGER_CORRUPT)
    delete_issue(hass, public_id, RepairKind.LEDGER_FUTURE)


async def _async_complete_cleanup(
    cleanup: Coroutine[Any, Any, None],
    *,
    name: str,
) -> None:
    """Complete ordered cleanup before re-propagating caller cancellation."""
    cleanup_task = asyncio.create_task(cleanup, name=name)
    cancellation: asyncio.CancelledError | None = None
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as error:
            if cleanup_task.cancelled():
                raise
            cancellation = error
    cleanup_task.result()
    if cancellation is not None:
        raise cancellation


async def _async_cleanup_failed_setup(
    hass: HomeAssistant,
    entry: EntergyConfigEntry,
    client: EntergyApiClient,
    coordinator: EntergyDataUpdateCoordinator | None,
) -> None:
    """Finish private cleanup before propagating caller cancellation."""

    async def cleanup() -> None:
        try:
            if hasattr(entry, "runtime_data"):
                with suppress(Exception):
                    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
            if coordinator is not None:
                with suppress(Exception):
                    await coordinator.async_shutdown()
            try:
                if client.authenticated:
                    await client.async_logout(RequestBudget())
            except Exception:
                pass
        finally:
            with suppress(Exception):
                client.clear_token()
            if hasattr(entry, "runtime_data"):
                with suppress(Exception):
                    object.__delattr__(entry, "runtime_data")

    await _async_complete_cleanup(cleanup(), name=f"{DOMAIN} failed setup cleanup")


async def async_setup_entry(hass: HomeAssistant, entry: EntergyConfigEntry) -> bool:
    """Set up one entry only after retained privacy migration work is safe."""
    if not await async_recover_migration(hass, entry):
        raise ConfigEntryNotReady("ledger_repair")

    public_id = str(entry.data["public_id"])
    ledger = EntergyLedger(hass, public_id)
    client = EntergyApiClient(
        async_get_clientsession(hass),
        Credentials(
            str(entry.data[CONF_USERNAME]),
            str(entry.data[CONF_PASSWORD]),
        ),
        language=str(entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)),
    )
    coordinator: EntergyDataUpdateCoordinator | None = None
    try:
        coordinator = EntergyDataUpdateCoordinator(
            hass,
            entry,
            client,
            ledger,
            time_zone=str(entry.data.get("time_zone") or hass.config.time_zone),
        )
        try:
            await coordinator.async_initialize()
        except LedgerRepairError as error:
            kind = RepairKind(error.kind.value)
            create_issue(hass, public_id, kind)
            raise ConfigEntryNotReady(kind.value) from None
        _resolve_verified_ledger_issues(hass, public_id, ledger)

        try:
            await coordinator.async_config_entry_first_refresh()
        except BaseException:
            _create_active_runtime_issues(hass, public_id, coordinator)
            raise

        if ledger.state.revision <= 0:
            active, _ = _create_active_runtime_issues(hass, public_id, coordinator)
            for kind in (
                RepairKind.DATA_RETRACTION,
                RepairKind.SCHEMA_DRIFT,
                RepairKind.LEDGER_FUTURE,
                RepairKind.LEDGER_CORRUPT,
            ):
                if kind in active:
                    raise ConfigEntryNotReady(kind.value)
            create_issue(hass, public_id, RepairKind.LEDGER_CORRUPT)
            raise ConfigEntryNotReady(RepairKind.LEDGER_CORRUPT.value)
        _resolve_verified_ledger_issues(hass, public_id, ledger)
        if not bool(entry.data.get("ledger_initialized", False)):
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, "ledger_initialized": True},
            )
            if entry.data.get("ledger_initialized") is not True:
                create_issue(hass, public_id, RepairKind.LEDGER_CORRUPT)
                raise ConfigEntryNotReady(RepairKind.LEDGER_CORRUPT.value)

        entry.runtime_data = EntergyRuntimeData(client, ledger, coordinator)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        _reconcile_runtime_issues(hass, public_id, coordinator)
        entry.async_on_unload(
            coordinator.async_add_listener(
                lambda: _reconcile_runtime_issues(hass, public_id, coordinator)
            )
        )
        await coordinator.async_start_backfill()
    except BaseException:
        await _async_cleanup_failed_setup(hass, entry, client, coordinator)
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EntergyConfigEntry) -> bool:
    """Unload entities before stopping work and discarding authentication."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    runtime = entry.runtime_data

    async def cleanup() -> None:
        try:
            await runtime.coordinator.async_shutdown()
        finally:
            try:
                with suppress(Exception):
                    await runtime.client.async_logout(RequestBudget())
            finally:
                runtime.client.clear_token()

    await _async_complete_cleanup(cleanup(), name=f"{DOMAIN} unload cleanup")
    return True


def _migration_issue(hass: HomeAssistant, public_id: str, key: str) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{key}_{public_id}",
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=key,
    )


def _migrate_registries(
    hass: HomeAssistant, entry: ConfigEntry, public_id: str, *, recovery: bool = False
) -> None:
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
    owned_deleted = [
        item
        for item in entities.deleted_entities.values()
        if item.config_entry_id == entry.entry_id
    ]
    # Public alias updates only change aliases_v2. HA retains compat_aliases in
    # serialized live/deleted history and exposes no supported scrub operation.
    # Block before any registry mutation rather than finalize with retained PII.
    if any(unsafe(alias) for item in attached for alias in item.compat_aliases) or any(
        unsafe(alias) for item in owned_deleted for alias in item.compat_aliases
    ):
        raise LedgerRepairError
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
        if rename_all and (
            not recovery
            or not entity.unique_id.startswith(f"{public_id}_")
            or unsafe(entity.entity_id)
        ):
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
            intermediate_prefix = f"entergy_migration_{public_id}_"
            key = entity.unique_id.removeprefix(prefix)
            if entity.unique_id.startswith(intermediate_prefix):
                key = entity.unique_id.removeprefix(intermediate_prefix)
            if entity.unique_id.startswith(f"{public_id}_"):
                key = entity.unique_id.removeprefix(f"{public_id}_")
            already_public = entity.unique_id.startswith(f"{public_id}_")
            previous_private = entity.previous_unique_id is not None and unsafe(
                entity.previous_unique_id
            )
            # A startup guard must not rewrite future runtime sensor identities.
            migrate_identity = not recovery or not already_public or previous_private
            if migrate_identity:
                if key not in known:
                    key = f"legacy_{entity.id}"
                final_id = f"{public_id}_{key}"
                intermediate_id = f"{intermediate_prefix}{key}"
                if entity.unique_id != final_id or previous_private:
                    conflict = entities.async_get_entity_id(
                        entity.domain, entity.platform, final_id
                    )
                    if conflict is not None and conflict != entity.entity_id:
                        raise LedgerRepairError
                    # HA preserves current unique_id as previous_unique_id. Use
                    # two public transitions so both retained values are pseudonymous.
                    if entity.unique_id != intermediate_id:
                        entity = entities.async_update_entity(
                            entity.entity_id, new_unique_id=intermediate_id
                        )
                    changes["new_unique_id"] = final_id
            if key in {"total_import_kwh", "total_export_kwh"}:
                changes["disabled_by"] = er.RegistryEntryDisabler.INTEGRATION
            if migrate_identity:
                changes.update(name=None, original_name="Entergy", aliases=[])
                # The public get-or-create API also updates existing entries;
                # remove cached legacy naming inputs from serialized registry data.
                entity = entities.async_get_or_create(
                    entity.domain,
                    entity.platform,
                    entity.unique_id,
                    config_entry=entry,
                    suggested_object_id=None,
                    object_id_base=None,
                    supported_features=entity.supported_features,
                )
        elif rename_all:
            changes.update(name=None, aliases=[])
        changes = {
            key: value
            for key, value in changes.items()
            if key.startswith("new_") or getattr(entity, key) != value
        }
        if changes:
            entities.async_update_entity(entity.entity_id, **cast(Any, changes))
    for device in owned_devices:
        devices.async_update_device(
            device.id,
            new_identifiers={(DOMAIN, public_id)},
            name="Entergy",
            name_by_user=None,
            serial_number=None,
        )
    _migration_issue(hass, public_id, "legacy_energy_source")


def _checkpoint_or_corrupt_artifact_exists(path: str) -> bool:
    """Check only owned checkpoint names, including retained/broken symlinks.

    Store may move malformed JSON before a repair issue can reach disk. Its
    retained corrupt artifact must therefore prevent allocating a new identity
    even when both entry data and the repair notice were lost in that crash.
    """
    if os.path.lexists(path):
        return True
    directory, filename = os.path.split(path)
    try:
        with os.scandir(directory) as entries:
            return any(
                item.name.startswith(f"{filename}.corrupt.") and os.path.lexists(item.path)
                for item in entries
            )
    except FileNotFoundError:
        return False


async def _migration_identity(
    hass: HomeAssistant, entry: ConfigEntry, *, required: bool = True
) -> str | None:
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
    checkpoint_store = store(read_only=True)
    # Store can rename malformed JSON and return None, even in read-only mode.
    # Inspect existence first, never reading checkpoint content outside Store.
    existed = await hass.async_add_executor_job(
        _checkpoint_or_corrupt_artifact_exists, checkpoint_store.path
    )
    checkpoint = await checkpoint_store.async_load()
    corruption_id = f"checkpoint_corrupt_{entry.entry_id}"
    prior_corruption = ir.async_get(hass).async_get_issue(DOMAIN, corruption_id)
    if checkpoint is None and (existed or prior_corruption is not None):
        ir.async_create_issue(
            hass,
            DOMAIN,
            corruption_id,
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="ledger_repair",
        )
        raise LedgerRepairError
    if checkpoint is None and not required and "migration_rename_all" not in entry.data:
        return None
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
    ir.async_delete_issue(hass, DOMAIN, corruption_id)
    return public_id


async def async_recover_migration(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Recheck retained migration work before runtime on every startup.

    Config-entry versions can reach disk before delayed registry saves. Task 10
    must retain this guard before client, coordinator, and entity setup. Fresh
    v2 entries without a migration checkpoint/marker require no registry work.
    """
    public_id = entry.entry_id
    try:
        identity = await _migration_identity(hass, entry, required=False)
        if identity is None:
            return True
        public_id = identity
        if entry.data.get("public_id") != public_id or entry.unique_id != public_id:
            raise LedgerRepairError
        _migrate_registries(hass, entry, public_id, recovery=True)
    except Exception:
        _migration_issue(hass, public_id, "ledger_repair")
        return False
    ir.async_delete_issue(hass, DOMAIN, f"ledger_repair_{public_id}")
    ir.async_delete_issue(hass, DOMAIN, f"ledger_repair_{entry.entry_id}")
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Persist identity first, verified ledger next, and version last."""
    if entry.version > 2:
        return False
    if entry.version == 2:
        return True
    public_id = entry.entry_id  # Value-free repair identity until checkpoint validation.
    try:
        identity = await _migration_identity(hass, entry)
        assert identity is not None
        public_id = identity
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
