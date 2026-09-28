"""Shared Home Assistant test fixtures."""

import pytest

pytest_plugins = ("pytest_homeassistant_custom_component",)


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations in every test."""
