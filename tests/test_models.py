"""Privacy and value contracts for normalized utility data."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from custom_components.entergy_mobile.errors import (
    AuthError,
    EntergyError,
    ErrorCategory,
    PayloadError,
)
from custom_components.entergy_mobile.models import (
    Account,
    ClientMetadata,
    Credentials,
    EnergyInterval,
    LoginResult,
)


def test_secret_bearing_models_are_immutable_and_hidden() -> None:
    credentials = Credentials(username="private@example.test", password="fake")
    metadata = ClientMetadata(client_id="synthetic-client")
    login = LoginResult(access_token="fake")
    account = Account(account_id="0001234567", nickname="Home")
    for value in (credentials, metadata, login, account):
        rendered = repr(value)
        assert "fake" not in rendered
        assert "private@example.test" not in rendered
        assert "synthetic-client" not in rendered
        assert "access_token='fake'" not in rendered
        assert "0001234567" not in rendered
        with pytest.raises(FrozenInstanceError):
            value.secret = "changed"  # type: ignore[attr-defined]


def test_interval_fingerprint_is_stable_and_contains_no_account_data() -> None:
    start = datetime(2026, 9, 27, 10, tzinfo=UTC)
    interval = EnergyInterval(
        start=start,
        end=start + timedelta(hours=1),
        import_kwh=Decimal("1.25"),
        return_kwh=Decimal("0"),
        amount=Decimal("0.22"),
        currency="USD",
        is_estimated=False,
        received_at=datetime(2026, 9, 28, tzinfo=UTC),
        source_revision=None,
    )
    again = EnergyInterval(
        start=start,
        end=start + timedelta(hours=1),
        import_kwh=Decimal("1.25"),
        return_kwh=Decimal("0"),
        amount=Decimal("0.22"),
        currency="USD",
        is_estimated=False,
        received_at=datetime(2026, 9, 29, tzinfo=UTC),
        source_revision=None,
    )
    assert interval.fingerprint == again.fingerprint
    assert len(interval.fingerprint) == 64
    assert "0001234567" not in interval.fingerprint
    assert "2026-09-28" not in interval.fingerprint
    assert "0001234567" not in repr(interval)


def test_interval_fingerprint_uses_numeric_value_not_decimal_spelling() -> None:
    start = datetime(2026, 9, 27, 10, tzinfo=UTC)
    common = {
        "start": start,
        "end": start + timedelta(hours=1),
        "currency": "USD",
        "is_estimated": False,
        "received_at": datetime(2026, 9, 28, tzinfo=UTC),
        "source_revision": None,
    }
    first = EnergyInterval(
        import_kwh=Decimal("1.0"),
        return_kwh=Decimal("0.0"),
        amount=Decimal("0.20"),
        **common,
    )
    second = EnergyInterval(
        import_kwh=Decimal("1"),
        return_kwh=Decimal("0"),
        amount=Decimal("0.2"),
        **common,
    )
    assert first.fingerprint == second.fingerprint


def test_direct_interval_requires_utc_hour_boundary() -> None:
    start = datetime(2026, 9, 27, 10, 30, tzinfo=UTC)
    with pytest.raises(ValueError):
        EnergyInterval(
            start=start,
            end=start + timedelta(hours=1),
            import_kwh=Decimal("1"),
            return_kwh=Decimal("0"),
            amount=None,
            currency="USD",
            is_estimated=False,
            received_at=datetime(2026, 9, 28, tzinfo=UTC),
        )


def test_errors_never_echo_sensitive_values() -> None:
    for error in (
        EntergyError(ErrorCategory.PAYLOAD, status=400),
        AuthError(status=401),
        PayloadError(),
    ):
        assert "password" not in str(error).lower()
        assert "token" not in repr(error).lower()
        assert "0001234567" not in repr(error)
    assert AuthError(status=401).category is ErrorCategory.AUTH
