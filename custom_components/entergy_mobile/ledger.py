"""Private copy-on-write ledger persistence; no Recorder side effects."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum, StrEnum
from hashlib import sha256
from typing import Any

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import UnsupportedStorageVersionError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .models import EnergyInterval, LedgerMutation, LedgerState, LedgerTotals, validate_decimal
from .models import exact_sum as _exact_sum

_SCHEMA_VERSION = 2
_REPAIR = "ledger_repair"


class _Unchanged(Enum):
    VALUE = "unchanged"


UNCHANGED = _Unchanged.VALUE


def _add_interval(totals: LedgerTotals, item: EnergyInterval) -> LedgerTotals:
    # Baselines discard source provenance, so they must never absorb foreign
    # currency. Use the same eligible contributions for retained running totals.
    amount = (
        item.amount if item.amount is not None and item.currency in (None, "USD") else Decimal(0)
    )
    return LedgerTotals(
        _exact_sum((totals.import_kwh, item.import_kwh)),
        _exact_sum((totals.return_kwh, item.return_kwh)),
        _exact_sum((totals.cost, max(amount, Decimal(0)))),
        _exact_sum((totals.compensation, max(amount.copy_negate(), Decimal(0)))),
    )


def cumulative_totals(state: LedgerState) -> tuple[tuple[datetime, LedgerTotals], ...]:
    """Rebuild exact cumulative values from the baseline and canonical hours.

    Consumers may select the suffix starting at earliest_statistics_hour. A
    corrected hour contributes its current value, including downward revisions.
    """
    totals = state.baseline
    rows = []
    for item in state.intervals:
        totals = _add_interval(totals, item)
        rows.append((item.start, totals))
    return tuple(rows)


def reconcile(
    state: LedgerState,
    incoming: Iterable[EnergyInterval],
    *,
    received_at: datetime,
    backfill_cursor: datetime | None | _Unchanged = UNCHANGED,
    backfill_complete: bool | _Unchanged = UNCHANGED,
) -> LedgerMutation:
    """Prepare one immutable revision without Store or Recorder operations.

    Missing records never delete known hours. Conflicting duplicates and mass
    revisions reject the entire response, including pruning and marker changes.
    Hours already outside retention and absent from the ledger are ignored:
    their contributions may already be compacted into the opaque baseline.
    """
    _iso(received_at)
    cursor = state.backfill_cursor if backfill_cursor is UNCHANGED else backfill_cursor
    complete = state.backfill_complete if backfill_complete is UNCHANGED else backfill_complete
    assert not isinstance(cursor, _Unchanged) and not isinstance(complete, _Unchanged)
    _iso(cursor)
    _boolean(complete)
    known = {item.start: item for item in state.intervals}
    candidate: dict[datetime, EnergyInterval] = {}
    for item in incoming:
        prior = candidate.get(item.start)
        if prior is not None and prior.fingerprint != item.fingerprint:
            return LedgerMutation(state, deferred=True, repair="usage_quarantine")
        candidate[item.start] = item

    overlap = [item for start, item in candidate.items() if start in known]
    changed = [item for item in overlap if item.fingerprint != known[item.start].fingerprint]
    if len(overlap) >= 24 and len(changed) * 4 > len(overlap) * 3:
        prior_energy = _exact_sum(
            value
            for item in overlap
            for value in (known[item.start].import_kwh, known[item.start].return_kwh)
        )
        new_energy = _exact_sum(
            value for item in overlap for value in (item.import_kwh, item.return_kwh)
        )
        delta = _exact_sum((new_energy, prior_energy.copy_negate())).copy_abs()
        if _exact_sum((delta,) * 4) > prior_energy:
            return LedgerMutation(state, deferred=True, repair="usage_quarantine")

    cutoff = received_at - timedelta(days=400)
    inserted = corrected = estimated = 0
    changed_hours = []
    for start, item in candidate.items():
        prior = known.get(start)
        if prior is None and item.end <= cutoff:
            continue
        if prior is not None and prior.fingerprint == item.fingerprint:
            continue
        known[start] = item
        inserted += prior is None
        corrected += prior is not None
        estimated += item.is_estimated
        changed_hours.append(start)

    baseline = state.baseline
    retained = []
    for item in sorted(known.values(), key=lambda item: item.start):
        if item.end <= cutoff:
            baseline = _add_interval(baseline, item)
            changed_hours.append(item.start)
        else:
            retained.append(item)
    if (
        not changed_hours
        and cursor == state.backfill_cursor
        and complete == state.backfill_complete
    ):
        return LedgerMutation(state)
    next_state = replace(
        state,
        revision=state.revision + 1,
        intervals=tuple(retained),
        baseline=baseline,
        backfill_cursor=cursor,
        backfill_complete=complete,
    )
    return LedgerMutation(
        next_state,
        inserted=inserted,
        corrected=corrected,
        estimated=estimated,
        earliest_statistics_hour=min(changed_hours) if changed_hours else None,
    )


class LedgerRepairKind(StrEnum):
    """Secret-free reason a ledger needs supported repair."""

    CORRUPT = "ledger_corrupt"
    FUTURE = "ledger_future"


class LedgerRepairError(Exception):
    """Setup must block and surface the secret-free ledger repair instruction."""

    def __init__(self, kind: LedgerRepairKind = LedgerRepairKind.CORRUPT) -> None:
        self.kind = kind
        super().__init__(_REPAIR)


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("invalid ledger mapping")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid ledger integer")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("invalid ledger boolean")
    return value


def _text(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("invalid ledger text")
    return value


def _decimal(value: object, *, legacy: bool = False, derived: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float) if legacy else str):
        raise ValueError("invalid ledger decimal")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("invalid ledger decimal")
    validate_decimal(result, derived=derived)
    return result


def _datetime(value: object, *, legacy: bool = False) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid ledger timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or (not legacy and parsed.utcoffset() != timedelta(0)):
        raise ValueError("invalid ledger timestamp")
    return parsed.astimezone(UTC)


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _datetime(value)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("invalid ledger timestamp")
    return value.astimezone(UTC).isoformat()


def _fingerprint(data: dict[str, Any]) -> str:
    return sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _encode(state: LedgerState) -> dict[str, Any]:
    intervals = {}
    for item in state.intervals:
        key = _iso(item.start)
        if key in intervals:
            raise ValueError("duplicate ledger interval")
        intervals[key] = {
            "end": _iso(item.end),
            "import_kwh": str(item.import_kwh),
            "return_kwh": str(item.return_kwh),
            "amount": None if item.amount is None else str(item.amount),
            "currency": item.currency,
            "is_estimated": item.is_estimated,
            "received_at": _iso(item.received_at),
            "source_revision": item.source_revision,
            "fingerprint": item.fingerprint,
        }
    data = {
        "schema_version": state.schema_version,
        "revision": state.revision,
        "intervals": intervals,
        "baseline": {
            "import_kwh": str(state.baseline.import_kwh),
            "return_kwh": str(state.baseline.return_kwh),
            "cost": str(state.baseline.cost),
            "compensation": str(state.baseline.compensation),
        },
        "backfill_cursor": _iso(state.backfill_cursor),
        "backfill_complete": state.backfill_complete,
        "statistics_pending_from": _iso(state.statistics_pending_from),
        "statistics_pending_fingerprint": state.statistics_pending_fingerprint,
        "statistics_verified_through": _iso(state.statistics_verified_through),
        "statistics_verified_fingerprint": state.statistics_verified_fingerprint,
    }
    return {**data, "fingerprint": _fingerprint(data)}


def _decode(raw: object) -> LedgerState:
    data = dict(_mapping(raw))
    schema = data.get("schema_version")
    if type(schema) is int and schema > _SCHEMA_VERSION:
        raise LedgerRepairError(LedgerRepairKind.FUTURE)
    fingerprint = data.pop("fingerprint")
    if fingerprint != _fingerprint(data) or _integer(data["schema_version"]) != _SCHEMA_VERSION:
        raise ValueError("invalid ledger fingerprint or schema")
    intervals = []
    for start, raw_item in _mapping(data["intervals"]).items():
        item = _mapping(raw_item)
        interval = EnergyInterval(
            start=_datetime(start),
            end=_datetime(item["end"]),
            import_kwh=_decimal(item["import_kwh"]),
            return_kwh=_decimal(item["return_kwh"]),
            amount=None if item["amount"] is None else _decimal(item["amount"]),
            currency=_text(item["currency"]),
            is_estimated=_boolean(item["is_estimated"]),
            received_at=_datetime(item["received_at"]),
            source_revision=_text(item["source_revision"]),
        )
        if interval.fingerprint != item["fingerprint"]:
            raise ValueError("invalid interval fingerprint")
        intervals.append(interval)
    baseline = _mapping(data["baseline"])
    state = LedgerState(
        schema_version=_SCHEMA_VERSION,
        revision=_integer(data["revision"]),
        intervals=tuple(sorted(intervals, key=lambda item: item.start)),
        baseline=LedgerTotals(
            *(
                _decimal(baseline[key], derived=True)
                for key in ("import_kwh", "return_kwh", "cost", "compensation")
            )
        ),
        backfill_cursor=_optional_datetime(data["backfill_cursor"]),
        backfill_complete=_boolean(data["backfill_complete"]),
        statistics_pending_from=_optional_datetime(data["statistics_pending_from"]),
        statistics_pending_fingerprint=_text(data["statistics_pending_fingerprint"]),
        statistics_verified_through=_optional_datetime(data["statistics_verified_through"]),
        statistics_verified_fingerprint=_text(data["statistics_verified_fingerprint"]),
    )
    # Reject extra fields, duplicate UTC keys, and noncanonical timestamp spellings.
    if _encode(state) != raw:
        raise ValueError("noncanonical ledger snapshot")
    for hour in (state.statistics_pending_from, state.statistics_verified_through):
        if hour is not None and (hour.minute or hour.second or hour.microsecond):
            raise ValueError("invalid statistics hour")
    return state


class EntergyLedger:
    """Persist prepared mutations and expose only verified immutable state.

    Call async_load before mutations. A missing, uninitialized store starts with
    an empty v2 snapshot; the caller sets ledger_initialized only after a mutation
    returns deferred=False. Repair results block further writes until setup loads
    a repaired store. Shutdown deferrals are safe to retry after restart.
    """

    def __init__(self, hass: HomeAssistant, public_id: str) -> None:
        self._hass = hass
        self._key = f"entergy_mobile.ledger_{public_id}"
        self._store = self._new_store()
        self._state = LedgerState(schema_version=_SCHEMA_VERSION)
        self._lock = asyncio.Lock()
        self._loaded = False
        self._blocked = False

    def _new_store(self, *, read_only: bool = False) -> Store[dict[str, Any]]:
        return Store[dict[str, Any]](
            self._hass,
            _SCHEMA_VERSION,
            self._key,
            private=True,
            atomic_writes=True,
            read_only=read_only,
        )

    @property
    def state(self) -> LedgerState:
        """The last verified snapshot (or empty state before first persistence)."""
        return self._state

    def _stopping(self) -> bool:
        return self._hass.state in (CoreState.stopping, CoreState.final_write)

    def _deferred(self, *, repair: bool = False) -> LedgerMutation:
        return LedgerMutation(self._state, deferred=True, repair=_REPAIR if repair else None)

    async def async_load(self, *, initialized: bool) -> LedgerState:
        """Load validated v2 state, blocking instead of overwriting damaged history."""
        async with self._lock:
            try:
                raw = await self._new_store(read_only=True).async_load()
                if raw is None:
                    if initialized or self._loaded:
                        raise LedgerRepairError
                    candidate = self._state
                else:
                    candidate = _decode(raw)
            except UnsupportedStorageVersionError:
                self._blocked = True
                raise LedgerRepairError(LedgerRepairKind.FUTURE) from None
            except LedgerRepairError:
                self._blocked = True
                raise
            except Exception:
                self._blocked = True
                raise LedgerRepairError from None
            if self._stopping():
                raise LedgerRepairError from None
            self._state = candidate
            self._loaded = True
            self._blocked = False
            return self._state

    async def _persist(self, mutation: LedgerMutation) -> LedgerMutation:
        """Only write path; called with the mutation lock held."""
        if not self._loaded or self._blocked:
            return self._deferred(repair=True)
        if self._stopping() or mutation.deferred:
            return self._deferred()
        if mutation.repair or mutation.state.revision != self._state.revision + 1:
            return self._deferred(repair=True)
        try:
            candidate = _encode(mutation.state)
            _decode(candidate)
            await self._store.async_save(candidate)
            readback = await self._new_store(read_only=True).async_load()
            if self._stopping():
                return self._deferred()
            verified = _decode(readback)
            if verified.revision != mutation.state.revision or _encode(verified) != candidate:
                raise LedgerRepairError
        except Exception:
            if self._stopping():
                return self._deferred()
            self._blocked = True
            return self._deferred(repair=True)
        self._state = verified
        return replace(mutation, state=verified)

    async def async_ingest(self, mutation: LedgerMutation) -> LedgerMutation:
        """Verify a prepared reconciliation mutation; never reconcile here."""
        async with self._lock:
            return await self._persist(mutation)

    async def async_mark_statistics_pending(
        self, *, from_hour: datetime, fingerprint: str
    ) -> LedgerMutation:
        """Persist the earliest pending hour before any Recorder queue attempt."""
        async with self._lock:
            earliest = self._state.statistics_pending_from
            state = replace(
                self._state,
                revision=self._state.revision + 1,
                statistics_pending_from=min(earliest, from_hour) if earliest else from_hour,
                statistics_pending_fingerprint=fingerprint,
            )
            return await self._persist(LedgerMutation(state))

    async def async_mark_statistics_verified(
        self, *, through: datetime, fingerprint: str
    ) -> LedgerMutation:
        """Acknowledge matching pending work only after Recorder verification."""
        async with self._lock:
            pending = self._state.statistics_pending_from
            if (
                pending is None
                or fingerprint != self._state.statistics_pending_fingerprint
                or through < pending
                or (
                    self._state.statistics_verified_through is not None
                    and through < self._state.statistics_verified_through
                )
            ):
                return self._deferred()
            state = replace(
                self._state,
                revision=self._state.revision + 1,
                statistics_pending_from=None,
                statistics_pending_fingerprint=None,
                statistics_verified_through=through,
                statistics_verified_fingerprint=fingerprint,
            )
            return await self._persist(LedgerMutation(state))


def _legacy_contribution(value: object, current: Decimal) -> Decimal:
    """Validate a retained hour's cumulative contribution under v1 max accounting."""
    cached = _decimal(value, legacy=True)
    # v1 stored maxima of Python float values rounded to six places. Reproduce
    # only that validation boundary; v2 interval values keep their exact Decimal.
    if (
        cached < 0
        or cached != Decimal(str(round(float(cached), 6)))
        or cached < Decimal(str(round(float(current), 6)))
    ):
        raise ValueError("inconsistent legacy accounting")
    return cached


