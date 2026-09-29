"""Pure validators for the reviewed Entergy response shapes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ChallengeError, PayloadError
from .models import (
    Account,
    ClientMetadata,
    EnergyInterval,
    Freshness,
    LedgerState,
    LoginResult,
    UsageSnapshot,
    exact_sum,
    validate_decimal,
)

_ACCOUNT_ALIASES = ("accountId", "accountID", "account_id", "accountNumber", "account_number")
_TOKEN_ALIASES = ("accessToken", "access_token", "token")
_ACCOUNT_WRAPPERS = ("accounts", "data", "items", "results")
_HOUR = timedelta(hours=1)
_MAX_RECORDS = 512
_MAX_KWH = Decimal("10000")
_MAX_AMOUNT = Decimal("1000000")
# Currency codes used by the reviewed USD source and common alternate ISO 4217 responses.
_CURRENCIES = frozenset(
    [
        "AED",
        "AFN",
        "ALL",
        "AMD",
        "AOA",
        "ARS",
        "AUD",
        "AWG",
        "AZN",
        "BAM",
        "BBD",
        "BDT",
        "BGN",
        "BHD",
        "BIF",
        "BMD",
        "BND",
        "BOB",
        "BRL",
        "BSD",
        "BTN",
        "BWP",
        "BYN",
        "BZD",
        "CAD",
        "CDF",
        "CHF",
        "CLP",
        "CNY",
        "COP",
        "CRC",
        "CUP",
        "CVE",
        "CZK",
        "DJF",
        "DKK",
        "DOP",
        "DZD",
        "EGP",
        "ERN",
        "ETB",
        "EUR",
        "FJD",
        "FKP",
        "GBP",
        "GEL",
        "GHS",
        "GIP",
        "GMD",
        "GNF",
        "GTQ",
        "GYD",
        "HKD",
        "HNL",
        "HTG",
        "HUF",
        "IDR",
        "ILS",
        "INR",
        "IQD",
        "IRR",
        "ISK",
        "JMD",
        "JOD",
        "JPY",
        "KES",
        "KGS",
        "KHR",
        "KMF",
        "KRW",
        "KWD",
        "KYD",
        "KZT",
        "LAK",
        "LBP",
        "LKR",
        "LRD",
        "LSL",
        "LYD",
        "MAD",
        "MDL",
        "MGA",
        "MKD",
        "MMK",
        "MNT",
        "MOP",
        "MRU",
        "MUR",
        "MVR",
        "MWK",
        "MXN",
        "MYR",
        "MZN",
        "NAD",
        "NGN",
        "NIO",
        "NOK",
        "NPR",
        "NZD",
        "OMR",
        "PAB",
        "PEN",
        "PGK",
        "PHP",
        "PKR",
        "PLN",
        "PYG",
        "QAR",
        "RON",
        "RSD",
        "RUB",
        "RWF",
        "SAR",
        "SBD",
        "SCR",
        "SDG",
        "SEK",
        "SGD",
        "SHP",
        "SLE",
        "SOS",
        "SRD",
        "SSP",
        "STN",
        "SVC",
        "SYP",
        "SZL",
        "THB",
        "TJS",
        "TMT",
        "TND",
        "TOP",
        "TRY",
        "TTD",
        "TWD",
        "TZS",
        "UAH",
        "UGX",
        "USD",
        "UYU",
        "UZS",
        "VES",
        "VND",
        "VUV",
        "WST",
        "XAF",
        "XCD",
        "XOF",
        "XPF",
        "YER",
        "ZAR",
        "ZMW",
        "ZWG",
    ]
)


def _object(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PayloadError
    return payload


def _nonempty_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PayloadError
    return value.strip()


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise PayloadError from None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PayloadError
    return value.astimezone(UTC)


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise PayloadError
    try:
        result = Decimal(str(value))
    except InvalidOperation, ValueError, OverflowError:
        raise PayloadError from None
    if not result.is_finite():
        raise PayloadError
    try:
        validate_decimal(result)
    except ValueError:
        raise PayloadError from None
    return Decimal(0) if result.is_zero() else result


def _pick_string(payload: dict[str, Any], aliases: tuple[str, ...]) -> str | None:
    for key in aliases:
        if key in payload:
            return _nonempty_string(payload[key])
    return None


def parse_client_metadata(payload: object) -> ClientMetadata:
    """Read clientId at the root or under data."""
    body = _object(payload)
    identifier = _pick_string(body, ("clientId",))
    if identifier is None and "data" in body:
        identifier = _pick_string(_object(body["data"]), ("clientId",))
    if identifier is None:
        raise PayloadError
    return ClientMetadata(client_id=identifier)


def parse_login(payload: object) -> LoginResult:
    """Read a token, failing closed if an interactive action is requested."""
    body = _object(payload)
    # Inspect the entire JSON tree before token/schema selection. Unknown action
    # shapes (including false/empty containers) are unsupported, not successful.
    pending: list[object] = [body]
    while pending:
        source = pending.pop()
        if isinstance(source, dict):
            for key, value in source.items():
                if key in ("nextAction", "challenge") and value is not None and value != "":
                    raise ChallengeError
                if isinstance(value, (dict, list)):
                    pending.append(value)
        elif isinstance(source, list):
            pending.extend(source)
    nested = _object(body["data"]) if "data" in body else {}
    token = _pick_string(body, _TOKEN_ALIASES) or _pick_string(nested, _TOKEN_ALIASES)
    if token is None:
        raise PayloadError
    return LoginResult(access_token=token)


def _parse_account_item(payload: object) -> Account:
    item = _object(payload)
    identifier = _pick_string(item, _ACCOUNT_ALIASES)
    if identifier is None:
        raise PayloadError
    nickname = _pick_string(item, ("nickname", "name"))
    time_zone = item.get("timeZone")
    if time_zone is not None:
        time_zone = _nonempty_string(time_zone)
        _zone(time_zone)
    return Account(account_id=identifier, nickname=nickname, time_zone=time_zone)


def parse_accounts(payload: object) -> tuple[Account, ...]:
    """Parse an account list in one of the reviewed wrappers."""
    items = payload
    if isinstance(payload, dict):
        for key in _ACCOUNT_WRAPPERS:
            if key in payload:
                items = payload[key]
                break
    if not isinstance(items, list):
        raise PayloadError
    return tuple(_parse_account_item(item) for item in items)


def parse_account(payload: object, expected_account_id: str) -> Account:
    """Validate a single account response against the requested identity."""
    item = payload
    if isinstance(payload, dict) and "data" in payload:
        item = payload["data"]
    account = _parse_account_item(item)
    if account.account_id != expected_account_id:
        raise PayloadError
    return account


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise PayloadError
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise PayloadError from None
    start = _aware_utc(parsed)
    if start.minute or start.second or start.microsecond:
        raise PayloadError
    return start


def parse_usage(
    payload: object, *, source_time_zone: str, received_at: datetime
) -> tuple[EnergyInterval, ...]:
    """Normalize strict weekly usage into sorted absolute UTC hours."""
    _zone(source_time_zone)
    receipt = _aware_utc(received_at)
    body = _object(payload)
    data = _object(body.get("data"))
    daily = _object(data.get("daily"))
    electric = daily.get("electric")
    if not isinstance(electric, list) or not electric:
        raise PayloadError
    intervals: dict[datetime, EnergyInterval] = {}
    seen = 0
    for day in electric:
        hours = _object(day).get("hourly")
        if not isinstance(hours, list):
            raise PayloadError
        for raw in hours:
            seen += 1
            if seen > _MAX_RECORDS:
                raise PayloadError
            hour = _object(raw)
            if hour.keys() - {"date", "usage", "cost", "isEstimated", "currency", "sourceRevision"}:
                raise PayloadError
            start = _timestamp(hour.get("date"))
            signed_usage = _decimal(hour.get("usage"))
            if signed_usage.copy_abs() > _MAX_KWH:
                raise PayloadError
            amount = _decimal(hour["cost"]) if "cost" in hour and hour["cost"] is not None else None
            if amount is not None and amount.copy_abs() > _MAX_AMOUNT:
                raise PayloadError
            estimated = hour.get("isEstimated")
            if not isinstance(estimated, bool):
                raise PayloadError
            currency_value = hour.get("currency", data.get("currency", "USD"))
            currency = _nonempty_string(currency_value).upper()
            if currency not in _CURRENCIES:
                raise PayloadError
            revision = hour.get("sourceRevision")
            if revision is not None:
                revision = _nonempty_string(revision)
            interval = EnergyInterval(
                start=start,
                end=start + _HOUR,
                import_kwh=signed_usage if signed_usage > 0 else Decimal(0),
                return_kwh=signed_usage.copy_negate() if signed_usage < 0 else Decimal(0),
                amount=amount,
                currency=currency,
                is_estimated=estimated,
                received_at=receipt,
                source_revision=revision,
            )
            prior = intervals.get(start)
            if prior is not None and prior.fingerprint != interval.fingerprint:
                raise PayloadError
            intervals[start] = interval
    return tuple(sorted(intervals.values(), key=lambda interval: interval.start))


def summarize_usage(state: LedgerState, *, time_zone: str, now: datetime) -> UsageSnapshot:
    """Summarize intervals in the configured local calendar and age window."""
    zone = _zone(time_zone)
    current = _aware_utc(now)
    intervals = state.intervals
    monetary_allowed = all(item.currency in (None, "USD") for item in intervals)
    newest = intervals[-1] if intervals else None
    if newest is None:
        freshness = Freshness.UNKNOWN
    else:
        age = current - newest.end
        if age <= timedelta(hours=36):
            freshness = Freshness.FRESH
        elif age <= timedelta(hours=72):
            freshness = Freshness.DELAYED
        else:
            freshness = Freshness.STALE

    local_today = current.astimezone(zone).date()
    buckets: dict[str, list[EnergyInterval]] = {"today": [], "seven_day": [], "month": []}
    for interval in intervals:
        local_date = interval.start.astimezone(zone).date()
        if local_date == local_today:
            buckets["today"].append(interval)
        if local_today - timedelta(days=6) <= local_date <= local_today:
            buckets["seven_day"].append(interval)
        if local_date.year == local_today.year and local_date.month == local_today.month:
            buckets["month"].append(interval)

    def total(items: list[EnergyInterval], kind: str) -> Decimal | None:
        if not items:
            return None
        if kind == "import":
            return exact_sum(item.import_kwh for item in items)
        if kind == "return":
            return exact_sum(item.return_kwh for item in items)
        if not monetary_allowed:
            return None
        amounts = [item.amount for item in items if item.amount is not None]
        if not amounts:
            return None
        if kind == "cost":
            return exact_sum(max(amount, Decimal(0)) for amount in amounts)
        return exact_sum(max(amount.copy_negate(), Decimal(0)) for amount in amounts)

    latest = [newest] if newest is not None else []
    return UsageSnapshot(
        newest_interval_start=newest.start if newest is not None else None,
        freshness=freshness,
        latest_import_kwh=total(latest, "import"),
        latest_return_kwh=total(latest, "return"),
        today_import_kwh=total(buckets["today"], "import"),
        today_return_kwh=total(buckets["today"], "return"),
        seven_day_import_kwh=total(buckets["seven_day"], "import"),
        seven_day_return_kwh=total(buckets["seven_day"], "return"),
        month_import_kwh=total(buckets["month"], "import"),
        month_return_kwh=total(buckets["month"], "return"),
        latest_cost=total(latest, "cost"),
        today_cost=total(buckets["today"], "cost"),
        seven_day_cost=total(buckets["seven_day"], "cost"),
        month_cost=total(buckets["month"], "cost"),
        latest_compensation=total(latest, "compensation"),
        today_compensation=total(buckets["today"], "compensation"),
        seven_day_compensation=total(buckets["seven_day"], "compensation"),
        month_compensation=total(buckets["month"], "compensation"),
    )
