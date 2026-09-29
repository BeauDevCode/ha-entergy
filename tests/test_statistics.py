"""Correction-safe external statistics, including persisted crash recovery."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from custom_components.entergy_mobile.ledger import EntergyLedger, reconcile
from custom_components.entergy_mobile.models import EnergyInterval, LedgerState, LedgerTotals
from custom_components.entergy_mobile.statistics import (
    StatisticsQueueResult,
    async_queue_external_statistics,
    async_verify_queued_statistics,
    build_hourly_statistics,
    statistic_ids,
    statistics_fingerprint,
)
from homeassistant.components.recorder.core import Recorder
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    statistics_during_period,
    valid_statistic_id,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util.unit_conversion import EnergyConverter
from pytest_homeassistant_custom_component.components.recorder.common import (  # type: ignore[import-untyped]
    async_wait_recording_done,
)

PUBLIC_ID = "a" * 32
HOUR = datetime(2026, 9, 1, 10, tzinfo=UTC)
NEXT = HOUR + timedelta(hours=1)
IDS = statistic_ids(PUBLIC_ID)
type Batch = tuple[StatisticMetaData, tuple[StatisticData, ...]]


def interval(
    hour: datetime = HOUR,
    energy: str = "4",
    returned: str = "1",
    amount: str | None = "2",
    currency: str | None = "USD",
) -> EnergyInterval:
    return EnergyInterval(
        hour,
        hour + timedelta(hours=1),
        Decimal(energy),
        Decimal(returned),
        None if amount is None else Decimal(amount),
        currency,
        False,
        NEXT + timedelta(days=1),
    )


def build(
    state: LedgerState, start: datetime | None = None, currency: str = "USD"
) -> tuple[Batch, ...]:
    return build_hourly_statistics(state, public_id=PUBLIC_ID, currency=currency, start=start)


def test_ids_metadata_and_separate_hourly_and_cumulative_values() -> None:
    state = LedgerState(
        intervals=(interval(), interval(NEXT, "3", "2", "-0.5")),
        baseline=LedgerTotals(Decimal(10), Decimal(5), Decimal(7), Decimal(1)),
    )
    batches = build(state)
    expected = {
        IDS.consumption: [(4, 14), (3, 17)],
        IDS.return_: [(1, 6), (2, 8)],
        IDS.cost: [(2, 9), (0, 9)],
        IDS.compensation: [(0, 1), (0.5, 1.5)],
    }
    assert [meta["statistic_id"] for meta, _ in batches] == [
        f"entergy_mobile:{PUBLIC_ID}_{suffix}"
        for suffix in ("consumption", "return", "cost", "compensation")
    ]
    for meta, rows in batches:
        assert valid_statistic_id(meta["statistic_id"])
        assert meta["source"] == "entergy_mobile"
        assert meta["mean_type"] is StatisticMeanType.NONE
        assert meta["has_sum"] is True
        energy = meta["statistic_id"] in (IDS.consumption, IDS.return_)
        assert meta["unit_class"] == (EnergyConverter.UNIT_CLASS if energy else None)
        assert meta["unit_of_measurement"] == ("kWh" if energy else None)
        assert [(row["state"], row["sum"]) for row in rows] == expected[meta["statistic_id"]]
        assert [row["start"] for row in rows] == [HOUR, NEXT]
        assert all(row["start"].tzinfo is UTC for row in rows)
        assert all(
            not (row["start"].minute or row["start"].second or row["start"].microsecond)
            for row in rows
        )


def test_correction_suffix_keeps_baseline_and_all_preceding_hours() -> None:
    state = LedgerState(
        intervals=(interval(), interval(NEXT, "3", "2", "-0.5")),
        baseline=LedgerTotals(Decimal(10), Decimal(5), Decimal(7), Decimal(1)),
    )
    corrected = reconcile(state, (interval(energy="1", amount="0.25"),), received_at=NEXT)
    assert corrected.earliest_statistics_hour == HOUR
    rows = {meta["statistic_id"]: rows for meta, rows in build(corrected.state, NEXT)}
    assert rows[IDS.consumption] == ({"start": NEXT, "state": 3.0, "sum": 14.0},)
    assert rows[IDS.cost] == ({"start": NEXT, "state": 0.0, "sum": 7.25},)
    assert rows[IDS.return_][0]["sum"] == 8
    assert rows[IDS.compensation][0]["sum"] == 1.5


@pytest.mark.parametrize(
    "ha_currency,source_currency", [("EUR", "USD"), ("USD", "EUR"), ("USD", "usd")]
)
def test_invalid_currency_suppresses_money_but_preserves_energy(
    ha_currency: str,
    source_currency: str,
) -> None:
    # The incompatible currency before the suffix still contributes to its cumulative sum.
    state = LedgerState(intervals=(interval(currency=source_currency), interval(NEXT)))
    batches = build(state, NEXT, ha_currency)
    assert [meta["statistic_id"] for meta, _ in batches] == [IDS.consumption, IDS.return_]
    assert batches[0][1] == ({"start": NEXT, "state": 4.0, "sum": 8.0},)


def test_omitted_currency_and_negative_amount_produce_nonnegative_compensation() -> None:
    batches = dict(
        (meta["statistic_id"], rows)
        for meta, rows in build(LedgerState(intervals=(interval(amount="-2.5", currency=None),)))
    )
    assert batches[IDS.compensation] == ({"start": HOUR, "state": 2.5, "sum": 2.5},)
    assert batches[IDS.cost] == ({"start": HOUR, "state": 0.0, "sum": 0.0},)


def test_absent_amounts_leave_optional_batches_empty() -> None:
    batches = build(LedgerState(intervals=(interval(amount=None),)))
    assert len(batches) == 4
    assert batches[2][1] == batches[3][1] == ()
    assert all(not rows for _, rows in build(LedgerState()))


async def rows_in_recorder(hass: HomeAssistant) -> dict[str, Any]:
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        HOUR,
        NEXT + timedelta(hours=2),
        {IDS.consumption, IDS.return_, IDS.cost, IDS.compensation},
        "hour",
        None,
        {"state", "sum"},
    )


async def test_queue_corrections_update_each_timestamp_once(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    state = LedgerState(intervals=(interval(), interval(NEXT)))
    result = await async_queue_external_statistics(hass, build(state))
    assert isinstance(result, StatisticsQueueResult)
    assert result.statistic_ids == (IDS.consumption, IDS.return_, IDS.cost, IDS.compensation)
    assert result.row_count == 8 and result.through == NEXT
    assert result.fingerprint == statistics_fingerprint(build(state))
    await async_wait_recording_done(hass)
    corrected = replace(state, intervals=(interval(energy="1", amount="0.25"), interval(NEXT)))
    await async_queue_external_statistics(hass, build(corrected))
    await async_queue_external_statistics(hass, build(corrected))
    await async_wait_recording_done(hass)
    rows = await rows_in_recorder(hass)
    assert len(rows[IDS.consumption]) == 2
    assert [(r["state"], r["sum"]) for r in rows[IDS.consumption]] == [(1, 1), (4, 5)]
    assert [(r["state"], r["sum"]) for r in rows[IDS.cost]] == [(0.25, 0.25), (2, 2.25)]
    assert all(len(items) == 2 for items in rows.values())


@pytest.mark.parametrize("series", [0, 1, 2, 3])
@pytest.mark.parametrize("field", ["state", "sum"])
async def test_verification_checks_both_values_for_every_nonempty_series(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    series: int,
    field: str,
) -> None:
    batches = build(LedgerState(intervals=(interval(), interval(NEXT, amount="-1"))))
    await async_queue_external_statistics(hass, batches)
    await async_wait_recording_done(hass)
    assert await async_verify_queued_statistics(
        hass, ids=IDS, expected_batches=batches, through=NEXT
    )
    tampered = list(batches)
    meta, rows = tampered[series]
    changed = rows[-1].copy()
    if field == "state":
        changed["state"] += 0.25
    else:
        changed["sum"] += 0.25
    tampered[series] = (meta, (rows[0], changed))
    assert not await async_verify_queued_statistics(
        hass, ids=IDS, expected_batches=tampered, through=NEXT
    )


async def test_verification_requires_exact_boundary_and_every_expected_series(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    batches = build(LedgerState(intervals=(interval(), interval(NEXT))))
    # Only consumption is committed: presence of that series cannot verify return or money.
    await async_queue_external_statistics(hass, batches[:1])
    await async_wait_recording_done(hass)
    assert not await async_verify_queued_statistics(
        hass, ids=IDS, expected_batches=batches, through=NEXT
    )
    await async_queue_external_statistics(hass, batches)
    await async_wait_recording_done(hass)
    assert not await async_verify_queued_statistics(
        hass,
        ids=IDS,
        expected_batches=batches,
        through=NEXT + timedelta(hours=1),
    )
    # The requested persisted boundary may precede the latest row in Recorder.
    assert await async_verify_queued_statistics(
        hass, ids=IDS, expected_batches=batches, through=HOUR
    )
    missing_boundary = ((batches[0][0], (batches[0][1][0],)), *batches[1:])
    assert not await async_verify_queued_statistics(
        hass,
        ids=IDS,
        expected_batches=missing_boundary,
        through=NEXT,
    )


async def test_empty_optional_batches_do_not_block_verification(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    batches = build(LedgerState(intervals=(interval(amount=None),)))
    result = await async_queue_external_statistics(hass, batches)
    assert result.statistic_ids == (IDS.consumption, IDS.return_) and result.row_count == 2
    await async_wait_recording_done(hass)
    assert await async_verify_queued_statistics(
        hass, ids=IDS, expected_batches=batches, through=HOUR
    )
    assert not await async_verify_queued_statistics(
        hass, ids=IDS, expected_batches=(), through=HOUR
    )
    assert not await async_verify_queued_statistics(
        hass,
        ids=statistic_ids("b" * 32),
        expected_batches=batches,
        through=HOUR,
    )


@pytest.mark.parametrize("queue_lost", [False, True])
async def test_restart_verifies_or_requeues_persisted_downward_correction(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    queue_lost: bool,
) -> None:
    ledger = EntergyLedger(hass, PUBLIC_ID)
    await ledger.async_load(initialized=False)
    assert not (
        await ledger.async_ingest(
            reconcile(
                ledger.state,
                (interval(), interval(NEXT)),
                received_at=NEXT,
            )
        )
    ).deferred
    initial_batches = build(ledger.state)
    initial_fingerprint = statistics_fingerprint(initial_batches)
    await ledger.async_mark_statistics_pending(from_hour=HOUR, fingerprint=initial_fingerprint)
    await async_queue_external_statistics(hass, initial_batches)
    await async_wait_recording_done(hass)
    assert await async_verify_queued_statistics(
        hass, ids=IDS, expected_batches=initial_batches, through=NEXT
    )
    await ledger.async_mark_statistics_verified(through=NEXT, fingerprint=initial_fingerprint)
    correction = reconcile(
        ledger.state,
        (interval(energy="1", amount="0.25"),),
        received_at=NEXT,
    )
    assert not (await ledger.async_ingest(correction)).deferred
    assert correction.earliest_statistics_hour == HOUR
    corrected_batches = build(ledger.state, correction.earliest_statistics_hour)
    corrected_fingerprint = statistics_fingerprint(corrected_batches)
    assert not (
        await ledger.async_mark_statistics_pending(
            from_hour=HOUR, fingerprint=corrected_fingerprint
        )
    ).deferred
    persisted = hass_storage[f"entergy_mobile.ledger_{PUBLIC_ID}"]["data"]
    assert persisted["statistics_pending_from"] == HOUR.isoformat()
    corrected_batches = build(ledger.state, ledger.state.statistics_pending_from)
    # Drop only the public enqueue call to model a crash before Recorder durably writes.
    if queue_lost:
        with patch("custom_components.entergy_mobile.statistics.async_add_external_statistics"):
            queued = await async_queue_external_statistics(hass, corrected_batches)
    else:
        queued = await async_queue_external_statistics(hass, corrected_batches)
    assert queued.row_count == 8
    assert queued.fingerprint == corrected_fingerprint
    assert ledger.state.statistics_verified_fingerprint == initial_fingerprint
    assert ledger.state.statistics_pending_fingerprint == corrected_fingerprint
    del ledger, corrected_batches
    await async_wait_recording_done(hass)
    restarted = EntergyLedger(hass, PUBLIC_ID)
    restored = await restarted.async_load(initialized=True)
    expected = build(restored, restored.statistics_pending_from)
    assert statistics_fingerprint(expected) == restored.statistics_pending_fingerprint
    through = restored.intervals[-1].start
    assert through == restored.statistics_verified_through
    verified = await async_verify_queued_statistics(
        hass, ids=IDS, expected_batches=expected, through=through
    )
    assert verified is not queue_lost
    if not verified:
        await async_queue_external_statistics(hass, expected)
        await async_wait_recording_done(hass)
        assert await async_verify_queued_statistics(
            hass, ids=IDS, expected_batches=expected, through=through
        )
    assert not (
        await restarted.async_mark_statistics_verified(
            through=through, fingerprint=corrected_fingerprint
        )
    ).deferred
    assert restarted.state.statistics_pending_from is None
    assert restarted.state.statistics_verified_fingerprint == corrected_fingerprint
    rows = await rows_in_recorder(hass)
    assert [(r["state"], r["sum"]) for r in rows[IDS.consumption]] == [(1, 1), (4, 5)]
    assert [(r["state"], r["sum"]) for r in rows[IDS.cost]] == [(0.25, 0.25), (2, 2.25)]
    assert all(len(items) == 2 for items in rows.values())


def test_fingerprint_is_restart_and_batch_order_stable_but_tracks_payload_changes() -> None:
    batches = build(LedgerState(intervals=(interval(), interval(NEXT))))
    fingerprint = statistics_fingerprint(batches)
    assert len(fingerprint) == 64
    assert fingerprint == statistics_fingerprint(tuple(reversed(batches)))
    assert fingerprint == statistics_fingerprint(
        build(
            LedgerState(
                intervals=(
                    replace(interval(), received_at=NEXT + timedelta(days=2)),
                    interval(NEXT),
                )
            )
        )
    )
    for changed in (
        LedgerState(intervals=(interval(energy="1"), interval(NEXT))),
        LedgerState(intervals=(interval(), interval(NEXT)), baseline=LedgerTotals(Decimal(1))),
        LedgerState(intervals=(interval(NEXT),)),
    ):
        assert fingerprint != statistics_fingerprint(build(changed))
    other_ids = build_hourly_statistics(
        LedgerState(intervals=(interval(), interval(NEXT))),
        public_id="b" * 32,
        currency="USD",
        start=None,
    )
    assert fingerprint != statistics_fingerprint(other_ids)


async def test_empty_queue_has_no_commit_claim_or_boundary(hass: HomeAssistant) -> None:
    result = await async_queue_external_statistics(hass, ())
    assert result.row_count == 0 and result.statistic_ids == () and result.through is None
    assert result.fingerprint == statistics_fingerprint(())


@pytest.mark.parametrize(
    "currency,cost,compensation",
    [("USD", 8, 2), (None, 8, 2), ("EUR", 1, 0)],
)
def test_pruned_non_usd_amounts_never_reappear_as_usd_statistics(
    currency: str | None,
    cost: int,
    compensation: int,
) -> None:
    now = HOUR + timedelta(days=401)
    recent = now - timedelta(hours=1)
    state = LedgerState(
        intervals=(
            interval(amount="7", currency=currency),
            interval(NEXT, amount="-2", currency=currency),
            interval(recent, amount="1"),
        )
    )
    before = build(state)
    assert len(before) == (2 if currency == "EUR" else 4)
    pruned = reconcile(state, (), received_at=now).state
    assert len(pruned.intervals) == 1
    batches = {meta["statistic_id"]: rows for meta, rows in build(pruned)}
    assert batches[IDS.consumption] == ({"start": recent, "state": 4.0, "sum": 12.0},)
    assert batches[IDS.return_] == ({"start": recent, "state": 1.0, "sum": 3.0},)
    assert batches[IDS.cost] == ({"start": recent, "state": 1.0, "sum": cost},)
    assert batches[IDS.compensation] == ({"start": recent, "state": 0.0, "sum": compensation},)


@pytest.mark.parametrize("public_id", ["has space", "has/slash", "has:colon"])
def test_invalid_public_id_cannot_create_recorder_identity(public_id: str) -> None:
    with pytest.raises(ValueError, match="invalid public statistics identity"):
        statistic_ids(public_id)


async def test_empty_expected_statistics_cannot_verify_pending_work(hass: HomeAssistant) -> None:
    assert not await async_verify_queued_statistics(
        hass, ids=IDS, expected_batches=(), through=HOUR
    )


@pytest.mark.parametrize(
    "through",
    [
        HOUR.replace(tzinfo=None),
        HOUR + timedelta(minutes=1),
        HOUR.astimezone(timezone(timedelta(hours=1))),
    ],
)
async def test_invalid_verification_boundary_never_confirms_commit(
    hass: HomeAssistant, through: datetime
) -> None:
    assert not await async_verify_queued_statistics(
        hass, ids=IDS, expected_batches=build(LedgerState(intervals=(interval(),))), through=through
    )


async def test_offsetting_corrections_must_verify_entire_pending_suffix(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A matching last hour cannot prove that earlier corrections survived a crash."""
    third = NEXT + timedelta(hours=1)
    original = build(
        LedgerState(
            intervals=(
                interval(energy="10"),
                interval(NEXT, energy="20"),
                interval(third, energy="30"),
            )
        )
    )
    corrected = build(
        LedgerState(
            intervals=(
                interval(energy="11"),
                interval(NEXT, energy="19"),
                interval(third, energy="30"),
            )
        )
    )
    await async_queue_external_statistics(hass, original)
    await async_wait_recording_done(hass)
    assert original[0][1][-1] == corrected[0][1][-1]
    assert statistics_fingerprint(original) != statistics_fingerprint(corrected)
    assert not await async_verify_queued_statistics(
        hass, ids=IDS, expected_batches=corrected, through=third
    )
    await async_queue_external_statistics(hass, corrected)
    await async_wait_recording_done(hass)
    assert await async_verify_queued_statistics(
        hass, ids=IDS, expected_batches=corrected, through=third
    )
    actual = await rows_in_recorder(hass)
    assert [(row["state"], row["sum"]) for row in actual[IDS.consumption]] == [
        (11, 11),
        (19, 30),
        (30, 60),
    ]
