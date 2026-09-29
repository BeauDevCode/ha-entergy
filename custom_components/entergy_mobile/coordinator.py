"""Bounded polling, Recorder recovery, and low-rate historical backfill."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
import logging
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EntergyApiClient, RequestBudget
from .const import (
    CONF_ACCOUNT_ID,
    CONF_SCAN_INTERVAL_SECONDS,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    MAX_SCAN_INTERVAL_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
)
from .errors import (
    AuthError,
    ChallengeError,
    EntergyError,
    ErrorCategory,
    PayloadError,
    PolicyError,
    RateLimitError,
)
from .ledger import EntergyLedger, LedgerRepairError, reconcile
from .models import Account, EnergyInterval, LedgerMutation, LedgerState, UsageSnapshot
from .parser import summarize_usage
from .statistics import (
    StatisticsBatch,
    async_queue_external_statistics,
    async_verify_queued_statistics,
    build_hourly_statistics,
    statistic_ids,
    statistics_fingerprint,
)

_LOGGER = logging.getLogger(__name__)

_NORMAL_DAYS = 45
_NORMAL_PAGES = 7
_PAGE_DAYS = 7
_BACKFILL_DAYS = 370
_BACKFILL_INTERVAL = 30 * 60
_BACKOFF = (3600, 7200, 14_400, 28_800, 86_400)

type _DiagnosticsValue = str | int | bool | None | list[str]


class _Clock(Protocol):
    """Cancellable time source; tests provide a deterministic implementation."""

    def now(self) -> datetime:
        """Return aware UTC now."""

    async def sleep(self, seconds: float) -> None:
        """Sleep without shielding cancellation."""


class _SystemClock:
    def now(self) -> datetime:
        now = dt_util.utcnow()
        return now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _PriorityGate:
    """A cancel-safe single-flight gate where queued normal work wins."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active = False
        self._normal_waiters = 0

    @asynccontextmanager
    async def hold(self, *, normal: bool) -> AsyncIterator[None]:
        async with self._condition:
            if normal:
                self._normal_waiters += 1
                try:
                    await self._condition.wait_for(lambda: not self._active)
                    self._active = True
                finally:
                    self._normal_waiters -= 1
                    self._condition.notify_all()
            else:
                await self._condition.wait_for(
                    lambda: not self._active and self._normal_waiters == 0
                )
                self._active = True
        try:
            yield
        finally:
            async with self._condition:
                self._active = False
                self._condition.notify_all()


def _utc_now(clock: _Clock) -> datetime:
    now = clock.now()
    if now.tzinfo is None:
        raise ValueError("clock must return aware UTC")
    return now.astimezone(UTC)


