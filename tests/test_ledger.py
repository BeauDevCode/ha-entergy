"""Durable ledger behavior against Home Assistant's public Store API."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from custom_components.entergy_mobile.ledger import (
    EntergyLedger,
    LedgerRepairError,
    async_import_legacy_v1_store,
)
from custom_components.entergy_mobile.models import EnergyInterval, LedgerMutation, LedgerTotals
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError

PUBLIC_ID = "a" * 32
KEY = f"entergy_mobile.ledger_{PUBLIC_ID}"
LEGACY_KEY = "entergy_mobile_usage_test-entry"
HOUR = datetime(2026, 9, 1, 10, tzinfo=UTC)
FINGERPRINT = "b" * 64


def interval() -> EnergyInterval:
    """An exact decimal interval with optional provenance."""
    return EnergyInterval(
        HOUR,
        HOUR + timedelta(hours=1),
        Decimal("1.234567890123456789"),
        Decimal("0"),
        Decimal("-0.0123456789"),
        "USD",
        True,
        HOUR + timedelta(days=1),
        "source-v2",
    )


async def loaded(hass: HomeAssistant) -> EntergyLedger:
    ledger = EntergyLedger(hass, PUBLIC_ID)
    await ledger.async_load(initialized=False)
    return ledger


def mutation(ledger: EntergyLedger) -> LedgerMutation:
    return LedgerMutation(
        replace(ledger.state, revision=ledger.state.revision + 1, intervals=(interval(),)),
        inserted=1,
        earliest_statistics_hour=HOUR,
    )


async def test_store_round_trip(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    ledger = await loaded(hass)
    candidate = replace(
        mutation(ledger).state,
        baseline=LedgerTotals(Decimal("20.75"), Decimal("2.5"), Decimal("3.10"), Decimal("0.2")),
        backfill_cursor=HOUR - timedelta(days=7),
        backfill_complete=True,
        statistics_pending_from=HOUR,
        statistics_pending_fingerprint=FINGERPRINT,
        statistics_verified_through=HOUR - timedelta(hours=1),
        statistics_verified_fingerprint="c" * 64,
    )
    result = await ledger.async_ingest(LedgerMutation(candidate, inserted=1))
    assert result.state == candidate == ledger.state
    assert result.inserted == 1 and not result.deferred
    persisted = hass_storage[KEY]
    assert persisted["version"] == 2
    data = persisted["data"]
    assert data["schema_version"] == 2 and data["revision"] == 1
    assert data["baseline"]["cost"] == "3.10"
    assert data["intervals"][HOUR.isoformat()]["import_kwh"] == "1.234567890123456789"
    assert data["intervals"][HOUR.isoformat()]["end"] == "2026-09-01T11:00:00+00:00"
    assert len(data["fingerprint"]) == 64
    restarted = EntergyLedger(hass, PUBLIC_ID)
    assert await restarted.async_load(initialized=True) == candidate
    assert restarted.state == candidate


async def test_store_options(hass: HomeAssistant) -> None:
    with patch.object(Store, "__init__", autospec=True, side_effect=Store.__init__) as factory:
        await loaded(hass)
    assert any(
        call.args[1:] == (hass, 2, KEY)
        and call.kwargs.get("private") is True
        and call.kwargs.get("atomic_writes") is True
        for call in factory.call_args_list
    )


async def test_new_store_is_empty_until_verified_first_save(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    ledger = await loaded(hass)
    assert ledger.state.schema_version == 2
    assert ledger.state.revision == 0 and ledger.state.intervals == ()
    assert KEY not in hass_storage
    assert not (await ledger.async_ingest(mutation(ledger))).deferred
    assert KEY in hass_storage


@pytest.mark.parametrize("initialized", [True, False])
async def test_future_store_version_never_overwritten(
    hass: HomeAssistant, hass_storage: dict[str, Any], initialized: bool
) -> None:
    hass_storage[KEY] = {"version": 3, "data": {"private": "canary"}}
    before = deepcopy(hass_storage[KEY])
    ledger = EntergyLedger(hass, PUBLIC_ID)
    with pytest.raises(LedgerRepairError, match="ledger_repair") as error:
        await ledger.async_load(initialized=initialized)
    assert "canary" not in str(error.value)
    result = await ledger.async_ingest(mutation(ledger))
    assert result.deferred and result.repair and result.earliest_statistics_hour is None
    assert hass_storage[KEY] == before


async def test_missing_initialized_or_corrupt_store_blocks(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    ledger = EntergyLedger(hass, PUBLIC_ID)
    before = ledger.state
    # HA reports both a missing file and recovered corrupt JSON as None.
    with pytest.raises(LedgerRepairError):
        await ledger.async_load(initialized=True)
    assert ledger.state == before
    result = await ledger.async_ingest(mutation(ledger))
    assert result.deferred and result.repair and KEY not in hass_storage


@pytest.mark.parametrize("bad", [[], {}, {"schema_version": 3}, {"schema_version": True}])
async def test_corrupt_payload_blocks_even_when_uninitialized(
    hass: HomeAssistant, hass_storage: dict[str, Any], bad: object
) -> None:
    hass_storage[KEY] = {"version": 2, "data": bad}
    ledger = EntergyLedger(hass, PUBLIC_ID)
    with pytest.raises(LedgerRepairError):
        await ledger.async_load(initialized=False)
    assert hass_storage[KEY]["data"] == bad


@pytest.mark.parametrize("failure", ["swallowed", "old_revision", "none", "exception", "tampered"])
async def test_save_failure_never_commits_memory_or_statistics(
    hass: HomeAssistant, hass_storage: dict[str, Any], failure: str
) -> None:
    ledger = await loaded(hass)
    await ledger.async_ingest(mutation(ledger))
    before = ledger.state
    candidate = LedgerMutation(
        replace(before, revision=2, backfill_complete=True), earliest_statistics_hour=HOUR
    )
    old_data = deepcopy(hass_storage[KEY]["data"])
    kwargs: dict[str, Any]
    if failure == "swallowed":
        target, kwargs = (
            "homeassistant.helpers.storage.Store._async_write_data",
            {"side_effect": WriteError("synthetic write failure")},
        )
    elif failure == "exception":
        target, kwargs = (
            "homeassistant.helpers.storage.Store.async_save",
            {"side_effect": OSError("private canary")},
        )
    else:
        target = "homeassistant.helpers.storage.Store.async_load"
        if failure == "tampered":
            old_data["revision"] = 2
            old_data["backfill_complete"] = True
        kwargs = {"return_value": None if failure == "none" else old_data}
    with patch(target, **kwargs):
        result = await ledger.async_ingest(candidate)
    assert ledger.state is before
    assert result.state is before and result.deferred and result.repair
    assert result.inserted == result.corrected == 0
    assert result.earliest_statistics_hour is None


@pytest.mark.parametrize("state", [CoreState.stopping, CoreState.final_write])
@pytest.mark.parametrize("when", ["before", "save", "readback"])
async def test_shutdown_defers_save_without_memory_swap(
    hass: HomeAssistant, hass_storage: dict[str, Any], state: CoreState, when: str
) -> None:
    ledger = await loaded(hass)
    before = ledger.state
    real_save, real_load = Store.async_save, Store.async_load

    async def save(store: Store[dict[str, Any]], data: dict[str, Any]) -> None:
        hass.set_state(state)
        await real_save(store, data)

    async def read(store: Store[dict[str, Any]]) -> dict[str, Any] | None:
        data = await real_load(store)
        hass.set_state(state)
        return data

    if when == "before":
        hass.set_state(state)
    target = "async_save" if when == "save" else "async_load"
    replacement = save if when == "save" else read
    try:
        with patch.object(Store, target, replacement):
            result = await ledger.async_ingest(mutation(ledger))
        assert result.deferred and result.repair is None
        assert ledger.state is before and result.state is before
        assert result.earliest_statistics_hour is None
        if when == "before":
            assert KEY not in hass_storage
    finally:
        hass.set_state(CoreState.running)


async def test_statistics_markers_survive_restart_and_require_matching_fingerprint(
    hass: HomeAssistant,
) -> None:
    ledger = await loaded(hass)
    await ledger.async_ingest(mutation(ledger))
    result = await ledger.async_mark_statistics_pending(from_hour=HOUR, fingerprint=FINGERPRINT)
    assert not result.deferred and ledger.state.revision == 2
    await ledger.async_mark_statistics_pending(
        from_hour=HOUR + timedelta(hours=1), fingerprint=FINGERPRINT
    )
    assert ledger.state.statistics_pending_from == HOUR
    before = ledger.state
    stale = await ledger.async_mark_statistics_verified(through=HOUR, fingerprint="c" * 64)
    assert stale.deferred and ledger.state is before
    verified = await ledger.async_mark_statistics_verified(through=HOUR, fingerprint=FINGERPRINT)
    assert not verified.deferred
    restarted = EntergyLedger(hass, PUBLIC_ID)
    await restarted.async_load(initialized=True)
    assert restarted.state.statistics_pending_from is None
    assert restarted.state.statistics_pending_fingerprint is None
    assert restarted.state.statistics_verified_through == HOUR
    assert restarted.state.statistics_verified_fingerprint == FINGERPRINT


@pytest.mark.parametrize("method", ["pending", "verified"])
async def test_statistics_marker_save_failure_keeps_previous_state(
    hass: HomeAssistant, method: str
) -> None:
    ledger = await loaded(hass)
    await ledger.async_ingest(mutation(ledger))
    await ledger.async_mark_statistics_pending(from_hour=HOUR, fingerprint=FINGERPRINT)
    before = ledger.state
    with patch.object(Store, "async_save", return_value=None):
        if method == "pending":
            result = await ledger.async_mark_statistics_pending(
                from_hour=HOUR - timedelta(hours=1), fingerprint="c" * 64
            )
        else:
            result = await ledger.async_mark_statistics_verified(
                through=HOUR, fingerprint=FINGERPRINT
            )
    assert result.deferred and ledger.state is before


async def test_stale_candidate_does_not_overwrite_new_revision(hass: HomeAssistant) -> None:
    ledger = await loaded(hass)
    candidate = mutation(ledger)
    await ledger.async_ingest(candidate)
    stale = await ledger.async_ingest(
        replace(candidate, state=replace(candidate.state, backfill_complete=True))
    )
    assert stale.deferred and not ledger.state.backfill_complete


def legacy_data() -> dict[str, Any]:
    return {
        "total_import_kwh": 12.5,
        "total_export_kwh": 4.75,
        "intervals": {
            "2026-09-01T10:00:00Z": {
                "usage": 2.5,
                "import": 2.5,
                "export": 0,
                "cost": 0.25,
                "isEstimated": False,
            },
            "2026-09-01T11:00:00Z": {
                "usage": -0.75,
                "import": 0,
                "export": 0.75,
                "cost": None,
                "isEstimated": True,
            },
        },
    }


async def test_legacy_store_recomputes_signed_usage_and_preserves_rollback(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    hass_storage[LEGACY_KEY] = {"version": 1, "data": legacy_data()}
    before = deepcopy(hass_storage)
    state = await async_import_legacy_v1_store(hass, entry_id="test-entry", public_id=PUBLIC_ID)
    assert state is not None and state.schema_version == 2
    assert state.baseline == LedgerTotals(Decimal("10"), Decimal("4"))
    assert state.intervals[0].import_kwh == Decimal("2.5")
    assert state.intervals[1].return_kwh == Decimal("0.75")
    assert state.intervals[0].amount == Decimal("0.25")
    assert state.intervals[1].amount is None
    assert state.statistics_pending_from == HOUR
    assert hass_storage == before
    ledger = await loaded(hass)
    assert not (await ledger.async_ingest(LedgerMutation(replace(state, revision=1)))).deferred
    assert hass_storage[LEGACY_KEY] == before[LEGACY_KEY]


async def test_legacy_missing_store_returns_none(hass: HomeAssistant) -> None:
    assert (
        await async_import_legacy_v1_store(hass, entry_id="test-entry", public_id=PUBLIC_ID) is None
    )


@pytest.mark.parametrize("bad", [True, "NaN", "Infinity", {}, None])
async def test_legacy_store_rejects_unsafe_signed_usage(
    hass: HomeAssistant, hass_storage: dict[str, Any], bad: object
) -> None:
    data = legacy_data()
    data["intervals"]["2026-09-01T10:00:00Z"]["usage"] = bad
    hass_storage[LEGACY_KEY] = {"version": 1, "data": data}
    before = deepcopy(hass_storage)
    with pytest.raises(LedgerRepairError):
        await async_import_legacy_v1_store(hass, entry_id="test-entry", public_id=PUBLIC_ID)
    assert hass_storage == before


@pytest.mark.parametrize(
    "problem", ["negative_baseline", "naive", "duplicate", "future_version", "boolean_estimated"]
)
async def test_legacy_store_rejects_ambiguous_history(
    hass: HomeAssistant, hass_storage: dict[str, Any], problem: str
) -> None:
    data = legacy_data()
    if problem == "negative_baseline":
        data["total_import_kwh"] = 1
    if problem == "naive":
        data["intervals"]["2026-09-01T10:00:00"] = data["intervals"].pop("2026-09-01T10:00:00Z")
    if problem == "duplicate":
        data["intervals"][HOUR.isoformat()] = data["intervals"]["2026-09-01T10:00:00Z"]
    if problem == "boolean_estimated":
        data["intervals"]["2026-09-01T10:00:00Z"]["isEstimated"] = "false"
    hass_storage[LEGACY_KEY] = {"version": 3 if problem == "future_version" else 1, "data": data}
    before = deepcopy(hass_storage)
    with pytest.raises(LedgerRepairError):
        await async_import_legacy_v1_store(hass, entry_id="test-entry", public_id=PUBLIC_ID)
    assert hass_storage == before


async def test_legacy_store_normalizes_aware_offset_timestamp(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    data = legacy_data()
    data["intervals"]["2026-09-01T05:00:00-05:00"] = data["intervals"].pop("2026-09-01T10:00:00Z")
    hass_storage[LEGACY_KEY] = {"version": 1, "data": data}
    state = await async_import_legacy_v1_store(hass, entry_id="test-entry", public_id=PUBLIC_ID)
    assert state is not None and state.intervals[0].start == HOUR


async def test_legacy_store_rejects_unbounded_decimal_precision(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    data = legacy_data()
    data["intervals"]["2026-09-01T10:00:00Z"]["usage"] = "1e-5000"
    hass_storage[LEGACY_KEY] = {"version": 1, "data": data}
    with pytest.raises(LedgerRepairError):
        await async_import_legacy_v1_store(hass, entry_id="test-entry", public_id=PUBLIC_ID)


async def test_concurrent_mutations_do_not_lose_a_verified_revision(hass: HomeAssistant) -> None:
    import asyncio

    ledger = await loaded(hass)
    candidate = mutation(ledger)
    results = await asyncio.gather(
        ledger.async_ingest(candidate),
        ledger.async_ingest(
            replace(candidate, state=replace(candidate.state, backfill_complete=True))
        ),
    )
    assert [result.deferred for result in results] == [False, True]
    assert ledger.state.revision == 1 and not ledger.state.backfill_complete


async def test_unreadable_store_never_exposes_error_payload(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    ledger = EntergyLedger(hass, PUBLIC_ID)
    with (
        patch.object(Store, "async_load", side_effect=OSError("private canary")),
        pytest.raises(LedgerRepairError) as error,
    ):
        await ledger.async_load(initialized=False)
    assert str(error.value) == "ledger_repair" and error.value.__suppress_context__
    assert (await ledger.async_ingest(mutation(ledger))).deferred
    assert KEY not in hass_storage


async def test_queue_failure_leaves_durable_pending_marker(hass: HomeAssistant) -> None:
    ledger = await loaded(hass)
    candidate = replace(
        mutation(ledger).state,
        statistics_pending_from=HOUR,
        statistics_pending_fingerprint=FINGERPRINT,
    )
    assert not (await ledger.async_ingest(LedgerMutation(candidate))).deferred
    # A caller's later queue failure cannot clear the already persisted work.
    restarted = EntergyLedger(hass, PUBLIC_ID)
    await restarted.async_load(initialized=True)
    assert restarted.state.statistics_pending_from == HOUR
    assert restarted.state.statistics_pending_fingerprint == FINGERPRINT


@pytest.mark.parametrize("problem", ["schema", "duplicate", "naive_cursor", "pending_minute"])
async def test_invalid_candidate_is_rejected_before_store_save(
    hass: HomeAssistant, hass_storage: dict[str, Any], problem: str
) -> None:
    ledger = await loaded(hass)
    candidate = mutation(ledger)
    if problem == "schema":
        state = replace(candidate.state, schema_version=3)
    elif problem == "duplicate":
        state = replace(candidate.state, intervals=(interval(), interval()))
    elif problem == "naive_cursor":
        state = replace(candidate.state, backfill_cursor=HOUR.replace(tzinfo=None))
    else:
        state = replace(candidate.state, statistics_pending_from=HOUR + timedelta(minutes=1))
    before = ledger.state
    result = await ledger.async_ingest(replace(candidate, state=state))
    assert result.deferred and result.repair and ledger.state is before
    assert KEY not in hass_storage


async def test_corrupt_json_store_recovery_none_blocks_initialized_ledger(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Exercise HA's corruption-to-None behavior beneath the public load method."""
    from json import JSONDecodeError

    from homeassistant.exceptions import HomeAssistantError

    error = HomeAssistantError("synthetic corrupt JSON")
    error.__cause__ = JSONDecodeError("synthetic syntax error", "{", 1)
    ledger = EntergyLedger(hass, PUBLIC_ID)
    before = ledger.state
    with (
        # Bypass only the fixture's in-memory loader; retain Store.async_load.
        patch.object(Store, "_async_load", Store._async_load_data),
        patch("homeassistant.helpers.storage.json_util.load_json", side_effect=error),
        patch("homeassistant.helpers.storage.os.rename"),
        patch("homeassistant.helpers.issue_registry.async_create_issue"),
        pytest.raises(LedgerRepairError),
    ):
        await ledger.async_load(initialized=True)
    result = await ledger.async_ingest(mutation(ledger))
    assert ledger.state is before and result.deferred and result.repair
    assert KEY not in hass_storage


