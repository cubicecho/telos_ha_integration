"""Telos todo list platform: one todo list per open project."""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TelosData
from .api import TelosApiError
from .const import DOMAIN
from .coordinator import TelosCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one TelosTodoListEntity per project, and more as projects appear."""
    data: TelosData = entry.runtime_data
    coordinator = data.coordinator
    known: set[str] = set()

    @callback
    def _add_new_projects() -> None:
        if coordinator.data is None:
            return
        new = [
            p for p in coordinator.data.get("projects", []) if p["id"] not in known
        ]
        known.update(p["id"] for p in new)
        if new:
            async_add_entities(
                TelosTodoListEntity(coordinator, entry, p) for p in new
            )

    _add_new_projects()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_projects))


class TelosTodoListEntity(CoordinatorEntity[TelosCoordinator], TodoListEntity):
    """A Telos project, as a todo list."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )

    def __init__(
        self,
        coordinator: TelosCoordinator,
        entry: ConfigEntry,
        project: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._project_id: str = project["id"]
        self._project_name: str = project["name"]
        self._attr_unique_id = f"{entry.entry_id}_todo_{self._project_id}"
        self._entry = entry

    @property
    def name(self) -> str:
        project = self.coordinator.project(self._project_id)
        return project["name"] if project else self._project_name

    @property
    def available(self) -> bool:
        # A project that was archived or deleted drops out of the feed.
        return super().available and self.coordinator.project(self._project_id) is not None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="Telos",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        project = self.coordinator.project(self._project_id)
        if project and project.get("url"):
            return {"url": project["url"]}
        return None

    @property
    def todo_items(self) -> list[TodoItem] | None:
        if self.coordinator.data is None:
            return None
        raw = self.coordinator.data.get("todos_by_project", {}).get(self._project_id, [])
        return [_to_todo_item(t) for t in raw]

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def async_create_todo_item(self, item: TodoItem) -> None:
        try:
            await self.coordinator.client.create_todo(
                project_id=self._project_id,
                title=item.summary or "",
                notes=item.description or None,
                due_at=_to_iso(item.due) if item.due is not None else None,
            )
        except TelosApiError as err:
            raise HomeAssistantError(f"Telos could not add the todo: {err}") from err
        await self.coordinator.async_refresh()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        uid: str = item.uid  # type: ignore[assignment]
        current = self.coordinator.todo(self._project_id, uid) or {}

        # HA hands over the whole item with the change merged in, so a None
        # here means "cleared". Only what differs is sent, so the todo's
        # history in Telos records just the real edit.
        update_fields: dict[str, Any] = {}
        if item.summary is not None and item.summary != current.get("title"):
            update_fields["title"] = item.summary
        notes = item.description or None
        if notes != current.get("notes"):
            update_fields["notes"] = notes
        if _parse_due(item.due) != _parse_due(current.get("dueAt")):
            update_fields["dueAt"] = _to_iso(item.due) if item.due is not None else None

        was_done = current.get("completedAt") is not None
        try:
            if update_fields:
                await self.coordinator.client.update_todo(uid, **update_fields)
            if item.status == TodoItemStatus.COMPLETED and not was_done:
                await self.coordinator.client.complete_todo(uid)
            elif item.status == TodoItemStatus.NEEDS_ACTION and was_done:
                await self.coordinator.client.reopen_todo(uid)
        except TelosApiError as err:
            # e.g. completing a todo that still waits on an open dependency.
            raise HomeAssistantError(f"Telos could not update the todo: {err}") from err
        finally:
            await self.coordinator.async_refresh()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Archive items (Telos's delete; they can be restored in the app)."""
        try:
            for uid in uids:
                await self.coordinator.client.archive_todo(uid)
        except TelosApiError as err:
            raise HomeAssistantError(f"Telos could not remove the todo: {err}") from err
        finally:
            await self.coordinator.async_refresh()


# ------------------------------------------------------------------
# Conversion helpers
# ------------------------------------------------------------------


def _parse_due(value: str | date | datetime | None) -> datetime | None:
    """Normalise a due value (ISO string, date or datetime) to aware UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_todo_item(raw: dict[str, Any]) -> TodoItem:
    status = (
        TodoItemStatus.COMPLETED
        if raw.get("completedAt") is not None
        else TodoItemStatus.NEEDS_ACTION
    )
    return TodoItem(
        uid=raw["id"],
        summary=raw["title"],
        status=status,
        description=raw.get("notes"),
        due=_parse_due(raw.get("dueAt")),
    )


def _to_iso(due: date | datetime) -> str:
    parsed = _parse_due(due)
    assert parsed is not None
    return parsed.isoformat()
