"""Signing in through Telos's OIDC provider with the device flow (RFC 8628).

Telos signs people in through an OpenID Connect provider (heautou). Home
Assistant cannot take part in a browser redirect, so it uses the device flow
instead. It asks the provider for a code, shows you a link to approve it, and
polls until you do. What comes back is an access token whose audience is Telos
(RFC 8707 resource indicator), which Telos accepts as you, plus a refresh token
that keeps it fresh.

Where to go comes from Telos itself:
- ``/.well-known/oauth-protected-resource`` (RFC 9728) names the provider
  (``authorization_servers``) and Telos's audience (``resource``).
- ``authConfig.sso.deviceClientId`` names the public client to use.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from .api import TelosApiError, TelosAuthError, TelosConnectionError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_AUDIENCE,
    CONF_CLIENT_ID,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_ENDPOINT,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SCOPE = "openid email offline_access"

# Refresh this long before the access token expires.
REFRESH_MARGIN = 60  # seconds
# RFC 8628 §3.5: add 5 seconds to the interval on slow_down.
_SLOW_DOWN_STEP = 5


class DeviceFlowError(TelosApiError):
    """The device sign-in ended without a token."""


class DeviceExpired(DeviceFlowError):
    """The code expired before it was approved."""


class DeviceDenied(DeviceFlowError):
    """The code was denied at the provider."""


@dataclass(frozen=True)
class ProviderEndpoints:
    issuer: str
    device_authorization_endpoint: str
    token_endpoint: str


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: float  # unix time


async def _json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    **kwargs: Any,
) -> tuple[int, dict[str, Any]]:
    try:
        async with session.request(method, url, timeout=_TIMEOUT, **kwargs) as resp:
            try:
                body = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                body = None
            return resp.status, body if isinstance(body, dict) else {}
    except aiohttp.ClientConnectionError as err:
        raise TelosConnectionError(f"Cannot connect to {url}") from err
    except (TimeoutError, aiohttp.ClientError) as err:
        raise TelosConnectionError(f"Request to {url} failed: {err}") from err


async def get_resource_metadata(
    session: aiohttp.ClientSession, url: str
) -> dict[str, Any] | None:
    """Telos's protected-resource metadata, or None when it serves none."""
    status, body = await _json(
        session,
        "GET",
        f"{url.rstrip('/')}/.well-known/oauth-protected-resource",
        headers={"accept": "application/json"},
    )
    if status != 200 or not body.get("resource"):
        return None
    return body


async def discover(session: aiohttp.ClientSession, issuer: str) -> ProviderEndpoints:
    """Read the provider's discovery document and check it is the issuer's own."""
    issuer = issuer.rstrip("/")
    status, doc = await _json(
        session,
        "GET",
        f"{issuer}/.well-known/openid-configuration",
        headers={"accept": "application/json"},
    )
    if status != 200 or str(doc.get("issuer", "")).rstrip("/") != issuer:
        raise TelosApiError(f"{issuer} is not an OpenID Connect provider")
    device = doc.get("device_authorization_endpoint")
    token = doc.get("token_endpoint")
    if not device or not token:
        raise TelosApiError(f"{issuer} does not offer device sign-in")
    return ProviderEndpoints(issuer, device, token)


def _error(body: dict[str, Any], status: int) -> str:
    return str(body.get("error_description") or body.get("error") or f"HTTP {status}")


async def start_device_authorization(
    session: aiohttp.ClientSession,
    endpoints: ProviderEndpoints,
    client_id: str,
    resource: str,
) -> DeviceAuthorization:
    status, body = await _json(
        session,
        "POST",
        endpoints.device_authorization_endpoint,
        data={"client_id": client_id, "scope": SCOPE, "resource": resource},
    )
    if status != 200 or not body.get("device_code"):
        raise TelosApiError(
            f"The provider refused to start a sign-in: {_error(body, status)}",
            body.get("error"),
        )
    return DeviceAuthorization(
        device_code=body["device_code"],
        user_code=body["user_code"],
        verification_uri=body["verification_uri"],
        verification_uri_complete=body.get("verification_uri_complete"),
        expires_in=int(body.get("expires_in", 600)),
        interval=int(body.get("interval", 5)),
    )


