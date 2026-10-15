"""Telos Home Assistant integration."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TelosApiClient
from .const import CONF_TOKEN, CONF_URL, PLATFORMS
from .coordinator import TelosCoordinator


@dataclass
class TelosData:
    """Runtime data stored on the config entry."""

    coordinator: TelosCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Telos from a config entry."""
    session = async_get_clientsession(hass)
    client = TelosApiClient(
        url=entry.data[CONF_URL],
        token=entry.data[CONF_TOKEN],
        session=session,
    )
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