async def async_import_legacy_v1_store(
    hass: HomeAssistant, *, entry_id: str, public_id: str
) -> LedgerState | None:
    """Read v1 signed usage without saving, deleting, or migrating its Store.

    Signed usage supplies the new intervals. Validated cached import/export
    maxima establish how much retained hours contributed to old lifetime totals;
    subtract those contributions to isolate pre-window baselines. Historical
    cost/compensation is unrecoverable and is deliberately omitted.
    The caller persists this candidate through the v2 ledger before marking its
    config entry initialized. public_id identifies that destination, not v1.
    """
    del public_id
    try:
        raw = await Store[dict[str, Any]](
            hass, 1, f"entergy_mobile_usage_{entry_id}", read_only=True
        ).async_load()
        if raw is None:
            return None
        data = _mapping(raw)
        received_at = dt_util.utcnow()
        intervals = []
        contributions = []
        for stamp, raw_item in _mapping(data["intervals"]).items():
            start = _datetime(stamp, legacy=True)
            item = _mapping(raw_item)
            usage = _decimal(item["usage"], legacy=True)
            import_kwh = max(usage, Decimal(0))
            return_kwh = usage.copy_negate() if usage < 0 else Decimal(0)
            contributions.append(
                (
                    _legacy_contribution(item["import"], import_kwh),
                    _legacy_contribution(item["export"], return_kwh),
                )
            )
            amount = None if item.get("cost") is None else _decimal(item["cost"], legacy=True)
            intervals.append(
                EnergyInterval(
                    start=start,
                    end=start + timedelta(hours=1),
                    import_kwh=import_kwh,
                    return_kwh=return_kwh,
                    amount=amount,
                    currency=None,
                    is_estimated=_boolean(item["isEstimated"]),
                    received_at=received_at,
                )
            )
        intervals.sort(key=lambda item: item.start)
        if len({item.start for item in intervals}) != len(intervals):
            raise ValueError("duplicate legacy hour")
        totals = [
            _decimal(data[key], legacy=True, derived=True)
            for key in ("total_import_kwh", "total_export_kwh")
        ]
        # v1 never subtracted downward corrections. Removing the cached retained
        # contributions avoids misclassifying their overcounts as older energy.
        baseline = LedgerTotals(
            _exact_sum((totals[0], *(pair[0].copy_negate() for pair in contributions))),
            _exact_sum((totals[1], *(pair[1].copy_negate() for pair in contributions))),
        )
        return LedgerState(
            schema_version=_SCHEMA_VERSION,
            intervals=tuple(intervals),
            baseline=baseline,
            statistics_pending_from=intervals[0].start if intervals else None,
        )
    except Exception:
        raise LedgerRepairError from None
