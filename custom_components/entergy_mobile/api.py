"""Fixed-origin, bounded transport for the reviewed Entergy operations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from enum import Enum
import json
import re

import aiohttp

from .const import API_ORIGIN, DEFAULT_APP_VERSION, DEFAULT_LANGUAGE
from .errors import (
    AuthError,
    ChallengeError as ChallengeError,
    EntergyError,
    ErrorCategory,
    PayloadError,
    PolicyError,
    RateLimitError,
)
from .models import Account, ClientMetadata, Credentials, EnergyInterval
from .parser import parse_account, parse_accounts, parse_client_metadata, parse_login, parse_usage

_MAX_BODY_BYTES = 2 * 1024 * 1024
_TIMEOUT = aiohttp.ClientTimeout(connect=10, sock_read=20, total=30)
_ACCOUNT_SEGMENT = re.compile(r"[A-Za-z0-9._~-]{1,128}\Z", re.ASCII)
_DELTA_SECONDS = re.compile(r"-?\d+(?:\.\d+)?\Z", re.ASCII)
_HEADER_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ApiOperation(Enum):
    """The complete reviewed V1 method/path set."""

    APP = ("GET", "/api/app")
    LOGIN = ("POST", "/api/login")
    LOGOUT = ("POST", "/api/logout")
    ACCOUNTS = ("GET", "/api/accounts")
    ACCOUNT = ("GET", "/api/accounts/{account_id}")
    WEEKLY_USAGE = ("GET", "/api/accounts/{account_id}/weeklyusage")


@dataclass(slots=True)
class RequestBudget:
    """Bound all nested calls in one request chain."""

    limit: int = 12
    used: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.limit) is not int
            or self.limit < 0
            or type(self.used) is not int
            or not 0 <= self.used <= min(self.limit, 12)
        ):
            raise PolicyError from None

    def consume(self) -> None:
        """Reserve one network attempt before touching the session."""
        if self.used >= min(self.limit, 12):
            raise PolicyError from None
        self.used += 1


class EntergyApiClient:
    """Use only fixed operations on the reviewed HTTPS origin."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        credentials: Credentials,
        *,
        language: str = DEFAULT_LANGUAGE,
        app_version: str = DEFAULT_APP_VERSION,
    ) -> None:
        if (
            not isinstance(language, str)
            or not language
            or not isinstance(app_version, str)
            or not app_version
        ):
            raise PolicyError from None
        self._session = session
        self._credentials = credentials
        self._language = language
        self._app_version = app_version
        self._client_id: str | None = None
        self._access_token: str | None = None
        self._account_zones: dict[str, str] = {}

    @property
    def client_id(self) -> str | None:
        """Current reviewed client identifier, if initialized."""
        return self._client_id

    @property
    def access_token(self) -> str | None:
        """Current access token, if authenticated."""
        return self._access_token

    @property
    def authenticated(self) -> bool:
        """Whether a fully validated login token is currently held in memory."""
        return self._access_token is not None

    def clear_token(self) -> None:
        """Discard authentication without making a network request."""
        self._access_token = None

    def _headers(self, operation: ApiOperation) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if operation is not ApiOperation.APP:
            if self._client_id is None:
                raise PolicyError from None
            bearer = self._access_token or "0"
            if _HEADER_CONTROL.search(self._client_id) or _HEADER_CONTROL.search(bearer):
                raise PayloadError from None
            headers["clientId"] = self._client_id
            headers["Authorization"] = f"Bearer {bearer}"
        return headers

    async def _request_json(
        self,
        operation: ApiOperation,
        budget: RequestBudget,
        *,
        account_id: str | None = None,
        start_date: date | None = None,
    ) -> object:
        """Issue one allowlisted request and read at most 2 MiB of decoded JSON."""
        if not isinstance(operation, ApiOperation):
            raise PolicyError from None
        if operation in (ApiOperation.ACCOUNT, ApiOperation.WEEKLY_USAGE):
            if (
                not isinstance(account_id, str)
                or not _ACCOUNT_SEGMENT.fullmatch(account_id)
                or ".." in account_id
                or account_id == "."
            ):
                raise PolicyError from None
        elif account_id is not None:
            raise PolicyError from None
        if operation is ApiOperation.WEEKLY_USAGE:
            if not isinstance(start_date, date) or isinstance(start_date, datetime):
                raise PolicyError from None
        elif start_date is not None:
            raise PolicyError from None
        requires_auth = operation in (
            ApiOperation.ACCOUNTS,
            ApiOperation.ACCOUNT,
            ApiOperation.WEEKLY_USAGE,
        )
        if requires_auth and not self.authenticated:
            await self.async_login(budget)
        try:
            return await self._send_json(
                operation, budget, account_id=account_id, start_date=start_date
            )
        except AuthError:
            self.clear_token()
            if not requires_auth:
                raise

        # One recovery only: login and retry spend the original chain budget.
        recovered = False
        try:
            await self.async_login(budget)
            payload = await self._send_json(
                operation, budget, account_id=account_id, start_date=start_date
            )
            recovered = True
            return payload
        finally:
            if not recovered:
                self.clear_token()

    async def _send_json(
        self,
        operation: ApiOperation,
        budget: RequestBudget,
        *,
        account_id: str | None,
        start_date: date | None,
    ) -> object:
        """Send one validated operation through the fixed transport boundary."""
        method, path = operation.value
        if account_id is not None:
            path = path.replace("{account_id}", account_id)
        params = {"appVersion": self._app_version, "language": self._language}
        if operation is ApiOperation.WEEKLY_USAGE:
            assert start_date is not None
            params.update({"view": "day", "startDate": start_date.isoformat()})
        headers = self._headers(operation)
        if operation is ApiOperation.LOGIN:
            headers["Content-Type"] = "application/json"
        budget.consume()
        attempts = 0

        async def count_attempt(
            request: aiohttp.ClientRequest,
            handler: Callable[[aiohttp.ClientRequest], Awaitable[aiohttp.ClientResponse]],
        ) -> aiohttp.ClientResponse:
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                budget.consume()
            return await handler(request)

        # aiohttp per-request middleware replaces the session list, so compose explicitly.
        # Placing this innermost counts retries made by session middleware too.
        session_middlewares = getattr(self._session, "_middlewares", ()) or ()
        try:
            async with self._session.request(
                method,
                API_ORIGIN + path,
                params=params,
                headers=headers,
                json={
                    "username": self._credentials.username,
                    "password": self._credentials.password,
                }
                if operation is ApiOperation.LOGIN
                else None,
                timeout=_TIMEOUT,
                allow_redirects=False,
                auto_decompress=True,
                middlewares=(*session_middlewares, count_attempt),
            ) as response:
                status = response.status
                if 300 <= status < 400:
                    raise PolicyError from None
                if status in (401, 403):
                    raise AuthError(status) from None
                if status == 429:
                    raise RateLimitError(
                        status, _retry_after(response.headers.get("Retry-After"))
                    ) from None
                if status >= 400:
                    raise EntergyError(ErrorCategory.TRANSIENT, status) from None
                media_type = (
                    response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                )
                if media_type != "application/json" and not (
                    media_type.startswith("application/") and media_type.endswith("+json")
                ):
                    raise PayloadError from None
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    if len(body) + len(chunk) > _MAX_BODY_BYTES:
                        raise PayloadError from None
                    body.extend(chunk)
        except TimeoutError, aiohttp.ClientError, OSError, ValueError:
            raise EntergyError(ErrorCategory.TRANSIENT) from None
        try:
            return json.loads(body)
        except UnicodeDecodeError, json.JSONDecodeError, ValueError:
            raise PayloadError from None

    async def async_initialize(self, budget: RequestBudget) -> ClientMetadata:
        """Load client metadata through the bounded transport."""
        payload = await self._request_json(ApiOperation.APP, budget)
        result = _parse_reviewed(lambda: parse_client_metadata(payload))
        self._client_id = result.client_id
        return result

    async def async_login(self, budget: RequestBudget) -> None:
        """Authenticate once, retaining nothing from any failed login attempt."""
        self.clear_token()
        chain = budget
        if self._client_id is None:
            await self.async_initialize(chain)
        payload = await self._request_json(ApiOperation.LOGIN, chain)
        result = _parse_reviewed(lambda: parse_login(payload))
        if _HEADER_CONTROL.search(result.access_token):
            raise PayloadError from None
        self._access_token = result.access_token

    async def async_logout(self, budget: RequestBudget) -> None:
        """Best-effort logout for an initialized session."""
        try:
            if self._client_id is not None:
                await self._request_json(ApiOperation.LOGOUT, budget)
        except EntergyError:
            pass
        finally:
            self.clear_token()

    async def async_get_accounts(self, budget: RequestBudget) -> tuple[Account, ...]:
        """List strictly parsed accounts."""
        payload = await self._request_json(ApiOperation.ACCOUNTS, budget)
        accounts = _parse_reviewed(lambda: parse_accounts(payload))
        self._account_zones.update(
            {account.account_id: account.time_zone for account in accounts if account.time_zone}
        )
        return accounts

    async def async_get_account(self, account_id: str, budget: RequestBudget) -> Account:
        """Confirm one account identity."""
        payload = await self._request_json(ApiOperation.ACCOUNT, budget, account_id=account_id)
        account = _parse_reviewed(lambda: parse_account(payload, account_id))
        if account.time_zone:
            self._account_zones[account_id] = account.time_zone
        return account

    async def async_get_weekly_usage(
        self,
        account_id: str,
        start_date: date,
        budget: RequestBudget,
        *,
        fallback_time_zone: str = "America/Chicago",
    ) -> tuple[EnergyInterval, ...]:
        """Fetch one weekly page and validate at most 512 normalized intervals."""
        payload = await self._request_json(
            ApiOperation.WEEKLY_USAGE,
            budget,
            account_id=account_id,
            start_date=start_date,
        )
        intervals = _parse_reviewed(
            lambda: parse_usage(
                payload,
                source_time_zone=self._account_zones.get(account_id) or fallback_time_zone,
                received_at=datetime.now(UTC),
            )
        )
        if len(intervals) > 512:
            raise PayloadError from None
        return intervals


def _parse_reviewed[T](parser: Callable[[], T]) -> T:
    """Keep unanticipated data-derived parser failures value-free at the API edge."""
    try:
        return parser()
    except EntergyError:
        raise
    except Exception:
        raise PayloadError from None


def _retry_after(value: str | None) -> float | None:
    """Parse a delta or HTTP date without exposing the server-supplied text."""
    if value is None:
        return None
    if _DELTA_SECONDS.fullmatch(value):
        # The ASCII decimal grammar guarantees float conversion; arbitrarily
        # large magnitudes become infinity and are clamped by the same bounds.
        return min(max(float(value), 0.0), 86400.0)
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            return None
        return min(max((target - datetime.now(UTC)).total_seconds(), 0.0), 86400.0)
    except TypeError, ValueError, OverflowError:
        return None
