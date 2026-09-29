"""Local credentials and pseudonymous account setup."""

from __future__ import annotations

from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from aiohttp import ClientError
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_TIME_ZONE, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EntergyApiClient, RequestBudget
from .const import (
    CONF_ACCOUNT_ID,
    CONF_LANGUAGE,
    CONF_SCAN_INTERVAL_SECONDS,
    DEFAULT_LANGUAGE,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    MAX_SCAN_INTERVAL_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
)
from .errors import AuthError, ChallengeError, EntergyError
from .models import Account, Credentials
from .statistics import statistic_ids


def _user_schema(default_language: str = DEFAULT_LANGUAGE) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD, autocomplete="current-password"
                )
            ),
            vol.Optional(CONF_LANGUAGE, default=default_language): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["en", "es"], mode=selector.SelectSelectorMode.DROPDOWN
                )
            ),
        }
    )


def _account_label(account: Account) -> str:
    label = f"Account ending ••••{account.account_id[-4:]}"
    nickname = (account.nickname or "").strip()
    if 1 <= len(nickname) <= 40 and not any(
        char.isdigit() or char in "\r\n\v\f\x85\u2028\u2029,@" for char in nickname
    ):
        label += f" ({nickname})"
    return label


async def _validate_and_fetch_accounts(
    hass: HomeAssistant, data: dict[str, Any]
) -> tuple[Account, ...]:
    client = EntergyApiClient(
        async_get_clientsession(hass),
        Credentials(data[CONF_USERNAME], data[CONF_PASSWORD]),
        language=data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
    )
    budget = RequestBudget()
    try:
        await client.async_initialize(budget)
        await client.async_login(budget)
        accounts = await client.async_get_accounts(budget)
        return tuple(accounts)
    finally:
        await client.async_logout(budget)


class EntergyMobileConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure only after verifying an account with local credentials."""

    VERSION = 2
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict[str, Any] = {}
        self._accounts: tuple[Account, ...] = ()
        self._selected_account: Account | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._credentials_step("user", user_input)

    async def _credentials_step(
        self, step: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors = {}
        language = DEFAULT_LANGUAGE
        if user_input is not None:
            language = user_input.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
            try:
                accounts = await _validate_and_fetch_accounts(self.hass, user_input)
            except ChallengeError:
                errors["base"] = "unsupported_challenge"
            except AuthError:
                errors["base"] = "invalid_auth"
            except EntergyError, ClientError:
                errors["base"] = "cannot_connect"
            except Exception:
                # Never log exception strings or untrusted payload details.
                errors["base"] = "unknown"
            else:
                credentials = {key: user_input[key] for key in (CONF_USERNAME, CONF_PASSWORD)}
                credentials[CONF_LANGUAGE] = language
                if step == "reauth_confirm":
                    entry = self._get_reauth_entry()
                    if not any(
                        account.account_id == entry.data[CONF_ACCOUNT_ID] for account in accounts
                    ):
                        return self.async_abort(reason="unique_id_mismatch")
                    await self.async_set_unique_id(entry.data["public_id"])
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(entry, data_updates=credentials)
                if accounts:
                    self._credentials, self._accounts = credentials, accounts
                    return await self.async_step_account()
                errors["base"] = "no_accounts"
        return self.async_show_form(step_id=step, data_schema=_user_schema(language), errors=errors)

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._accounts:
            return await self.async_step_user()
        choices: list[selector.SelectOptionDict] = [
            {"value": str(index), "label": _account_label(account)}
            for index, account in enumerate(self._accounts)
        ]
        if user_input is not None:
            selection = user_input.get(CONF_ACCOUNT_ID)
            if selection not in {choice["value"] for choice in choices}:
                return self.async_abort(reason="invalid_account")
            account = self._accounts[int(selection)]
            if any(
                entry.data.get(CONF_ACCOUNT_ID) == account.account_id
                for entry in self._async_current_entries()
            ):
                return self.async_abort(reason="already_configured")
            self._selected_account = account
            if account.time_zone is None:
                return await self.async_step_time_zone()
            return await self._create_account_entry(account.time_zone)
        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCOUNT_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=choices, mode=selector.SelectSelectorMode.DROPDOWN
                        )
                    ),
                }
            ),
        )

    async def async_step_time_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._selected_account is None:
            return await self.async_step_user()
        errors = {}
        if user_input is not None:
            time_zone = user_input.get(CONF_TIME_ZONE, self.hass.config.time_zone)
            try:
                ZoneInfo(time_zone)
            except TypeError, ValueError, ZoneInfoNotFoundError:
                errors[CONF_TIME_ZONE] = "invalid_time_zone"
            else:
                return await self._create_account_entry(time_zone)
        time_zones = await self.hass.async_add_executor_job(available_timezones)
        return self.async_show_form(
            step_id="time_zone",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TIME_ZONE, default=self.hass.config.time_zone
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=sorted(time_zones),
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def _create_account_entry(self, time_zone: str) -> ConfigFlowResult:
        account = self._selected_account
        if account is None:
            return await self.async_step_user()
        if any(
            entry.data.get(CONF_ACCOUNT_ID) == account.account_id
            for entry in self._async_current_entries()
        ):
            return self.async_abort(reason="already_configured")
        public_id = uuid4().hex
        statistic_ids(public_id)
        await self.async_set_unique_id(public_id)
        return self.async_create_entry(
            title="Entergy",
            data={
                **self._credentials,
                CONF_ACCOUNT_ID: account.account_id,
                "public_id": public_id,
                "time_zone": time_zone,
                "ledger_initialized": False,
            },
            options={CONF_SCAN_INTERVAL_SECONDS: DEFAULT_SCAN_INTERVAL_SECONDS},
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._get_reauth_entry()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._credentials_step("reauth_confirm", user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EntergyOptionsFlow:
        return EntergyOptionsFlow()


class EntergyOptionsFlow(OptionsFlowWithReload):
    """Let HA perform exactly one reload when options change."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        interval = max(
            MIN_SCAN_INTERVAL_SECONDS,
            min(
                MAX_SCAN_INTERVAL_SECONDS,
                int(
                    self.config_entry.options.get(
                        CONF_SCAN_INTERVAL_SECONDS, DEFAULT_SCAN_INTERVAL_SECONDS
                    )
                ),
            ),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_SCAN_INTERVAL_SECONDS, default=interval): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL_SECONDS, max=MAX_SCAN_INTERVAL_SECONDS),
                    ),
                }
            ),
        )
