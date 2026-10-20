"""Tests for the Telos config flow."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.telos.api import TelosApiError, TelosConnectionError
from custom_components.telos.config_flow import _token_from_link
from custom_components.telos.const import (
    AUTH_OIDC,
    AUTH_SESSION,
    CONF_ACCESS_TOKEN,
    CONF_AUDIENCE,
    CONF_AUTH_METHOD,
    CONF_CLIENT_ID,
    CONF_EMAIL,
    CONF_EXPIRES_AT,
    CONF_ISSUER,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN,
    CONF_TOKEN_ENDPOINT,
    CONF_URL,
    CONF_USER_ID,
    DOMAIN,
)
from custom_components.telos.oidc import (
    DeviceAuthorization,
    DeviceDenied,
    DeviceExpired,
    ProviderEndpoints,
    TokenSet,
)

from .conftest import MOCK_EMAIL, MOCK_TOKEN, MOCK_URL, MOCK_USER_ID

LINK = "http://telos.local:3000/auth/verify?token=magic-123"
SESSION = {"token": MOCK_TOKEN, "userId": MOCK_USER_ID}

ISSUER = "https://id.example.com/oidc"
AUDIENCE = "http://telos.local:3001/graphql"
CLIENT_ID = "home-assistant"
ENDPOINTS = ProviderEndpoints(ISSUER, f"{ISSUER}/device/auth", f"{ISSUER}/token")
DEVICE = DeviceAuthorization(
    device_code="dev-code",
    user_code="ABCD-EFGH",
    verification_uri="https://id.example.com/device",
    verification_uri_complete="https://id.example.com/device?user_code=ABCD-EFGH",
    expires_in=600,
    interval=5,
)
TOKENS = TokenSet("access-1", "refresh-1", 2_000_000_000.0)

EMAIL_ONLY = {"magicLink": True, "secureLocalNet": False, "sso": None}
BOTH = {
    "magicLink": True,
    "secureLocalNet": False,
    "sso": {"name": "heautou", "deviceClientId": CLIENT_ID},
}
METADATA = {"resource": AUDIENCE, "authorization_servers": [ISSUER]}


@pytest.fixture
def flow_client():
    client = MagicMock()
    client.session = MagicMock()
    client.get_auth_config = AsyncMock(return_value=EMAIL_ONLY)
    client.get_user_id = AsyncMock(return_value=MOCK_USER_ID)
    client.sign_in_magic_link = AsyncMock(
        return_value={"status": True, "token": None, "userId": None}
    )
    client.magic_link_verify = AsyncMock(return_value=SESSION)
    with patch(
        "custom_components.telos.config_flow.TelosApiClient", return_value=client
    ), patch("custom_components.telos.async_setup_entry", return_value=True):
        yield client


@pytest.fixture
def device():
    """The provider's side of the device flow.

    Polling waits for ``gate`` (set by _through_progress), so the progress
    step shows first, then answers the next of ``outcomes`` (a TokenSet, or an
    exception to raise).
    """
    gate = asyncio.Event()
    outcomes: list = [TOKENS]

    async def _poll(*_args):
        await gate.wait()
        gate.clear()
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with patch(
        "custom_components.telos.config_flow.get_resource_metadata",
        AsyncMock(return_value=METADATA),
    ), patch(
        "custom_components.telos.config_flow.discover",
        AsyncMock(return_value=ENDPOINTS),
    ), patch(
        "custom_components.telos.config_flow.start_device_authorization",
        AsyncMock(return_value=DEVICE),
    ) as start, patch(
        "custom_components.telos.config_flow.poll_device_token",
        AsyncMock(side_effect=_poll),
    ) as poll:
        yield MagicMock(start=start, poll=poll, gate=gate, outcomes=outcomes)


def test_token_from_link():
    assert _token_from_link(LINK) == "magic-123"
    assert _token_from_link("  magic-123 ") == "magic-123"


async def _start(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_URL: MOCK_URL + "/"}
    )


async def _email(hass, result):
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_EMAIL: MOCK_EMAIL}
    )


async def _through_progress(hass, result, gate):
    """Let the polling task finish, then take the flow past the progress step."""
    assert result["type"] == FlowResultType.SHOW_PROGRESS
    gate.set()
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(result["flow_id"])


# ----------------------------------------------------------------------
# Choosing a method
# ----------------------------------------------------------------------


async def test_email_only_skips_menu(hass, flow_client) -> None:
    result = await _start(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "email"


async def test_menu_when_both_offered(hass, flow_client, device) -> None:
    flow_client.get_auth_config.return_value = BOTH
    result = await _start(hass)
    assert result["type"] == FlowResultType.MENU
    assert result["menu_options"] == ["browser", "email"]
    assert result["description_placeholders"] == {"provider": "heautou"}


async def test_sso_without_device_client_aborts(hass, flow_client, device) -> None:
    flow_client.get_auth_config.return_value = {
        "magicLink": False,
        "secureLocalNet": False,
        "sso": {"name": "heautou", "deviceClientId": None},
    }
    result = await _start(hass)
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "device_sign_in_off"


async def test_nothing_offered_aborts(hass, flow_client) -> None:
    flow_client.get_auth_config.return_value = {
        "magicLink": False,
        "secureLocalNet": False,
        "sso": None,
    }
    result = await _start(hass)
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "no_sign_in_method"


async def test_server_unreachable(hass, flow_client) -> None:
    flow_client.get_auth_config.side_effect = TelosConnectionError("down")
    result = await _start(hass)
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


# ----------------------------------------------------------------------
# Browser (device flow)
# ----------------------------------------------------------------------


async def _browser(hass, flow_client):
    flow_client.get_auth_config.return_value = BOTH
    result = await _start(hass)
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "browser"}
    )


async def test_device_flow(hass, flow_client, device) -> None:
    result = await _browser(hass, flow_client)
    assert result["type"] == FlowResultType.SHOW_PROGRESS
    assert result["description_placeholders"] == {
        "provider": "heautou",
        "url": DEVICE.verification_uri_complete,
        "code": "ABCD-EFGH",
    }
    device.start.assert_awaited_once()
    assert device.start.await_args.args[2:] == (CLIENT_ID, AUDIENCE)

    result = await _through_progress(hass, result, device.gate)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_URL: MOCK_URL,
        CONF_USER_ID: MOCK_USER_ID,
        CONF_AUTH_METHOD: AUTH_OIDC,
        CONF_ACCESS_TOKEN: "access-1",
        CONF_REFRESH_TOKEN: "refresh-1",
        CONF_EXPIRES_AT: 2_000_000_000.0,
        CONF_ISSUER: ISSUER,
        CONF_AUDIENCE: AUDIENCE,
        CONF_CLIENT_ID: CLIENT_ID,
        CONF_TOKEN_ENDPOINT: ENDPOINTS.token_endpoint,
    }


async def test_device_flow_only_option_skips_menu(hass, flow_client, device) -> None:
    flow_client.get_auth_config.return_value = {**BOTH, "magicLink": False}
    result = await _start(hass)
    assert result["type"] == FlowResultType.SHOW_PROGRESS


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (DeviceExpired("late"), "device_expired"),
        (DeviceDenied("no"), "device_denied"),
        (TelosConnectionError("down"), "cannot_connect_provider"),
    ],
)
async def test_device_flow_fails_then_retries(hass, flow_client, device, exc, error) -> None:
    device.outcomes[:] = [exc, TOKENS]
    result = await _through_progress(hass, await _browser(hass, flow_client), device.gate)
    assert result["step_id"] == "browser_retry"
    assert result["errors"] == {"base": error}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _through_progress(hass, result, device.gate)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert device.start.await_count == 2


async def test_device_flow_token_refused(hass, flow_client, device) -> None:
    flow_client.get_user_id.side_effect = TelosApiError("UNAUTHENTICATED")
    result = await _through_progress(hass, await _browser(hass, flow_client), device.gate)
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "token_refused"


async def test_device_start_refused(hass, flow_client, device) -> None:
    device.start.side_effect = TelosApiError("unauthorized_client")
    result = await _browser(hass, flow_client)
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "device_start_failed"


async def test_abandoned_flow_stops_polling(hass, flow_client, device) -> None:
    result = await _browser(hass, flow_client)
    task = hass.config_entries.flow._progress[result["flow_id"]]._device_task
    hass.config_entries.flow.async_abort(result["flow_id"])
    await asyncio.sleep(0)
    assert task.cancelled()


# ----------------------------------------------------------------------
# Email
# ----------------------------------------------------------------------


async def test_magic_link_flow(hass, flow_client) -> None:
    result = await _email(hass, await _start(hass))
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
        CONF_USER_ID: MOCK_USER_ID,
        CONF_AUTH_METHOD: AUTH_SESSION,
        CONF_EMAIL: MOCK_EMAIL,
        CONF_TOKEN: MOCK_TOKEN,
    }


async def test_direct_sign_in(hass, flow_client) -> None:
    """SECURE_LOCAL_NET hands back a session with no link step."""
    flow_client.get_auth_config.return_value = {**EMAIL_ONLY, "magicLink": False, "secureLocalNet": True}
    flow_client.sign_in_magic_link.return_value = {"status": True, **SESSION}

    result = await _email(hass, await _start(hass))

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

    result = await _email(hass, await _start(hass))

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "email"
    assert result["errors"] == {"base": error}


async def test_invalid_link(hass, flow_client) -> None:
    flow_client.magic_link_verify.side_effect = TelosApiError("Invalid or expired magic link")
    result = await _email(hass, await _start(hass))

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"magic_link": "nope"}
    )

    assert result["step_id"] == "link"
    assert result["errors"] == {"base": "invalid_link"}


async def test_duplicate_entry_aborted(hass, flow_client, mock_config_entry) -> None:
    result = await _email(hass, await _start(hass))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"magic_link": LINK}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ----------------------------------------------------------------------
# Reauth
# ----------------------------------------------------------------------


async def test_reauth_by_email(hass, flow_client, mock_config_entry) -> None:
    flow_client.magic_link_verify.return_value = {**SESSION, "token": "fresh-token"}

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "email"
    result = await _email(hass, result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"magic_link": LINK}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_TOKEN] == "fresh-token"


async def test_reauth_switches_to_browser(hass, flow_client, device, mock_config_entry) -> None:
    flow_client.get_auth_config.return_value = {**BOTH, "magicLink": False}

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _through_progress(hass, result, device.gate)

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_AUTH_METHOD] == AUTH_OIDC
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == "access-1"


async def test_reauth_wrong_account(hass, flow_client, mock_config_entry) -> None:
    flow_client.magic_link_verify.return_value = {**SESSION, "userId": "someone-else"}

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _email(hass, result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"magic_link": LINK}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert mock_config_entry.data[CONF_TOKEN] == MOCK_TOKEN
