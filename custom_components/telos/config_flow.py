"""Config flow for the Telos integration.

Telos's API keys are its AI/MCP door, so the integration signs in as a person
with a magic link, exactly as the Telos app does:

1. ``user``: server URL + email → ``signInMagicLink``. A server running with
   ``SECURE_LOCAL_NET`` answers with a session straight away.
2. ``link``: paste the emailed link or its token → ``magicLinkVerify`` → a
   30-day session token, refreshed as it is used.

When the session lapses the coordinator raises ConfigEntryAuthFailed and
``reauth_confirm`` runs the same two steps for the stored URL.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TelosApiClient, TelosApiError, TelosConnectionError
from .const import CONF_EMAIL, CONF_TOKEN, CONF_URL, CONF_USER_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_MAGIC_LINK = "magic_link"

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Required(CONF_EMAIL): str,
    }
)


def _token_from_link(value: str) -> str:
    """Accept a full magic link (``…/auth/verify?token=…``) or a bare token."""
    value = value.strip()
    token = parse_qs(urlparse(value).query).get("token")
    return token[0] if token else value


class TelosConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in to Telos with a magic link."""

    VERSION = 1

    def __init__(self) -> None:
        self._url: str = ""
        self._email: str = ""

    def _client(self, token: str | None = None) -> TelosApiClient:
        return TelosApiClient(self._url, token, async_get_clientsession(self.hass))

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._url = user_input[CONF_URL].strip().rstrip("/")
            self._email = user_input[CONF_EMAIL].strip()
            result, error = await self._request_link()
            if result is not None:
                return result
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        self._url = entry_data[CONF_URL]
        self._email = entry_data[CONF_EMAIL]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
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
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_EMAIL, default=self._email): str}
            ),
            description_placeholders={"url": self._url},
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
                return await self._finish(session["token"], session["userId"])

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
            return await self._finish(answer["token"], answer["userId"]), ""

        return await self.async_step_link(), ""

    async def _finish(self, token: str, user_id: str) -> ConfigFlowResult:
        await self.async_set_unique_id(f"{self._url}_{user_id}")
        data = {
            CONF_URL: self._url,
            CONF_EMAIL: self._email,
            CONF_TOKEN: token,
            CONF_USER_ID: user_id,
        }

        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=data
            )

        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=_entry_title(self._url), data=data)


def _entry_title(url: str) -> str:
    """Derive a human-readable title from the server URL."""
    # Strip scheme for brevity: "http://192.168.1.10:3001" → "192.168.1.10:3001"
    for scheme in ("https://", "http://"):
        if url.startswith(scheme):
            return url[len(scheme):]
    return url