@pytest.mark.parametrize(
    ("updates", "historical", "old_import", "old_return", "new_import", "new_return"),
    [
        ([5.0, 2.0], False, "5", "0", "2", "0"),
        ([5.0, -2.0], False, "5", "2", "0", "2"),
        ([-5.0, 2.0], False, "2", "5", "2", "0"),
        ([5.0, 2.0], True, "15", "4", "2", "0"),
        ([5.0000004, 2.0000004], False, "5", "0", "2.0000004", "0"),
    ],
)
async def test_legacy_corrections_do_not_become_invented_baselines(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    updates: list[float],
    historical: bool,
    old_import: str,
    old_return: str,
    new_import: str,
    new_return: str,
) -> None:
    """Generate persisted correction fixtures through the actual old writer."""
    from custom_components.entergy_mobile.coordinator import EntergyUsageStore, HourlyUsage

    old = EntergyUsageStore(hass, "test-entry")
    await old.async_load()
    with patch("custom_components.entergy_mobile.coordinator._utcnow", return_value=HOUR):
        if historical:
            await old.async_process(
                [
                    HourlyUsage((HOUR - timedelta(days=372)).isoformat(), 10.0, None, False),
                    HourlyUsage((HOUR - timedelta(days=371)).isoformat(), -4.0, None, False),
                ]
            )
        for usage in updates:
            await old.async_process([HourlyUsage(HOUR.isoformat(), usage, None, False)])
    before = deepcopy(hass_storage)
    data = before[LEGACY_KEY]["data"]
    assert Decimal(str(data["total_import_kwh"])) == Decimal(old_import)
    assert Decimal(str(data["total_export_kwh"])) == Decimal(old_return)
    assert len(data["intervals"]) == 1
    state = await async_import_legacy_v1_store(hass, entry_id="test-entry", public_id=PUBLIC_ID)
    assert state is not None
    assert state.baseline == (
        LedgerTotals(Decimal("10"), Decimal("4")) if historical else LedgerTotals()
    )
    assert state.intervals[0].import_kwh == Decimal(new_import)
    assert state.intervals[0].return_kwh == Decimal(new_return)
    assert state.intervals[0].amount is None
    assert hass_storage == before


