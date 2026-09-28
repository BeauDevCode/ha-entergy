"""Fixed-origin, bounded transport for the reviewed Entergy operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import Enum
import json
import re

import aiohttp

from .const import API_ORIGIN, DEFAULT_APP_VERSION, DEFAULT_LANGUAGE
from .errors import (
    AuthError,
    ChallengeError,
    EntergyError,
    ErrorCategory,
    PayloadError,
    PolicyError,
    RateLimitError,
)
from .models import Account, ClientMetadata, Credentials, EnergyInterval, LoginResult
from .parser import parse_account, parse_accounts, parse_client_metadata, parse_login, parse_usage

_MAX_BODY_BYTES = 2 * 1024 * 1024
_TIMEOUT = aiohttp.ClientTimeout(connect=10, sock_read=20, total=30)
_ACCOUNT_SEGMENT = re.compile(r"[A-Za-z0-9._~-]{1,128}\Z", re.ASCII)
_DELTA_SECONDS = re.compile(r"-?\d+(?:\.\d+)?\Z", re.ASCII)
_HEADER_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Internal bridge for v0.1.1 callers. Task 10 removes these aliases and legacy paths.
EntergyApiError = EntergyError
EntergyAuthError = AuthError
EntergyMfaRequired = ChallengeError
EntergyLoginResult = LoginResult


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
        credentials: Credentials | None = None,
        *,
        language: str = DEFAULT_LANGUAGE,
        app_version: str = DEFAULT_APP_VERSION,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        # Internal, deprecated constructor bridge for v0.1.1 lifecycle/config callers.
        self._legacy_raw = credentials is None
        if credentials is None:
            if username is None or password is None:
                raise PolicyError from None
            credentials = Credentials(username, password)
        elif username is not None or password is not None:
            raise PolicyError from None
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

    def _budget(self, budget: RequestBudget | None) -> RequestBudget:
        # Internal, deprecated optional-budget bridge; new call sites pass one chain budget.
        return budget if budget is not None else RequestBudget()

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
            ):
                raise PolicyError from None
        elif account_id is not None:
            raise PolicyError from None
        if operation is ApiOperation.WEEKLY_USAGE:
            if not isinstance(start_date, date) or isinstance(start_date, datetime):
                raise PolicyError from None
        elif start_date is not None:
            raise PolicyError from None
        if operation in (ApiOperation.ACCOUNTS, ApiOperation.ACCOUNT, ApiOperation.WEEKLY_USAGE):
            if self._client_id is None:
                await self.async_initialize(budget)
            if self._access_token is None:
                await self.async_login(budget)
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

    async def async_initialize(self, budget: RequestBudget | None = None) -> ClientMetadata:
        """Load client metadata through the bounded transport."""
        payload = await self._request_json(ApiOperation.APP, self._budget(budget))
        result = parse_client_metadata(payload)
        self._client_id = result.client_id
        return result

    async def async_login(self, budget: RequestBudget | None = None) -> LoginResult:
        """Authenticate once; Task 4 owns token refresh policy."""
        chain = self._budget(budget)
        if self._client_id is None:
            await self.async_initialize(chain)
        payload = await self._request_json(ApiOperation.LOGIN, chain)
        result = parse_login(payload)
        self._access_token = result.access_token
        return result

    async def async_logout(self, budget: RequestBudget | None = None) -> None:
        """Best-effort logout for an initialized session."""
        if self._client_id is None:
            return
        try:
            await self._request_json(ApiOperation.LOGOUT, self._budget(budget))
        except EntergyError:
            pass
        finally:
            self._access_token = None

    async def async_get_accounts(
        self, budget: RequestBudget | None = None
    ) -> tuple[Account, ...] | object:
        """List strictly parsed accounts (legacy constructor returns raw payload)."""
        payload = await self._request_json(ApiOperation.ACCOUNTS, self._budget(budget))
        accounts = parse_accounts(payload)
        self._account_zones.update(
            {account.account_id: account.time_zone for account in accounts if account.time_zone}
        )
        return payload if self._legacy_raw else accounts

    async def async_get_account(
        self, account_id: str, budget: RequestBudget | None = None
    ) -> Account | object:
        """Confirm one account identity."""
        payload = await self._request_json(
            ApiOperation.ACCOUNT, self._budget(budget), account_id=account_id
        )
        account = parse_account(payload, account_id)
        if account.time_zone:
            self._account_zones[account_id] = account.time_zone
        return payload if self._legacy_raw else account

    async def async_get_weekly_usage(
        self, account_id: str, start_date: date, budget: RequestBudget | None = None
    ) -> tuple[EnergyInterval, ...] | object:
        """Fetch one weekly page and validate at most 512 normalized intervals."""
        payload = await self._request_json(
            ApiOperation.WEEKLY_USAGE,
            self._budget(budget),
            account_id=account_id,
            start_date=start_date,
        )
        intervals = parse_usage(
            payload,
            source_time_zone=self._account_zones.get(account_id, "America/Chicago"),
            received_at=datetime.now(UTC),
        )
        if len(intervals) > 512:
            raise PayloadError from None
        return payload if self._legacy_raw else intervals

    async def async_fetch_current_usage(
        self, account_id: str, budget: RequestBudget | None = None
    ) -> object:
        """Internal, deprecated six-day wrapper for the v0.1.1 coordinator."""
        return await self.async_get_weekly_usage(
            account_id, date.today() - timedelta(days=6), self._budget(budget)
        )


def _retry_after(value: str | None) -> float | None:
    """Parse a delta or HTTP date without exposing the server-supplied text."""
    if value is None:
        return None
    if _DELTA_SECONDS.fullmatch(value):
        try:
            return min(max(float(value), 0.0), 86400.0)
        except ValueError, OverflowError:
            return None
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            return None
        return min(max((target - datetime.now(UTC)).total_seconds(), 0.0), 86400.0)
    except TypeError, ValueError, OverflowError:
        return None
