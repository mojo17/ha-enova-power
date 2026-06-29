"""Shared fixtures for Enova Power tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    yield


@pytest.fixture
def mock_client():
    """Patch AsyncEnovaClient in the config flow with a logged-in mock."""
    with patch(
        "custom_components.enova_power.config_flow.AsyncEnovaClient", autospec=True
    ) as mock_cls:
        client = mock_cls.return_value
        client.login = AsyncMock()
        client.close = AsyncMock()
        client.account_number = "1234567890"
        client.meter_id = "111111"
        client.meter_ids = ["111111"]
        yield client
