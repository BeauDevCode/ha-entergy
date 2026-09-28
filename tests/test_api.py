"""Fixed-origin transport boundary tests."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from custom_components.entergy_mobile import api
from custom_components.entergy_mobile.errors import (
    AuthError,
    EntergyError,
    ErrorCategory,
    PayloadError,
    PolicyError,
    RateLimitError,
)
from custom_components.entergy_mobile.models import Credentials


class FakeResponse:
    def __init__(
        self,
        body: object,
        *,
        status: int = 200,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
        chunk_size: int = 4096,
    ) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type, **(headers or {})}
        encoded = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.content = SimpleNamespace(iter_chunked=lambda _: self._chunks(encoded, chunk_size))

    async def _chunks(self, encoded: bytes, chunk_size: int) -> Any:
        for offset in range(0, len(encoded), chunk_size):
            yield encoded[offset : offset + chunk_size]

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeSession:
    def __init__(self, *responses: FakeResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("POST", url, **kwargs)


def client(session: FakeSession) -> api.EntergyApiClient:
    return api.EntergyApiClient(session, Credentials("secret-user", "secret-password"))


def authenticated(session: FakeSession) -> api.EntergyApiClient:
    result = client(session)
    result._client_id = "secret-client"
    result._access_token = "secret"
    return result


def usage(hours: int) -> dict[str, object]:
    base = datetime(2026, 9, 1, tzinfo=UTC)
    records = [
        {
            "date": (base + timedelta(hours=hour)).isoformat(),
            "usage": 1,
            "isEstimated": False,
        }
        for hour in range(hours)
    ]
    return {"data": {"daily": {"electric": [{"hourly": records}]}}}


@pytest.mark.parametrize(
    "account_id",
    [
        "",
        "a/b",
        "a\\b",
        "a..b",
        "../evil",
        "a\x00b",
        "a\nb",
        "é",
        "a" * 129,
        "https://evil.example/x",
        "//evil.example",
        "a?b",
        "a#b",
        "a@b",
    ],
)
async def test_origin_rejects_unsafe_account_segment_before_session(account_id: str) -> None:
    session = FakeSession()
    subject = authenticated(session)
    with pytest.raises(PolicyError):
        await subject.async_get_account(account_id, api.RequestBudget())
    assert session.calls == []


async def test_origin_only_six_approved_operations_and_query_keys() -> None:
    session = FakeSession(
        FakeResponse({"clientId": "secret-client"}),
        FakeResponse({"token": "secret-token"}),
        FakeResponse({"accounts": [{"accountId": "a-._~1"}]}),
        FakeResponse({"accountId": "a-._~1"}),
        FakeResponse(usage(1)),
        FakeResponse({}),
    )
    subject = client(session)
    budget = api.RequestBudget()
    await subject.async_initialize(budget)
    await subject.async_login(budget)
    await subject.async_get_accounts(budget)
    await subject.async_get_account("a-._~1", budget)
    await subject.async_get_weekly_usage("a-._~1", date(2026, 9, 1), budget)
    await subject.async_logout(budget)
    assert [(method, urlsplit(url).path) for method, url, _ in session.calls] == [
        ("GET", "/api/app"),
        ("POST", "/api/login"),
        ("GET", "/api/accounts"),
        ("GET", "/api/accounts/a-._~1"),
        ("GET", "/api/accounts/a-._~1/weeklyusage"),
        ("POST", "/api/logout"),
    ]
    for _, url, kwargs in session.calls:
        split = urlsplit(url)
        assert (split.scheme, split.hostname, split.port, split.username, split.fragment) == (
            "https",
            "prod.entergy.mindgrb.io",
            None,
            None,
            "",
        )
        assert split.query == ""
        assert kwargs["allow_redirects"] is False
        assert kwargs["auto_decompress"] is True
        assert kwargs["timeout"].connect == 10
        assert kwargs["timeout"].sock_read == 20
        assert kwargs["timeout"].total == 30
        assert set(kwargs["params"]) <= {"appVersion", "language", "view", "startDate"}
    assert session.calls[-2][2]["params"] == {
        "appVersion": "3.59.0",
        "language": "en",
        "view": "day",
        "startDate": "2026-09-01",
    }
    assert budget.used == 6


async def test_origin_rejects_caller_url_as_operation() -> None:
    session = FakeSession()
    subject = authenticated(session)
    with pytest.raises(PolicyError):
        await subject._request_json("https://evil.example/api/accounts", api.RequestBudget())
    assert session.calls == []


async def test_redirect_is_rejected_without_following_location() -> None:
    session = FakeSession(
        FakeResponse({}, status=302, headers={"Location": "https://evil.example/"})
    )
    subject = authenticated(session)
    with pytest.raises(PolicyError):
        await subject.async_get_accounts(api.RequestBudget())
    assert len(session.calls) == 1
    assert session.calls[0][2]["allow_redirects"] is False


async def test_budget_blocks_thirteenth_request_even_across_nested_initialization() -> None:
    session = FakeSession(
        FakeResponse({"clientId": "secret-client"}),
        FakeResponse({"token": "secret-token"}),
        *(FakeResponse({"accounts": []}) for _ in range(10)),
    )
    subject = client(session)
    budget = api.RequestBudget()
    for _ in range(10):
        await subject.async_get_accounts(budget)
    assert len(session.calls) == 12
    assert budget.used == 12
    with pytest.raises(PolicyError):
        await subject.async_get_accounts(budget)
    assert len(session.calls) == 12


async def test_chunked_gzip_body_exceeding_decompressed_limit_fails_without_leak(
    caplog: Any,
) -> None:
    sentinel = "secret-body-987654"
    body = (sentinel * 140000).encode()
    session = FakeSession(FakeResponse(body, headers={"Content-Encoding": "gzip"}, chunk_size=127))
    subject = authenticated(session)
    with pytest.raises(PayloadError) as caught:
        await subject.async_get_accounts(api.RequestBudget())
    exposed = str(caught.value) + repr(caught.value) + caplog.text
    assert sentinel not in exposed
    assert "Bearer secret" not in exposed


@pytest.mark.parametrize("content_type,body", [("text/html", b"{}"), ("application/json", b"{")])
async def test_malformed_content_type_or_json_is_safe(
    content_type: str, body: bytes, caplog: Any
) -> None:
    session = FakeSession(FakeResponse(body, content_type=content_type))
    with pytest.raises(PayloadError) as caught:
        await authenticated(session).async_get_accounts(api.RequestBudget())
    assert "secret" not in str(caught.value) + repr(caught.value) + caplog.text


async def test_weekly_usage_rejects_513_normalized_intervals() -> None:
    session = FakeSession(FakeResponse(usage(513)))
    with pytest.raises(PayloadError):
        await authenticated(session).async_get_weekly_usage(
            "valid", date(2026, 9, 1), api.RequestBudget()
        )


async def test_timeout_converts_to_sanitized_transient_error(caplog: Any) -> None:
    session = FakeSession(TimeoutError("secret-timeout-987654"))
    with pytest.raises(EntergyError) as caught:
        await authenticated(session).async_get_accounts(api.RequestBudget())
    assert caught.value.category == ErrorCategory.TRANSIENT
    assert caught.value.__cause__ is None
    assert "secret" not in str(caught.value) + repr(caught.value) + caplog.text


@pytest.mark.parametrize(
    "header,expected",
    [
        ("45", 45.0),
        ("999999", 86400.0),
        ("-5", 0.0),
        (format_datetime(datetime.now(UTC) + timedelta(days=2), usegmt=True), 86400.0),
    ],
)
async def test_retry_after_is_clamped(header: str, expected: float) -> None:
    session = FakeSession(FakeResponse({}, status=429, headers={"Retry-After": header}))
    with pytest.raises(RateLimitError) as caught:
        await authenticated(session).async_get_accounts(api.RequestBudget())
    assert caught.value.retry_after == expected


async def test_retry_after_invalid_uses_normal_schedule() -> None:
    session = FakeSession(FakeResponse({}, status=429, headers={"Retry-After": "invalid"}))
    with pytest.raises(RateLimitError) as caught:
        await authenticated(session).async_get_accounts(api.RequestBudget())
    assert caught.value.retry_after is None


async def test_auth_failure_is_sanitized() -> None:
    session = FakeSession(FakeResponse({"secret": "secret-body"}, status=401))
    with pytest.raises(AuthError) as caught:
        await authenticated(session).async_get_accounts(api.RequestBudget())
    assert caught.value.status == 401
    assert "secret" not in str(caught.value) + repr(caught.value)


async def test_legacy_constructor_preserves_raw_callers_with_bounded_chain() -> None:
    session = FakeSession(
        FakeResponse({"clientId": "client"}),
        FakeResponse({"token": "token"}),
        FakeResponse({"accounts": [{"accountId": "a"}]}),
        FakeResponse(usage(1)),
    )
    subject = api.EntergyApiClient(session, username="user", password="password")
    assert await subject.async_get_accounts() == {"accounts": [{"accountId": "a"}]}
    raw = await subject.async_fetch_current_usage("a")
    assert isinstance(raw, dict)
    assert len(session.calls) == 4


async def test_budget_cannot_raise_hard_twelve_request_ceiling() -> None:
    session = FakeSession(*(FakeResponse({"accounts": []}) for _ in range(13)))
    subject = authenticated(session)
    budget = api.RequestBudget(limit=100)
    for _ in range(12):
        await subject.async_get_accounts(budget)
    with pytest.raises(PolicyError):
        await subject.async_get_accounts(budget)
    assert len(session.calls) == 12


async def test_remote_token_with_control_character_never_reaches_request_headers(
    caplog: Any,
) -> None:
    session = FakeSession(
        FakeResponse({"clientId": "client"}),
        FakeResponse({"token": "secret-token\nforged"}),
        FakeResponse({"accounts": []}),
    )
    with pytest.raises(PayloadError) as caught:
        await client(session).async_get_accounts(api.RequestBudget())
    assert len(session.calls) == 2
    assert "secret-token" not in str(caught.value) + repr(caught.value) + caplog.text


async def test_session_value_error_is_sanitized(caplog: Any) -> None:
    session = FakeSession(ValueError("secret-header-987654"))
    with pytest.raises(EntergyError) as caught:
        await authenticated(session).async_get_accounts(api.RequestBudget())
    assert caught.value.category == ErrorCategory.TRANSIENT
    assert "secret-header" not in str(caught.value) + repr(caught.value) + caplog.text