def _local_midnight_utc(value: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(value, time.min, zone).astimezone(UTC)


def _through(batches: Sequence[StatisticsBatch]) -> datetime | None:
    hours = [row["start"] for _, rows in batches for row in rows]
    return max(hours) if hours else None


def _monetary_allowed(state: LedgerState, home_currency: str) -> bool:
    return home_currency == "USD" and all(
        item.currency in (None, "USD") for item in state.intervals
    )


class EntergyDataUpdateCoordinator(DataUpdateCoordinator[UsageSnapshot]):
    """Coordinate atomic utility reconciliation without starving normal polls."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EntergyApiClient,
        ledger: EntergyLedger,
        *,
        time_zone: str,
        _clock: _Clock | None = None,
        _jitter: Callable[[], float] | None = None,
    ) -> None:
        configured = int(
            entry.options.get(CONF_SCAN_INTERVAL_SECONDS, DEFAULT_SCAN_INTERVAL_SECONDS)
        )
        self._configured_poll_seconds = max(
            MIN_SCAN_INTERVAL_SECONDS, min(MAX_SCAN_INTERVAL_SECONDS, configured)
        )
        super().__init__(
            hass,
            logger=_LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=self._configured_poll_seconds),
            always_update=False,
        )
        self._entry = entry
        self._client = client
        self._ledger = ledger
        self._account_id = str(entry.data[CONF_ACCOUNT_ID])
        self._public_id = str(entry.data["public_id"])
        self._time_zone = time_zone
        self._zone = ZoneInfo(time_zone)
        self._clock = _clock or _SystemClock()
        self._jitter = _jitter or __import__("random").random
        self._gate = _PriorityGate()
        self._next_poll_seconds: float | None = float(self._configured_poll_seconds)
        self._normal_failures = 0
        self._normal_network_healthy = False
        self._backfill_failures = 0
        self._backfill_delay = float(_BACKFILL_INTERVAL)
        self._backfill_pages = 0
        self._backfill_task: asyncio.Task[None] | None = None
        self._active_chain_tasks: set[asyncio.Task[Any]] = set()
        self._shutdown_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._initialized = False
        self._retained_listener_unsub: Callable[[], None] | None = None
        self._last_successful_fetch: datetime | None = None
        self._last_backfill_progress: datetime | None = None
        self._backfill_started_at: datetime | None = None
        self._backfill_stalled = False
        self._condition: str | None = None
        self._repair_conditions: set[str] = set()
        self._last_inserted = 0
        self._last_corrected = 0
        self._notification_generation = 0
        self._notified_generation = 0
        self._last_notified_status = self._status_signature()

    def _base_disabled(self) -> bool:
        return (
            self._stopped
            or self._entry.disabled_by is not None
            or self._entry.pref_disable_polling
            or self.hass.is_stopping
        )

    def _normal_request_allowed(self) -> bool:
        return not self._base_disabled() and self._entry.state in {
            ConfigEntryState.SETUP_IN_PROGRESS,
            ConfigEntryState.LOADED,
        }

    def _backfill_request_allowed(self) -> bool:
        return not self._base_disabled() and self._entry.state is ConfigEntryState.LOADED

    def _backfill_worker_allowed(self) -> bool:
        return not self._base_disabled() and self._entry.state in {
            ConfigEntryState.SETUP_IN_PROGRESS,
            ConfigEntryState.LOADED,
        }

    def _snapshot(self, state: LedgerState) -> UsageSnapshot:
        snapshot = summarize_usage(state, time_zone=self._time_zone, now=_utc_now(self._clock))
        if self.hass.config.currency == "USD":
            return snapshot
        return replace(
            snapshot,
            latest_cost=None,
            today_cost=None,
            seven_day_cost=None,
            month_cost=None,
            latest_compensation=None,
            today_compensation=None,
            seven_day_compensation=None,
            month_compensation=None,
        )

    def _status_signature(self) -> tuple[str | None, tuple[str, ...], bool, int | None]:
        return (
            self._condition,
            tuple(sorted(self._repair_conditions)),
            self._backfill_stalled,
            int(self._next_poll_seconds) if self._next_poll_seconds is not None else None,
        )

    def _advance_notification(self, *, force: bool = False) -> int | None:
        status = self._status_signature()
        if not force and status == self._last_notified_status:
            return None
        self._last_notified_status = status
        self._notification_generation += 1
        return self._notification_generation

    @callback
    def async_update_listeners(self) -> None:
        """Track base notifications so deferred status callbacks do not duplicate them."""
        self._notified_generation = self._notification_generation
        super().async_update_listeners()

    @callback
    def _deliver_deferred_notification(self, generation: int) -> None:
        if self._stopped or generation <= self._notified_generation:
            return
        self.async_update_listeners()

    def _notify_status_if_changed(self, *, force: bool = False) -> None:
        if self._advance_notification(force=force) is not None:
            self.async_update_listeners()

    def _defer_status_notification(self, *, force: bool = False) -> None:
        generation = self._advance_notification(force=force)
        if generation is not None:
            self.hass.loop.call_soon(self._deliver_deferred_notification, generation)

    def _restore_normal_interval(self) -> None:
        self._next_poll_seconds = (
            self.update_interval.total_seconds()
            if self.update_interval is not None
            else float(self._configured_poll_seconds)
        )

    async def async_initialize(self) -> None:
        """Load only verified local state; Task 10 owns the entry marker."""
        if self._initialized:
            return
        state = await self._ledger.async_load(
            initialized=bool(self._entry.data.get("ledger_initialized", False))
        )
        if (
            state.statistics_pending_from is None
            and state.statistics_pending_fingerprint is not None
        ):
            raise LedgerRepairError from None
        if state.statistics_pending_from is not None:
            batches = self._batches(state, state.statistics_pending_from)
            through = _through(batches)
            if through is None:
                raise LedgerRepairError from None
            fingerprint = statistics_fingerprint(batches)
            if (
                state.statistics_pending_fingerprint is not None
                and state.statistics_pending_fingerprint
                not in (
                    fingerprint,
                    self._alternate_pending_fingerprint(state, state.statistics_pending_from),
                )
            ):
                raise LedgerRepairError from None
        self.data = self._snapshot(state)
        today = _utc_now(self._clock).astimezone(self._zone).date()
        if state.backfill_cursor is not None:
            distance = max(
                0,
                (
                    today + timedelta(days=1) - state.backfill_cursor.astimezone(self._zone).date()
                ).days,
            )
            self._backfill_pages = min(53, (distance + _PAGE_DAYS - 1) // _PAGE_DAYS)
        # External statistics must continue polling even if every entity is disabled.
        self._retained_listener_unsub = self.async_add_listener(lambda: None)
        self._initialized = True

    def _normal_delay(self, error: EntergyError) -> float:
        self._normal_failures += 1
        if error.retry_after is not None:
            delay = error.retry_after
        else:
            delay = float(_BACKOFF[min(self._normal_failures - 1, len(_BACKOFF) - 1)])
        self._next_poll_seconds = delay
        return delay

    def _raise_update(self, error: EntergyError, *, network_unhealthy: bool = True) -> None:
        self._condition = error.category.value
        if network_unhealthy:
            self._normal_network_healthy = False
        delay = self._normal_delay(error)
        self._refresh_backfill_stalled()
        self._defer_status_notification()
        raise UpdateFailed(error.category.value, retry_after=delay) from None

    @asynccontextmanager
    async def _track_chain(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("request chain requires an asyncio task")
        self._active_chain_tasks.add(task)
        try:
            yield
        finally:
            self._active_chain_tasks.discard(task)

    def _batches(self, state: LedgerState, start: datetime) -> tuple[StatisticsBatch, ...]:
        return build_hourly_statistics(
            state,
            public_id=self._public_id,
            currency=self.hass.config.currency,
            start=start,
        )

    def _alternate_pending_fingerprint(self, state: LedgerState, start: datetime) -> str:
        """Accept only the other reviewed USD/energy-only payload for this suffix."""
        alternate_currency = "EUR" if self.hass.config.currency == "USD" else "USD"
        alternate = build_hourly_statistics(
            state, public_id=self._public_id, currency=alternate_currency, start=start
        )
        return statistics_fingerprint(alternate)

    def _use_confirmed_account_zone(self, account: Account) -> None:
        if account.time_zone is None or account.time_zone == self._time_zone:
            return
        try:
            zone = ZoneInfo(account.time_zone)
        except ValueError, ZoneInfoNotFoundError:
            return
        self._time_zone = account.time_zone
        self._zone = zone

    async def _async_recover_pending_statistics(self) -> None:
        state = self._ledger.state
        pending = state.statistics_pending_from
        if pending is None:
            return
        batches = self._batches(state, pending)
        fingerprint = statistics_fingerprint(batches)
        through = _through(batches)
        if through is None:
            self._condition = "ledger_repair"
            self._repair_conditions.add("ledger_repair")
            raise UpdateFailed("ledger") from None
        if (
            state.statistics_pending_fingerprint is None
            or state.statistics_pending_fingerprint != fingerprint
            and state.statistics_pending_fingerprint
            == self._alternate_pending_fingerprint(state, pending)
        ):
            mutation = await self._ledger.async_mark_statistics_pending(
                from_hour=pending, fingerprint=fingerprint
            )
            if mutation.deferred:
                self._condition = mutation.repair or "ledger_repair"
                self._repair_conditions.add(self._condition)
                raise UpdateFailed("ledger") from None
            state = self._ledger.state
            batches = self._batches(state, pending)
        elif state.statistics_pending_fingerprint != fingerprint:
            self._condition = "ledger_repair"
            self._repair_conditions.add("ledger_repair")
            raise UpdateFailed("ledger") from None
        if await async_verify_queued_statistics(
            self.hass,
            ids=statistic_ids(self._public_id),
            expected_batches=batches,
            through=through,
        ):
            mutation = await self._ledger.async_mark_statistics_verified(
                through=through, fingerprint=fingerprint
            )
            if mutation.deferred:
                self._condition = mutation.repair or "ledger_repair"
                self._repair_conditions.add(self._condition)
                raise UpdateFailed("ledger") from None
            return
        await async_queue_external_statistics(self.hass, batches)

    def _with_pending_statistics(self, mutation: LedgerMutation) -> LedgerMutation:
        changed = mutation.earliest_statistics_hour
        if changed is None:
            return mutation
        existing = mutation.state.statistics_pending_from
        pending = min(existing, changed) if existing is not None else changed
        batches = self._batches(mutation.state, pending)
        state = replace(
            mutation.state,
            statistics_pending_from=pending,
            statistics_pending_fingerprint=statistics_fingerprint(batches),
        )
        return replace(mutation, state=state)

    async def _async_persist_and_queue(self, mutation: LedgerMutation) -> bool:
        if mutation.state is self._ledger.state:
            return True
        prepared = self._with_pending_statistics(mutation)
        persisted = await self._ledger.async_ingest(prepared)
        if persisted.deferred:
            self._condition = persisted.repair or "ledger_repair"
            self._repair_conditions.add(self._condition)
            return False
        if prepared.earliest_statistics_hour is not None:
            pending = self._ledger.state.statistics_pending_from
            if pending is None:
                self._condition = "ledger_repair"
                self._repair_conditions.add("ledger_repair")
                return False
            await async_queue_external_statistics(
                self.hass, self._batches(self._ledger.state, pending)
            )
        return True

    def _normal_starts(self, today: date) -> tuple[date, ...]:
        first = today - timedelta(days=_NORMAL_DAYS - 1)
        return tuple(first + timedelta(days=_PAGE_DAYS * page) for page in range(_NORMAL_PAGES))

    def _publish_verified_normal(self, mutation: LedgerMutation, now: datetime) -> UsageSnapshot:
        self._last_inserted = mutation.inserted
        self._last_corrected = mutation.corrected
        self._last_successful_fetch = now
        self._repair_conditions.discard("schema_drift")
        self._repair_conditions.discard("data_retraction")
        self._repair_conditions.discard("ledger_repair")
        if _monetary_allowed(self._ledger.state, self.hass.config.currency):
            self._repair_conditions.discard("currency_mismatch")
            self._condition = None
        else:
            self._repair_conditions.add("currency_mismatch")
            self._condition = "currency_mismatch"
        self._normal_failures = 0
        jitter = min(1.0, max(0.0, float(self._jitter())))
        next_seconds = self._configured_poll_seconds * (1 + jitter * 0.1)
        self._next_poll_seconds = next_seconds
        self.update_interval = timedelta(seconds=next_seconds)
        snapshot = self._snapshot(self._ledger.state)
        self.data = snapshot
        self._refresh_backfill_stalled()
        self._defer_status_notification(force=True)
        return snapshot

    async def _async_normal_chain(self) -> UsageSnapshot:
        await self._async_recover_pending_statistics()
        now = _utc_now(self._clock)
        budget = RequestBudget()
        account = await self._client.async_get_account(self._account_id, budget)
        self._use_confirmed_account_zone(account)
        today = now.astimezone(self._zone).date()
        floor = today - timedelta(days=_NORMAL_DAYS - 1)
        pages: list[EnergyInterval] = []
        for start in self._normal_starts(today):
            page = await self._client.async_get_weekly_usage(
                self._account_id,
                start,
                budget,
                fallback_time_zone=self._time_zone,
            )
            pages.extend(page)
        self._normal_network_healthy = True
        incoming = tuple(
            item for item in pages if floor <= item.start.astimezone(self._zone).date() <= today
        )
        mutation = reconcile(self._ledger.state, incoming, received_at=now)
        if mutation.deferred:
            self._condition = (
                "data_retraction" if mutation.repair == "usage_quarantine" else "ledger_repair"
            )
            self._repair_conditions.add(self._condition)
            self._restore_normal_interval()
            self._refresh_backfill_stalled()
            self._defer_status_notification()
            return self.data
        # Even an empty first response must establish a verified durable revision.
        if mutation.state is self._ledger.state and self._ledger.state.revision == 0:
            mutation = LedgerMutation(replace(self._ledger.state, revision=1))
        state_before = self._ledger.state
        published = False
        try:
            persisted = await self._async_persist_and_queue(mutation)
        finally:
            if self._ledger.state is not state_before:
                self._publish_verified_normal(mutation, now)
                published = True
        if not persisted:
            raise UpdateFailed("ledger") from None
        return self.data if published else self._publish_verified_normal(mutation, now)

    async def _async_update_data(self) -> UsageSnapshot:
        if not self._normal_request_allowed():
            return self.data
        try:
            async with self._track_chain(), self._gate.hold(normal=True):
                if not self._normal_request_allowed():
                    return self.data
                return await self._async_normal_chain()
        except (AuthError, ChallengeError) as error:
            self._condition = error.category.value
            self._normal_network_healthy = False
            self._next_poll_seconds = None
            self._refresh_backfill_stalled()
            self._defer_status_notification()
            raise ConfigEntryAuthFailed(error.category.value) from None
        except PayloadError:
            self._condition = "schema_drift"
            self._repair_conditions.add("schema_drift")
            self._normal_network_healthy = True
            self._restore_normal_interval()
            self._refresh_backfill_stalled()
            self._defer_status_notification()
            return self.data
        except PolicyError as error:
            self._condition = error.category.value
            self._restore_normal_interval()
            self._refresh_backfill_stalled()
            self._defer_status_notification()
            raise UpdateFailed(error.category.value) from None
        except RateLimitError as error:
            self._raise_update(error)
        except EntergyError as error:
            if error.category in (ErrorCategory.AUTH, ErrorCategory.CHALLENGE):
                self._condition = error.category.value
                self._normal_network_healthy = False
                self._next_poll_seconds = None
                self._refresh_backfill_stalled()
                self._defer_status_notification()
                raise ConfigEntryAuthFailed(error.category.value) from None
            if error.category is ErrorCategory.PAYLOAD:
                self._condition = "schema_drift"
                self._repair_conditions.add("schema_drift")
                self._normal_network_healthy = True
                self._restore_normal_interval()
                self._refresh_backfill_stalled()
                self._defer_status_notification()
                return self.data
            self._raise_update(error)
        except LedgerRepairError:
            self._condition = "ledger_repair"
            self._repair_conditions.add("ledger_repair")
            self._restore_normal_interval()
            self._refresh_backfill_stalled()
            self._defer_status_notification()
            raise UpdateFailed("ledger") from None
        except UpdateFailed as error:
            if error.retry_after is None:
                self._restore_normal_interval()
            self._defer_status_notification()
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            self._raise_update(EntergyError(ErrorCategory.TRANSIENT), network_unhealthy=False)
        raise AssertionError("unreachable")

    def _next_backfill_page(self, now: datetime) -> tuple[date, date, bool]:
        today = now.astimezone(self._zone).date()
        floor = today - timedelta(days=_BACKFILL_DAYS - 1)
        cursor = self._ledger.state.backfill_cursor
        cursor_date = (
            today + timedelta(days=1) if cursor is None else cursor.astimezone(self._zone).date()
        )
        start = max(floor, cursor_date - timedelta(days=_PAGE_DAYS))
        return start, floor, start == floor

    def _backfill_failure(self, error: EntergyError, *, condition: str | None = None) -> None:
        self._backfill_failures += 1
        delay = (
            error.retry_after
            if error.retry_after is not None
            else _BACKOFF[min(self._backfill_failures - 1, len(_BACKOFF) - 1)]
        )
        self._backfill_delay = float(max(_BACKFILL_INTERVAL, delay))
        self._condition = condition or error.category.value
        if self._condition in {
            "schema_drift",
            "data_retraction",
            "ledger_repair",
            "currency_mismatch",
        }:
            self._repair_conditions.add(self._condition)
        self._refresh_backfill_stalled()
        self._notify_status_if_changed()

    def _refresh_backfill_stalled(self, *, notify: bool = False) -> None:
        reference = self._last_backfill_progress or self._backfill_started_at
        stalled = bool(
            reference is not None
            and not self._ledger.state.backfill_complete
            and _utc_now(self._clock) - reference >= timedelta(hours=24)
            and self._normal_network_healthy
        )
        self._backfill_stalled = stalled
        if notify:
            self._notify_status_if_changed()

    def _publish_verified_backfill(self, mutation: LedgerMutation, now: datetime) -> UsageSnapshot:
        """Publish a durable backfill checkpoint, even if Recorder queueing failed."""
        self._backfill_failures = 0
        self._backfill_delay = float(_BACKFILL_INTERVAL)
        self._backfill_pages += 1
        self._last_inserted = mutation.inserted
        self._last_corrected = mutation.corrected
        self._last_successful_fetch = now
        self._last_backfill_progress = now
        self._backfill_stalled = False
        self._repair_conditions.discard("schema_drift")
        self._repair_conditions.discard("data_retraction")
        self._repair_conditions.discard("ledger_repair")
        if _monetary_allowed(self._ledger.state, self.hass.config.currency):
            self._repair_conditions.discard("currency_mismatch")
            self._condition = None
        else:
            self._repair_conditions.add("currency_mismatch")
            self._condition = "currency_mismatch"
        snapshot = self._snapshot(self._ledger.state)
        self.data = snapshot
        self._notify_status_if_changed(force=True)
        return snapshot

    async def async_run_backfill_once(self) -> bool:
        """Fetch and checkpoint exactly one older weekly page."""
        if not self._backfill_request_allowed() or self._ledger.state.backfill_complete:
            return False
        try:
            async with self._track_chain(), self._gate.hold(normal=False):
                if not self._backfill_request_allowed() or self._ledger.state.backfill_complete:
                    return False
                await self._async_recover_pending_statistics()
                now = _utc_now(self._clock)
                budget = RequestBudget()
                account = await self._client.async_get_account(self._account_id, budget)
                self._use_confirmed_account_zone(account)
                start, floor, complete = self._next_backfill_page(now)
                incoming = await self._client.async_get_weekly_usage(
                    self._account_id,
                    start,
                    budget,
                    fallback_time_zone=self._time_zone,
                )
                cursor_before = self._ledger.state.backfill_cursor
                cursor_date = (
                    now.astimezone(self._zone).date() + timedelta(days=1)
                    if cursor_before is None
                    else cursor_before.astimezone(self._zone).date()
                )
                bounded = tuple(
                    item
                    for item in incoming
                    if start <= item.start.astimezone(self._zone).date() < cursor_date
                )
                mutation = reconcile(
                    self._ledger.state,
                    bounded,
                    received_at=now,
                    backfill_cursor=_local_midnight_utc(start, self._zone),
                    backfill_complete=complete,
                )
                if mutation.deferred:
                    self._condition = (
                        "data_retraction"
                        if mutation.repair == "usage_quarantine"
                        else "ledger_repair"
                    )
                    self._backfill_failure(
                        EntergyError(ErrorCategory.PAYLOAD), condition=self._condition
                    )
                    return False
                state_before = self._ledger.state
                published = False
                try:
                    persisted = await self._async_persist_and_queue(mutation)
                finally:
                    if self._ledger.state is not state_before:
                        self._publish_verified_backfill(mutation, now)
                        published = True
                if not persisted:
                    self._backfill_failure(
                        EntergyError(ErrorCategory.LEDGER), condition="ledger_repair"
                    )
                    return False
                if not published:
                    self._publish_verified_backfill(mutation, now)
                return True
        except (AuthError, ChallengeError) as error:
            self._condition = error.category.value
            self._normal_network_healthy = False
            self._restore_normal_interval()
            self._refresh_backfill_stalled()
            self._notify_status_if_changed()
            self._entry.async_start_reauth_if_available(self.hass)
            raise ConfigEntryAuthFailed(error.category.value) from None
        except PayloadError:
            self._backfill_failure(PayloadError(), condition="schema_drift")
            return False
        except RateLimitError as error:
            self._backfill_failure(error)
            return False
        except EntergyError as error:
            if error.category in (ErrorCategory.AUTH, ErrorCategory.CHALLENGE):
                self._condition = error.category.value
                self._normal_network_healthy = False
                self._restore_normal_interval()
                self._refresh_backfill_stalled()
                self._notify_status_if_changed()
                self._entry.async_start_reauth_if_available(self.hass)
                raise ConfigEntryAuthFailed(error.category.value) from None
            self._backfill_failure(error)
            return False
        except LedgerRepairError:
            self._backfill_failure(EntergyError(ErrorCategory.LEDGER), condition="ledger_repair")
            return False
        except UpdateFailed:
            self._backfill_failure(EntergyError(ErrorCategory.LEDGER), condition="ledger_repair")
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            self._backfill_failure(EntergyError(ErrorCategory.TRANSIENT))
            return False

    async def _async_backfill_worker(self) -> None:
        delay = float(_BACKFILL_INTERVAL)
        self._backfill_started_at = _utc_now(self._clock)
        while self._backfill_worker_allowed() and not self._ledger.state.backfill_complete:
            await self._clock.sleep(delay)
            if not self._backfill_worker_allowed():
                break
            self._refresh_backfill_stalled(notify=True)
            if not self._backfill_request_allowed():
                delay = float(_BACKFILL_INTERVAL)
                continue
            try:
                progressed = await self.async_run_backfill_once()
            except ConfigEntryAuthFailed:
                break
            delay = float(_BACKFILL_INTERVAL) if progressed else self._backfill_delay

    async def async_start_backfill(self) -> None:
        """Start one idempotent low-rate worker."""
        if (
            not self._backfill_worker_allowed()
            or self._ledger.state.backfill_complete
            or (self._backfill_task is not None and not self._backfill_task.done())
        ):
            return
        self._backfill_task = self._entry.async_create_background_task(
            self.hass,
            self._async_backfill_worker(),
            name=f"{DOMAIN} historical backfill",
            eager_start=True,
        )

    async def async_shutdown(self) -> None:
        """Stop all work; concurrent callers await the same cleanup task."""
        if self._shutdown_task is None:
            self._stopped = True
            self._shutdown_task = asyncio.create_task(
                self._async_shutdown_impl(),
                name=f"{DOMAIN} coordinator shutdown",
            )
        await asyncio.shield(self._shutdown_task)

    async def _async_shutdown_impl(self) -> None:
        """Cancel custom workers and active request chains before base shutdown."""
        task, self._backfill_task = self._backfill_task, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        current = asyncio.current_task()
        chains = [
            task for task in self._active_chain_tasks if task is not current and not task.done()
        ]
        for chain in chains:
            chain.cancel()
        if chains:
            await asyncio.gather(*chains, return_exceptions=True)
        if self._retained_listener_unsub is not None:
            self._retained_listener_unsub()
            self._retained_listener_unsub = None
        await super().async_shutdown()

    def diagnostics(self) -> dict[str, _DiagnosticsValue]:
        """Return only explicitly reviewed primitives with no identity or payload data."""
        state = self._ledger.state
        snapshot = self.data
        repairs = set(self._repair_conditions)
        if self._backfill_stalled:
            repairs.add("backfill_stalled")
        progress = 100 if state.backfill_complete else min(99, self._backfill_pages * 100 // 53)
        return {
            "configured_poll_seconds": self._configured_poll_seconds,
            "next_poll_seconds": int(self._next_poll_seconds)
            if self._next_poll_seconds is not None
            else None,
            "last_successful_fetch": self._last_successful_fetch.isoformat()
            if self._last_successful_fetch is not None
            else None,
            "newest_interval": snapshot.newest_interval_start.isoformat()
            if snapshot.newest_interval_start is not None
            else None,
            "error_category": self._condition,
            "retained_interval_count": len(state.intervals),
            "estimated_interval_count": sum(item.is_estimated for item in state.intervals),
            "last_inserted_count": self._last_inserted,
            "last_corrected_count": self._last_corrected,
            "freshness": snapshot.freshness.value,
            "backfill_pages_completed": self._backfill_pages,
            "backfill_pages_total": 53,
            "backfill_progress_percent": progress,
            "backfill_complete": state.backfill_complete,
            "repair_conditions": sorted(repairs),
        }
