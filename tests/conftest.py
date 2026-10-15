"""Shared test fixtures."""
from __future__ import annotations

import asyncio as _asyncio
import copy
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

MOCK_URL = "http://telos.local:3001"
MOCK_EMAIL = "me@example.com"
MOCK_TOKEN = "session-token-abc"
MOCK_USER_ID = "11111111-1111-1111-1111-111111111111"

MOCK_TODOS = [
    {
        "id": "todo-1",
        "title": "Write tests",
        "notes": "Add coverage",
        "dueAt": None,
        "completedAt": None,
    },
    {
        "id": "todo-2",
        "title": "Buy groceries",
        "notes": None,
        "dueAt": "2026-05-15T00:00:00.000Z",
        "completedAt": None,
    },
    {
        "id": "todo-3",
        "title": "Old task",
        "notes": None,
        "dueAt": None,
        "completedAt": "2026-05-10T10:30:00.000Z",
    },
]

# As TelosApiClient.get_projects returns them: projects with nested todos.
MOCK_PROJECTS = [
    {
        "id": "project-1",
        "name": "Work",
        "url": "http://telos.local:3000/projects/project-1",
        "todos": [MOCK_TODOS[0], MOCK_TODOS[2]],
    },
    {
        "id": "project-2",
        "name": "Personal",
        "url": "http://telos.local:3000/projects/project-2",
        "todos": [MOCK_TODOS[1]],
    },
]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom integrations in tests."""
    yield


@pytest.fixture
def mock_api_client():
    """Return a mock TelosApiClient."""
    client = MagicMock()
    client.get_projects = AsyncMock(return_value=copy.deepcopy(MOCK_PROJECTS))
    client.create_todo = AsyncMock(return_value={**MOCK_TODOS[0], "id": "todo-new"})
    client.update_todo = AsyncMock(return_value=MOCK_TODOS[0])
    client.complete_todo = AsyncMock(return_value=MOCK_TODOS[0])
    client.reopen_todo = AsyncMock(return_value=MOCK_TODOS[2])
    client.archive_todo = AsyncMock(return_value={"id": "todo-1"})

    async def _subscribe_never(project_ids):
        # Blocks without yielding — simulates an idle connected subscription.
        # Cancelled cleanly when the background task is torn down.
        await _asyncio.Event().wait()
        yield  # unreachable; makes this an async generator

    client.subscribe_board_changes = _subscribe_never
    return client


@pytest.fixture
def mock_config_entry(hass):
    """Return a mock config entry."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.telos.const import (
        CONF_EMAIL,
        CONF_TOKEN,
        CONF_URL,
        CONF_USER_ID,
        DOMAIN,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{MOCK_URL}_{MOCK_USER_ID}",
        data={
            CONF_URL: MOCK_URL,
            CONF_EMAIL: MOCK_EMAIL,
            CONF_TOKEN: MOCK_TOKEN,
            CONF_USER_ID: MOCK_USER_ID,
        },
        title="telos.local:3001",
    )
    entry.add_to_hass(hass)
    return entry
