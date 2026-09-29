"""Fixed-origin transport boundary tests."""

from __future__ import annotations

import gzip
import json
import traceback
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
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
from yarl import URL


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
    session = FakeSession(
        FakeResponse({"secret": "secret-body"}, status=401),
        FakeResponse({"token": "replacement"}),
        FakeResponse({"secret": "secret-body"}, status=401),
    )
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


async def test_real_aiohttp_implicit_retry_cannot_send_request_thirteen(
    monkeypatch: Any, socket_enabled: None
) -> None:
    received = 0
    middleware_calls = 0

    async def handler(_: web.Request) -> web.Response:
        nonlocal received
        received += 1
        return web.json_response({"accounts": []})

    async def existing_middleware(
        request: aiohttp.ClientRequest,
        next_handler: Any,
    ) -> aiohttp.ClientResponse:
        nonlocal middleware_calls
        middleware_calls += 1
        response = await next_handler(request)
        if middleware_calls == 12:
            response.close()
            raise aiohttp.ServerDisconnectedError()
        return response

    app = web.Application()
    app.router.add_get("/api/accounts", handler)
    async with TestServer(app) as server:
        monkeypatch.setattr(api, "API_ORIGIN", str(server.make_url("/")).rstrip("/"))
        async with aiohttp.ClientSession(middlewares=(existing_middleware,)) as session:
            subject = authenticated(session)
            budget = api.RequestBudget()
            for _ in range(11):
                await subject.async_get_accounts(budget)
            with pytest.raises(PolicyError):
                await subject.async_get_accounts(budget)
            assert received == 12
            assert middleware_calls == 13
            assert budget.used == 12


async def test_dot_account_is_rejected_before_actual_yarl_path_normalization() -> None:
    assert URL("https://prod.entergy.mindgrb.io/api/accounts/.").path == "/api/accounts/"
    session = FakeSession()
    with pytest.raises(PolicyError):
        await authenticated(session).async_get_account(".", api.RequestBudget())
    assert session.calls == []


async def test_remote_timezone_parser_value_error_is_sanitized(caplog: Any) -> None:
    sentinel = "secret-zone-987654"
    session = FakeSession(
        FakeResponse({"accounts": [{"accountId": "valid", "timeZone": "../" + sentinel}]})
    )
    with pytest.raises(PayloadError) as caught:
        await authenticated(session).async_get_accounts(api.RequestBudget())
    exposed = "".join(traceback.format_exception(caught.value)) + repr(caught.value) + caplog.text
    assert sentinel not in exposed


async def test_real_gzip_decoded_limit_releases_response_without_leak(
    monkeypatch: Any, caplog: Any, socket_enabled: None
) -> None:
    sentinel = "secret-gzip-body-987654"
    decoded = json.dumps({"accounts": [], "padding": sentinel * 140000}).encode()
    compressed = gzip.compress(decoded)
    assert len(compressed) < 2 * 1024 * 1024 < len(decoded)
    assert len(compressed) > 2048
    responses: list[aiohttp.ClientResponse] = []
    trace = aiohttp.TraceConfig()

    async def saw_response(_: Any, __: Any, params: Any) -> None:
        responses.append(params.response)

    trace.on_request_end.append(saw_response)

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"}
        )
        response.enable_chunked_encoding()
        await response.prepare(request)
        for offset in range(0, len(compressed), 1024):
            await response.write(compressed[offset : offset + 1024])
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/api/accounts", handler)
    async with TestServer(app) as server:
        monkeypatch.setattr(api, "API_ORIGIN", str(server.make_url("/")).rstrip("/"))
        async with aiohttp.ClientSession(trace_configs=(trace,)) as session:
            with pytest.raises(PayloadError) as caught:
                await authenticated(session).async_get_accounts(api.RequestBudget())
            assert len(responses) == 1
            assert responses[0].headers["Transfer-Encoding"] == "chunked"
            assert "Content-Length" not in responses[0].headers
            assert responses[0].closed
            exposed = (
                "".join(traceback.format_exception(caught.value)) + repr(caught.value) + caplog.text
            )
            assert sentinel not in exposed

            monkeypatch.setattr(api, "_MAX_BODY_BYTES", len(decoded))
            assert await authenticated(session).async_get_accounts(api.RequestBudget()) == ()
            assert len(responses) == 2
            assert responses[1].closed


