"""Telos Home Assistant integration."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TelosApiClient, TokenSource
from .const import (
    AUTH_OIDC,
    AUTH_SESSION,
    CONF_ACCESS_TOKEN,
    CONF_AUTH_METHOD,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN,
    CONF_URL,
    PLATFORMS,
)
from .coordinator import TelosCoordinator
from .oidc import OidcTokenSource, TokenSet

_LOGGER = logging.getLogger(__name__)


@dataclass
class TelosData:
    """Runtime data stored on the config entry."""

    coordinator: TelosCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Telos from a config entry."""
    session = async_get_clientsession(hass)
    token: str | TokenSource
    if entry.data.get(CONF_AUTH_METHOD) == AUTH_OIDC:

        def _save(tokens: TokenSet) -> None:
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_ACCESS_TOKEN: tokens.access_token,
                    CONF_REFRESH_TOKEN: tokens.refresh_token,
                    CONF_EXPIRES_AT: tokens.expires_at,
                },
            )

        token = OidcTokenSource(session, dict(entry.data), _save)
    else:
        token = entry.data[CONF_TOKEN]
    client = TelosApiClient(url=entry.data[CONF_URL], token=token, session=session)
    coordinator = TelosCoordinator(hass, entry, client)

    # Raises ConfigEntryAuthFailed (→ reauth) or ConfigEntryNotReady (→ retry).
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = TelosData(coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    sub_task = hass.async_create_background_task(
        coordinator.async_subscribe_updates(),
        f"telos_subscription_{entry.entry_id}",
    )

    def _cancel_subscription() -> None:
        sub_task.cancel()

    entry.async_on_unload(_cancel_subscription)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """1.1 → 1.2: say how the entry signs in. Every older entry holds a session."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        hass.config_entries.async_update_entry(
            entry,
            data={CONF_AUTH_METHOD: AUTH_SESSION, **entry.data},
            minor_version=2,
        )
        _LOGGER.debug("Migrated Telos entry %s to 1.2", entry.entry_id)
    return True
