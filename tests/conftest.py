"""Shared Home Assistant test fixtures."""

import pytest

pytest_plugins = ("pytest_homeassistant_custom_component",)


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations in every test."""


@pytest.fixture
def recorder_config() -> dict[str, object]:
    """Keep synthetic statistics in the isolated in-memory test Recorder."""
    return {"db_url": "sqlite://", "auto_purge": False, "auto_repack": False}


@pytest.fixture
def mock_recorder_before_hass(request: pytest.FixtureRequest) -> None:
    """Resolve the Recorder database fixture before HA's autouse fixtures."""
    if "recorder_mock" in request.fixturenames:
        request.getfixturevalue("recorder_db_url")