@pytest.mark.parametrize("nested", [False, True])
async def test_login_uses_reviewed_metadata_and_token_schemas(nested: bool) -> None:
    metadata = {"clientId": "synthetic-client"}
    login = {"accessToken": "synthetic-token"}
    session = FakeSession(
        FakeResponse({"data": metadata} if nested else metadata),
        FakeResponse({"data": login} if nested else login),
        FakeResponse({"accounts": []}),
    )
    subject = client(session)
    budget = api.RequestBudget()
    assert await subject.async_login(budget) is None
    assert subject.authenticated
    assert await subject.async_get_accounts(budget) == ()
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer 0"
    assert session.calls[1][2]["json"] == {"username": "secret-user", "password": "secret-password"}
    assert session.calls[2][2]["headers"]["Authorization"] == "Bearer synthetic-token"
    assert budget.used == 3
    subject.clear_token()
    assert not subject.authenticated
    assert subject.access_token is None


@pytest.mark.parametrize(
    "response,error",
    [
        (FakeResponse({}, status=401), AuthError),
        (FakeResponse({}, status=403), AuthError),
        (FakeResponse({}), PayloadError),
        (FakeResponse({"token": 123}), PayloadError),
        (FakeResponse({"token": "secret\nforged"}), PayloadError),
        (TimeoutError("secret"), EntergyError),
    ],
)
async def test_login_failure_clears_old_token_without_retry(
    response: FakeResponse | Exception, error: type[EntergyError]
) -> None:
    session = FakeSession(response)
    subject = authenticated(session)
    with pytest.raises(error):
        await subject.async_login(api.RequestBudget())
    assert subject.access_token is None
    assert not subject.authenticated
    assert len(session.calls) == 1
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer 0"


async def test_login_bad_client_metadata_stops_before_login() -> None:
    session = FakeSession(FakeResponse({"client_id": "unreviewed"}))
    subject = client(session)
    with pytest.raises(PayloadError):
        await subject.async_get_accounts(api.RequestBudget())
    assert not subject.authenticated
    assert len(session.calls) == 1


@pytest.mark.parametrize("challenge", ["MFA", "CAPTCHA", "consent", "unknown"])
@pytest.mark.parametrize("token_at_root", [False, True])
async def test_token_plus_unknown_challenge_never_authenticates(
    challenge: str, token_at_root: bool, caplog: Any
) -> None:
    attacker = "https://attacker.invalid/secret-challenge"
    action = {"steps": [{"nextAction": {"type": challenge, "url": attacker}}]}
    payload = (
        {"token": "secret-token", "data": action}
        if token_at_root
        else {"data": {"token": "secret-token"}, "flow": action}
    )
    session = FakeSession(
        FakeResponse({"clientId": "client"}),
        FakeResponse(payload),
        FakeResponse({"accounts": []}),
    )
    subject = client(session)
    with pytest.raises(api.ChallengeError) as caught:
        await subject.async_get_accounts(api.RequestBudget())
    assert subject.access_token is None
    assert not subject.authenticated
    assert [urlsplit(url).path for _, url, _ in session.calls] == ["/api/app", "/api/login"]
    assert all(url.startswith(api.API_ORIGIN + "/api/") for _, url, _ in session.calls)
    assert attacker not in "".join(traceback.format_exception(caught.value)) + caplog.text


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("operation", ["accounts", "account", "usage"])
async def test_authenticated_operation_refreshes_once(status: int, operation: str) -> None:
    session = FakeSession(
        FakeResponse({}, status=status),
        FakeResponse({"token": "replacement"}),
        FakeResponse(
            {"accounts": []}
            if operation == "accounts"
            else {"accountId": "valid"}
            if operation == "account"
            else usage(1)
        ),
    )
    subject = authenticated(session)
    budget = api.RequestBudget()
    if operation == "accounts":
        assert await subject.async_get_accounts(budget) == ()
    elif operation == "account":
        assert (await subject.async_get_account("valid", budget)).account_id == "valid"
    else:
        assert len(await subject.async_get_weekly_usage("valid", date(2026, 9, 1), budget)) == 1
    assert subject.authenticated
    assert budget.used == 3
    assert [urlsplit(url).path for _, url, _ in session.calls][1] == "/api/login"
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer secret"
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer 0"
    assert session.calls[2][2]["headers"]["Authorization"] == "Bearer replacement"
    assert session.calls[0][1] == session.calls[2][1]
    assert session.calls[0][2]["params"] == session.calls[2][2]["params"]


