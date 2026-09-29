"""Strict parser boundary tests using synthetic utility payloads."""

import json
import traceback
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
from custom_components.entergy_mobile.errors import ChallengeError, PayloadError
from custom_components.entergy_mobile.models import EnergyInterval, Freshness, LedgerState
from custom_components.entergy_mobile.parser import (
    parse_account,
    parse_accounts,
    parse_client_metadata,
    parse_login,
    parse_usage,
    summarize_usage,
)

FIXTURES = Path(__file__).parent / "fixtures"
RECEIVED = datetime(2026, 9, 28, tzinfo=UTC)


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def usage(*records: dict[str, object]) -> dict[str, object]:
    return {"data": {"daily": {"electric": [{"hourly": list(records)}]}}}


def record(date: str, value: object = 1, **extra: object) -> dict[str, object]:
    return {"date": date, "usage": value, "isEstimated": False, **extra}


def parse(payload: object) -> tuple[EnergyInterval, ...]:
    return parse_usage(payload, source_time_zone="America/Chicago", received_at=RECEIVED)


def test_metadata_and_login_accept_only_reviewed_locations_and_aliases() -> None:
    assert parse_client_metadata(fixture("app.json")).client_id == "synthetic-client"
    assert parse_client_metadata({"clientId": "root-client"}).client_id == "root-client"
    assert parse_login(fixture("login.json")).access_token == "synthetic-token"
    for alias in ("accessToken", "access_token", "token"):
        assert parse_login({alias: "synthetic-token"}).access_token == "synthetic-token"
        assert parse_login({"data": {alias: "synthetic-token"}}).access_token == "synthetic-token"
    with pytest.raises(ChallengeError):
        parse_login({"nextAction": "synthetic-challenge", "token": "synthetic-token"})
    with pytest.raises(ChallengeError):
        parse_login({"data": {"nextAction": "synthetic-challenge"}})
    with pytest.raises(PayloadError):
        parse_client_metadata({"client_id": "unreviewed"})


def test_account_aliases_masking_and_expected_identity() -> None:
    assert parse_accounts(fixture("accounts.json"))[0].account_id == "0001234567"
    for alias in ("accountId", "accountID", "account_id", "accountNumber", "account_number"):
        for wrapper in (
            lambda x: [x],
            lambda x: {"accounts": [x]},
            lambda x: {"data": [x]},
            lambda x: {"items": [x]},
            lambda x: {"results": [x]},
        ):
            accounts = parse_accounts(wrapper({alias: "0001234567", "name": "Home"}))
            assert accounts[0].account_id == "0001234567"
            assert accounts[0].display_name == "••••4567 (Home)"
    for alias in ("id", "number"):
        with pytest.raises(PayloadError):
            parse_accounts([{alias: "0001234567"}])
    account = parse_account({"accountNumber": "0001234567"}, "0001234567")
    assert account.display_name == "••••4567"
    with pytest.raises(PayloadError):
        parse_account({"accountNumber": "0001234567"}, "different")
    assert (
        "123 Main"
        not in parse_accounts([{"accountId": "0001234567", "serviceAddress": "123 Main"}])[
            0
        ].display_name
    )


def test_usage_sign_decimal_conversion_and_hour_identity() -> None:
    intervals = parse(fixture("weekly_usage.json"))
    assert len(intervals) == 2
    assert intervals[0].start == datetime(2026, 9, 27, 10, tzinfo=UTC)
    assert intervals[0].end == datetime(2026, 9, 27, 11, tzinfo=UTC)
    assert intervals[0].import_kwh == Decimal("1.25")
    assert intervals[0].return_kwh == 0
    assert intervals[1].import_kwh == 0
    assert intervals[1].return_kwh == Decimal("0.5")
    assert intervals[1].amount == Decimal("-0.03")
    assert intervals[1].is_estimated is True


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), "NaN", "Infinity"])
def test_usage_rejects_non_finite_or_boolean_values(value: object) -> None:
    with pytest.raises(PayloadError):
        parse(usage(record("2026-09-27T10:00:00Z", value)))


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "NaN"])
def test_cost_rejects_non_finite_or_boolean_values(value: object) -> None:
    with pytest.raises(PayloadError):
        parse(usage(record("2026-09-27T10:00:00Z", 1, cost=value)))


@pytest.mark.parametrize("payload", [{}, {"data": {}}, {"data": {"daily": {"electric": []}}}])
def test_missing_usage_data_fails_closed(payload: object) -> None:
    with pytest.raises(PayloadError):
        parse(payload)


