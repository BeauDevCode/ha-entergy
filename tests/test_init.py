"""Typed setup, cleanup, and repair lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from custom_components import entergy_mobile as integration
from custom_components.entergy_mobile.const import DOMAIN
from custom_components.entergy_mobile.errors import EntergyError, ErrorCategory, PayloadError
from custom_components.entergy_mobile.issues import RepairKind
from custom_components.entergy_mobile.ledger import LedgerRepairError, LedgerRepairKind
from custom_components.entergy_mobile.models import Freshness, LedgerState, UsageSnapshot
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component import common  # type: ignore[import-untyped]

PUBLIC_ID = "a" * 32


def snapshot() -> UsageSnapshot:
    """Return a small safe dashboard snapshot."""
    return UsageSnapshot(
        newest_interval_start=datetime(2026, 9, 28, 10, tzinfo=UTC),
        freshness=Freshness.FRESH,
        latest_import_kwh=Decimal("1"),
        latest_return_kwh=Decimal("0"),
        today_import_kwh=Decimal("2"),
        today_return_kwh=Decimal("0"),
        seven_day_import_kwh=Decimal("3"),
        seven_day_return_kwh=Decimal("0"),
        month_import_kwh=Decimal("4"),
        month_return_kwh=Decimal("0"),
        latest_cost=Decimal("0.1"),
        today_cost=Decimal("0.2"),
        seven_day_cost=Decimal("0.3"),
        month_cost=Decimal("0.4"),
        latest_compensation=Decimal("0"),
        today_compensation=Decimal("0"),
        seven_day_compensation=Decimal("0"),
        month_compensation=Decimal("0"),
    )


def entry(hass: HomeAssistant, *, initialized: bool = False) -> Any:
    """Add one synthetic modern config entry."""
    item = common.MockConfigEntry(
        domain=DOMAIN,
        unique_id=PUBLIC_ID,
        version=2,
        minor_version=1,
        data={
            "username": "private-user-canary",
            "password": "private-password-canary",
            "account_id": "private-account-canary",
            "public_id": PUBLIC_ID,
            "time_zone": "America/Chicago",
            "ledger_initialized": initialized,
        },
        options={"scan_interval_seconds": 14_400},
    )
    item.add_to_hass(hass)
    return item


def fakes(*, revision: int = 1) -> tuple[Mock, Mock, Mock]:
    """Return client, ledger, and coordinator doubles at public boundaries."""
    client = Mock()
    client.authenticated = True
    client.async_logout = AsyncMock()
    client.clear_token = Mock()
    ledger = Mock()
    ledger.state = LedgerState(schema_version=2, revision=revision)
    coordinator = Mock()
    coordinator.data = snapshot()
    coordinator.async_initialize = AsyncMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    coordinator.async_start_backfill = AsyncMock()
    coordinator.async_shutdown = AsyncMock()
    coordinator.async_add_listener = Mock(return_value=Mock())
    coordinator.diagnostics = Mock(
        return_value={
            "repair_conditions": [],
            "configured_poll_seconds": 14_400,
            "next_poll_seconds": 14_400,
            "last_successful_fetch": "2026-09-28T12:00:00+00:00",
            "newest_interval": "2026-09-28T10:00:00+00:00",
            "error_category": None,
            "retained_interval_count": 1,
            "estimated_interval_count": 0,
            "last_inserted_count": 1,
            "last_corrected_count": 0,
            "freshness": "fresh",
            "backfill_pages_completed": 0,
            "backfill_pages_total": 53,
            "backfill_progress_percent": 0,
            "backfill_complete": False,
        }
    )
    return client, ledger, coordinator


async def test_migration_guard_precedes_all_runtime_creation(hass: HomeAssistant) -> None:
    item = entry(hass)
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=False)),
        patch.object(integration, "EntergyLedger") as ledger,
        patch.object(integration, "EntergyApiClient") as client,
        patch.object(integration, "EntergyDataUpdateCoordinator") as coordinator,
        pytest.raises(ConfigEntryNotReady, match="ledger_repair"),
    ):
        await integration.async_setup_entry(hass, item)
    ledger.assert_not_called()
    client.assert_not_called()
    coordinator.assert_not_called()
    assert DOMAIN not in hass.data


async def test_setup_orders_local_load_refresh_runtime_platforms_issues_and_backfill(
    hass: HomeAssistant,
) -> None:
    item = entry(hass)
    client, ledger, coordinator = fakes()
    events: list[str] = []

    async def initialize() -> None:
        events.append("initialize")

    async def refresh() -> None:
        events.append("refresh")

    def add_listener(callback: Any) -> Mock:
        del callback
        events.append("listener")
        return Mock()

    async def start_backfill() -> None:
        events.append("backfill")

    coordinator.async_initialize.side_effect = initialize
    coordinator.async_config_entry_first_refresh.side_effect = refresh
    coordinator.async_add_listener.side_effect = add_listener
    coordinator.async_start_backfill.side_effect = start_backfill
    real_update = hass.config_entries.async_update_entry

    def update(config_entry: Any, **changes: Any) -> None:
        events.append("marker")
        real_update(config_entry, **changes)

    async def forward(config_entry: Any, platforms: Any) -> None:
        del config_entry, platforms
        events.append("forward")

    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_update_entry", side_effect=update),
        patch.object(hass.config_entries, "async_forward_entry_setups", side_effect=forward),
        patch.object(
            integration,
            "_reconcile_runtime_issues",
            side_effect=lambda *args: events.append("issues"),
        ),
    ):
        assert await integration.async_setup_entry(hass, item)

    assert events == [
        "initialize",
        "refresh",
        "marker",
        "forward",
        "issues",
        "listener",
        "backfill",
    ]
    assert item.data["ledger_initialized"] is True
    assert isinstance(item.runtime_data, integration.EntergyRuntimeData)
    assert item.runtime_data.client is client
    assert item.runtime_data.ledger is ledger
    assert item.runtime_data.coordinator is coordinator
    assert DOMAIN not in hass.data


@pytest.mark.parametrize("kind", [LedgerRepairKind.CORRUPT, LedgerRepairKind.FUTURE])
async def test_local_ledger_failure_creates_matching_persistent_issue_before_network(
    hass: HomeAssistant, kind: LedgerRepairKind
) -> None:
    item = entry(hass, initialized=True)
    client, ledger, coordinator = fakes()
    coordinator.async_initialize.side_effect = LedgerRepairError(kind)
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        pytest.raises(ConfigEntryNotReady, match=kind.value),
    ):
        await integration.async_setup_entry(hass, item)
    coordinator.async_config_entry_first_refresh.assert_not_awaited()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{kind.value}_{PUBLIC_ID}")
    assert issue is not None and issue.is_persistent
    coordinator.async_shutdown.assert_awaited_once()
    client.clear_token.assert_called_once()


async def test_revision_zero_never_marks_initialized_or_starts_backfill(
    hass: HomeAssistant,
) -> None:
    item = entry(hass)
    client, ledger, coordinator = fakes(revision=0)
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        pytest.raises(ConfigEntryNotReady, match="ledger_corrupt"),
    ):
        await integration.async_setup_entry(hass, item)
    assert item.data["ledger_initialized"] is False
    coordinator.async_start_backfill.assert_not_awaited()


async def test_initialization_marker_deferral_never_starts_backfill(
    hass: HomeAssistant,
) -> None:
    item = entry(hass)
    client, ledger, coordinator = fakes()
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_update_entry"),
        pytest.raises(ConfigEntryNotReady, match="ledger_corrupt"),
    ):
        await integration.async_setup_entry(hass, item)
    assert item.data["ledger_initialized"] is False
    coordinator.async_start_backfill.assert_not_awaited()


@pytest.mark.parametrize(
    ("condition", "kind"),
    [
        ("schema_drift", RepairKind.SCHEMA_DRIFT),
        ("data_retraction", RepairKind.DATA_RETRACTION),
    ],
)
async def test_initial_refresh_failure_surfaces_safe_actionable_issue(
    hass: HomeAssistant, condition: str, kind: RepairKind, caplog: pytest.LogCaptureFixture
) -> None:
    item = entry(hass, initialized=True)
    client, ledger, coordinator = fakes()
    coordinator.diagnostics.return_value["repair_conditions"] = [condition]
    coordinator.async_config_entry_first_refresh.side_effect = ConfigEntryNotReady(condition)
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        pytest.raises(ConfigEntryNotReady, match=condition),
    ):
        await integration.async_setup_entry(hass, item)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{kind.value}_{PUBLIC_ID}")
    assert issue is not None and issue.translation_placeholders is None
    assert "private-canary" not in caplog.text and "private-account" not in str(issue)


@pytest.mark.parametrize(
    ("error_type", "category"),
    [
        (ConfigEntryAuthFailed, "auth"),
        (ConfigEntryAuthFailed, "challenge"),
        (ConfigEntryNotReady, "transient"),
    ],
)
async def test_first_refresh_preserves_safe_setup_failure_mapping(
    hass: HomeAssistant,
    error_type: type[Exception],
    category: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    item = entry(hass, initialized=True)
    client, ledger, coordinator = fakes()
    coordinator.async_config_entry_first_refresh.side_effect = error_type(category)
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        pytest.raises(error_type, match=category),
    ):
        await integration.async_setup_entry(hass, item)
    coordinator.async_shutdown.assert_awaited_once()
    client.clear_token.assert_called_once()
    assert "private-user-canary" not in caplog.text


async def test_transient_initial_refresh_preserves_existing_repair_issue(
    hass: HomeAssistant,
) -> None:
    from custom_components.entergy_mobile.issues import create_issue

    item = entry(hass, initialized=True)
    client, ledger, coordinator = fakes()
    create_issue(hass, PUBLIC_ID, RepairKind.SCHEMA_DRIFT)
    coordinator.async_config_entry_first_refresh.side_effect = ConfigEntryNotReady("transient")
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        pytest.raises(ConfigEntryNotReady, match="transient"),
    ):
        await integration.async_setup_entry(hass, item)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"schema_drift_{PUBLIC_ID}") is not None


async def test_verified_local_load_resolves_only_local_ledger_repairs(
    hass: HomeAssistant,
) -> None:
    from custom_components.entergy_mobile.issues import create_issue

    item = entry(hass, initialized=True)
    client, ledger, coordinator = fakes()
    for kind in (
        RepairKind.LEDGER_CORRUPT,
        RepairKind.LEDGER_FUTURE,
        RepairKind.SCHEMA_DRIFT,
    ):
        create_issue(hass, PUBLIC_ID, kind)
    coordinator.async_config_entry_first_refresh.side_effect = ConfigEntryNotReady("transient")
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        pytest.raises(ConfigEntryNotReady, match="transient"),
    ):
        await integration.async_setup_entry(hass, item)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, f"ledger_corrupt_{PUBLIC_ID}") is None
    assert registry.async_get_issue(DOMAIN, f"ledger_future_{PUBLIC_ID}") is None
    assert registry.async_get_issue(DOMAIN, f"schema_drift_{PUBLIC_ID}") is not None


@pytest.mark.parametrize(
    ("condition", "kind"),
    [
        ("schema_drift", RepairKind.SCHEMA_DRIFT),
        ("data_retraction", RepairKind.DATA_RETRACTION),
    ],
)
async def test_unverified_first_payload_keeps_specific_repair_classification(
    hass: HomeAssistant, condition: str, kind: RepairKind
) -> None:
    item = entry(hass)
    client, ledger, coordinator = fakes(revision=0)
    coordinator.diagnostics.return_value["repair_conditions"] = [condition]
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        pytest.raises(ConfigEntryNotReady, match=condition),
    ):
        await integration.async_setup_entry(hass, item)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, f"{kind.value}_{PUBLIC_ID}") is not None
    assert registry.async_get_issue(DOMAIN, f"ledger_corrupt_{PUBLIC_ID}") is None


@pytest.mark.parametrize(
    "kind",
    [
        RepairKind.SCHEMA_DRIFT,
        RepairKind.CURRENCY_MISMATCH,
        RepairKind.DATA_RETRACTION,
    ],
)
async def test_real_coordinator_transient_startup_preserves_prior_repairs(
    hass: HomeAssistant, kind: RepairKind
) -> None:
    from custom_components.entergy_mobile.issues import create_issue

    item = entry(hass, initialized=True)
    client, ledger, _ = fakes()
    ledger.async_load = AsyncMock(return_value=ledger.state)
    client.async_get_account = AsyncMock()
    client.async_get_weekly_usage = AsyncMock(side_effect=EntergyError(ErrorCategory.TRANSIENT))
    create_issue(hass, PUBLIC_ID, kind)
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
    ):
        assert not await hass.config_entries.async_setup(item.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{kind.value}_{PUBLIC_ID}") is not None


async def test_real_coordinator_new_schema_drift_is_not_ledger_corruption(
    hass: HomeAssistant,
) -> None:
    item = entry(hass)
    client, ledger, _ = fakes(revision=0)
    ledger.async_load = AsyncMock(return_value=ledger.state)
    client.async_get_account = AsyncMock()
    client.async_get_weekly_usage = AsyncMock(side_effect=PayloadError())
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
    ):
        assert not await hass.config_entries.async_setup(item.entry_id)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, f"schema_drift_{PUBLIC_ID}") is not None
    assert registry.async_get_issue(DOMAIN, f"ledger_corrupt_{PUBLIC_ID}") is None
    assert item.data["ledger_initialized"] is False


async def test_real_coordinator_unverified_schema_keeps_prior_utility_repairs(
    hass: HomeAssistant,
) -> None:
    from custom_components.entergy_mobile.issues import create_issue

    item = entry(hass, initialized=True)
    client, ledger, _ = fakes()
    ledger.async_load = AsyncMock(return_value=ledger.state)
    client.async_get_account = AsyncMock()
    client.async_get_weekly_usage = AsyncMock(side_effect=PayloadError())
    for kind in (RepairKind.CURRENCY_MISMATCH, RepairKind.DATA_RETRACTION):
        create_issue(hass, PUBLIC_ID, kind)
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
    ):
        assert await hass.config_entries.async_setup(item.entry_id)
        registry = ir.async_get(hass)
        for kind in (
            RepairKind.SCHEMA_DRIFT,
            RepairKind.CURRENCY_MISMATCH,
            RepairKind.DATA_RETRACTION,
        ):
            assert registry.async_get_issue(DOMAIN, f"{kind.value}_{PUBLIC_ID}") is not None
        assert item.runtime_data.coordinator.diagnostics()["last_successful_fetch"] is None
        assert await hass.config_entries.async_unload(item.entry_id)


async def test_runtime_issue_listener_reconciles_only_observed_conditions(
    hass: HomeAssistant,
) -> None:
    item = entry(hass, initialized=True)
    client, ledger, coordinator = fakes()
    conditions = ["currency_mismatch", "backfill_stalled"]
    coordinator.diagnostics.return_value["repair_conditions"] = conditions
    callback: Any = None

    def add_listener(value: Any) -> Mock:
        nonlocal callback
        callback = value
        return Mock()

    coordinator.async_add_listener.side_effect = add_listener
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
    ):
        assert await integration.async_setup_entry(hass, item)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, f"currency_mismatch_{PUBLIC_ID}")
    assert registry.async_get_issue(DOMAIN, f"backfill_stalled_{PUBLIC_ID}")
    integration._migration_issue(hass, PUBLIC_ID, "legacy_entity_id")
    coordinator.diagnostics.return_value["repair_conditions"] = []
    callback()
    assert registry.async_get_issue(DOMAIN, f"currency_mismatch_{PUBLIC_ID}") is None
    assert registry.async_get_issue(DOMAIN, f"backfill_stalled_{PUBLIC_ID}") is None
    assert registry.async_get_issue(DOMAIN, f"legacy_entity_id_{PUBLIC_ID}")


async def test_runtime_issue_deletion_waits_for_verified_fetch(
    hass: HomeAssistant,
) -> None:
    from custom_components.entergy_mobile.issues import create_issue

    item = entry(hass, initialized=True)
    client, ledger, coordinator = fakes()
    coordinator.diagnostics.return_value["last_successful_fetch"] = None
    coordinator.diagnostics.return_value["repair_conditions"] = ["schema_drift"]
    for kind in (RepairKind.CURRENCY_MISMATCH, RepairKind.DATA_RETRACTION):
        create_issue(hass, PUBLIC_ID, kind)
    callback: Any = None

    def add_listener(value: Any) -> Mock:
        nonlocal callback
        callback = value
        return Mock()

    coordinator.async_add_listener.side_effect = add_listener
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
    ):
        assert await integration.async_setup_entry(hass, item)
    registry = ir.async_get(hass)
    for kind in (
        RepairKind.SCHEMA_DRIFT,
        RepairKind.CURRENCY_MISMATCH,
        RepairKind.DATA_RETRACTION,
    ):
        assert registry.async_get_issue(DOMAIN, f"{kind.value}_{PUBLIC_ID}") is not None

    coordinator.diagnostics.return_value["last_successful_fetch"] = "2026-09-28T12:00:00+00:00"
    coordinator.diagnostics.return_value["repair_conditions"] = []
    callback()
    for kind in (
        RepairKind.SCHEMA_DRIFT,
        RepairKind.CURRENCY_MISMATCH,
        RepairKind.DATA_RETRACTION,
    ):
        assert registry.async_get_issue(DOMAIN, f"{kind.value}_{PUBLIC_ID}") is None


async def test_successful_unload_stops_work_before_bounded_logout_and_clear(
    hass: HomeAssistant,
) -> None:
    item = entry(hass, initialized=True)
    client, ledger, coordinator = fakes()
    item.runtime_data = integration.EntergyRuntimeData(client, ledger, coordinator)
    events: list[str] = []
    coordinator.async_shutdown.side_effect = lambda: events.append("shutdown")
    client.async_logout.side_effect = lambda budget: events.append(f"logout:{budget.limit}")
    client.clear_token.side_effect = lambda: events.append("clear")
    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)):
        assert await integration.async_unload_entry(hass, item)
    assert events == ["shutdown", "logout:12", "clear"]


async def test_logout_failure_is_best_effort_and_still_clears_token(
    hass: HomeAssistant,
) -> None:
    item = entry(hass, initialized=True)
    client, ledger, coordinator = fakes()
    client.async_logout.side_effect = RuntimeError("private-canary")
    item.runtime_data = integration.EntergyRuntimeData(client, ledger, coordinator)
    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)):
        assert await integration.async_unload_entry(hass, item)
    coordinator.async_shutdown.assert_awaited_once()
    client.async_logout.assert_awaited_once()
    client.clear_token.assert_called_once()


@pytest.mark.parametrize("boundary", ["shutdown", "logout"])
async def test_unload_cancellation_preserves_worker_before_auth_cleanup_order(
    hass: HomeAssistant, boundary: str
) -> None:
    item = entry(hass, initialized=True)
    client, ledger, coordinator = fakes()
    item.runtime_data = integration.EntergyRuntimeData(client, ledger, coordinator)
    started = asyncio.Event()
    release = asyncio.Event()
    events: list[str] = []

    async def shutdown() -> None:
        events.append("shutdown-start")
        if boundary == "shutdown":
            started.set()
            await release.wait()
        events.append("shutdown-done")

    async def logout(budget: Any) -> None:
        del budget
        events.append("logout-start")
        if boundary == "logout":
            started.set()
            await release.wait()
        events.append("logout-done")

    coordinator.async_shutdown.side_effect = shutdown
    client.async_logout.side_effect = logout
    client.clear_token.side_effect = lambda: events.append("clear")
    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)):
        unload = asyncio.create_task(integration.async_unload_entry(hass, item))
        await started.wait()
        unload.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await unload
    assert events == [
        "shutdown-start",
        "shutdown-done",
        "logout-start",
        "logout-done",
        "clear",
    ]


async def test_failed_platform_unload_leaves_runtime_untouched(hass: HomeAssistant) -> None:
    item = entry(hass, initialized=True)
    client, ledger, coordinator = fakes()
    runtime = integration.EntergyRuntimeData(client, ledger, coordinator)
    item.runtime_data = runtime
    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)):
        assert not await integration.async_unload_entry(hass, item)
    coordinator.async_shutdown.assert_not_awaited()
    client.async_logout.assert_not_awaited()
    client.clear_token.assert_not_called()
    assert item.runtime_data is runtime


def test_repair_api_rejects_identity_and_placeholder_data(hass: HomeAssistant) -> None:
    from custom_components.entergy_mobile.issues import create_issue

    with pytest.raises(ValueError):
        create_issue(hass, "private-account", RepairKind.SCHEMA_DRIFT)
    with pytest.raises(ValueError):
        create_issue(hass, PUBLIC_ID, RepairKind.SCHEMA_DRIFT, {"value": "private-canary"})


async def test_core_managed_setup_holds_setup_state_through_refresh_and_forward(
    hass: HomeAssistant,
) -> None:
    item = entry(hass)
    client, ledger, coordinator = fakes()

    async def refresh() -> None:
        assert item.state is ConfigEntryState.SETUP_IN_PROGRESS

    async def forward(config_entry: Any, platforms: Any) -> None:
        del config_entry, platforms
        assert item.state is ConfigEntryState.SETUP_IN_PROGRESS

    coordinator.async_config_entry_first_refresh.side_effect = refresh
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_forward_entry_setups", side_effect=forward),
    ):
        assert await hass.config_entries.async_setup(item.entry_id)
    assert item.state is ConfigEntryState.LOADED
    assert hasattr(item, "runtime_data")


async def test_core_managed_post_assignment_failure_removes_runtime_and_stops_work(
    hass: HomeAssistant,
) -> None:
    item = entry(hass)
    client, ledger, coordinator = fakes()
    coordinator.async_start_backfill.side_effect = RuntimeError("private-canary")
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
        ) as unload,
    ):
        assert not await hass.config_entries.async_setup(item.entry_id)
    assert item.state is ConfigEntryState.SETUP_ERROR
    assert not hasattr(item, "runtime_data")
    unload.assert_awaited_once()
    coordinator.async_shutdown.assert_awaited_once()
    client.clear_token.assert_called_once()
    coordinator.async_start_backfill.assert_awaited_once()


async def test_cancellation_during_failed_setup_cleanup_finishes_private_cleanup(
    hass: HomeAssistant,
) -> None:
    item = entry(hass)
    client, ledger, coordinator = fakes()
    coordinator.async_start_backfill.side_effect = RuntimeError("private-canary")
    shutdown_started = asyncio.Event()
    shutdown_release = asyncio.Event()
    shutdown_worker: asyncio.Task[None] | None = None

    async def shutdown() -> None:
        nonlocal shutdown_worker

        async def finish_shutdown() -> None:
            shutdown_started.set()
            await shutdown_release.wait()

        if shutdown_worker is None:
            shutdown_worker = asyncio.create_task(finish_shutdown())
        await asyncio.shield(shutdown_worker)

    coordinator.async_shutdown.side_effect = shutdown
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)),
    ):
        setup = asyncio.create_task(integration.async_setup_entry(hass, item))
        await shutdown_started.wait()
        setup.cancel()
        shutdown_release.set()
        with pytest.raises(asyncio.CancelledError):
            await setup
    assert shutdown_worker is not None and shutdown_worker.done()
    client.clear_token.assert_called_once()
    assert not hasattr(item, "runtime_data")


async def test_cancellation_during_failed_setup_logout_still_deletes_runtime(
    hass: HomeAssistant,
) -> None:
    item = entry(hass)
    client, ledger, coordinator = fakes()
    coordinator.async_start_backfill.side_effect = RuntimeError("private-canary")
    logout_started = asyncio.Event()
    logout_release = asyncio.Event()

    async def logout(budget: Any) -> None:
        del budget
        logout_started.set()
        await logout_release.wait()

    client.async_logout.side_effect = logout
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)),
    ):
        setup = asyncio.create_task(integration.async_setup_entry(hass, item))
        await logout_started.wait()
        setup.cancel()
        logout_release.set()
        with pytest.raises(asyncio.CancelledError):
            await setup
    client.clear_token.assert_called_once()
    assert not hasattr(item, "runtime_data")


async def test_core_managed_platform_forward_failure_removes_runtime(
    hass: HomeAssistant,
) -> None:
    item = entry(hass)
    client, ledger, coordinator = fakes()
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(side_effect=RuntimeError("private-canary")),
        ),
        patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
        ) as unload,
    ):
        assert not await hass.config_entries.async_setup(item.entry_id)
    assert item.state is ConfigEntryState.SETUP_ERROR
    assert not hasattr(item, "runtime_data")
    unload.assert_awaited_once()
    coordinator.async_shutdown.assert_awaited_once()
    coordinator.async_start_backfill.assert_not_awaited()
    client.clear_token.assert_called_once()


async def test_core_managed_failed_unload_retains_runtime(
    hass: HomeAssistant,
) -> None:
    item = entry(hass)
    client, ledger, coordinator = fakes()
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
    ):
        assert await hass.config_entries.async_setup(item.entry_id)
    runtime = item.runtime_data
    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)):
        assert not await hass.config_entries.async_unload(item.entry_id)
    assert item.state is ConfigEntryState.FAILED_UNLOAD
    assert item.runtime_data is runtime
    coordinator.async_shutdown.assert_not_awaited()
    client.async_logout.assert_not_awaited()


async def test_core_managed_successful_unload_deletes_runtime(hass: HomeAssistant) -> None:
    item = entry(hass)
    client, ledger, coordinator = fakes()
    with (
        patch.object(integration, "async_recover_migration", AsyncMock(return_value=True)),
        patch.object(integration, "EntergyLedger", return_value=ledger),
        patch.object(integration, "EntergyApiClient", return_value=client),
        patch.object(integration, "EntergyDataUpdateCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
    ):
        assert await hass.config_entries.async_setup(item.entry_id)
    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)):
        assert await hass.config_entries.async_unload(item.entry_id)
    assert item.state is ConfigEntryState.NOT_LOADED
    assert not hasattr(item, "runtime_data")
    coordinator.async_shutdown.assert_awaited_once()
    client.async_logout.assert_awaited_once()
    client.clear_token.assert_called_once()


async def test_runtime_repair_is_active_after_issue_registry_reload(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    from custom_components.entergy_mobile.issues import create_issue

    create_issue(hass, PUBLIC_ID, RepairKind.SCHEMA_DRIFT)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    assert ir.STORAGE_KEY in hass_storage
    restored = ir.IssueRegistry(hass)
    await restored.async_load()
    issue = restored.async_get_issue(DOMAIN, f"schema_drift_{PUBLIC_ID}")
    assert issue is not None and issue.active and issue.is_persistent
    assert issue.severity == ir.IssueSeverity.ERROR
    assert issue.translation_key == "schema_drift"
    assert issue.translation_placeholders is None and issue.data is None
