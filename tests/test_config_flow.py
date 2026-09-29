"""Private setup and reauthentication contracts."""

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import voluptuous as vol
from custom_components.entergy_mobile.config_flow import EntergyMobileConfigFlow, _user_schema
from custom_components.entergy_mobile.const import DOMAIN
from custom_components.entergy_mobile.errors import AuthError, ChallengeError
from custom_components.entergy_mobile.models import Account
from custom_components.entergy_mobile.statistics import statistic_ids
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import InvalidData
from homeassistant.helpers import selector
from pytest_homeassistant_custom_component import common  # type: ignore[import-untyped]

CREDS = {"username": "synthetic-user", "password": "synthetic-password", "language": "en"}
PUBLIC = "a" * 32


@pytest.fixture
def client() -> Generator[AsyncMock]:
    api = AsyncMock()
    api.async_get_accounts.return_value = (Account("987654321", " Cabin ", "America/Chicago"),)
    with (
        patch("custom_components.entergy_mobile.config_flow.EntergyApiClient", return_value=api),
        patch("custom_components.entergy_mobile.async_setup_entry", return_value=True),
    ):
        yield api


async def start(hass: HomeAssistant) -> ConfigFlowResult:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=CREDS
    )


def test_password_selector_and_disclosure() -> None:
    schema = _user_schema().schema
    password = next(value for key, value in schema.items() if key.schema == "password")
    assert isinstance(password, selector.TextSelector)
    assert password.config["type"] == selector.TextSelectorType.PASSWORD
    assert password.config["autocomplete"] == "current-password"
    strings = json.loads(Path("custom_components/entergy_mobile/strings.json").read_text())
    for step in ("user", "reauth_confirm"):
        assert "password" in strings["config"]["step"][step]["description"].lower()
        assert "stored" in strings["config"]["step"][step]["description"].lower()


@pytest.mark.parametrize(
    "nickname,label",
    [
        (" Cabin ", "Account ending ••••4321 (Cabin)"),
        ("123 Main", "Account ending ••••4321"),
        ("Home, Lane", "Account ending ••••4321"),
        ("a@b", "Account ending ••••4321"),
        ("a\nb", "Account ending ••••4321"),
        ("Home\u2028Lane", "Account ending ••••4321"),
        ("x" * 41, "Account ending ••••4321"),
    ],
)
async def test_masked_choices(
    hass: HomeAssistant, client: AsyncMock, nickname: str, label: str
) -> None:
    client.async_get_accounts.return_value = (Account("987654321", nickname),)
    result = await start(hass)
    assert result["step_id"] == "account"
    assert result["data_schema"] is not None
    options = next(iter(result["data_schema"].schema.values())).config["options"]
    assert options == [{"value": "0", "label": label}]
    assert "987654321" not in str(result)
    client.async_logout.assert_awaited_once()


async def test_create_private_identity(hass: HomeAssistant, client: AsyncMock) -> None:
    result = await start(hass)
    with patch(
        "custom_components.entergy_mobile.config_flow.uuid4", wraps=__import__("uuid").uuid4
    ) as uuid:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"account_id": "0"}
        )
        uuid.assert_called_once()
    assert result["type"] == "create_entry"
    entry = result["result"]
    assert UUID(entry.unique_id).hex == entry.data["public_id"]
    assert entry.unique_id is not None
    statistic_ids(entry.unique_id)
    assert entry.title == "Entergy"
    assert entry.data["account_id"] == "987654321"
    assert entry.data["time_zone"] == "America/Chicago"
    assert entry.version == 2 and entry.minor_version == 1
    assert entry.options["scan_interval_seconds"] == 14400
    await hass.async_block_till_done()


async def test_missing_account_zone_requires_visible_defaulted_timezone(
    hass: HomeAssistant, client: AsyncMock
) -> None:
    client.async_get_accounts.return_value = (Account("987654321"),)
    hass.config.time_zone = "America/New_York"
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"account_id": "0"})
    assert result["type"] == "form" and result["step_id"] == "time_zone"
    assert result["data_schema"]({}) == {"time_zone": "America/New_York"}
    marker, field = next(iter(result["data_schema"].schema.items()))
    assert isinstance(marker, vol.Required)
    assert isinstance(field, selector.SelectSelector)
    assert "America/New_York" in field.config["options"]
    for path in ("strings.json", "translations/en.json"):
        strings = json.loads(Path("custom_components/entergy_mobile", path).read_text())
        assert strings["config"]["step"]["time_zone"]["data"]["time_zone"]
        assert strings["config"]["error"]["invalid_time_zone"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert result["result"].data["time_zone"] == "America/New_York"


async def test_timezone_pause_rechecks_private_account_duplicate(
    hass: HomeAssistant, client: AsyncMock
) -> None:
    client.async_get_accounts.return_value = (Account("987654321"),)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"account_id": "0"})
    common.MockConfigEntry(
        domain=DOMAIN, unique_id="b" * 32, data={"account_id": "987654321"}
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"time_zone": "America/Chicago"}
    )
    assert result["type"] == "abort" and result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_missing_account_zone_accepts_valid_override(
    hass: HomeAssistant, client: AsyncMock
) -> None:
    client.async_get_accounts.return_value = (Account("987654321"),)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"account_id": "0"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"time_zone": "America/Denver"}
    )
    assert result["type"] == "create_entry"
    assert result["result"].data["time_zone"] == "America/Denver"


