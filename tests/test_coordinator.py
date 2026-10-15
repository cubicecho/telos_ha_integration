"""Tests for TelosCoordinator and the API client's error mapping."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.telos.api import (
    TelosApiClient,
    TelosApiError,
    TelosAuthError,
)
from custom_components.telos.coordinator import TelosCoordinator

from .conftest import MOCK_PROJECTS, MOCK_TOKEN, MOCK_URL


async def test_update_splits_projects_and_todos(hass, mock_api_client, mock_config_entry):
    coordinator = TelosCoordinator(hass, mock_config_entry, mock_api_client)
    data = await coordinator._async_update_data()

    assert [p["id"] for p in data["projects"]] == ["project-1", "project-2"]
    assert "todos" not in data["projects"][0]
    assert [t["id"] for t in data["todos_by_project"]["project-1"]] == ["todo-1", "todo-3"]


async def test_expired_session_starts_reauth(hass, mock_api_client, mock_config_entry):
    mock_api_client.get_projects.side_effect = TelosAuthError("UNAUTHENTICATED")
    with patch("custom_components.telos.TelosApiClient", return_value=mock_api_client):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_server_error_retries_setup(hass, mock_api_client, mock_config_entry):
    mock_api_client.get_projects.side_effect = TelosApiError("boom")
    with patch("custom_components.telos.TelosApiClient", return_value=mock_api_client):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_subscription_refreshes_and_follows_projects(
    hass, mock_api_client, mock_config_entry
):
    """Events trigger a refresh; a new project reconnects with the new set."""
    subscribed: list[list[str]] = []
    events: asyncio.Queue[dict] = asyncio.Queue()

    async def _subscribe(project_ids):
        subscribed.append(list(project_ids))
        while True:
            yield await events.get()

    mock_api_client.subscribe_board_changes = _subscribe
    coordinator = TelosCoordinator(hass, mock_config_entry, mock_api_client)
    await coordinator.async_refresh()
    task = asyncio.ensure_future(coordinator.async_subscribe_updates())
    await _settle()
    assert subscribed == [["project-1", "project-2"]]

    mock_api_client.get_projects.return_value = [
        *MOCK_PROJECTS,
        {"id": "project-3", "name": "Garden", "url": "", "todos": []},
    ]
    with patch.object(coordinator, "async_request_refresh", coordinator.async_refresh):
        await events.put({"type": "next"})
        await _settle()

    assert subscribed[-1] == ["project-1", "project-2", "project-3"]
    task.cancel()
    await task  # exits cleanly on cancel


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def _response(status: int, payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=payload)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


@pytest.mark.parametrize(
    ("status", "payload", "exc"),
    [
        (401, {}, TelosAuthError),
        (
            200,
            {"errors": [{"message": "Not signed in", "extensions": {"code": "UNAUTHENTICATED"}}]},
            TelosAuthError,
        ),
        (200, {"errors": [{"message": "Todo is blocked"}]}, TelosApiError),
    ],
)
async def test_graphql_error_mapping(status, payload, exc):
    session = MagicMock()
    session.post = MagicMock(return_value=_response(status, payload))
    client = TelosApiClient(MOCK_URL, MOCK_TOKEN, session)

    with pytest.raises(exc):
        await client.get_projects()


async def test_graphql_sends_bearer_session():
    session = MagicMock()
    session.post = MagicMock(return_value=_response(200, {"data": {"projects": []}}))
    client = TelosApiClient(MOCK_URL + "/", MOCK_TOKEN, session)

    assert await client.get_projects() == []
    args, kwargs = session.post.call_args
    assert args[0] == f"{MOCK_URL}/graphql"
    assert kwargs["headers"] == {"Authorization": f"Bearer {MOCK_TOKEN}"}
