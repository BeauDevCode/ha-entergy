"""Prepare, enqueue, and independently verify correction-safe Recorder statistics."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
    valid_statistic_id,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DOMAIN
from .ledger import cumulative_totals
from .models import LedgerState

type StatisticsBatch = tuple[StatisticMetaData, Sequence[StatisticData]]


@dataclass(frozen=True, slots=True)
class StatisticIds:
    """Public, account-independent Recorder identities."""

    consumption: str
    return_: str
    cost: str
    compensation: str


@dataclass(frozen=True, slots=True)
class StatisticsQueueResult:
    """Work submitted to Recorder; none of these fields confirms a commit."""

    statistic_ids: tuple[str, ...]
    row_count: int
    through: datetime | None
    fingerprint: str


def statistic_ids(public_id: str) -> StatisticIds:
    """Derive IDs solely from the persisted public identity."""
    ids = tuple(
        f"{DOMAIN}:{public_id}_{suffix}"
        for suffix in (
            "consumption",
            "return",
            "cost",
            "compensation",
        )
    )
    if not all(valid_statistic_id(value) for value in ids):
        raise ValueError("invalid public statistics identity")
    return StatisticIds(*ids)


def _metadata(statistic_id: str, label: str, *, energy: bool) -> StatisticMetaData:
    return StatisticMetaData(
        statistic_id=statistic_id,
        source=DOMAIN,
        name=f"Entergy {label}",
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        unit_class=EnergyConverter.UNIT_CLASS if energy else None,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR if energy else None,
    )


def build_hourly_statistics(
    state: LedgerState,
    *,
    public_id: str,
    currency: str,
    start: datetime | None,
) -> tuple[tuple[StatisticMetaData, tuple[StatisticData, ...]], ...]:
    """Build a suffix, accumulating the full canonical ledger before selecting it.

    Both monetary series include zero hours whenever any amounts are available,
    so sign changes and downward corrections replace prior values. No currency
    conversion is implied: a mismatch anywhere in retained history suppresses
    money, including when that history precedes the requested suffix.
    """
    ids = statistic_ids(public_id)
    monetary = currency == "USD" and all(item.currency in (None, "USD") for item in state.intervals)
    amounts_present = any(item.amount is not None for item in state.intervals) or bool(
        state.baseline.cost or state.baseline.compensation
    )
    consumption: list[StatisticData] = []
    returned: list[StatisticData] = []
    cost: list[StatisticData] = []
    compensation: list[StatisticData] = []
    for item, (hour, totals) in zip(state.intervals, cumulative_totals(state), strict=True):
        if start is not None and hour < start:
            continue
        hour = hour.astimezone(UTC)
        consumption.append(
            StatisticData(start=hour, state=float(item.import_kwh), sum=float(totals.import_kwh))
        )
        returned.append(
            StatisticData(start=hour, state=float(item.return_kwh), sum=float(totals.return_kwh))
        )
        if monetary and amounts_present:
            amount = item.amount if item.amount is not None else Decimal(0)
            cost.append(
                StatisticData(
                    start=hour, state=float(max(amount, Decimal(0))), sum=float(totals.cost)
                )
            )
            compensation.append(
                StatisticData(
                    start=hour,
                    state=float(max(amount.copy_negate(), Decimal(0))),
                    sum=float(totals.compensation),
                )
            )
    batches = (
        (_metadata(ids.consumption, "consumption", energy=True), tuple(consumption)),
        (_metadata(ids.return_, "return", energy=True), tuple(returned)),
    )
    if not monetary:
        return batches
    return (
        *batches,
        (_metadata(ids.cost, "cost", energy=False), tuple(cost)),
        (_metadata(ids.compensation, "compensation", energy=False), tuple(compensation)),
    )


def statistics_fingerprint(batches: Sequence[StatisticsBatch]) -> str:
    """Hash exact queued values, independently of receipt time or batch ordering.

    Call before queueing and persist this value with the pending marker. Floats
    match Recorder's storage boundary; empty batches have no payload semantics.
    """
    rows = sorted(
        (
            meta["statistic_id"],
            row["start"].astimezone(UTC).isoformat(),
            float(row["state"]),
            float(row["sum"]),
        )
        for meta, batch in batches
        for row in batch
    )
    return sha256(json.dumps(rows, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


async def async_queue_external_statistics(
    hass: HomeAssistant,
    batches: Sequence[StatisticsBatch],
) -> StatisticsQueueResult:
    """Queue an idempotent suffix after its pending marker has been persisted.

    A return only confirms enqueueing. The caller must retain pending work until
    a later run verifies Recorder, and may safely repeat this operation.
    """
    fingerprint = statistics_fingerprint(batches)
    queued_ids = []
    hours: list[datetime] = []
    for meta, rows in batches:
        if not rows:
            continue
        async_add_external_statistics(hass, meta, rows)
        queued_ids.append(meta["statistic_id"])
        hours.extend(row["start"] for row in rows)
    return StatisticsQueueResult(
        tuple(queued_ids), len(hours), max(hours, default=None), fingerprint
    )


async def async_verify_queued_statistics(
    hass: HomeAssistant,
    *,
    ids: StatisticIds,
    expected_batches: Sequence[StatisticsBatch],
    through: datetime,
) -> bool:
    """Check every pending row through the persisted boundary for each series.

    Query in Recorder's executor without flushing its queue or implying that a
    queue return was durable. Missing boundaries or mismatched identities keep
    the work pending. Empty optional batches impose no verification requirement.
    """
    if (
        through.tzinfo is None
        or through.utcoffset() != timedelta(0)
        or (through.minute or through.second or through.microsecond)
    ):
        return False
    allowed = {ids.consumption, ids.return_, ids.cost, ids.compensation}
    expected: dict[str, list[StatisticData]] = {}
    for meta, rows in expected_batches:
        if not rows:
            continue
        statistic_id = meta["statistic_id"]
        boundary = [row for row in rows if row["start"] == through]
        if statistic_id not in allowed or statistic_id in expected or len(boundary) != 1:
            return False
        expected[statistic_id] = [row for row in rows if row["start"] <= through]
    if not expected:
        return False
    recorded = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        min(row["start"] for rows in expected.values() for row in rows),
        through + timedelta(hours=1),
        set(expected),
        "hour",
        None,
        {"state", "sum"},
    )
    for statistic_id, wanted in expected.items():
        actual = recorded.get(statistic_id, ())
        expected_by_start = {row["start"].timestamp(): row for row in wanted}
        actual_by_start = {row["start"]: row for row in actual}
        if (
            len(expected_by_start) != len(wanted)
            or len(actual_by_start) != len(actual)
            or expected_by_start.keys() != actual_by_start.keys()
        ):
            return False
        if any(
            actual_by_start[start].get(key) != row[key]
            for start, row in expected_by_start.items()
            for key in ("state", "sum")
        ):
            return False
    return True
