"""Telos DataUpdateCoordinator."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TelosApiClient, TelosApiError, TelosAuthError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

_RETRY_DELAY = 30  # seconds before reconnecting a dropped subscription


class TelosCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches open projects and their todos from Telos.

    ``data`` is ``{"projects": [...], "todos_by_project": {project_id: [...]}}``.
    Polls every 15 minutes, and refreshes on every ``boardChanged`` event.
    """

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: TelosApiClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        # The live subscription's deadline. A refresh that finds a different
        # set of projects than it watches moves the deadline to now, which
        # breaks it out to reconnect with the new set.
        self._interrupt: asyncio.Timeout | None = None
        self._subscribed_ids: frozenset[str] = frozenset()

    @property
    def project_ids(self) -> frozenset[str]:
        if self.data is None:
            return frozenset()
        return frozenset(p["id"] for p in self.data.get("projects", []))

    def project(self, project_id: str) -> dict[str, Any] | None:
        if self.data is None:
            return None
        for project in self.data.get("projects", []):
            if project["id"] == project_id:
                return project
        return None

    def todo(self, project_id: str, todo_id: str) -> dict[str, Any] | None:
        if self.data is None:
            return None
        for todo in self.data.get("todos_by_project", {}).get(project_id, []):
            if todo["id"] == todo_id:
                return todo
        return None

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            projects = await self.client.get_projects()
        except TelosAuthError as err:
            raise ConfigEntryAuthFailed(f"Telos session expired: {err}") from err
        except TelosApiError as err:
            raise UpdateFailed(f"Error fetching Telos data: {err}") from err

        todos_by_project = {p["id"]: p.get("todos") or [] for p in projects}
        project_list = [
            {k: v for k, v in p.items() if k != "todos"} for p in projects
        ]

        if self._interrupt is not None and frozenset(todos_by_project) != self._subscribed_ids:
            self._interrupt.reschedule(asyncio.get_running_loop().time())

        return {"projects": project_list, "todos_by_project": todos_by_project}

    async def async_subscribe_updates(self) -> None:
        """Keep a live boardChanged subscription and refresh on every event.

        Reconnects after any error, and whenever the set of projects changes.
        Exits cleanly when the background task is cancelled (entry unloaded).
        """
        while True:
            self._subscribed_ids = self.project_ids
            interrupt = None
            try:
                async with asyncio.timeout(None) as interrupt:
                    self._interrupt = interrupt
                    await self._consume(self._subscribed_ids)
            except asyncio.CancelledError:
                return
            except Exception as err:  # noqa: BLE001
                if interrupt is not None and interrupt.expired():
                    continue  # the project set changed; reconnect now
                _LOGGER.warning(
                    "Telos subscription disconnected (%s) — reconnecting in %ds",
                    err,
                    _RETRY_DELAY,
                )
                try:
                    await asyncio.sleep(_RETRY_DELAY)
                except asyncio.CancelledError:
                    return
            finally:
                self._interrupt = None

    async def _consume(self, project_ids: frozenset[str]) -> None:
        if not project_ids:
            # Nothing to watch; wait for a refresh to find a project.
            await asyncio.Event().wait()
        async for _ in self.client.subscribe_board_changes(sorted(project_ids)):
            _LOGGER.debug("Board change — requesting refresh")
            await self.async_request_refresh()
        # The server closed the stream cleanly; treat it as a disconnect.
        raise TelosApiError("subscription ended")