@pytest.mark.parametrize("field", ["import", "export"])
@pytest.mark.parametrize(
    "bad", ["missing", None, True, -1, "NaN", "Infinity", {}, "0.0000001", 999]
)
async def test_legacy_rejects_missing_or_unsafe_cached_provenance(
    hass: HomeAssistant, hass_storage: dict[str, Any], field: str, bad: object
) -> None:
    data = legacy_data()
    record = data["intervals"]["2026-09-01T10:00:00Z"]
    if bad == "missing":
        record.pop(field)
    else:
        record[field] = bad
    hass_storage[LEGACY_KEY] = {"version": 1, "data": data}
    before = deepcopy(hass_storage)
    with pytest.raises(LedgerRepairError, match="ledger_repair"):
        await async_import_legacy_v1_store(hass, entry_id="test-entry", public_id=PUBLIC_ID)
    assert hass_storage == before


@pytest.mark.parametrize(
    "field,stamp,bad",
    [
        ("import", "2026-09-01T10:00:00Z", 2.0),
        ("export", "2026-09-01T11:00:00Z", 0.5),
    ],
)
async def test_legacy_rejects_cached_maximum_below_current_signed_usage(
    hass: HomeAssistant, hass_storage: dict[str, Any], field: str, stamp: str, bad: float
) -> None:
    data = legacy_data()
    data["intervals"][stamp][field] = bad
    hass_storage[LEGACY_KEY] = {"version": 1, "data": data}
    before = deepcopy(hass_storage)
    with pytest.raises(LedgerRepairError, match="ledger_repair"):
        await async_import_legacy_v1_store(hass, entry_id="test-entry", public_id=PUBLIC_ID)
    assert hass_storage == before
