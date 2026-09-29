"""Idempotent privacy migration with verified ledger persistence."""

from copy import deepcopy
from typing import Any
from unittest.mock import patch

import pytest
from custom_components import entergy_mobile as integration
from custom_components.entergy_mobile.const import DOMAIN
from custom_components.entergy_mobile.ledger import EntergyLedger
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component import common  # type: ignore[import-untyped]


@pytest.mark.parametrize(
    "entity_id,renamed",
    [("sensor.private_user_12_34_latest", True), ("sensor.kitchen_usage", False)],
)
async def test_migrate_registries_and_idempotent_ledger(
    hass: HomeAssistant, hass_storage: dict[str, Any], entity_id: str, renamed: bool
) -> None:
    entry = common.MockConfigEntry(
        domain=DOMAIN,
        version=1,
        unique_id="private-user_12-34",
        title="Entergy 12-34",
        data={"username": "private-user", "password": "synthetic", "account_id": "12-34"},
        options={"scan_interval_seconds": 60},
    )
    entry.add_to_hass(hass)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "12-34")}, name="Entergy 12-34"
    )
    sensor = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{entry.entry_id}_12-34_latest_hour_net_kwh",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id=entity_id.split(".")[1],
    )
    total = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{entry.entry_id}_12-34_total_import_kwh",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="old_total",
    )
    assert hasattr(integration, "async_migrate_entry")
    assert await integration.async_migrate_entry(hass, entry)
    public = entry.data["public_id"]
    assert entry.unique_id == public and entry.title == "Entergy"
    assert entry.version == 2 and entry.minor_version == 1
    assert entry.data["ledger_initialized"] is True
    ledger = EntergyLedger(hass, public)
    assert (await ledger.async_load(initialized=True)).revision == 1
    updated_device = devices.async_get(device.id)
    assert updated_device is not None and updated_device.identifiers == {(DOMAIN, public)}
    changed = entities.async_get(sensor.id)
    assert changed is not None and changed.unique_id == f"{public}_latest_hour_net_kwh"
    assert (changed.entity_id != entity_id) == renamed
    retired = entities.async_get(total.id)
    assert retired is not None and retired.disabled_by == er.RegistryEntryDisabler.INTEGRATION
    if renamed:
        assert retired.entity_id != total.entity_id
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"legacy_entity_id_{public}")
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"legacy_energy_source_{public}")
    assert entry.options["scan_interval_seconds"] == 3600
    before = deepcopy(hass_storage)
    assert await integration.async_migrate_entry(hass, entry)
    assert hass_storage == before


