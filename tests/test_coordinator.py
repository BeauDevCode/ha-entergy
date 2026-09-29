"""Bounded polling, Recorder recovery, and historical backfill orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from custom_components.entergy_mobile import coordinator as module
from custom_components.entergy_mobile.errors import (
    AuthError,
    ChallengeError,
    EntergyError,
    ErrorCategory,
    PayloadError,
    RateLimitError,
)
from custom_components.entergy_mobile.models import (
    Account,
    EnergyInterval,
    Freshness,
    LedgerMutation,
    LedgerState,
)
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component import common  # type: ignore[import-untyped]

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
PUBLIC_ID = "a" * 32
ACCOUNT_ID = "synthetic-account"
CREATED: list[module.EntergyDataUpdateCoordinator] = []


def interval(
    start: datetime = NOW - timedelta(hours=1),
    *,
    energy: str = "1",
    amount: str | None = "0.25",
    currency: str | None = "USD",
    estimated: bool = False,
) -> EnergyInterval:
    return EnergyInterval(
        start=start,
        end=start + timedelta(hours=1),
        import_kwh=Decimal(energy),
        return_kwh=Decimal(0),
        amount=None if amount is None else Decimal(amount),
        currency=currency,
        is_estimated=estimated,
        received_at=NOW,
    )


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.value = now
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(0)


class ControlledClock(FakeClock):
    def __init__(self, now: datetime = NOW) -> None:
        super().__init__(now)
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.waiting.set()
        await self.release.wait()
        self.value += timedelta(seconds=seconds)


class FakeLedger:
    def __init__(self, state: LedgerState | None = None) -> None:
        self._state = state or LedgerState(schema_version=2)
        self.load_calls: list[bool] = []
        self.ingests: list[LedgerMutation] = []
        self.pending_calls: list[tuple[datetime, str]] = []
        self.verified_calls: list[tuple[datetime, str]] = []
        self.fail_ingest = False
        self.fail_pending = False

    @property
    def state(self) -> LedgerState:
        return self._state

    async def async_load(self, *, initialized: bool) -> LedgerState:
        self.load_calls.append(initialized)
        return self._state

    async def async_ingest(self, mutation: LedgerMutation) -> LedgerMutation:
        self.ingests.append(mutation)
        if self.fail_ingest:
            return LedgerMutation(self._state, deferred=True, repair="ledger_repair")
        if mutation.state is self._state:
            return mutation
        self._state = mutation.state
        return mutation

    async def async_mark_statistics_pending(
        self, *, from_hour: datetime, fingerprint: str
    ) -> LedgerMutation:
        self.pending_calls.append((from_hour, fingerprint))
        if self.fail_pending:
            return LedgerMutation(self._state, deferred=True, repair="ledger_repair")
        self._state = replace(
            self._state,
            revision=self._state.revision + 1,
            statistics_pending_from=from_hour,
            statistics_pending_fingerprint=fingerprint,
        )
        return LedgerMutation(self._state)

    async def async_mark_statistics_verified(
        self, *, through: datetime, fingerprint: str
    ) -> LedgerMutation:
        self.verified_calls.append((through, fingerprint))
        self._state = replace(
            self._state,
            revision=self._state.revision + 1,
            statistics_pending_from=None,
            statistics_pending_fingerprint=None,
            statistics_verified_through=through,
            statistics_verified_fingerprint=fingerprint,
        )
        return LedgerMutation(self._state)


class FakeClient:
    def __init__(
        self,
        pages: tuple[EnergyInterval, ...] = (),
        *,
        failure: Exception | None = None,
        failures: list[Exception | None] | None = None,
        consume_cold: bool = False,
        recovery_attempts: int = 0,
    ) -> None:
        self.pages = pages
        self.failure = failure
        self.failures = list(failures or [])
        self.consume_cold = consume_cold
        self.recovery_attempts = recovery_attempts
        self.account_time_zone: str | None = None
        self.calls: list[tuple[str, object, object]] = []
        self.fallback_zones: list[str] = []
        self.active = 0
        self.max_active = 0
        self.block: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def async_get_account(self, account_id: str, budget: Any) -> Account:
        self.calls.append(("account", account_id, budget))
        if self.consume_cold:
            for _ in range(3):
                budget.consume()
            self.consume_cold = False
        else:
            budget.consume()
        return Account(account_id, time_zone=self.account_time_zone)

    async def async_get_weekly_usage(
        self,
        account_id: str,
        start_date: date,
        budget: Any,
        *,
        fallback_time_zone: str,
    ) -> tuple[EnergyInterval, ...]:
        self.calls.append(("page", start_date, budget))
        self.fallback_zones.append(fallback_time_zone)
        budget.consume()
        if self.recovery_attempts:
            for _ in range(self.recovery_attempts):
                budget.consume()
            self.recovery_attempts = 0
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.block is not None and not self.block.is_set():
                self.block.set()
                assert self.release is not None
                await self.release.wait()
            if self.failures:
                failure = self.failures.pop(0)
                if failure is not None:
                    raise failure
            if self.failure is not None:
                raise self.failure
            return self.pages
        finally:
            self.active -= 1


def entry(
    hass: HomeAssistant,
    *,
    initialized: bool = True,
    disabled: bool = False,
    polling_disabled: bool = False,
    state: ConfigEntryState | None = ConfigEntryState.LOADED,
) -> Any:
    item = common.MockConfigEntry(
        domain="entergy_mobile",
        data={
            "username": "synthetic-user",
            "password": "synthetic-password",
            "account_id": ACCOUNT_ID,
            "public_id": PUBLIC_ID,
            "time_zone": "America/Chicago",
            "ledger_initialized": initialized,
        },
        options={"scan_interval_seconds": 14_400},
        disabled_by=ConfigEntryDisabler.USER if disabled else None,
        pref_disable_polling=polling_disabled,
        state=state,
    )
    item.add_to_hass(hass)
    hass.config.currency = "USD"
    return item


def coordinator(
    hass: HomeAssistant,
    client: FakeClient,
    ledger: FakeLedger,
    *,
    clock: FakeClock | None = None,
    jitter: Callable[[], float] | None = None,
    config_entry: Any | None = None,
) -> module.EntergyDataUpdateCoordinator:
    subject = module.EntergyDataUpdateCoordinator(
        hass,
        config_entry or entry(hass),
        client,
        ledger,
        time_zone="America/Chicago",
        _clock=clock or FakeClock(),
        _jitter=jitter or (lambda: 0),
    )
    CREATED.append(subject)
    return subject


@pytest.fixture(autouse=True)
async def cleanup_coordinators() -> Any:
    CREATED.clear()
    yield
    for subject in reversed(CREATED):
        await subject.async_shutdown()
        if isinstance(subject._entry, common.MockConfigEntry):
            subject._entry.mock_state(subject.hass, ConfigEntryState.NOT_LOADED)
    CREATED.clear()


@pytest.fixture
def recorder_stubs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    order: list[str] = []

    async def verify(*_: Any, **__: Any) -> bool:
        order.append("verify")
        return False

    async def queue(*_: Any, **__: Any) -> object:
        order.append("queue")
        return object()

    monkeypatch.setattr(module, "async_verify_queued_statistics", verify)
    monkeypatch.setattr(module, "async_queue_external_statistics", queue)
    return order


async def test_initialize_is_local_only_and_does_not_flip_entry_marker(
    hass: HomeAssistant,
) -> None:
    config_entry = entry(hass, initialized=False)
    client = FakeClient((interval(),))
    ledger = FakeLedger(LedgerState(schema_version=2, intervals=(interval(),)))
    subject = coordinator(hass, client, ledger, config_entry=config_entry)
    await subject.async_initialize()
    assert client.calls == []
    assert ledger.load_calls == [False]
    assert config_entry.data["ledger_initialized"] is False
    assert subject.data.freshness is Freshness.FRESH


async def test_normal_sync_fetches_seven_pages_in_one_shared_budget_and_one_ingest(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    client = FakeClient((interval(),), consume_cold=True)
    ledger = FakeLedger()
    subject = coordinator(hass, client, ledger)
    await subject.async_initialize()
    result = await subject._async_update_data()
    pages = [call for call in client.calls if call[0] == "page"]
    budgets = {id(call[2]) for call in client.calls}
    assert [call[1] for call in pages] == [
        date(2026, 8, 15),
        date(2026, 8, 22),
        date(2026, 8, 29),
        date(2026, 9, 5),
        date(2026, 9, 12),
        date(2026, 9, 19),
        date(2026, 9, 26),
    ]
    assert len(budgets) == 1
    assert next(iter({call[2].used for call in client.calls})) == 10
    assert len(ledger.ingests) == 1
    assert result.latest_import_kwh == 1


async def test_confirmed_account_zone_controls_same_chain_dates_and_later_fallback(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    client = FakeClient()
    client.account_time_zone = "America/Los_Angeles"
    config_entry = entry(hass)
    subject = coordinator(
        hass,
        client,
        FakeLedger(),
        clock=FakeClock(datetime(2026, 9, 28, 5, 30, tzinfo=UTC)),
        config_entry=config_entry,
    )
    await subject.async_initialize()
    await subject._async_update_data()
    assert [call[1] for call in client.calls if call[0] == "page"][-1] == date(2026, 9, 25)
    assert client.fallback_zones == ["America/Los_Angeles"] * 7
    assert config_entry.data["time_zone"] == "America/Chicago"
    client.account_time_zone = None
    await subject._async_update_data()
    assert client.fallback_zones == ["America/Los_Angeles"] * 14


async def test_confirmed_account_zone_controls_first_backfill_page(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    client = FakeClient()
    client.account_time_zone = "America/Los_Angeles"
    subject = coordinator(
        hass, client, FakeLedger(), clock=FakeClock(datetime(2026, 9, 28, 5, 30, tzinfo=UTC))
    )
    await subject.async_initialize()
    assert await subject.async_run_backfill_once()
    assert [call[1] for call in client.calls if call[0] == "page"] == [date(2026, 9, 21)]
    assert client.fallback_zones == ["America/Los_Angeles"]


async def test_normal_chain_leaves_exact_budget_for_one_auth_recovery(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    client = FakeClient(consume_cold=True, recovery_attempts=2)
    subject = coordinator(hass, client, FakeLedger())
    await subject.async_initialize()
    await subject._async_update_data()
    budget = client.calls[0][2]
    assert budget.used == 12


async def test_empty_first_success_still_persists_verified_initial_revision(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    ledger = FakeLedger()
    subject = coordinator(hass, FakeClient(), ledger)
    await subject.async_initialize()
    await subject._async_update_data()
    assert len(ledger.ingests) == 1
    assert ledger.state.revision == 1


async def test_page_failure_is_atomic_and_keeps_prior_snapshot(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    original = interval(NOW - timedelta(days=2), energy="3")
    ledger = FakeLedger(LedgerState(schema_version=2, intervals=(original,)))
    client = FakeClient((interval(),), failures=[None, EntergyError(ErrorCategory.TRANSIENT)])
    subject = coordinator(hass, client, ledger)
    await subject.async_initialize()
    before = subject.data
    with pytest.raises(UpdateFailed, match="transient"):
        await subject._async_update_data()
    assert ledger.ingests == []
    assert ledger.state.intervals == (original,)
    assert subject.data is before


async def test_candidate_persists_statistics_intent_before_queue(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = FakeLedger()
    client = FakeClient((interval(),))
    subject = coordinator(hass, client, ledger)
    await subject.async_initialize()
    observed: list[tuple[datetime | None, str | None]] = []

    async def queue(*_: Any, **__: Any) -> object:
        observed.append(
            (
                ledger.state.statistics_pending_from,
                ledger.state.statistics_pending_fingerprint,
            )
        )
        raise RuntimeError("synthetic queue failure")

    monkeypatch.setattr(module, "async_queue_external_statistics", queue)
    monkeypatch.setattr(module, "async_verify_queued_statistics", _false)
    with pytest.raises(UpdateFailed, match="transient"):
        await subject._async_update_data()
    assert observed and observed[0][0] is not None and observed[0][1]
    assert ledger.ingests[0].state.statistics_pending_from is not None
    assert subject.data.latest_import_kwh == 1
    assert subject.diagnostics()["last_successful_fetch"] == NOW.isoformat()
    assert subject.diagnostics()["last_inserted_count"] == 1


async def _false(*_: Any, **__: Any) -> bool:
    return False


async def test_pending_statistics_are_processed_before_utility_calls(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = LedgerState(
        schema_version=2,
        intervals=(interval(),),
        statistics_pending_from=interval().start,
    )
    ledger = FakeLedger(state)
    client = FakeClient((interval(),))
    subject = coordinator(hass, client, ledger)
    await subject.async_initialize()
    order: list[str] = []
    original_get = client.async_get_account

    async def account(*args: Any, **kwargs: Any) -> Account:
        order.append("account")
        return await original_get(*args, **kwargs)

    async def verify(*_: Any, **__: Any) -> bool:
        order.append("verify")
        return False

    async def queue(*_: Any, **__: Any) -> object:
        order.append("queue")
        return object()

    client.async_get_account = account  # type: ignore[method-assign]
    monkeypatch.setattr(module, "async_verify_queued_statistics", verify)
    monkeypatch.setattr(module, "async_queue_external_statistics", queue)
    await subject._async_update_data()
    assert order[:3] == ["verify", "queue", "account"]
    assert ledger.pending_calls and ledger.state.statistics_pending_fingerprint


async def test_pending_fingerprint_mismatch_fails_closed_before_utility(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    record = interval()
    ledger = FakeLedger(
        LedgerState(
            schema_version=2,
            intervals=(record,),
            statistics_pending_from=record.start,
            statistics_pending_fingerprint="f" * 64,
        )
    )
    client = FakeClient((record,))
    subject = coordinator(hass, client, ledger)
    with pytest.raises(module.LedgerRepairError):
        await subject.async_initialize()
    assert client.calls == []


async def test_initialize_rejects_pending_marker_with_empty_suffix(
    hass: HomeAssistant,
) -> None:
    ledger = FakeLedger(
        LedgerState(
            schema_version=2,
            statistics_pending_from=NOW,
            statistics_pending_fingerprint="f" * 64,
        )
    )
    client = FakeClient()
    subject = coordinator(hass, client, ledger)
    with pytest.raises(module.LedgerRepairError):
        await subject.async_initialize()
    assert client.calls == []


async def test_exact_pending_verification_clears_without_requeue(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = LedgerState(schema_version=2, intervals=(interval(),))
    batches = module.build_hourly_statistics(
        base, public_id=PUBLIC_ID, currency="USD", start=interval().start
    )
    fingerprint = module.statistics_fingerprint(batches)
    ledger = FakeLedger(
        replace(
            base,
            statistics_pending_from=interval().start,
            statistics_pending_fingerprint=fingerprint,
        )
    )
    client = FakeClient((interval(),))
    subject = coordinator(hass, client, ledger)
    await subject.async_initialize()
    monkeypatch.setattr(module, "async_verify_queued_statistics", _true)
    queued = False

    async def queue(*_: Any, **__: Any) -> object:
        nonlocal queued
        queued = True
        return object()

    monkeypatch.setattr(module, "async_queue_external_statistics", queue)
    await subject._async_update_data()
    assert ledger.verified_calls
    assert not queued


@pytest.mark.parametrize(
    ("stored_currency", "home_currency", "expected_series"),
    [("USD", "EUR", 2), ("EUR", "USD", 4)],
)
async def test_currency_mode_switch_persists_new_pending_before_requeue(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    stored_currency: str,
    home_currency: str,
    expected_series: int,
) -> None:
    record = interval()
    base = LedgerState(schema_version=2, intervals=(record,))
    stored = module.statistics_fingerprint(
        module.build_hourly_statistics(
            base, public_id=PUBLIC_ID, currency=stored_currency, start=record.start
        )
    )
    ledger = FakeLedger(
        replace(base, statistics_pending_from=record.start, statistics_pending_fingerprint=stored)
    )
    client = FakeClient()
    subject = coordinator(hass, client, ledger)
    hass.config.currency = home_currency
    await subject.async_initialize()
    seen: list[tuple[str | None, int]] = []

    async def queue(_: HomeAssistant, batches: Any) -> object:
        seen.append((ledger.state.statistics_pending_fingerprint, len(batches)))
        return object()

    monkeypatch.setattr(module, "async_verify_queued_statistics", _false)
    monkeypatch.setattr(module, "async_queue_external_statistics", queue)
    await subject._async_update_data()
    assert seen and seen[0][1] == expected_series
    assert ledger.pending_calls == [(record.start, seen[0][0])]
    assert seen[0][0] != stored
    assert ledger.state.statistics_pending_from == record.start
    assert subject.diagnostics()["repair_conditions"] == (
        ["currency_mismatch"] if home_currency == "EUR" else []
    )


async def test_currency_mode_migration_deferral_queues_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = interval()
    base = LedgerState(schema_version=2, intervals=(record,))
    old = module.statistics_fingerprint(
        module.build_hourly_statistics(
            base, public_id=PUBLIC_ID, currency="USD", start=record.start
        )
    )
    ledger = FakeLedger(
        replace(base, statistics_pending_from=record.start, statistics_pending_fingerprint=old)
    )
    ledger.fail_pending = True
    client = FakeClient()
    subject = coordinator(hass, client, ledger)
    hass.config.currency = "EUR"
    await subject.async_initialize()
    queued = False

    async def queue(*_: Any, **__: Any) -> object:
        nonlocal queued
        queued = True
        return object()

    monkeypatch.setattr(module, "async_queue_external_statistics", queue)
    with pytest.raises(UpdateFailed, match="ledger"):
        await subject._async_update_data()
    assert ledger.state.statistics_pending_fingerprint == old
    assert not queued and client.calls == []


async def _true(*_: Any, **__: Any) -> bool:
    return True


@pytest.mark.parametrize(
    ("failure", "condition"),
    [(PayloadError(), "schema_drift"), (None, "data_retraction")],
)
async def test_schema_and_quarantine_preserve_prior_snapshot(
    hass: HomeAssistant,
    recorder_stubs: list[str],
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception | None,
    condition: str,
) -> None:
    prior = interval(NOW - timedelta(days=1), energy="4")
    ledger = FakeLedger(LedgerState(schema_version=2, intervals=(prior,)))
    client = FakeClient((interval(),), failure=failure)
    subject = coordinator(hass, client, ledger)
    await subject.async_initialize()
    before = subject.data
    if failure is None:
        original = module.reconcile

        def quarantined(*args: Any, **kwargs: Any) -> LedgerMutation:
            return LedgerMutation(args[0], deferred=True, repair="usage_quarantine")

        monkeypatch.setattr(module, "reconcile", quarantined)
    result = await subject._async_update_data()
    if failure is None:
        monkeypatch.setattr(module, "reconcile", original)
    assert result is before
    assert ledger.ingests == []
    assert subject.diagnostics()["error_category"] == condition


async def test_foreign_currency_ingests_energy_and_suppresses_money(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = FakeLedger()
    subject = coordinator(hass, FakeClient((interval(currency="EUR"),)), ledger)
    await subject.async_initialize()
    queued: list[Any] = []

    async def queue(_: HomeAssistant, batches: Any) -> object:
        queued.extend(batches)
        return object()

    monkeypatch.setattr(module, "async_queue_external_statistics", queue)
    monkeypatch.setattr(module, "async_verify_queued_statistics", _false)
    result = await subject._async_update_data()
    assert result.latest_import_kwh == 1
    assert len(queued) == 2
    assert all("cost" not in meta["statistic_id"] for meta, _ in queued)
    assert subject.diagnostics()["error_category"] == "currency_mismatch"


async def test_home_assistant_currency_mismatch_hides_snapshot_money(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = FakeLedger()
    subject = coordinator(hass, FakeClient((interval(),)), ledger)
    hass.config.currency = "EUR"
    await subject.async_initialize()
    monkeypatch.setattr(module, "async_verify_queued_statistics", _false)
    monkeypatch.setattr(module, "async_queue_external_statistics", _noop_queue)
    snapshot = await subject._async_update_data()
    assert snapshot.latest_import_kwh == 1
    assert snapshot.latest_cost is None
    assert subject.diagnostics()["repair_conditions"] == ["currency_mismatch"]


async def _noop_queue(*_: Any, **__: Any) -> object:
    return object()


@pytest.mark.parametrize("failure", [AuthError(), ChallengeError()])
async def test_auth_and_challenge_require_reauthentication(
    hass: HomeAssistant, recorder_stubs: list[str], failure: Exception
) -> None:
    subject = coordinator(hass, FakeClient(failure=failure), FakeLedger())
    await subject.async_initialize()
    with pytest.raises(ConfigEntryAuthFailed):
        await subject._async_update_data()


async def test_first_transient_becomes_config_entry_not_ready(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    config_entry = entry(hass, state=ConfigEntryState.SETUP_IN_PROGRESS)
    subject = coordinator(
        hass,
        FakeClient(failure=EntergyError(ErrorCategory.TRANSIENT)),
        FakeLedger(),
        config_entry=config_entry,
    )
    await subject.async_initialize()
    with pytest.raises(ConfigEntryNotReady):
        await subject.async_config_entry_first_refresh()


async def test_transient_backoff_retry_after_and_success_jitter(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    client = FakeClient(failure=EntergyError(ErrorCategory.TRANSIENT))
    subject = coordinator(hass, client, FakeLedger(), jitter=lambda: 1)
    await subject.async_initialize()
    for expected in (3600, 7200, 14_400, 28_800, 86_400, 86_400):
        with pytest.raises(UpdateFailed) as caught:
            await subject._async_update_data()
        assert caught.value.retry_after == expected
        assert subject.diagnostics()["next_poll_seconds"] == expected
    client.failure = RateLimitError(retry_after=1234)
    with pytest.raises(UpdateFailed) as caught:
        await subject._async_update_data()
    assert caught.value.retry_after == 1234
    assert subject.diagnostics()["next_poll_seconds"] == 1234
    client.failure = None
    await subject._async_update_data()
    assert subject.update_interval == timedelta(seconds=15_840)
    client.failure = EntergyError(ErrorCategory.TRANSIENT)
    with pytest.raises(UpdateFailed) as caught:
        await subject._async_update_data()
    assert caught.value.retry_after == 3600


async def test_zero_jitter_is_exact_configured_interval(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    subject = coordinator(hass, FakeClient(), FakeLedger(), jitter=lambda: 0)
    await subject.async_initialize()
    await subject._async_update_data()
    assert subject.update_interval == timedelta(seconds=14_400)


async def test_disabled_and_shutdown_make_no_requests(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    disabled_entry = entry(hass, disabled=True)
    client = FakeClient((interval(),))
    disabled = coordinator(hass, client, FakeLedger(), config_entry=disabled_entry)
    await disabled.async_initialize()
    assert await disabled._async_update_data() is disabled.data
    assert client.calls == []
    await disabled.async_shutdown()
    await disabled.async_shutdown()
    assert await disabled._async_update_data() is disabled.data
    assert client.calls == []


@pytest.mark.parametrize(
    "state", [ConfigEntryState.NOT_LOADED, ConfigEntryState.UNLOAD_IN_PROGRESS]
)
async def test_unloaded_states_make_no_normal_or_backfill_requests(
    hass: HomeAssistant, recorder_stubs: list[str], state: ConfigEntryState
) -> None:
    config_entry = entry(hass, state=state)
    client = FakeClient((interval(),))
    subject = coordinator(hass, client, FakeLedger(), config_entry=config_entry)
    await subject.async_initialize()
    assert await subject._async_update_data() is subject.data
    assert not await subject.async_run_backfill_once()
    await subject.async_start_backfill()
    assert subject._backfill_task is None
    assert client.calls == []


async def test_queued_normal_rechecks_unload_state_before_request(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    config_entry = entry(hass)
    client = FakeClient((interval(),))
    client.block, client.release = asyncio.Event(), asyncio.Event()
    subject = coordinator(hass, client, FakeLedger(), config_entry=config_entry)
    await subject.async_initialize()
    active = asyncio.create_task(subject.async_run_backfill_once())
    await client.block.wait()
    normal = asyncio.create_task(subject._async_update_data())
    await asyncio.sleep(0)
    config_entry.mock_state(hass, ConfigEntryState.UNLOAD_IN_PROGRESS)
    client.release.set()
    await active
    assert await normal is subject.data
    assert len([call for call in client.calls if call[0] == "account"]) == 1


async def test_preference_disabled_polling_blocks_normal_and_backfill(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    config_entry = entry(hass, polling_disabled=True)
    client = FakeClient((interval(),))
    subject = coordinator(hass, client, FakeLedger(), config_entry=config_entry)
    await subject.async_initialize()
    assert await subject._async_update_data() is subject.data
    assert not await subject.async_run_backfill_once()
    await subject.async_start_backfill()
    assert subject._backfill_task is None
    assert client.calls == []


async def test_shutdown_cancels_active_chain_and_is_idempotent(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    client = FakeClient((interval(),))
    client.block, client.release = asyncio.Event(), asyncio.Event()
    subject = coordinator(hass, client, FakeLedger())
    await subject.async_initialize()
    refresh = asyncio.create_task(subject._async_update_data())
    await client.block.wait()
    await subject.async_shutdown()
    with pytest.raises(asyncio.CancelledError):
        await refresh
    await subject.async_shutdown()
    assert len(client.calls) == 2


async def test_concurrent_shutdown_callers_wait_for_one_cleanup(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = coordinator(hass, FakeClient(), FakeLedger())
    await subject.async_initialize()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def delayed_base_shutdown(_: Any) -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    monkeypatch.setattr(module.DataUpdateCoordinator, "async_shutdown", delayed_base_shutdown)
    first = asyncio.create_task(subject.async_shutdown())
    await entered.wait()
    second = asyncio.create_task(subject.async_shutdown())
    await asyncio.sleep(0)
    returned_before_cleanup = first.done() or second.done()
    release.set()
    await asyncio.gather(first, second)
    assert not returned_before_cleanup
    assert calls == 1


async def test_priority_gate_runs_waiting_normal_before_next_backfill(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    client = FakeClient((interval(),))
    client.block, client.release = asyncio.Event(), asyncio.Event()
    ledger = FakeLedger()
    subject = coordinator(hass, client, ledger)
    await subject.async_initialize()
    first = asyncio.create_task(subject.async_run_backfill_once(), name="backfill-one")
    await client.block.wait()
    normal = asyncio.create_task(subject._async_update_data(), name="normal")
    second = asyncio.create_task(subject.async_run_backfill_once(), name="backfill-two")
    await asyncio.sleep(0)
    client.release.set()
    await asyncio.gather(first, normal, second)
    account_task_names = [call[1] for call in client.calls if call[0] == "account"]
    # Every chain is single-flight; after the active backfill, the normal chain
    # consumes its seven pages before the second backfill acquires the gate.
    assert client.max_active == 1
    pages = [call[1] for call in client.calls if call[0] == "page"]
    assert len(pages) == 9
    assert pages[1:8] == [
        date(2026, 8, 15),
        date(2026, 8, 22),
        date(2026, 8, 29),
        date(2026, 9, 5),
        date(2026, 9, 12),
        date(2026, 9, 19),
        date(2026, 9, 26),
    ]
    assert account_task_names == [ACCOUNT_ID, ACCOUNT_ID, ACCOUNT_ID]


async def test_backfill_worker_waits_each_slot_and_completes_only_page_53(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    clock = FakeClock()
    ledger = FakeLedger()
    client = FakeClient()
    subject = coordinator(hass, client, ledger, clock=clock)
    await subject.async_initialize()
    await subject.async_start_backfill()
    task = subject._backfill_task
    assert task is not None
    await task
    pages = [call[1] for call in client.calls if call[0] == "page"]
    assert len(pages) == 53
    assert pages[0] == date(2026, 9, 22)
    assert pages[-1] == date(2025, 9, 24)
    assert clock.sleeps == [1800] * 53
    assert sum(clock.sleeps) == 26.5 * 3600
    assert ledger.state.backfill_complete
    assert subject.diagnostics()["backfill_pages_completed"] == 53
    assert subject.diagnostics()["backfill_progress_percent"] == 100
    assert len(clock.sleeps[:48]) == 48 and sum(clock.sleeps[:48]) == 24 * 3600


async def test_backfill_does_not_run_before_first_half_hour(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    clock = ControlledClock()
    client = FakeClient()
    subject = coordinator(hass, client, FakeLedger(), clock=clock)
    await subject.async_initialize()
    await subject.async_start_backfill()
    await clock.waiting.wait()
    assert client.calls == []
    await subject.async_shutdown()


async def test_backfill_start_is_idempotent(hass: HomeAssistant, recorder_stubs: list[str]) -> None:
    clock = ControlledClock()
    subject = coordinator(hass, FakeClient(), FakeLedger(), clock=clock)
    await subject.async_initialize()
    await subject.async_start_backfill()
    first = subject._backfill_task
    await subject.async_start_backfill()
    assert subject._backfill_task is first
    await subject.async_shutdown()


async def test_backfill_advances_empty_page_and_resumes_exactly(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    ledger = FakeLedger()
    first = coordinator(hass, FakeClient(), ledger)
    await first.async_initialize()
    assert await first.async_run_backfill_once()
    cursor = ledger.state.backfill_cursor
    assert cursor is not None
    restarted_client = FakeClient()
    restarted = coordinator(hass, restarted_client, ledger)
    await restarted.async_initialize()
    assert await restarted.async_run_backfill_once()
    pages = [call[1] for call in restarted_client.calls if call[0] == "page"]
    assert pages == [date(2026, 9, 15)]
    assert ledger.state.backfill_cursor != cursor


async def test_backfill_save_failure_does_not_advance_or_queue(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = FakeLedger()
    ledger.fail_ingest = True
    subject = coordinator(hass, FakeClient((interval(),)), ledger)
    await subject.async_initialize()
    queued = False

    async def queue(*_: Any, **__: Any) -> object:
        nonlocal queued
        queued = True
        return object()

    monkeypatch.setattr(module, "async_queue_external_statistics", queue)
    monkeypatch.setattr(module, "async_verify_queued_statistics", _false)
    assert not await subject.async_run_backfill_once()
    assert ledger.state.backfill_cursor is None
    assert not queued


async def test_backfill_queue_crash_publishes_verified_final_page(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    floor = datetime(2025, 9, 24, 5, tzinfo=UTC)
    ledger = FakeLedger(LedgerState(schema_version=2, backfill_cursor=floor + timedelta(days=7)))
    subject = coordinator(hass, FakeClient((interval(floor + timedelta(hours=12)),)), ledger)
    await subject.async_initialize()

    async def queue(*_: Any, **__: Any) -> object:
        raise RuntimeError("synthetic queue failure")

    monkeypatch.setattr(module, "async_queue_external_statistics", queue)
    monkeypatch.setattr(module, "async_verify_queued_statistics", _false)
    assert not await subject.async_run_backfill_once()
    diagnostic = subject.diagnostics()
    assert ledger.state.backfill_complete
    assert subject.data.latest_import_kwh == 1
    assert diagnostic["backfill_pages_completed"] == 53
    assert diagnostic["backfill_progress_percent"] == 100
    assert diagnostic["last_successful_fetch"] == NOW.isoformat()


async def test_backfill_drops_intervals_older_than_moving_floor(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    too_old = interval(datetime(2025, 9, 23, 12, tzinfo=UTC))
    in_range = interval(datetime(2026, 9, 22, 12, tzinfo=UTC))
    ledger = FakeLedger()
    subject = coordinator(hass, FakeClient((too_old, in_range)), ledger)
    await subject.async_initialize()
    assert await subject.async_run_backfill_once()
    assert tuple(item.start for item in ledger.state.intervals) == (in_range.start,)


async def test_backfill_accepts_only_the_requested_local_date_page(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    requested = interval(datetime(2026, 9, 22, 12, tzinfo=UTC))
    older_page = interval(datetime(2026, 9, 15, 12, tzinfo=UTC))
    ledger = FakeLedger()
    subject = coordinator(hass, FakeClient((older_page, requested)), ledger)
    await subject.async_initialize()
    assert await subject.async_run_backfill_once()
    assert tuple(item.start for item in ledger.state.intervals) == (requested.start,)


async def test_backfill_rate_limit_pauses_independently_then_completes(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    floor = datetime(2025, 9, 24, 5, tzinfo=UTC)
    state = LedgerState(schema_version=2, backfill_cursor=floor + timedelta(days=7))
    ledger = FakeLedger(state)
    clock = FakeClock()
    client = FakeClient(failures=[RateLimitError(retry_after=7200), None])
    subject = coordinator(hass, client, ledger, clock=clock)
    await subject.async_initialize()
    await subject.async_start_backfill()
    assert subject._backfill_task is not None
    await subject._backfill_task
    assert clock.sleeps == [1800, 7200]
    assert ledger.state.backfill_complete
    assert subject._normal_failures == 0


async def test_backfill_auth_starts_supported_reauthentication(
    hass: HomeAssistant,
    recorder_stubs: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_entry = entry(hass)
    calls: list[HomeAssistant] = []

    def start_reauth(_entry: Any, target_hass: HomeAssistant, *_args: Any, **_kwargs: Any) -> None:
        calls.append(target_hass)

    monkeypatch.setattr(type(config_entry), "async_start_reauth_if_available", start_reauth)
    subject = coordinator(
        hass,
        FakeClient(failure=AuthError()),
        FakeLedger(),
        config_entry=config_entry,
    )
    await subject.async_initialize()
    await subject.async_start_backfill()
    assert subject._backfill_task is not None
    await subject._backfill_task
    assert calls == [hass]


@pytest.mark.parametrize(
    ("now", "cursor", "expected"),
    [
        (
            datetime(2026, 3, 9, 5, tzinfo=UTC),
            datetime(2026, 3, 9, 5, tzinfo=UTC),
            date(2026, 3, 2),
        ),
        (
            datetime(2026, 11, 2, 6, tzinfo=UTC),
            datetime(2026, 11, 2, 6, tzinfo=UTC),
            date(2026, 10, 26),
        ),
    ],
)
async def test_backfill_cursor_uses_local_dates_across_dst(
    hass: HomeAssistant,
    recorder_stubs: list[str],
    now: datetime,
    cursor: datetime,
    expected: date,
) -> None:
    ledger = FakeLedger(LedgerState(schema_version=2, backfill_cursor=cursor))
    client = FakeClient()
    subject = coordinator(hass, client, ledger, clock=FakeClock(now))
    await subject.async_initialize()
    assert await subject.async_run_backfill_once()
    assert [call[1] for call in client.calls if call[0] == "page"] == [expected]


async def test_diagnostics_are_allowlisted_and_reflect_only_verified_state(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    record = interval(estimated=True)
    ledger = FakeLedger(LedgerState(schema_version=2, intervals=(record,)))
    subject = coordinator(hass, FakeClient((record,)), ledger)
    await subject.async_initialize()
    await subject._async_update_data()
    diagnostic = subject.diagnostics()
    assert set(diagnostic) == {
        "configured_poll_seconds",
        "next_poll_seconds",
        "last_successful_fetch",
        "newest_interval",
        "error_category",
        "retained_interval_count",
        "estimated_interval_count",
        "last_inserted_count",
        "last_corrected_count",
        "freshness",
        "backfill_pages_completed",
        "backfill_pages_total",
        "backfill_progress_percent",
        "backfill_complete",
        "repair_conditions",
    }
    exposed = repr(diagnostic)
    for secret in (
        "synthetic-user",
        "synthetic-password",
        ACCOUNT_ID,
        PUBLIC_ID,
        "https://",
        "Bearer",
    ):
        assert secret not in exposed
    assert diagnostic["retained_interval_count"] == 1
    assert diagnostic["estimated_interval_count"] == 1
    assert diagnostic["freshness"] == "fresh"


async def test_currency_and_healthy_backfill_stall_conditions_coexist_and_notify(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    clock = FakeClock()
    subject = coordinator(hass, FakeClient(), FakeLedger(), clock=clock)
    await subject.async_initialize()
    notifications = 0

    def listener() -> None:
        nonlocal notifications
        notifications += 1

    subject.async_add_listener(listener)
    subject._repair_conditions.add("currency_mismatch")
    subject._condition = "currency_mismatch"
    subject._normal_network_healthy = True
    subject._backfill_started_at = NOW
    clock.value += timedelta(hours=24)
    subject._refresh_backfill_stalled(notify=True)
    diagnostic = subject.diagnostics()
    assert diagnostic["repair_conditions"] == ["backfill_stalled", "currency_mismatch"]
    assert diagnostic["backfill_progress_percent"] < 100
    assert notifications == 1


async def test_backfill_stall_tracks_normal_network_health(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    clock = FakeClock()
    client = FakeClient()
    subject = coordinator(hass, client, FakeLedger(), clock=clock)
    await subject.async_initialize()
    notifications = 0

    def listener() -> None:
        nonlocal notifications
        notifications += 1

    subject.async_add_listener(listener)
    subject._backfill_started_at = NOW
    clock.value += timedelta(hours=24)
    subject._refresh_backfill_stalled(notify=True)
    assert "backfill_stalled" not in subject.diagnostics()["repair_conditions"]

    await subject._async_update_data()
    await asyncio.sleep(0)
    assert "backfill_stalled" in subject.diagnostics()["repair_conditions"]
    assert notifications == 1

    client.failure = EntergyError(ErrorCategory.TRANSIENT)
    with pytest.raises(UpdateFailed):
        await subject._async_update_data()
    await asyncio.sleep(0)
    assert "backfill_stalled" not in subject.diagnostics()["repair_conditions"]
    assert notifications == 2

    client.failure = None
    await subject._async_update_data()
    await asyncio.sleep(0)
    assert "backfill_stalled" in subject.diagnostics()["repair_conditions"]
    assert notifications == 3


async def test_backfill_error_and_verified_progress_notify_listeners(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    client = FakeClient(failures=[RateLimitError(retry_after=1800), None])
    subject = coordinator(hass, client, FakeLedger())
    await subject.async_initialize()
    notifications = 0

    def listener() -> None:
        nonlocal notifications
        notifications += 1

    subject.async_add_listener(listener)
    assert not await subject.async_run_backfill_once()
    assert await subject.async_run_backfill_once()
    assert notifications == 2


async def test_public_refresh_notifies_once_after_committing_success_state(
    hass: HomeAssistant, recorder_stubs: list[str]
) -> None:
    clock = FakeClock()
    subject = coordinator(hass, FakeClient(), FakeLedger(), clock=clock)
    await subject.async_initialize()
    observed: list[tuple[bool, str | None]] = []

    def listener() -> None:
        observed.append(
            (subject.last_update_success, subject.diagnostics()["last_successful_fetch"])
        )

    subject.async_add_listener(listener)
    await subject.async_refresh()
    await asyncio.sleep(0)
    assert observed == [(True, NOW.isoformat())]

    observed.clear()
    clock.value += timedelta(minutes=5)
    await subject.async_refresh()
    await asyncio.sleep(0)
    assert observed == [(True, clock.value.isoformat())]


@pytest.mark.parametrize("outcome", ["schema", "quarantine", "policy", "ledger"])
async def test_non_backoff_outcome_restores_normal_next_poll_diagnostic(
    hass: HomeAssistant,
    recorder_stubs: list[str],
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    client = FakeClient(failure=EntergyError(ErrorCategory.TRANSIENT))
    ledger = FakeLedger()
    subject = coordinator(hass, client, ledger)
    await subject.async_initialize()
    with pytest.raises(UpdateFailed):
        await subject._async_update_data()
    assert subject.diagnostics()["next_poll_seconds"] == 3600

    if outcome == "schema":
        client.failure = PayloadError()
        await subject._async_update_data()
    elif outcome == "policy":
        client.failure = module.PolicyError()
        with pytest.raises(UpdateFailed):
            await subject._async_update_data()
    else:
        client.failure = None
        client.pages = (interval(),)
        if outcome == "quarantine":

            def quarantined(*args: Any, **kwargs: Any) -> LedgerMutation:
                return LedgerMutation(args[0], deferred=True, repair="usage_quarantine")

            monkeypatch.setattr(module, "reconcile", quarantined)
            await subject._async_update_data()
        else:
            ledger.fail_ingest = True
            with pytest.raises(UpdateFailed):
                await subject._async_update_data()
    assert subject.diagnostics()["next_poll_seconds"] == 14_400
