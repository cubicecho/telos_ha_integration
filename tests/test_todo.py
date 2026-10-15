"""Tests for TelosTodoListEntity."""
from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from homeassistant.components.todo import TodoItemStatus
from homeassistant.exceptions import HomeAssistantError

from custom_components.telos.api import TelosApiError
from custom_components.telos.todo import _to_iso, _to_todo_item

from .conftest import MOCK_PROJECTS, MOCK_TODOS

# ------------------------------------------------------------------
# Unit tests for conversion helpers
# ------------------------------------------------------------------


def test_to_todo_item_needs_action():
    item = _to_todo_item(MOCK_TODOS[0])
    assert item.uid == "todo-1"
    assert item.summary == "Write tests"
    assert item.description == "Add coverage"
    assert item.status == TodoItemStatus.NEEDS_ACTION


def test_to_todo_item_complete():
    item = _to_todo_item(MOCK_TODOS[2])
    assert item.status == TodoItemStatus.COMPLETED


def test_to_todo_item_with_due():
    item = _to_todo_item(MOCK_TODOS[1])
    assert item.due == datetime(2026, 5, 15, tzinfo=UTC)


def test_to_iso_date():
    assert _to_iso(date(2026, 5, 15)) == "2026-05-15T00:00:00+00:00"


def test_to_iso_datetime_naive():
    assert _to_iso(datetime(2026, 5, 15, 10, 0)) == "2026-05-15T10:00:00+00:00"


# ------------------------------------------------------------------
# Integration tests
# ------------------------------------------------------------------


async def _setup(hass, mock_api_client, mock_config_entry):
    with patch(
        "custom_components.telos.TelosApiClient",
        return_value=mock_api_client,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()


def _entity_id(hass, name: str) -> str:
    for state in hass.states.async_all("todo"):
        if name in (state.attributes.get("friendly_name") or state.name):
            return state.entity_id
    raise AssertionError(f"no todo entity named {name}")


async def test_todo_entities_created(hass, mock_api_client, mock_config_entry):
    """One entity per project, with its open-item count as the state."""
    await _setup(hass, mock_api_client, mock_config_entry)

    states = hass.states.async_all("todo")
    assert len(states) == 2
    work = hass.states.get(_entity_id(hass, "Work"))
    assert work.state == "1"  # todo-1 open, todo-3 done
    assert work.attributes["url"].endswith("/projects/project-1")


async def test_new_project_adds_entity(hass, mock_api_client, mock_config_entry):
    await _setup(hass, mock_api_client, mock_config_entry)
    mock_api_client.get_projects.return_value = [
        *MOCK_PROJECTS,
        {"id": "project-3", "name": "Garden", "url": "", "todos": []},
    ]

    await mock_config_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(hass.states.async_all("todo")) == 3


async def test_removed_project_unavailable(hass, mock_api_client, mock_config_entry):
    await _setup(hass, mock_api_client, mock_config_entry)
    entity_id = _entity_id(hass, "Personal")
    mock_api_client.get_projects.return_value = [MOCK_PROJECTS[0]]

    await mock_config_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "unavailable"


async def test_todo_create_item(hass, mock_api_client, mock_config_entry):
    await _setup(hass, mock_api_client, mock_config_entry)

    await hass.services.async_call(
        "todo",
        "add_item",
        {"item": "New task", "description": "Details", "due_datetime": "2026-06-01T09:00:00+00:00"},
        target={"entity_id": _entity_id(hass, "Work")},
        blocking=True,
    )

    mock_api_client.create_todo.assert_called_once_with(
        project_id="project-1",
        title="New task",
        notes="Details",
        due_at="2026-06-01T09:00:00+00:00",
    )


async def test_todo_complete_item(hass, mock_api_client, mock_config_entry):
    """Completing goes through completeTodo, with no plain field update."""
    await _setup(hass, mock_api_client, mock_config_entry)

    await hass.services.async_call(
        "todo",
        "update_item",
        {"item": "Write tests", "status": "completed"},
        target={"entity_id": _entity_id(hass, "Work")},
        blocking=True,
    )

    mock_api_client.complete_todo.assert_called_once_with("todo-1")
    mock_api_client.update_todo.assert_not_called()
    mock_api_client.reopen_todo.assert_not_called()


async def test_todo_reopen_item(hass, mock_api_client, mock_config_entry):
    await _setup(hass, mock_api_client, mock_config_entry)

    await hass.services.async_call(
        "todo",
        "update_item",
        {"item": "Old task", "status": "needs_action"},
        target={"entity_id": _entity_id(hass, "Work")},
        blocking=True,
    )

    mock_api_client.reopen_todo.assert_called_once_with("todo-3")
    mock_api_client.complete_todo.assert_not_called()


async def test_todo_rename_and_clear_due(hass, mock_api_client, mock_config_entry):
    """Only changed fields are sent; clearing the due date sends null."""
    await _setup(hass, mock_api_client, mock_config_entry)

    await hass.services.async_call(
        "todo",
        "update_item",
        {"item": "Buy groceries", "rename": "Buy food", "due_datetime": None},
        target={"entity_id": _entity_id(hass, "Personal")},
        blocking=True,
    )

    mock_api_client.update_todo.assert_called_once_with(
        "todo-2", title="Buy food", dueAt=None
    )
    mock_api_client.complete_todo.assert_not_called()


async def test_todo_complete_refused(hass, mock_api_client, mock_config_entry):
    """A refusal from Telos (e.g. an open dependency) surfaces as an HA error."""
    await _setup(hass, mock_api_client, mock_config_entry)
    mock_api_client.complete_todo.side_effect = TelosApiError("blocked by Ship it")

    with pytest.raises(HomeAssistantError, match="blocked by Ship it"):
        await hass.services.async_call(
            "todo",
            "update_item",
            {"item": "Write tests", "status": "completed"},
            target={"entity_id": _entity_id(hass, "Work")},
            blocking=True,
        )


async def test_todo_delete_item(hass, mock_api_client, mock_config_entry):
    """Removing an item archives it."""
    await _setup(hass, mock_api_client, mock_config_entry)

    await hass.services.async_call(
        "todo",
        "remove_item",
        {"item": "Write tests"},
        target={"entity_id": _entity_id(hass, "Work")},
        blocking=True,
    )

    mock_api_client.archive_todo.assert_called_once_with("todo-1")