async def test_migration_failure_persists_identity_then_reuses_it(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = common.MockConfigEntry(
        domain=DOMAIN, version=1, data={"username": "user", "account_id": "account"}
    )
    entry.add_to_hass(hass)
    assert hasattr(integration, "async_migrate_entry")
    with patch(
        "custom_components.entergy_mobile.EntergyLedger.async_load",
        side_effect=RuntimeError("private-canary"),
    ):
        assert not await integration.async_migrate_entry(hass, entry)
    public = entry.data["public_id"]
    assert entry.version == 1 and not entry.data.get("ledger_initialized")
    assert await integration.async_migrate_entry(hass, entry)
    assert entry.data["public_id"] == public
    assert f"entergy_mobile.ledger_{public}" in hass_storage


async def test_durable_checkpoint_recovers_id_after_entry_data_loss(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = common.MockConfigEntry(
        domain=DOMAIN, version=1, data={"username": "user", "account_id": "account"}
    )
    entry.add_to_hass(hass)
    original = dict(entry.data)
    with patch(
        "custom_components.entergy_mobile.EntergyLedger.async_load", side_effect=RuntimeError
    ):
        assert not await integration.async_migrate_entry(hass, entry)
    checkpoint_key = f"entergy_mobile.migration_{entry.entry_id}"
    assert checkpoint_key in hass_storage
    public = hass_storage[checkpoint_key]["data"]["public_id"]
    # Simulate the supported entry update not reaching disk before a crash.
    hass.config_entries.async_update_entry(entry, data=original)
    assert await integration.async_migrate_entry(hass, entry)
    assert entry.data["public_id"] == public
    assert hass_storage[checkpoint_key]["data"] == {"public_id": public}


@pytest.mark.parametrize(
    "checkpoint",
    [
        {"public_id": "b" * 32},
        {"public_id": "raw-account"},
        {},
        {"public_id": "a" * 32, "extra": "untrusted"},
    ],
)
async def test_checkpoint_conflicts_and_malformed_data_block_without_mutation(
    hass: HomeAssistant, hass_storage: dict[str, Any], checkpoint: dict[str, str]
) -> None:
    entry = common.MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={"username": "user", "account_id": "account", "public_id": "a" * 32},
    )
    entry.add_to_hass(hass)
    key = f"entergy_mobile.migration_{entry.entry_id}"
    hass_storage[key] = {"version": 1, "data": checkpoint}
    before = deepcopy(hass_storage)
    assert not await integration.async_migrate_entry(hass, entry)
    assert entry.version == 1 and not entry.data.get("ledger_initialized")
    assert hass_storage == before


async def test_interrupted_registry_rename_recovers_after_entry_checkpoint_loss(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = common.MockConfigEntry(
        domain=DOMAIN, version=1, data={"username": "private-user", "account_id": "12-34"}
    )
    entry.add_to_hass(hass)
    original = dict(entry.data)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "12-34")}
    )
    first = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{entry.entry_id}_12-34_latest_hour_net_kwh",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="12_34_usage",
    )
    second = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{entry.entry_id}_12-34_total_export_kwh",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="safe_total",
    )
    real_update = entities.async_update_entity

    def interrupted(entity_id: str, **changes: Any) -> er.RegistryEntry:
        if entity_id == second.entity_id:
            raise RuntimeError("synthetic-interruption")
        return real_update(entity_id, **changes)

    with patch.object(entities, "async_update_entity", side_effect=interrupted):
        assert not await integration.async_migrate_entry(hass, entry)
    public = entry.data["public_id"]
    migrated_first = entities.async_get(first.id)
    assert migrated_first is not None and migrated_first.entity_id != first.entity_id
    assert entry.version == 1
    ledger_before = deepcopy(hass_storage[f"entergy_mobile.ledger_{public}"])
    hass.config_entries.async_update_entry(entry, data=original)
    assert await integration.async_migrate_entry(hass, entry)
    retried_first = entities.async_get(first.id)
    assert retried_first is not None
    assert retried_first.entity_id == migrated_first.entity_id
    assert retried_first.unique_id == migrated_first.unique_id
    migrated_second = entities.async_get(second.id)
    assert migrated_second is not None and migrated_second.entity_id != second.entity_id
    assert migrated_second.disabled_by == er.RegistryEntryDisabler.INTEGRATION
    assert hass_storage[f"entergy_mobile.ledger_{public}"] == ledger_before
    assert entry.data["public_id"] == public


@pytest.mark.parametrize(
    "legacy",
    [
        None,
        {
            "total_import_kwh": 2,
            "total_export_kwh": 0,
            "intervals": {
                "2026-09-01T10:00:00Z": {"usage": 2, "import": 2, "export": 0, "isEstimated": False}
            },
        },
    ],
)
async def test_ledger_write_failure_leaves_version_and_initialized_unset(
    hass: HomeAssistant, hass_storage: dict[str, Any], legacy: dict[str, Any] | None
) -> None:
    from homeassistant.helpers.storage import Store

    entry = common.MockConfigEntry(
        domain=DOMAIN, version=1, data={"username": "user", "account_id": "account"}
    )
    entry.add_to_hass(hass)
    if legacy is not None:
        hass_storage[f"entergy_mobile_usage_{entry.entry_id}"] = {"version": 1, "data": legacy}
    real_save = Store.async_save

    async def fail_ledger(store: Store[Any], data: Any) -> None:
        if ".ledger_" in store.key:
            raise OSError("synthetic-failure")
        await real_save(store, data)

    with patch.object(Store, "async_save", new=fail_ledger):
        assert not await integration.async_migrate_entry(hass, entry)
    public = entry.data["public_id"]
    assert entry.version == 1 and entry.data["ledger_initialized"] is False
    assert await integration.async_migrate_entry(hass, entry)
    ledger = EntergyLedger(hass, public)
    state = await ledger.async_load(initialized=True)
    assert state.revision == 1
    assert len(state.intervals) == (1 if legacy else 0)
    if legacy:
        assert hass_storage[f"entergy_mobile_usage_{entry.entry_id}"]["data"] == legacy


