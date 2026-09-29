"""Immutable, privacy-safe values shared by the integration layers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, localcontext
from enum import StrEnum
from hashlib import sha256
from math import isfinite


def validate_decimal(value: Decimal, *, derived: bool = False) -> None:
    """Bound untrusted representation size before any arithmetic allocation."""
    parts = value.as_tuple()
    if (
        not value.is_finite()
        or not isinstance(parts.exponent, int)
        or not -1024 <= parts.exponent <= 1024
        or len(parts.digits) > (4096 if derived else 1024)
    ):
        raise ValueError("unsafe decimal representation")
    if derived and not isfinite(float(value)):
        raise ValueError("unsafe Recorder total")


def exact_sum(values: Iterable[Decimal]) -> Decimal:
    """Sum bounded decimals exactly, without ambient rounding or unbounded precision."""
    items = tuple(values)
    if not items:
        return Decimal(0)
    for item in items:
        validate_decimal(item, derived=True)
    precision = max(
        1,
        max(item.adjusted() for item in items)
        - min(int(item.as_tuple().exponent) for item in items)
        + len(str(len(items)))
        + 2,
    )
    if precision > 4096:
        raise ValueError("unsafe decimal precision")
    with localcontext(Context(prec=precision, Emax=MAX_EMAX, Emin=MIN_EMIN)):
        result = sum(items, Decimal(0))
    validate_decimal(result, derived=True)
    return result


def _canonical_decimal(value: Decimal) -> str:
    """Encode a finite number by coefficient and exponent without active context."""
    if value.is_zero():
        return "0"
    parts = value.as_tuple()
    digits = list(parts.digits)
    exponent = parts.exponent
    assert isinstance(exponent, int)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return f"{parts.sign}:{''.join(str(digit) for digit in digits)}:{exponent}"


@dataclass(frozen=True, slots=True, repr=False)
class Credentials:
    """Login inputs, never shown by repr."""

    username: str
    password: str


@dataclass(frozen=True, slots=True, repr=False)
class ClientMetadata:
    """Utility client identifier."""

    client_id: str


@dataclass(frozen=True, slots=True, repr=False)
class LoginResult:
    """A token and optional challenge marker."""

    access_token: str
    next_action: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class Account:
    """Account identity with a masked display label."""

    account_id: str
    nickname: str | None = None
    time_zone: str | None = None

    @property
    def display_name(self) -> str:
        """Show at most the final four account characters."""
        suffix = self.account_id[-4:]
        label = f"••••{suffix}"
        return f"{label} ({self.nickname})" if self.nickname else label


@dataclass(frozen=True, slots=True, repr=False)
class EnergyInterval:
    """A single absolute UTC hour of normalized usage."""

    start: datetime
    end: datetime
    import_kwh: Decimal
    return_kwh: Decimal
    amount: Decimal | None
    currency: str | None
    is_estimated: bool
    received_at: datetime
    source_revision: str | None = None
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            self.start.tzinfo is None
            or self.end.tzinfo is None
            or self.received_at.tzinfo is None
            or self.start.utcoffset() != timedelta(0)
            or self.end.utcoffset() != timedelta(0)
            or self.received_at.utcoffset() != timedelta(0)
            or self.start.minute != 0
            or self.start.second != 0
            or self.start.microsecond != 0
            or self.end - self.start != timedelta(hours=1)
            or self.import_kwh < 0
            or self.return_kwh < 0
            or not self.import_kwh.is_finite()
            or not self.return_kwh.is_finite()
            or (self.amount is not None and not self.amount.is_finite())
        ):
            raise ValueError("invalid energy interval")
        parts = (
            self.start.astimezone(UTC).isoformat(),
            self.end.astimezone(UTC).isoformat(),
            _canonical_decimal(self.import_kwh),
            _canonical_decimal(self.return_kwh),
            "" if self.amount is None else _canonical_decimal(self.amount),
            self.currency or "",
            "1" if self.is_estimated else "0",
            self.source_revision or "",
        )
        object.__setattr__(self, "fingerprint", sha256("\x1f".join(parts).encode()).hexdigest())


@dataclass(frozen=True, slots=True)
class LedgerTotals:
    """Non-negative lifetime cumulative values."""

    import_kwh: Decimal = Decimal(0)
    return_kwh: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)
    compensation: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if any(
            not value.is_finite() or value < 0
            for value in (self.import_kwh, self.return_kwh, self.cost, self.compensation)
        ):
            raise ValueError("invalid ledger totals")


@dataclass(frozen=True, slots=True, repr=False)
class LedgerState:
    """A revisioned interval ledger and statistics progress markers."""

    schema_version: int = 1
    revision: int = 0
    intervals: tuple[EnergyInterval, ...] = ()
    baseline: LedgerTotals = field(default_factory=LedgerTotals)
    backfill_cursor: datetime | None = None
    backfill_complete: bool = False
    statistics_pending_from: datetime | None = None
    statistics_pending_fingerprint: str | None = None
    statistics_verified_through: datetime | None = None
    statistics_verified_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version < 1 or self.revision < 0:
            raise ValueError("invalid ledger state")
        if tuple(sorted(self.intervals, key=lambda interval: interval.start)) != self.intervals:
            raise ValueError("unsorted ledger intervals")


@dataclass(frozen=True, slots=True, repr=False)
class LedgerMutation:
    """Result of applying a new interval window."""

    state: LedgerState
    inserted: int = 0
    corrected: int = 0
    estimated: int = 0
    earliest_statistics_hour: datetime | None = None
    deferred: bool = False
    repair: str | None = None


class Freshness(StrEnum):
    """Age of the newest utility interval."""

    FRESH = "fresh"
    DELAYED = "delayed"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, repr=False)
class UsageSnapshot:
    """Validated dashboard values at a chosen local time."""

    newest_interval_start: datetime | None
    freshness: Freshness
    latest_import_kwh: Decimal | None
    latest_return_kwh: Decimal | None
    today_import_kwh: Decimal | None
    today_return_kwh: Decimal | None
    seven_day_import_kwh: Decimal | None
    seven_day_return_kwh: Decimal | None
    month_import_kwh: Decimal | None
    month_return_kwh: Decimal | None
    latest_cost: Decimal | None
    today_cost: Decimal | None
    seven_day_cost: Decimal | None
    month_cost: Decimal | None
    latest_compensation: Decimal | None
    today_compensation: Decimal | None
    seven_day_compensation: Decimal | None
    month_compensation: Decimal | None