@pytest.mark.parametrize("stamp", ["2026-09-27T10:00:00", "invalid", "2026-09-27T10:30:00Z"])
def test_usage_requires_valid_aware_utc_hour(stamp: str) -> None:
    with pytest.raises(PayloadError):
        parse(usage(record(stamp)))


def test_rejected_values_do_not_appear_in_formatted_exceptions() -> None:
    rejected = "synthetic-secret-1234567"
    with pytest.raises(PayloadError) as caught:
        parse(usage(record(rejected)))
    assert rejected not in "".join(traceback.format_exception(caught.value))


def test_limits_and_duplicate_conflict() -> None:
    stamp = "2026-09-27T10:00:00Z"
    assert parse(usage(record(stamp, 10000, cost=1000000)))[0].import_kwh == 10000
    for value in (10000.001, -10000.001):
        with pytest.raises(PayloadError):
            parse(usage(record(stamp, value)))
    with pytest.raises(PayloadError):
        parse(usage(record(stamp, 1, cost=1000000.01)))
    with pytest.raises(PayloadError):
        parse(usage(*(record("2026-09-27T10:00:00Z") for _ in range(513))))
    assert len(parse(usage(record(stamp), record(stamp)))) == 1
    with pytest.raises(PayloadError):
        parse(usage(record(stamp, 1), record(stamp, 2)))


def test_dst_uses_absolute_utc_hours_without_synthesizing_gap() -> None:
    spring = parse(usage(record("2026-03-08T01:00:00-06:00"), record("2026-03-08T03:00:00-05:00")))
    assert [item.start.hour for item in spring] == [7, 8]
    fall = parse(usage(record("2026-11-01T01:00:00-05:00"), record("2026-11-01T01:00:00-06:00")))
    assert [item.start.hour for item in fall] == [6, 7]


def test_local_day_and_freshness_boundaries() -> None:
    items = parse(usage(record("2026-09-28T00:00:00Z", 2), record("2026-09-28T05:00:00Z", 3)))
    state = LedgerState(intervals=items)
    snapshot = summarize_usage(
        state, time_zone="America/Chicago", now=datetime(2026, 9, 28, 6, tzinfo=UTC)
    )
    assert snapshot.today_import_kwh == Decimal("3")
    assert snapshot.freshness is Freshness.FRESH
    for age, expected in (
        (timedelta(hours=36), Freshness.FRESH),
        (timedelta(hours=36, seconds=1), Freshness.DELAYED),
        (timedelta(hours=72), Freshness.DELAYED),
        (timedelta(hours=72, seconds=1), Freshness.STALE),
    ):
        assert (
            summarize_usage(
                LedgerState(intervals=items), time_zone="America/Chicago", now=items[-1].end + age
            ).freshness
            is expected
        )
    assert (
        summarize_usage(
            LedgerState(intervals=()), time_zone="America/Chicago", now=RECEIVED
        ).freshness
        is Freshness.UNKNOWN
    )


def test_foreign_currency_preserves_energy_without_usd_monetary_summary() -> None:
    items = parse(usage(record("2026-09-27T10:00:00Z", 2, cost=3, currency="CAD")))
    snapshot = summarize_usage(
        LedgerState(intervals=items), time_zone="America/Chicago", now=RECEIVED
    )
    assert snapshot.latest_import_kwh == Decimal("2")
    assert snapshot.latest_cost is None
    assert snapshot.latest_compensation is None


def test_usage_preserves_high_precision_and_tiny_finite_values() -> None:
    precise = "1.0000000000000000000000000001"
    tiny = "1e-1024"
    with localcontext() as context:
        context.prec = 5
        items = parse(
            usage(
                record("2026-09-27T10:00:00Z", precise),
                record("2026-09-27T11:00:00Z", "-" + precise),
                record("2026-09-27T12:00:00Z", tiny),
            )
        )
    assert items[0].import_kwh == Decimal(precise)
    assert items[1].return_kwh == Decimal(precise)
    assert items[2].import_kwh == Decimal(tiny)
    assert items[2].import_kwh != 0
    ordinary = parse(usage(record("2026-09-27T10:00:00Z", 1)))[0]
    assert items[0].fingerprint != ordinary.fingerprint