@pytest.mark.parametrize("problem", ["write", "readback", "shutdown"])
async def test_durable_checkpoint_failure_prevents_ledger_and_registry_mutation(
    hass: HomeAssistant, hass_storage: dict[str, Any], problem: str
) -> None:
    from homeassistant.core import CoreState
    from homeassistant.helpers.storage import Store

    entry = common.MockConfigEntry(
        domain=DOMAIN, version=1, data={"username": "user", "account_id": "account"}
    )
    entry.add_to_hass(hass)
    real_save = Store.async_save

    async def fail_checkpoint(store: Store[Any], data: Any) -> None:
        if problem == "write":
            raise OSError("synthetic-failure")
        if problem == "readback":
            await real_save(store, {"public_id": "b" * 32})
        else:
            await real_save(store, data)
            hass.set_state(CoreState.stopping)

    with patch.object(Store, "async_save", new=fail_checkpoint):
        assert not await integration.async_migrate_entry(hass, entry)
    hass.set_state(CoreState.running)
    assert entry.version == 1 and not entry.data.get("ledger_initialized")
    assert "public_id" not in entry.data
    assert not any(".ledger_" in key for key in hass_storage)


async def test_checkpoint_repair_is_cleared_after_successful_retry(hass: HomeAssistant) -> None:
    entry = common.MockConfigEntry(
        domain=DOMAIN, version=1, data={"username": "user", "account_id": "account"}
    )
    entry.add_to_hass(hass)
    with patch("custom_components.entergy_mobile._migration_identity", side_effect=OSError):
        assert not await integration.async_migrate_entry(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"ledger_repair_{entry.entry_id}")
    assert await integration.async_migrate_entry(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"ledger_repair_{entry.entry_id}") is None