@pytest.mark.parametrize("second_status", [401, 403])
async def test_second_auth_failure_clears_token_and_stops(second_status: int) -> None:
    session = FakeSession(
        FakeResponse({}, status=401),
        FakeResponse({"token": "replacement"}),
        FakeResponse({}, status=second_status),
    )
    subject = authenticated(session)
    with pytest.raises(AuthError) as caught:
        await subject.async_get_accounts(api.RequestBudget())
    assert caught.value.status == second_status
    assert subject.access_token is None
    assert len(session.calls) == 3


async def test_invalid_credentials_during_auth_refresh_never_loop() -> None:
    session = FakeSession(FakeResponse({}, status=403), FakeResponse({}, status=401))
    subject = authenticated(session)
    with pytest.raises(AuthError):
        await subject.async_get_accounts(api.RequestBudget())
    assert subject.access_token is None
    assert [urlsplit(url).path for _, url, _ in session.calls] == ["/api/accounts", "/api/login"]


@pytest.mark.parametrize("remaining", [1, 2])
async def test_request_budget_counts_recursive_auth_and_retries(remaining: int) -> None:
    session = FakeSession(
        *(FakeResponse({"accounts": []}) for _ in range(12 - remaining)),
        FakeResponse({}, status=401),
        FakeResponse({"token": "replacement"}),
    )
    subject = authenticated(session)
    budget = api.RequestBudget()
    for _ in range(12 - remaining):
        await subject.async_get_accounts(budget)
    with pytest.raises(PolicyError):
        await subject.async_get_accounts(budget)
    assert budget.used == 12
    assert len(session.calls) == 12
    assert sum(urlsplit(url).path == "/api/login" for _, url, _ in session.calls) <= 1
    assert subject.access_token is None
    assert not subject.authenticated


@pytest.mark.parametrize("failure", ["transport", "budget", "auth", "uninitialized"])
async def test_logout_clears_token_even_on_failure(failure: str) -> None:
    session = FakeSession(
        FakeResponse({}, status=401) if failure == "auth" else TimeoutError("secret")
    )
    subject = authenticated(session)
    if failure == "uninitialized":
        subject._client_id = None
    await subject.async_logout(api.RequestBudget(used=12 if failure == "budget" else 0))
    assert subject.access_token is None
    assert not subject.authenticated
    assert len(session.calls) == (1 if failure in ("auth", "transport") else 0)


async def test_challenge_during_auth_refresh_stops_before_account_retry() -> None:
    session = FakeSession(
        FakeResponse({}, status=401),
        FakeResponse({"token": "replacement", "data": {"nextAction": "MFA"}}),
    )
    subject = authenticated(session)
    with pytest.raises(api.ChallengeError):
        await subject.async_get_accounts(api.RequestBudget())
    assert subject.access_token is None
    assert [urlsplit(url).path for _, url, _ in session.calls] == ["/api/accounts", "/api/login"]


async def test_invalid_credentials_on_initial_login_prevent_account_call() -> None:
    session = FakeSession(FakeResponse({"clientId": "client"}), FakeResponse({}, status=401))
    subject = client(session)
    with pytest.raises(AuthError):
        await subject.async_get_accounts(api.RequestBudget())
    assert not subject.authenticated
    assert [urlsplit(url).path for _, url, _ in session.calls] == ["/api/app", "/api/login"]


async def test_token_is_cleared_when_initialization_fails_during_login() -> None:
    session = FakeSession(FakeResponse({"clientId": 123}))
    subject = authenticated(session)
    subject._client_id = None
    with pytest.raises(PayloadError):
        await subject.async_login(api.RequestBudget())
    assert not subject.authenticated
    assert len(session.calls) == 1


async def test_logout_success_clears_token_and_requires_new_login() -> None:
    session = FakeSession(
        FakeResponse({}), FakeResponse({"token": "replacement"}), FakeResponse({"accounts": []})
    )
    subject = authenticated(session)
    await subject.async_logout()
    assert not subject.authenticated
    assert await subject.async_get_accounts(api.RequestBudget()) == ()
    assert [urlsplit(url).path for _, url, _ in session.calls] == [
        "/api/logout",
        "/api/login",
        "/api/accounts",
    ]


async def test_budget_counts_initialization_login_and_failed_auth_recovery() -> None:
    session = FakeSession(
        FakeResponse({"clientId": "client"}),
        FakeResponse({"token": "token"}),
        FakeResponse({}, status=401),
    )
    subject = client(session)
    budget = api.RequestBudget(used=9)
    with pytest.raises(PolicyError):
        await subject.async_get_accounts(budget)
    assert not subject.authenticated
    assert budget.used == 12
    assert [urlsplit(url).path for _, url, _ in session.calls] == [
        "/api/app",
        "/api/login",
        "/api/accounts",
    ]
