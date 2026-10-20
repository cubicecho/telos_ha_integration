"""Tests for the device flow, token refresh, and how the client uses them."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.telos.api import TelosApiClient, TelosApiError, TelosAuthError
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
    OidcTokenSource,
    ProviderEndpoints,
    TokenSet,
    discover,
    get_resource_metadata,
    poll_device_token,
    refresh_tokens,
    start_device_authorization,
)

from .conftest import MOCK_EMAIL, MOCK_TOKEN, MOCK_URL, MOCK_USER_ID

ISSUER = "https://id.example.com/oidc"
TOKEN_URL = f"{ISSUER}/token"
DEVICE_URL = f"{ISSUER}/device/auth"
AUDIENCE = f"{MOCK_URL}/graphql"
ENDPOINTS = ProviderEndpoints(ISSUER, DEVICE_URL, TOKEN_URL)
DEVICE = DeviceAuthorization("dev", "ABCD", "https://id/device", None, 600, 5)


async def _no_sleep(_seconds: float) -> None:
    return None


def _form(call) -> dict[str, str]:
    """The form fields aioclient_mock recorded for a call."""
    return dict(call[2])


async def test_discover(hass, aioclient_mock) -> None:
    aioclient_mock.get(
        f"{ISSUER}/.well-known/openid-configuration",
        json={
            "issuer": ISSUER,
            "device_authorization_endpoint": DEVICE_URL,
            "token_endpoint": TOKEN_URL,
        },
    )
    assert await discover(async_get_clientsession(hass), ISSUER + "/") == ENDPOINTS


@pytest.mark.parametrize(
    "doc",
    [
        {"issuer": "https://evil.example.com", "device_authorization_endpoint": DEVICE_URL, "token_endpoint": TOKEN_URL},
        {"issuer": ISSUER, "token_endpoint": TOKEN_URL},
    ],
)
async def test_discover_refuses(hass, aioclient_mock, doc) -> None:
    aioclient_mock.get(f"{ISSUER}/.well-known/openid-configuration", json=doc)
    with pytest.raises(TelosApiError):
        await discover(async_get_clientsession(hass), ISSUER)


async def test_resource_metadata(hass, aioclient_mock) -> None:
    url = f"{MOCK_URL}/.well-known/oauth-protected-resource"
    aioclient_mock.get(url, json={"resource": AUDIENCE, "authorization_servers": [ISSUER]})
    meta = await get_resource_metadata(async_get_clientsession(hass), MOCK_URL)
    assert meta == {"resource": AUDIENCE, "authorization_servers": [ISSUER]}

    aioclient_mock.clear_requests()
    aioclient_mock.get(url, status=404)
    assert await get_resource_metadata(async_get_clientsession(hass), MOCK_URL) is None


async def test_start_device_authorization(hass, aioclient_mock) -> None:
    aioclient_mock.post(
        DEVICE_URL,
        json={
            "device_code": "dev",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://id/device",
            "verification_uri_complete": "https://id/device?user_code=ABCD-EFGH",
            "expires_in": 600,
            "interval": 5,
        },
    )
    device = await start_device_authorization(
        async_get_clientsession(hass), ENDPOINTS, "ha", AUDIENCE
    )
    assert device.user_code == "ABCD-EFGH"
    assert _form(aioclient_mock.mock_calls[0]) == {
        "client_id": "ha",
        "scope": "openid email offline_access",
        "resource": AUDIENCE,
    }


async def test_start_device_authorization_refused(hass, aioclient_mock) -> None:
    aioclient_mock.post(DEVICE_URL, status=400, json={"error": "invalid_target"})
    with pytest.raises(TelosApiError) as err:
        await start_device_authorization(async_get_clientsession(hass), ENDPOINTS, "ha", AUDIENCE)
    assert err.value.code == "invalid_target"


class _Answers:
    """Token endpoint answers, in order, as (status, json)."""

    def __init__(self, *answers):
        self.answers = list(answers)

    async def __call__(self, method, url, data):
        from pytest_homeassistant_custom_component.test_util.aiohttp import (
            AiohttpClientMockResponse,
        )

        status, body = self.answers.pop(0)
        return AiohttpClientMockResponse(method, url, status=status, json=body)


async def test_poll_pending_slow_down_then_tokens(hass, aioclient_mock) -> None:
    aioclient_mock.post(
        TOKEN_URL,
        side_effect=_Answers(
            (400, {"error": "authorization_pending"}),
            (400, {"error": "slow_down"}),
            (200, {"access_token": "a", "refresh_token": "r", "expires_in": 600}),
        ),
    )
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    tokens = await poll_device_token(async_get_clientsession(hass), ENDPOINTS, "ha", DEVICE, _sleep)

    assert tokens.access_token == "a"
    assert tokens.refresh_token == "r"
    assert tokens.expires_at == pytest.approx(time.time() + 600, abs=5)
    assert slept == [5, 5, 10]
    assert _form(aioclient_mock.mock_calls[0]) == {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": "dev",
        "client_id": "ha",
    }


@pytest.mark.parametrize(
    ("error", "exc"), [("access_denied", DeviceDenied), ("expired_token", DeviceExpired)]
)
async def test_poll_ends(hass, aioclient_mock, error, exc) -> None:
    aioclient_mock.post(TOKEN_URL, status=400, json={"error": error})
    with pytest.raises(exc):
        await poll_device_token(async_get_clientsession(hass), ENDPOINTS, "ha", DEVICE, _no_sleep)


async def test_refresh(hass, aioclient_mock) -> None:
    aioclient_mock.post(TOKEN_URL, json={"access_token": "a2", "expires_in": 600})
    tokens = await refresh_tokens(async_get_clientsession(hass), TOKEN_URL, "ha", "r1", AUDIENCE)
    # Not rotated: the old refresh token stays.
    assert (tokens.access_token, tokens.refresh_token) == ("a2", "r1")
    assert _form(aioclient_mock.mock_calls[0]) == {
        "grant_type": "refresh_token",
        "refresh_token": "r1",
        "client_id": "ha",
        "resource": AUDIENCE,
    }


async def test_refresh_refused_is_auth_error(hass, aioclient_mock) -> None:
    aioclient_mock.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
    with pytest.raises(TelosAuthError):
        await refresh_tokens(async_get_clientsession(hass), TOKEN_URL, "ha", "r1", AUDIENCE)


# ----------------------------------------------------------------------
# OidcTokenSource and the client
# ----------------------------------------------------------------------

OIDC_DATA = {
    CONF_ACCESS_TOKEN: "a1",
    CONF_REFRESH_TOKEN: "r1",
    CONF_EXPIRES_AT: time.time() + 3600,
    CONF_AUDIENCE: AUDIENCE,
    CONF_CLIENT_ID: "ha",
    CONF_TOKEN_ENDPOINT: TOKEN_URL,
}


def _source(data, refreshed):
    save = MagicMock()
    with patch(
        "custom_components.telos.oidc.refresh_tokens",
        AsyncMock(side_effect=lambda *a: refreshed.pop(0)),
    ) as refresh:
        yield OidcTokenSource(MagicMock(), data, save), save, refresh


async def test_token_source_uses_fresh_token() -> None:
    for source, save, refresh in _source(dict(OIDC_DATA), []):
        assert await source.async_token() == "a1"
        refresh.assert_not_called()
        save.assert_not_called()


async def test_token_source_refreshes_near_expiry() -> None:
    new = TokenSet("a2", "r2", time.time() + 600)
    data = {**OIDC_DATA, CONF_EXPIRES_AT: time.time() + 10}
    for source, save, refresh in _source(data, [new]):
        assert await source.async_token() == "a2"
        assert await source.async_token() == "a2"
        refresh.assert_awaited_once()
        assert refresh.await_args.args[1:] == (TOKEN_URL, "ha", "r1", AUDIENCE)
        save.assert_called_once_with(new)


async def test_client_refreshes_and_retries_once_on_auth_error(hass, aioclient_mock) -> None:
    """Telos refusing a token that has not expired yet: refresh, then retry once."""
    calls: list[str] = []

    async def _graphql(method, url, data):
        from pytest_homeassistant_custom_component.test_util.aiohttp import (
            AiohttpClientMockResponse,
        )

        calls.append(aioclient_mock.mock_calls[-1][3]["Authorization"])
        if len(calls) == 1:
            body = {"errors": [{"message": "no", "extensions": {"code": "UNAUTHENTICATED"}}]}
        else:
            body = {"data": {"users": [{"id": MOCK_USER_ID}]}}
        return AiohttpClientMockResponse(method, url, json=body)

    aioclient_mock.post(f"{MOCK_URL}/graphql", side_effect=_graphql)
    new = TokenSet("a2", "r2", time.time() + 600)
    for source, _save, _refresh in _source(dict(OIDC_DATA), [new]):
        client = TelosApiClient(MOCK_URL, source, async_get_clientsession(hass))
        assert await client.get_user_id() == MOCK_USER_ID
    assert calls == ["Bearer a1", "Bearer a2"]


async def test_session_client_does_not_retry(hass, aioclient_mock) -> None:
    aioclient_mock.post(
        f"{MOCK_URL}/graphql",
        json={"errors": [{"message": "no", "extensions": {"code": "UNAUTHENTICATED"}}]},
    )
    client = TelosApiClient(MOCK_URL, MOCK_TOKEN, async_get_clientsession(hass))
    with pytest.raises(TelosAuthError):
        await client.get_user_id()
    assert aioclient_mock.call_count == 1


# ----------------------------------------------------------------------
# Setup and migration
# ----------------------------------------------------------------------


async def test_migration_marks_old_entries_as_sessions(hass, mock_api_client) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=1,
        unique_id=f"{MOCK_URL}_{MOCK_USER_ID}",
        data={CONF_URL: MOCK_URL, CONF_EMAIL: MOCK_EMAIL, CONF_TOKEN: MOCK_TOKEN, CONF_USER_ID: MOCK_USER_ID},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.telos.TelosApiClient", return_value=mock_api_client):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.minor_version == 2
    assert entry.data[CONF_AUTH_METHOD] == AUTH_SESSION
    assert entry.data[CONF_TOKEN] == MOCK_TOKEN


async def test_oidc_entry_saves_refreshed_tokens(hass, mock_api_client) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        unique_id=f"{MOCK_URL}_{MOCK_USER_ID}",
        data={
            CONF_URL: MOCK_URL,
            CONF_USER_ID: MOCK_USER_ID,
            CONF_AUTH_METHOD: AUTH_OIDC,
            CONF_ISSUER: ISSUER,
            **OIDC_DATA,
            CONF_EXPIRES_AT: 0,
        },
    )
    entry.add_to_hass(hass)
    built: list = []

    def _client(url, token, session):
        built.append(token)
        return mock_api_client

    new = TokenSet("a2", "r2", 2_000_000_000.0)
    with patch("custom_components.telos.TelosApiClient", side_effect=_client), patch(
        "custom_components.telos.oidc.refresh_tokens", AsyncMock(return_value=new)
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # The coordinator's mock client never asks; ask as the real one would.
        assert await built[0].async_token() == "a2"

    assert isinstance(built[0], OidcTokenSource)
    assert entry.data[CONF_ACCESS_TOKEN] == "a2"
    assert entry.data[CONF_REFRESH_TOKEN] == "r2"
    assert entry.data[CONF_EXPIRES_AT] == 2_000_000_000.0