async def test_missing_account_zone_rejects_invalid_without_secrets(
    hass: HomeAssistant, client: AsyncMock
) -> None:
    client.async_get_accounts.return_value = (Account("987654321"),)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"account_id": "0"})
    with pytest.raises(InvalidData) as error:
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"time_zone": "private-canary/NotAZone"}
        )
    assert "private-canary" not in str(error.value)
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_timezone_handler_defensively_rejects_invalid_value(hass: HomeAssistant) -> None:
    flow = EntergyMobileConfigFlow()
    flow.hass = hass
    flow._selected_account = Account("987654321")
    result = await flow.async_step_time_zone({"time_zone": "private-canary/NotAZone"})
    assert result["step_id"] == "time_zone"
    assert result["errors"] == {"time_zone": "invalid_time_zone"}
    assert "private-canary" not in repr(result["errors"])


async def test_duplicate_checks_private_account(hass: HomeAssistant, client: AsyncMock) -> None:
    common.MockConfigEntry(
        domain=DOMAIN, unique_id=PUBLIC, data={"account_id": "987654321"}
    ).add_to_hass(hass)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"account_id": "0"})
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    "error,reason",
    [
        (AuthError(), "invalid_auth"),
        (ChallengeError(), "unsupported_challenge"),
        (RuntimeError("private-canary"), "unknown"),
    ],
)
@pytest.mark.parametrize("operation", ["async_initialize", "async_login", "async_get_accounts"])
async def test_errors_logout_and_no_password_prefill(
    hass: HomeAssistant,
    client: AsyncMock,
    error: Exception,
    reason: str,
    operation: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    getattr(client, operation).side_effect = error
    result = await start(hass)
    assert result["errors"] == {"base": reason}
    assert result["data_schema"] is not None
    password_key = next(key for key in result["data_schema"].schema if key.schema == "password")
    assert password_key.description is None or "suggested_value" not in password_key.description
    assert "private-canary" not in caplog.text
    client.async_logout.assert_awaited_once()


@pytest.mark.parametrize("account", ["987654321", "000054321"])
async def test_reauth_same_masked_suffix_but_different_raw_account_is_rejected(
    hass: HomeAssistant, client: AsyncMock, account: str
) -> None:
    entry = common.MockConfigEntry(
        domain=DOMAIN,
        unique_id=PUBLIC,
        version=2,
        minor_version=1,
        data={
            **CREDS,
            "account_id": "987654321",
            "public_id": PUBLIC,
            "ledger_initialized": True,
            "time_zone": "UTC",
        },
    )
    entry.add_to_hass(hass)
    before = dict(entry.data)
    client.async_get_accounts.return_value = (Account(account),)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}, data=dict(entry.data)
    )
    assert result["step_id"] == "reauth_confirm"
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**CREDS, "username": "new-user", "password": "new-password", "language": "es"},
        )
        await hass.async_block_till_done()
    assert result["reason"] == (
        "reauth_successful" if account == "987654321" else "unique_id_mismatch"
    )
    assert dict(entry.data) == (
        {**before, "username": "new-user", "password": "new-password", "language": "es"}
        if account == "987654321"
        else before
    )
    assert entry.unique_id == PUBLIC and len(hass.config_entries.async_entries(DOMAIN)) == 1
    client.async_logout.assert_awaited_once()


@pytest.mark.parametrize("old,expected", [(None, 14400), (60, 3600), (7200, 7200)])
async def test_options_clamp_and_reload_once(
    hass: HomeAssistant, old: int | None, expected: int
) -> None:
    entry = common.MockConfigEntry(
        domain=DOMAIN, data={}, options={} if old is None else {"scan_interval_seconds": old}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["data_schema"] is not None
    assert result["data_schema"]({})["scan_interval_seconds"] == expected
    with patch.object(hass.config_entries, "async_reload", return_value=True) as reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"scan_interval_seconds": 10800}
        )
        await hass.async_block_till_done()
    assert entry.options["scan_interval_seconds"] == 10800
    reload.assert_awaited_once()


@pytest.mark.parametrize("challenge", ["MFA", "CAPTCHA", "consent", "unknown"])
async def test_reauth_challenges_stop_without_entry_changes(
    hass: HomeAssistant, client: AsyncMock, challenge: str
) -> None:
    from custom_components.entergy_mobile.parser import parse_login

    entry = common.MockConfigEntry(
        domain=DOMAIN,
        unique_id=PUBLIC,
        version=2,
        data={**CREDS, "account_id": "987654321", "public_id": PUBLIC},
    )
    entry.add_to_hass(hass)
    before = dict(entry.data)

    def challenged(*args: Any) -> None:
        parse_login({"accessToken": "synthetic", "nextAction": challenge})

    client.async_login.side_effect = challenged
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}, data=before
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["errors"] == {"base": "unsupported_challenge"}
    assert entry.data == before and len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert result["data_schema"] is not None
    with pytest.raises(__import__("voluptuous").MultipleInvalid):
        result["data_schema"]({"username": "synthetic-user"})
    client.async_logout.assert_awaited_once()


async def test_reauth_invalid_auth_preserves_entry(hass: HomeAssistant, client: AsyncMock) -> None:
    entry = common.MockConfigEntry(
        domain=DOMAIN,
        unique_id=PUBLIC,
        version=2,
        data={**CREDS, "account_id": "987654321", "public_id": PUBLIC},
    )
    entry.add_to_hass(hass)
    before = dict(entry.data)
    client.async_login.side_effect = AuthError()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}, data=before
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data == before
    client.async_logout.assert_awaited_once()
