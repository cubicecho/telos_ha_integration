"""Tests for the Telos config flow."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.telos.api import TelosApiError, TelosConnectionError
from custom_components.telos.config_flow import _token_from_link
from custom_components.telos.const import (
    CONF_EMAIL,
    CONF_TOKEN,
    CONF_URL,
    CONF_USER_ID,
    DOMAIN,
)

from .conftest import MOCK_EMAIL, MOCK_TOKEN, MOCK_URL, MOCK_USER_ID

LINK = "http://telos.local:3000/auth/verify?token=magic-123"
SESSION = {"token": MOCK_TOKEN, "userId": MOCK_USER_ID}


@pytest.fixture
def flow_client():
    client = MagicMock()
    client.sign_in_magic_link = AsyncMock(
        return_value={"status": True, "token": None, "userId": None}
    )
    client.magic_link_verify = AsyncMock(return_value=SESSION)
    with patch(
        "custom_components.telos.config_flow.TelosApiClient", return_value=client
    ), patch("custom_components.telos.async_setup_entry", return_value=True):
        yield client


def test_token_from_link():
    assert _token_from_link(LINK) == "magic-123"
    assert _token_from_link("  magic-123 ") == "magic-123"


async def _start(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_URL: MOCK_URL + "/", CONF_EMAIL: MOCK_EMAIL},
    )


async def test_magic_link_flow(hass, flow_client) -> None:
    result = await _start(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "link"
    flow_client.sign_in_magic_link.assert_called_once_with(MOCK_EMAIL)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"magic_link": LINK}
    )

    flow_client.magic_link_verify.assert_called_once_with("magic-123")
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "telos.local:3001"
    assert result["data"] == {
        CONF_URL: MOCK_URL,
        CONF_EMAIL: MOCK_EMAIL,
        CONF_TOKEN: MOCK_TOKEN,
        CONF_USER_ID: MOCK_USER_ID,
    }


async def test_direct_sign_in(hass, flow_client) -> None:
    """SECURE_LOCAL_NET hands back a session with no link step."""
    flow_client.sign_in_magic_link.return_value = {"status": True, **SESSION}

    result = await _start(hass)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN] == MOCK_TOKEN
    flow_client.magic_link_verify.assert_not_called()


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (TelosConnectionError("down"), "cannot_connect"),
        (TelosApiError("slow down", "TOO_MANY_REQUESTS"), "rate_limited"),
        (TelosApiError("cannot email links", "BAD_USER_INPUT"), "request_refused"),
    ],
)
async def test_request_errors(hass, flow_client, exc, error) -> None:
    flow_client.sign_in_magic_link.side_effect = exc

    result = await _start(hass)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}


async def test_invalid_link(hass, flow_client) -> None:
    flow_client.magic_link_verify.side_effect = TelosApiError("Invalid or expired magic link")
    result = await _start(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"magic_link": "nope"}
    )

    assert result["step_id"] == "link"
    assert result["errors"] == {"base": "invalid_link"}


async def test_duplicate_entry_aborted(hass, flow_client, mock_config_entry) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"magic_link": LINK}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass, flow_client, mock_config_entry) -> None:
    flow_client.magic_link_verify.return_value = {**SESSION, "token": "fresh-token"}

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_EMAIL: MOCK_EMAIL}
    )
    assert result["step_id"] == "link"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"magic_link": LINK}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_TOKEN] == "fresh-token"


async def test_reauth_wrong_account(hass, flow_client, mock_config_entry) -> None:
    flow_client.magic_link_verify.return_value = {**SESSION, "userId": "someone-else"}

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_EMAIL: MOCK_EMAIL}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"magic_link": LINK}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert mock_config_entry.data[CONF_TOKEN] == MOCK_TOKEN