@pytest.mark.parametrize("field", ["usage", "cost"])
def test_extreme_finite_magnitude_has_safe_payload_error(field: str) -> None:
    rejected = "1e1000000"
    hourly = record("2026-09-27T10:00:00Z", 1)
    hourly[field] = rejected
    with pytest.raises(PayloadError) as caught:
        parse(usage(hourly))
    assert rejected not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "payload",
    [
        {"token": "secret", "data": {"nextAction": {"type": "MFA"}}},
        {"token": "secret", "nextAction": "CAPTCHA"},
        {"token": "secret", "nextAction": "consent"},
        {"token": "secret", "data": {"steps": [{"nextAction": "unknown"}]}},
        {"data": {"token": "secret"}, "nextAction": {"url": "https://attacker.invalid"}},
        {"token": "secret", "data": {"challenge": {"url": "https://attacker.invalid"}}},
        {"token": "secret", "nextAction": {}},
        {"token": "secret", "nextAction": []},
        {"token": "secret", "nextAction": False},
        {"token": "secret", "nextAction": 0},
        {"token": "secret", "nextAction": "MFA", "data": None},
    ],
)
def test_login_inspects_all_challenges_before_accepting_token(payload: object) -> None:
    with pytest.raises(ChallengeError):
        parse_login(payload)


@pytest.mark.parametrize("payload", [{}, {"data": {}}, {"token": ""}, {"token": True}])
def test_login_requires_nonempty_string_token(payload: object) -> None:
    with pytest.raises(PayloadError):
        parse_login(payload)


@pytest.mark.parametrize(
    "key", ["deleted", "isDeleted", "retracted", "retraction", "status", "unknown"]
)
def test_usage_quarantine_rejects_unreviewed_hourly_fields(key: str) -> None:
    payload = usage(
        record("2026-09-27T10:00:00Z"), record("2026-09-27T11:00:00Z", **{key: "private-canary"})
    )
    with pytest.raises(PayloadError) as error:
        parse(payload)
    assert "private-canary" not in str(error.value)
    assert key not in str(error.value)


@pytest.mark.parametrize(
    "change",
    [
        {"date": 17},
        {"usage": "not-a-number"},
        {"isEstimated": "false"},
        {"currency": "ZZZ"},
        {"sourceRevision": False},
    ],
)
def test_malformed_hour_fields_fail_closed_without_values(change: dict[str, object]) -> None:
    hour = record("2026-09-27T10:00:00Z")
    hour.update(change)
    with pytest.raises(PayloadError) as error:
        parse(usage(hour))
    assert str(error.value) == "payload"


def test_source_revision_is_preserved() -> None:
    assert (
        parse(usage(record("2026-09-27T10:00:00Z", sourceRevision="reviewed-revision")))[
            0
        ].source_revision
        == "reviewed-revision"
    )


@pytest.mark.parametrize("payload", [{}, {"accounts": {}}, "private-canary"])
def test_nonlist_accounts_fail_closed(payload: object) -> None:
    with pytest.raises(PayloadError):
        parse_accounts(payload)


def test_unknown_timezone_and_nonlist_hours_fail_closed() -> None:
    with pytest.raises(PayloadError):
        parse_usage(usage(), source_time_zone="invalid/zone", received_at=RECEIVED)
    with pytest.raises(PayloadError):
        parse({"data": {"daily": {"electric": [{"hourly": {}}]}}})


def test_nested_nonchallenge_lists_allow_valid_login() -> None:
    assert (
        parse_login({"token": "valid", "steps": [[{"nextAction": None}, 1, []]]}).access_token
        == "valid"
    )


@pytest.mark.parametrize("field", ["usage", "cost"])
@pytest.mark.parametrize("value", ["1e-1025", "0e1025", "1." + "0" * 1024])
def test_decimal_representation_limits_reject_small_hostile_payloads(
    field: str, value: str
) -> None:
    item = record("2026-09-27T10:00:00Z")
    item[field] = value
    with pytest.raises(PayloadError) as error:
        parse(usage(item))
    assert str(error.value) == "payload"


def test_summary_sums_and_compensation_ignore_ambient_decimal_context() -> None:
    exact = "1.12345678901234567890123456789"
    items = parse(
        usage(
            record("2026-09-27T10:00:00Z", exact, cost="-" + exact),
            record("2026-09-27T11:00:00Z", "-" + exact, cost=exact),
        )
    )
    with localcontext() as context:
        context.prec = 5
        snapshot = summarize_usage(LedgerState(intervals=items), time_zone="UTC", now=RECEIVED)
    assert snapshot.month_import_kwh == Decimal(exact)
    assert snapshot.month_return_kwh == Decimal(exact)
    assert snapshot.month_cost == Decimal(exact)
    assert snapshot.month_compensation == Decimal(exact)


@pytest.mark.parametrize("value", ["0." + "1" * 1024, "1e-1024", "0e1024"])
def test_source_decimal_representation_boundary_remains_exact(value: str) -> None:
    parsed = parse(usage(record("2026-09-27T10:00:00Z", value, cost=value)))[0]
    assert parsed.import_kwh == Decimal(value)
    assert parsed.amount == Decimal(value)