def _tokens(body: dict[str, Any], refresh_token: str | None = None) -> TokenSet:
    return TokenSet(
        access_token=body["access_token"],
        # A refresh that does not rotate the token leaves it out.
        refresh_token=body.get("refresh_token") or refresh_token,
        expires_at=time.time() + int(body.get("expires_in", 300)),
    )


async def poll_device_token(
    session: aiohttp.ClientSession,
    endpoints: ProviderEndpoints,
    client_id: str,
    device: DeviceAuthorization,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> TokenSet:
    """Poll the token endpoint until the code is approved, denied or expired."""
    interval = device.interval
    deadline = time.monotonic() + device.expires_in
    while True:
        await sleep(interval)
        status, body = await _json(
            session,
            "POST",
            endpoints.token_endpoint,
            data={
                "grant_type": _DEVICE_GRANT,
                "device_code": device.device_code,
                "client_id": client_id,
            },
        )
        if status == 200 and body.get("access_token"):
            return _tokens(body)
        error = body.get("error")
        if error == "authorization_pending":
            pass
        elif error == "slow_down":
            interval += _SLOW_DOWN_STEP
        elif error == "access_denied":
            raise DeviceDenied("The sign-in was denied", error)
        elif error == "expired_token":
            raise DeviceExpired("The sign-in code expired", error)
        else:
            raise DeviceFlowError(
                f"The provider refused the sign-in: {_error(body, status)}", error
            )
        if time.monotonic() >= deadline:
            raise DeviceExpired("The sign-in code expired", "expired_token")


async def refresh_tokens(
    session: aiohttp.ClientSession,
    token_endpoint: str,
    client_id: str,
    refresh_token: str,
    resource: str,
) -> TokenSet:
    """Trade the refresh token for a fresh access token for Telos.

    A refused refresh (``invalid_grant``: the grant was revoked or expired)
    raises TelosAuthError, which sends the entry to reauth.
    """
    status, body = await _json(
        session,
        "POST",
        token_endpoint,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "resource": resource,
        },
    )
    if status == 200 and body.get("access_token"):
        return _tokens(body, refresh_token)
    if status >= 500:
        raise TelosConnectionError(f"The provider answered {status} to a refresh")
    raise TelosAuthError(f"The provider refused the refresh: {_error(body, status)}")


class OidcTokenSource:
    """Hands out the entry's access token, refreshing it when it is about to expire.

    New tokens are written back to the entry, so a restart picks them up.
    One refresh runs at a time; callers that arrive meanwhile get its result.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        data: dict[str, Any],
        save: Callable[[TokenSet], None],
    ) -> None:
        self._session = session
        self._save = save
        self._client_id: str = data[CONF_CLIENT_ID]
        self._audience: str = data[CONF_AUDIENCE]
        self._token_endpoint: str = data[CONF_TOKEN_ENDPOINT]
        self._tokens = TokenSet(
            access_token=data[CONF_ACCESS_TOKEN],
            refresh_token=data.get(CONF_REFRESH_TOKEN),
            expires_at=float(data.get(CONF_EXPIRES_AT, 0)),
        )
        self._lock = asyncio.Lock()

    async def async_token(self) -> str | None:
        if self._tokens.expires_at - REFRESH_MARGIN <= time.time():
            await self._refresh(self._tokens.access_token)
        return self._tokens.access_token

    async def async_refresh(self) -> bool:
        if not self._tokens.refresh_token:
            return False
        await self._refresh(self._tokens.access_token)
        return True

    async def _refresh(self, stale: str) -> None:
        async with self._lock:
            if self._tokens.access_token != stale:
                return  # another caller refreshed while this one waited
            if not self._tokens.refresh_token:
                raise TelosAuthError("The access token expired and there is no refresh token")
            _LOGGER.debug("Refreshing the Telos access token")
            self._tokens = await refresh_tokens(
                self._session,
                self._token_endpoint,
                self._client_id,
                self._tokens.refresh_token,
                self._audience,
            )
            self._save(self._tokens)