async def test_final_version_startup_recovers_registry_snapshot_before_runtime(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A persisted final config entry must not skip delayed registry cleanup."""
    from unittest.mock import AsyncMock

    from custom_components.entergy_mobile.errors import AuthError
    from homeassistant.exceptions import ConfigEntryAuthFailed

    entry = common.MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={"username": "private-user", "password": "synthetic", "account_id": "12-34"},
    )
    entry.add_to_hass(hass)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "12-34")}, name="Entergy 12-34"
    )
    legacy_unique = f"{DOMAIN}_{entry.entry_id}_12-34_total_import_kwh"
    total = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        legacy_unique,
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="12_34_total",
    )
    assert await integration.async_migrate_entry(hass, entry)
    public = entry.data["public_id"]
    ledger_before = deepcopy(hass_storage[f"entergy_mobile.ledger_{public}"])
    migrated = entities.async_get(total.id)
    assert migrated is not None
    # Model a restart where config entry saves won the race over registry saves.
    entities.async_update_entity(
        migrated.entity_id,
        new_entity_id=total.entity_id,
        new_unique_id=legacy_unique,
        disabled_by=None,
    )
    devices.async_update_device(
        device.id, new_identifiers={(DOMAIN, "12-34")}, name="Entergy 12-34"
    )
    assert entry.version == 2 and entry.minor_version == 1
    client = AsyncMock()
    client.async_initialize.side_effect = AuthError()
    with (
        patch("custom_components.entergy_mobile.EntergyApiClient", return_value=client),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await integration.async_setup_entry(hass, entry)
    recovered = entities.async_get(total.id)
    assert recovered is not None and recovered.entity_id != total.entity_id
    assert recovered.unique_id == f"{public}_total_import_kwh"
    assert recovered.disabled_by == er.RegistryEntryDisabler.INTEGRATION
    recovered_device = devices.async_get(device.id)
    assert recovered_device is not None and recovered_device.identifiers == {(DOMAIN, public)}
    assert hass_storage[f"entergy_mobile.ledger_{public}"] == ledger_before
    assert entry.data["public_id"] == entry.unique_id == public
    assert await integration.async_recover_migration(hass, entry)
    assert entities.async_get(total.id) == recovered


@pytest.mark.parametrize(
    "payload", ["{broken", "{}", '{"version":1,"minor_version":1,"data":null}']
)
async def test_existing_unreadable_checkpoint_never_mints_identity(
    hass: HomeAssistant, hass_storage: dict[str, Any], payload: str
) -> None:
    """Exercise real Store decode/None behavior without editing storage files."""
    import json

    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.storage import Store

    entry = common.MockConfigEntry(
        domain=DOMAIN, version=1, data={"username": "user", "account_id": "account"}
    )
    entry.add_to_hass(hass)
    key = f"{DOMAIN}.migration_{entry.entry_id}"
    checkpoint_path = Store(hass, 1, key).path
    original_load = Store.async_load

    async def real_checkpoint_load(store: Store[Any]) -> Any:
        if store.key == key:
            # Bypass only the test fixture's outer Store mock, preserving real decode behavior.
            return await store._async_load_data()  # type: ignore[no-untyped-call]
        return await original_load(store)

    def load_json(path: str) -> Any:
        assert path == checkpoint_path
        try:
            return json.loads(payload)
        except json.JSONDecodeError as err:
            raise HomeAssistantError("synthetic malformed JSON") from err

    with (
        patch.object(Store, "async_load", new=real_checkpoint_load),
        patch("homeassistant.helpers.storage.json_util.load_json", side_effect=load_json),
        patch("os.path.lexists", return_value=True),
        patch("homeassistant.helpers.storage.os.rename"),
        patch("custom_components.entergy_mobile.uuid4") as mint,
    ):
        assert not await integration.async_migrate_entry(hass, entry)
    assert not mint.called
    assert entry.version == 1 and "public_id" not in entry.data
    assert not any(key.startswith(f"{DOMAIN}.ledger_") for key in hass_storage)
    assert key not in hass_storage
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"ledger_repair_{entry.entry_id}")
    assert issue is not None and issue.is_persistent


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("suggested", ["safe_total", "12_34_total"])
async def test_unique_id_transition_removes_previous_account_identity(
    hass: HomeAssistant, interrupted: bool, suggested: str
) -> None:
    import json

    from homeassistant.helpers.json import json_bytes

    entry = common.MockConfigEntry(
        domain=DOMAIN, version=1, data={"username": "private-user", "account_id": "12-34"}
    )
    entry.add_to_hass(hass)
    entities = er.async_get(hass)
    total = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{entry.entry_id}_12-34_total_import_kwh",
        config_entry=entry,
        suggested_object_id=suggested,
    )
    original_update = entities.async_update_entity

    def interrupt_after_intermediate(entity_id: str, **changes: Any) -> er.RegistryEntry:
        current = entities.async_get(entity_id)
        if (
            current
            and current.unique_id.startswith("entergy_migration_")
            and "new_unique_id" in changes
        ):
            raise RuntimeError("synthetic-interruption")
        return original_update(entity_id, **changes)

    if interrupted:
        with patch.object(
            entities, "async_update_entity", side_effect=interrupt_after_intermediate
        ):
            assert not await integration.async_migrate_entry(hass, entry)
        partial = entities.async_get(total.id)
        assert partial is not None and partial.unique_id.startswith("entergy_migration_")
    assert await integration.async_migrate_entry(hass, entry)
    updated = entities.async_get(total.id)
    assert updated is not None
    assert updated.unique_id == f"{entry.data['public_id']}_total_import_kwh"
    assert updated.previous_unique_id is not None and "12-34" not in updated.previous_unique_id
    assert updated.disabled_by == er.RegistryEntryDisabler.INTEGRATION
    serialized = json.loads(json_bytes(updated.as_storage_fragment))
    assert "12-34" not in json.dumps(serialized)
    assert "12_34" not in json.dumps(serialized)
    assert "private-user" not in json.dumps(serialized)


async def test_migration_notices_remain_active_after_registry_reload(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE

    entry = common.MockConfigEntry(
        domain=DOMAIN, version=1, data={"username": "private-user", "account_id": "12-34"}
    )
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{entry.entry_id}_12-34_total_import_kwh",
        config_entry=entry,
        suggested_object_id="12_34_total",
    )
    assert await integration.async_migrate_entry(hass, entry)
    # Exercise HA's public shutdown event and reload the resulting registry.
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    assert ir.STORAGE_KEY in hass_storage
    restored = ir.IssueRegistry(hass)
    await restored.async_load()
    for key in ("legacy_entity_id", "legacy_energy_source"):
        issue = restored.async_get_issue(DOMAIN, f"{key}_{entry.data['public_id']}")
        assert issue is not None and issue.active and issue.is_persistent
        assert issue.translation_key == key
        assert issue.translation_placeholders is None


async def test_startup_guard_leaves_fresh_and_future_runtime_entities_unchanged(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    public = "a" * 32
    entry = common.MockConfigEntry(
        domain=DOMAIN,
        unique_id=public,
        version=2,
        minor_version=1,
        data={"username": "user", "account_id": "12-34", "public_id": public},
    )
    entry.add_to_hass(hass)
    entities = er.async_get(hass)
    sensor = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{public}_future_runtime_key",
        config_entry=entry,
        suggested_object_id="daily_energy",
        original_name="Daily energy",
    )
    assert await integration.async_recover_migration(hass, entry)
    assert entities.async_get(sensor.id) == sensor
    assert not hass_storage
    assert not ir.async_get(hass).issues
    # Recovered legacy entries may later gain new runtime sensors.
    hass_storage[f"{DOMAIN}.migration_{entry.entry_id}"] = {
        "version": 1,
        "data": {"public_id": public},
    }
    hass.config_entries.async_update_entry(entry, data={**entry.data, "migration_rename_all": True})
    assert await integration.async_recover_migration(hass, entry)
    assert entities.async_get(sensor.id) == sensor


async def test_startup_checkpoint_failure_blocks_before_client_creation(
    hass: HomeAssistant,
) -> None:
    from homeassistant.exceptions import ConfigEntryNotReady

    entry = common.MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        data={"public_id": "a" * 32, "migration_rename_all": True},
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.entergy_mobile._migration_identity", side_effect=OSError),
        patch("custom_components.entergy_mobile.EntergyApiClient") as client,
        pytest.raises(ConfigEntryNotReady, match="ledger_repair"),
    ):
        await integration.async_setup_entry(hass, entry)
    assert not client.called


async def test_corruption_notice_blocks_retry_after_store_moves_bad_file(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE

    entry = common.MockConfigEntry(
        domain=DOMAIN, version=1, data={"username": "user", "account_id": "12-34"}
    )
    entry.add_to_hass(hass)
    with patch("os.path.lexists", return_value=True):
        assert not await integration.async_migrate_entry(hass, entry)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    restored = ir.IssueRegistry(hass)
    await restored.async_load()
    with (
        patch("homeassistant.helpers.issue_registry.async_get", return_value=restored),
        patch("os.path.lexists", return_value=False),
        patch("custom_components.entergy_mobile.uuid4") as mint,
    ):
        assert not await integration.async_migrate_entry(hass, entry)
    assert not mint.called and "public_id" not in entry.data
    assert not any(key.startswith(f"{DOMAIN}.ledger_") for key in hass_storage)


@pytest.mark.parametrize("retained", ["corrupt_file", "corrupt_symlink", "checkpoint_symlink"])
async def test_restart_after_store_rename_blocks_without_saved_entry_or_issue(
    hass: HomeAssistant, hass_storage: dict[str, Any], retained: str
) -> None:
    """Only the existence of HA's retained artifact survives this crash model."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from homeassistant.helpers.storage import Store

    entry = common.MockConfigEntry(
        domain=DOMAIN, version=1, data={"username": "user", "account_id": "12-34"}
    )
    entry.add_to_hass(hass)
    key = f"{DOMAIN}.migration_{entry.entry_id}"
    checkpoint_path = Store(hass, 1, key).path
    artifact_path = f"{checkpoint_path}.corrupt.synthetic-time"
    retained_path = checkpoint_path if retained == "checkpoint_symlink" else artifact_path
    listing = MagicMock()
    listing.__enter__.return_value = iter(
        [SimpleNamespace(name=artifact_path.rsplit("/", 1)[-1], path=artifact_path)]
    )
    assert not ir.async_get(hass).issues
    assert not hass_storage and "public_id" not in entry.data
    # Broken symlinks report exists=False and lexists=True, just like os itself.
    with (
        patch(
            "os.path.exists",
            side_effect=lambda path: retained == "corrupt_file" and path == artifact_path,
        ),
        patch("os.path.lexists", side_effect=lambda path: path == retained_path),
        patch("os.scandir", return_value=listing),
        patch("custom_components.entergy_mobile.uuid4") as mint,
    ):
        assert not await integration.async_migrate_entry(hass, entry)
    assert not mint.called
    assert "public_id" not in entry.data and entry.version == 1
    assert key not in hass_storage
    assert not any(name.startswith(f"{DOMAIN}.ledger_") for name in hass_storage)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"ledger_repair_{entry.entry_id}")
    assert issue is not None and issue.is_persistent
