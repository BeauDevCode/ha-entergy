"""Durable ledger behavior against Home Assistant's public Store API."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
from itertools import permutations
from typing import Any
from unittest.mock import patch

import pytest
from custom_components.entergy_mobile import ledger as api
from custom_components.entergy_mobile.ledger import (
    EntergyLedger,
    LedgerRepairError,
    async_import_legacy_v1_store,
)
from custom_components.entergy_mobile.models import (
    EnergyInterval,
    LedgerMutation,
    LedgerState,
    LedgerTotals,
)
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


def hour_record(index: int, value: str = "1") -> EnergyInterval:
    start = HOUR + timedelta(hours=index)
    return replace(
        interval(),
        start=start,
        end=start + timedelta(hours=1),
        import_kwh=Decimal(value),
        amount=None,
        is_estimated=False,
    )


def test_reconcile_first_insert_duplicates_and_order_independence() -> None:
    state = LedgerState(schema_version=2)
    records = (hour_record(0), hour_record(2), hour_record(1))
    before = api._encode(state)
    candidates = []
    for order in permutations(records):
        result = api.reconcile(state, (*order, order[0]), received_at=HOUR)
        assert (result.inserted, result.corrected, result.estimated) == (3, 0, 0)
        assert result.earliest_statistics_hour == HOUR
        assert result.state.revision == 1
        candidates.append(result.state)
        repeat = api.reconcile(result.state, reversed(records), received_at=HOUR)
        assert repeat.state is result.state
        assert repeat.earliest_statistics_hour is None
        assert repeat.inserted == repeat.corrected == 0
    assert all(candidate == candidates[0] for candidate in candidates)
    assert api._encode(state) == before


@pytest.mark.parametrize("value", ["0.5", "4"])
def test_correction_rebuilds_every_later_cumulative_sum(value: str) -> None:
    records = tuple(hour_record(index) for index in range(4))
    state = LedgerState(
        schema_version=2, revision=7, intervals=records, baseline=LedgerTotals(Decimal("10"))
    )
    result = api.reconcile(state, [hour_record(1, value)], received_at=HOUR)
    assert result.state.revision == 8
    assert result.corrected == 1 and result.inserted == 0
    assert result.earliest_statistics_hour == HOUR + timedelta(hours=1)
    assert state.intervals == records
    expected = ["11", "11.5", "12.5", "13.5"] if value == "0.5" else ["11", "15", "16", "17"]
    assert [total.import_kwh for _, total in api.cumulative_totals(result.state)] == list(
        map(Decimal, expected)
    )


def test_reconcile_estimated_to_actual_and_receipt_only_noop() -> None:
    estimated = replace(hour_record(0), is_estimated=True)
    result = api.reconcile(LedgerState(schema_version=2), [estimated], received_at=HOUR)
    assert result.estimated == 1
    actual = replace(estimated, is_estimated=False)
    correction = api.reconcile(result.state, [actual], received_at=HOUR)
    assert correction.corrected == 1 and correction.estimated == 0
    newer = replace(actual, received_at=HOUR + timedelta(days=1))
    assert api.reconcile(correction.state, [newer], received_at=HOUR).state is correction.state


def test_reconcile_missing_intervals_and_marker_only_revision() -> None:
    state = LedgerState(schema_version=2, intervals=(hour_record(0), hour_record(1)))
    assert api.reconcile(state, [hour_record(1)], received_at=HOUR).state is state
    result = api.reconcile(
        state, [], received_at=HOUR, backfill_cursor=HOUR, backfill_complete=True
    )
    assert result.state.intervals == state.intervals
    assert result.state.revision == 1 and result.state.backfill_complete
    assert result.state.backfill_cursor == HOUR and result.earliest_statistics_hour is None
    assert (
        api.reconcile(
            result.state, [], received_at=HOUR, backfill_cursor=HOUR, backfill_complete=True
        ).state
        is result.state
    )
    cleared = api.reconcile(
        result.state, [], received_at=HOUR, backfill_cursor=None, backfill_complete=False
    )
    assert cleared.state.revision == 2 and cleared.state.backfill_cursor is None
    assert not cleared.state.backfill_complete


@pytest.mark.parametrize(
    ("overlaps", "changed", "value", "quarantined"),
    [
        (24, 19, "2", True),
        (23, 23, "2", False),
        (24, 18, "2", False),
        (24, 24, "1.25", False),
        (24, 24, "1.2500000000000000000000000001", True),
        (24, 24, "0.75", False),
        (24, 24, "0.7499999999999999999999999999", True),
    ],
)
def test_quarantine_requires_all_three_strict_thresholds(
    overlaps: int, changed: int, value: str, quarantined: bool
) -> None:
    state = LedgerState(
        schema_version=2, revision=8, intervals=tuple(hour_record(i) for i in range(overlaps))
    )
    before = api._encode(state)
    incoming = [hour_record(i, value if i < changed else "1") for i in range(overlaps)] + [
        hour_record(30)
    ]
    with localcontext() as ctx:
        ctx.prec = 2
        result = api.reconcile(state, incoming, received_at=HOUR, backfill_complete=True)
    assert api._encode(state) == before
    if quarantined:
        assert result.state is state and result.deferred and result.repair
        assert result.earliest_statistics_hour is None
        assert result.inserted == result.corrected == result.estimated == 0
        assert api._encode(result.state) == before
    else:
        assert not result.deferred and result.repair is None
        assert result.state.revision == 9 and result.state.backfill_complete


def test_quarantine_uses_aggregate_import_plus_return_and_accepts_estimate_updates() -> None:
    records = tuple(
        replace(hour_record(i, "0"), return_kwh=Decimal("2"), is_estimated=True) for i in range(24)
    )
    state = LedgerState(schema_version=2, intervals=records)
    actual = [replace(item, is_estimated=False) for item in records]
    assert api.reconcile(state, actual, received_at=HOUR).corrected == 24
    # Direction changes without aggregate energy change are not mass changes.
    switched = [replace(item, import_kwh=Decimal("2"), return_kwh=Decimal(0)) for item in records]
    assert not api.reconcile(state, switched, received_at=HOUR).deferred
    reduced = [replace(item, return_kwh=Decimal("1")) for item in records]
    assert api.reconcile(state, reduced, received_at=HOUR).state is state


def test_quarantine_conflicting_same_hour_is_atomic() -> None:
    state = LedgerState(schema_version=2)
    result = api.reconcile(
        state, [hour_record(0), hour_record(1), hour_record(0, "2")], received_at=HOUR
    )
    assert result.state is state and result.deferred and result.repair


def test_retention_moves_exact_four_channel_totals_and_keeps_boundary_hour() -> None:
    records = (
        replace(
            hour_record(-2, "1.12345678901234567890123456789"),
            amount=Decimal("0.12345678901234567890123456789"),
        ),
        replace(
            hour_record(-1, "0"),
            return_kwh=Decimal("2.12345678901234567890123456789"),
            amount=Decimal("-0.22345678901234567890123456789"),
        ),
        hour_record(0, "3"),
        hour_record(1, "4"),
    )
    state = LedgerState(
        schema_version=2,
        intervals=records,
        baseline=LedgerTotals(Decimal("10"), Decimal("20"), Decimal("30"), Decimal("40")),
    )
    receipt = HOUR + timedelta(days=400, minutes=30)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = True
        result = api.reconcile(state, [], received_at=receipt)
        totals = api.cumulative_totals(result.state)
        assert totals == api.cumulative_totals(state)[2:]
    assert result.state.intervals == records[2:]
    assert result.state.baseline == LedgerTotals(
        Decimal("11.12345678901234567890123456789"),
        Decimal("22.12345678901234567890123456789"),
        Decimal("30.12345678901234567890123456789"),
        Decimal("40.22345678901234567890123456789"),
    )
    assert totals[-1][1].import_kwh == Decimal("18.12345678901234567890123456789")
    assert result.earliest_statistics_hour == records[0].start
    assert result.state.revision == 1
    # Compacted hours cannot be added to the baseline for a second time.
    assert api.reconcile(result.state, records, received_at=receipt).state is result.state
    assert state.intervals == records and state.baseline.import_kwh == 10


def test_retention_applies_correction_before_compacting_known_hour() -> None:
    state = LedgerState(schema_version=2, intervals=(hour_record(-1, "5"), hour_record(0)))
    result = api.reconcile(state, [hour_record(-1, "2")], received_at=HOUR + timedelta(days=400))
    assert result.corrected == 1 and result.state.baseline.import_kwh == 2
    assert result.state.intervals == (hour_record(0),)
    assert result.state.revision == 1


@pytest.mark.parametrize(
    "receipt", [HOUR.replace(tzinfo=None), HOUR.astimezone(timezone(timedelta(hours=1)))]
)
def test_reconcile_rejects_non_utc_receipt(receipt: datetime) -> None:
    with pytest.raises(ValueError, match="invalid ledger timestamp"):
        api.reconcile(LedgerState(schema_version=2), [], received_at=receipt)


def test_correction_rebuilds_return_cost_and_compensation_in_both_directions() -> None:
    records = tuple(
        replace(hour_record(i), return_kwh=Decimal("3"), amount=Decimal("0.5")) for i in range(3)
    )
    state = LedgerState(schema_version=2, intervals=records)
    correction = replace(records[0], return_kwh=Decimal("1"), amount=Decimal("-0.2"))
    result = api.reconcile(state, [correction], received_at=HOUR)
    assert [total for _, total in api.cumulative_totals(result.state)] == [
        LedgerTotals(Decimal("1"), Decimal("1"), Decimal("0"), Decimal("0.2")),
        LedgerTotals(Decimal("2"), Decimal("4"), Decimal("0.5"), Decimal("0.2")),
        LedgerTotals(Decimal("3"), Decimal("7"), Decimal("1"), Decimal("0.2")),
    ]
    restored = api.reconcile(result.state, [records[0]], received_at=HOUR)
    assert api.cumulative_totals(restored.state) == api.cumulative_totals(state)


def test_quarantine_preserves_pending_markers_baselines_and_prunable_hours() -> None:
    records = tuple(hour_record(i) for i in range(24))
    state = LedgerState(
        schema_version=2,
        revision=5,
        intervals=records,
        baseline=LedgerTotals(Decimal("10")),
        statistics_pending_from=HOUR,
        statistics_pending_fingerprint=FINGERPRINT,
    )
    before = api._encode(state)
    totals = api.cumulative_totals(state)
    result = api.reconcile(
        state,
        [hour_record(i, "2") for i in range(24)],
        received_at=HOUR + timedelta(days=402),
        backfill_complete=True,
    )
    assert result.state is state and result.deferred
    assert api._encode(result.state) == before
    assert api.cumulative_totals(result.state) == totals


async def test_quarantine_explicit_retraction_preserves_verified_store(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    from custom_components.entergy_mobile.errors import PayloadError
    from custom_components.entergy_mobile.parser import parse_usage

    ledger = await loaded(hass)
    await ledger.async_ingest(mutation(ledger))
    before = deepcopy(hass_storage[KEY])
    state = ledger.state
    totals = api.cumulative_totals(state)
    payload = {
        "data": {
            "daily": {
                "electric": [
                    {
                        "hourly": [
                            {
                                "date": HOUR.isoformat(),
                                "usage": "0",
                                "isEstimated": False,
                                "retracted": True,
                            },
                        ]
                    }
                ]
            }
        }
    }
    with pytest.raises(PayloadError):
        incoming = parse_usage(payload, source_time_zone="America/Chicago", received_at=HOUR)
        await ledger.async_ingest(api.reconcile(state, incoming, received_at=HOUR))
    assert ledger.state is state and hass_storage[KEY] == before
    assert api.cumulative_totals(ledger.state) == totals


def test_reconcile_exact_totals_ignore_active_exponent_limits() -> None:
    state = LedgerState(
        schema_version=2,
        intervals=(hour_record(0, "12345.00000000012345"),),
        baseline=LedgerTotals(Decimal("0.00000000000001")),
    )
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.Emax = 2
        ctx.Emin = -2
        ctx.traps[Inexact] = True
        totals = api.cumulative_totals(state)
    assert totals[0][1].import_kwh == Decimal("12345.00000000012346")


@pytest.mark.parametrize(
    "currency,cost,compensation",
    [("USD", "10", "3"), (None, "10", "3"), ("EUR", "3", "1")],
)
def test_retention_baselines_include_only_usd_or_omitted_source_currency(
    currency: str | None,
    cost: str,
    compensation: str,
) -> None:
    state = LedgerState(
        intervals=(
            replace(hour_record(0), return_kwh=Decimal(2), amount=Decimal(7), currency=currency),
            replace(hour_record(1), return_kwh=Decimal(2), amount=Decimal(-2), currency=currency),
        ),
        baseline=LedgerTotals(Decimal(10), Decimal(5), Decimal(3), Decimal(1)),
    )
    pruned = api.reconcile(state, (), received_at=HOUR + timedelta(days=401)).state
    assert pruned.intervals == ()
    assert pruned.baseline == LedgerTotals(
        Decimal(12),
        Decimal(9),
        Decimal(cost),
        Decimal(compensation),
    )
