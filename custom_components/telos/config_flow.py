"""Config flow for the Telos integration.

Telos's API keys are its AI/MCP door, so the integration signs in as a person,
in one of the ways the instance offers (``authConfig``):

- ``browser``: Telos signs people in through an OIDC provider (heautou). The
  integration runs the provider's device flow (oidc.py): you open a link,
  sign in there and approve, and Home Assistant gets an access token for Telos
  plus a refresh token.
- ``email``: ``signInMagicLink`` emails a link, and the ``link`` step trades it
  (``magicLinkVerify``) for a 30-day session token, refreshed as it is used.
  Under ``SECURE_LOCAL_NET`` the address alone opens a session.

When sign-in lapses the coordinator raises ConfigEntryAuthFailed, and
``reauth_confirm`` offers the same choice for the stored URL.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TelosApiClient, TelosApiError, TelosConnectionError
from .const import (
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
from .oidc import (
    DeviceAuthorization,
    DeviceDenied,
    DeviceExpired,
    ProviderEndpoints,
    TokenSet,
    discover,
    get_resource_metadata,
    poll_device_token,
    start_device_authorization,
)

_LOGGER = logging.getLogger(__name__)

CONF_MAGIC_LINK = "magic_link"

_URL_SCHEMA = vol.Schema({vol.Required(CONF_URL): str})


def _token_from_link(value: str) -> str:
    """Accept a full magic link (``…/auth/verify?token=…``) or a bare token."""
    value = value.strip()
    token = parse_qs(urlparse(value).query).get("token")
    return token[0] if token else value


class TelosConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in to Telos."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._url: str = ""
        self._email: str = ""
        # From the server: what it offers, and where its provider is.
        self._provider_name: str = ""
        self._client_id: str | None = None
        self._audience: str | None = None
        self._issuer: str | None = None
        # The device sign-in in progress.
        self._endpoints: ProviderEndpoints | None = None
        self._device: DeviceAuthorization | None = None
        self._device_task: asyncio.Task[TokenSet] | None = None
        self._device_error: str = ""
        self._tokens: TokenSet | None = None
        self._offers_email = False

    def _client(self, token: str | None = None) -> TelosApiClient:
        return TelosApiClient(self._url, token, async_get_clientsession(self.hass))

    # ------------------------------------------------------------------
    # Server, then how to sign in
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._url = user_input[CONF_URL].strip().rstrip("/")
            error = await self._probe()
            if not error:
                return await self._choose()
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_URL_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        self._url = entry_data[CONF_URL]
        self._email = entry_data.get(CONF_EMAIL, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            error = await self._probe()
            if not error:
                return await self._choose()
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={"url": self._url},
            errors=errors,
        )

    async def _probe(self) -> str:
        """Ask the server how it signs people in. Returns an error key, or ""."""
        client = self._client()
        try:
            config = await client.get_auth_config()
            sso = config.get("sso") or {}
            self._offers_email = bool(config.get("magicLink") or config.get("secureLocalNet"))
            self._provider_name = sso.get("name") or ""
            self._client_id = sso.get("deviceClientId")
            self._issuer = self._audience = None
            if sso:
                metadata = await get_resource_metadata(client.session, self._url)
                servers = (metadata or {}).get("authorization_servers") or []
                if metadata and servers:
                    self._audience = metadata["resource"]
                    self._issuer = servers[0]
        except TelosConnectionError:
            return "cannot_connect"
        except TelosApiError:
            # Not a Telos server, or one too old to answer authConfig.
            _LOGGER.exception("Telos did not say how to sign in")
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error reaching Telos")
            return "unknown"
        return ""

    @property
    def _offers_browser(self) -> bool:
        return bool(self._client_id and self._issuer and self._audience)

    async def _choose(self) -> ConfigFlowResult:
        options = []
        if self._offers_browser:
            options.append("browser")
        if self._offers_email:
            options.append("email")

        if not options:
            if self._provider_name:
                return self.async_abort(
                    reason="device_sign_in_off",
                    description_placeholders={"provider": self._provider_name},
                )
            return self.async_abort(reason="no_sign_in_method")
        if len(options) == 1:
            return await getattr(self, f"async_step_{options[0]}")()
        return self.async_show_menu(
            step_id="method",
            menu_options=options,
            description_placeholders={"provider": self._provider_name},
        )

    async def async_step_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The menu itself; HA routes a choice straight to its step."""
        return await self._choose()

    # ------------------------------------------------------------------
    # Browser: the provider's device flow
    # ------------------------------------------------------------------

    async def async_step_browser(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._device_task is None:
            assert self._issuer and self._audience and self._client_id
            session = async_get_clientsession(self.hass)
            try:
                self._endpoints = await discover(session, self._issuer)
                self._device = await start_device_authorization(
                    session, self._endpoints, self._client_id, self._audience
                )
            except TelosConnectionError:
                return self.async_abort(reason="cannot_connect_provider")
            except TelosApiError as err:
                _LOGGER.warning("Could not start a device sign-in: %s", err)
                return self.async_abort(
                    reason="device_start_failed",
                    description_placeholders={"provider": self._provider_name},
                )
            self._device_task = self.hass.async_create_task(
                poll_device_token(session, self._endpoints, self._client_id, self._device)
            )

        assert self._device is not None
        if not self._device_task.done():
            return self.async_show_progress(
                step_id="browser",
                progress_action="wait_for_approval",
                description_placeholders={
                    "provider": self._provider_name,
                    "url": self._device.verification_uri_complete
                    or self._device.verification_uri,
                    "code": self._device.user_code,
                },
                progress_task=self._device_task,
            )

        task, self._device_task = self._device_task, None
        try:
            self._tokens = task.result()
        except DeviceExpired:
            self._device_error = "device_expired"
        except DeviceDenied:
            self._device_error = "device_denied"
        except TelosConnectionError:
            self._device_error = "cannot_connect_provider"
        except Exception:
            _LOGGER.exception("Unexpected error during the device sign-in")
            self._device_error = "unknown"
        else:
            return self.async_show_progress_done(next_step_id="browser_done")
        return self.async_show_progress_done(next_step_id="browser_retry")

    async def async_step_browser_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The sign-in ended without a token; offer to start it again."""
        if user_input is not None:
            return await self.async_step_browser()
        return self.async_show_form(
            step_id="browser_retry",
            errors={"base": self._device_error},
            description_placeholders={"provider": self._provider_name},
        )

    async def async_step_browser_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._tokens and self._endpoints and self._client_id and self._audience
        try:
            user_id = await self._client(self._tokens.access_token).get_user_id()
        except TelosConnectionError:
            return self.async_abort(reason="cannot_connect")
        except TelosApiError as err:
            # Most likely: the provider account was never used to sign in to
            # Telos itself, so Telos has no link from it to a Telos account.
            _LOGGER.warning("Telos refused the provider's token: %s", err)
            return self.async_abort(
                reason="token_refused",
                description_placeholders={"provider": self._provider_name},
            )
        return await self._finish(
            user_id,
            {
                CONF_AUTH_METHOD: AUTH_OIDC,
                CONF_ACCESS_TOKEN: self._tokens.access_token,
                CONF_REFRESH_TOKEN: self._tokens.refresh_token,
                CONF_EXPIRES_AT: self._tokens.expires_at,
                CONF_ISSUER: self._endpoints.issuer,
                CONF_AUDIENCE: self._audience,
                CONF_CLIENT_ID: self._client_id,
                CONF_TOKEN_ENDPOINT: self._endpoints.token_endpoint,
            },
        )

    # ------------------------------------------------------------------
    # Email: a magic link, or SECURE_LOCAL_NET
    # ------------------------------------------------------------------

    async def async_step_email(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            result, error = await self._request_link()
            if result is not None:
                return result
            errors["base"] = error

        return self.async_show_form(
            step_id="email",
            data_schema=vol.Schema(
                {vol.Required(CONF_EMAIL, default=self._email or vol.UNDEFINED): str}
            ),
            errors=errors,
        )

    async def async_step_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            token = _token_from_link(user_input[CONF_MAGIC_LINK])
            try:
                session = await self._client().magic_link_verify(token)
            except TelosConnectionError:
                errors["base"] = "cannot_connect"
            except TelosApiError:
                errors["base"] = "invalid_link"
            except Exception:
                _LOGGER.exception("Unexpected error verifying a Telos magic link")
                errors["base"] = "unknown"
            else:
                return await self._finish_session(session["token"], session["userId"])

        return self.async_show_form(
            step_id="link",
            data_schema=vol.Schema({vol.Required(CONF_MAGIC_LINK): str}),
            description_placeholders={"email": self._email},
            errors=errors,
        )

    async def _request_link(self) -> tuple[ConfigFlowResult | None, str]:
        """Ask for a magic link; returns the next step, or an error key."""
        try:
            answer = await self._client().sign_in_magic_link(self._email)
        except TelosConnectionError:
            return None, "cannot_connect"
        except TelosApiError as err:
            _LOGGER.warning("Telos refused the magic link request: %s", err)
            if err.code == "TOO_MANY_REQUESTS":
                return None, "rate_limited"
            # BAD_USER_INPUT: not an address, or the instance cannot email links.
            return None, "request_refused"
        except Exception:
            _LOGGER.exception("Unexpected error requesting a Telos magic link")
            return None, "unknown"

        if answer.get("token") and answer.get("userId"):
            # SECURE_LOCAL_NET: signed in directly.
            return await self._finish_session(answer["token"], answer["userId"]), ""

        return await self.async_step_link(), ""

    # ------------------------------------------------------------------
    # The entry
    # ------------------------------------------------------------------

    async def _finish_session(self, token: str, user_id: str) -> ConfigFlowResult:
        return await self._finish(
            user_id,
            {CONF_AUTH_METHOD: AUTH_SESSION, CONF_EMAIL: self._email, CONF_TOKEN: token},
        )

    async def _finish(self, user_id: str, auth: dict[str, Any]) -> ConfigFlowResult:
        await self.async_set_unique_id(f"{self._url}_{user_id}")
        data = {CONF_URL: self._url, CONF_USER_ID: user_id, **auth}

        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=data
            )

        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=_entry_title(self._url), data=data)

    def async_remove(self) -> None:
        """Stop polling when the flow is abandoned."""
        if self._device_task is not None:
            self._device_task.cancel()


def _entry_title(url: str) -> str:
    """Derive a human-readable title from the server URL."""
    # Strip scheme for brevity: "http://192.168.1.10:3001" → "192.168.1.10:3001"
    for scheme in ("https://", "http://"):
        if url.startswith(scheme):
            return url[len(scheme):]
    return url
