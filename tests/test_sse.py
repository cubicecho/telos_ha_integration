"""Tests for boardChanged over GraphQL over SSE, and how the coordinator reconnects."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)

from custom_components.telos.api import (
    TelosApiClient,
    TelosAuthError,
    TelosConnectionError,
)
from custom_components.telos.coordinator import TelosCoordinator

from .conftest import MOCK_TOKEN, MOCK_URL

GRAPHQL = f"{MOCK_URL}/graphql"


def _next(project_id: str) -> str:
    result = {"data": {"boardChanged": {"projectId": project_id, "table": "todos"}}}
    return f"event: next\ndata: {json.dumps(result)}\n\n"


def _error(code: str, message: str = "no") -> str:
    result = {"errors": [{"message": message, "extensions": {"code": code}}]}
    return f"event: next\ndata: {json.dumps(result)}\n\n"


COMPLETE = "event: complete\ndata:\n\n"
KEEPALIVE = ":\n\n"


def _streams(bodies: dict[str, str]):
    """Answer each project's subscription with its own event stream."""

    async def _answer(method, url, data):
        body = bodies[data["variables"]["projectId"]]
        return AiohttpClientMockResponse(
            method,
            url,
            text=body,
            headers={"content-type": "text/event-stream"},
        )

    return _answer


async def _collect(client, project_ids):
    seen = []
    with pytest.raises(Exception) as err:
        async for msg in client.subscribe_board_changes(project_ids):
            seen.append(msg)
    return seen, err.value


async def test_merges_streams_and_sends_the_session(hass, aioclient_mock) -> None:
    aioclient_mock.post(
        GRAPHQL,
        side_effect=_streams(
            {
                "p1": KEEPALIVE + _next("p1") + _next("p1") + COMPLETE,
                "p2": _next("p2") + COMPLETE,
            }
        ),
    )
    client = TelosApiClient(MOCK_URL, MOCK_TOKEN, async_get_clientsession(hass))

    seen, err = await _collect(client, ["p1", "p2"])

    assert sorted(m["id"] for m in seen) == ["p1", "p1", "p2"]
    assert seen[0]["payload"]["data"]["boardChanged"]["table"] == "todos"
    # Both streams completing is a disconnect, for the coordinator to reconnect.
    assert isinstance(err, TelosConnectionError)
    for call in aioclient_mock.mock_calls:
        assert call[3]["Accept"] == "text/event-stream"
        assert call[3]["Authorization"] == f"Bearer {MOCK_TOKEN}"
        assert "boardChanged" in call[2]["query"]


async def test_one_failed_project_leaves_the_others(hass, aioclient_mock) -> None:
    gate = asyncio.Event()

    async def _answer(method, url, data):
        project_id = data["variables"]["projectId"]
        if project_id == "gone":
            body = _error("NOT_FOUND") + COMPLETE
        else:
            await gate.wait()
            body = _next(project_id) + COMPLETE
        return AiohttpClientMockResponse(
            method, url, text=body, headers={"content-type": "text/event-stream"}
        )

    aioclient_mock.post(GRAPHQL, side_effect=_answer)
    client = TelosApiClient(MOCK_URL, MOCK_TOKEN, async_get_clientsession(hass))

    stream = client.subscribe_board_changes(["gone", "p1"])
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.01)
    gate.set()
    msg = await first
    assert msg["id"] == "p1"
    await stream.aclose()


async def test_refused_credential_is_auth_error(hass, aioclient_mock) -> None:
    aioclient_mock.post(
        GRAPHQL,
        side_effect=_streams({"p1": _next("p1") + _error("UNAUTHENTICATED") + COMPLETE}),
    )
    client = TelosApiClient(MOCK_URL, MOCK_TOKEN, async_get_clientsession(hass))

    seen, err = await _collect(client, ["p1"])

    assert [m["id"] for m in seen] == ["p1"]
    assert isinstance(err, TelosAuthError)


async def test_refused_before_the_stream(hass, aioclient_mock) -> None:
    """Telos may answer a plain GraphQL error instead of opening a stream."""
    aioclient_mock.post(
        GRAPHQL,
        json={"errors": [{"message": "no", "extensions": {"code": "UNAUTHENTICATED"}}]},
    )
    client = TelosApiClient(MOCK_URL, None, async_get_clientsession(hass))

    _seen, err = await _collect(client, ["p1"])

    assert isinstance(err, TelosAuthError)


async def test_network_failure(hass, aioclient_mock) -> None:
    aioclient_mock.post(GRAPHQL, exc=TimeoutError())
    client = TelosApiClient(MOCK_URL, MOCK_TOKEN, async_get_clientsession(hass))

    _seen, err = await _collect(client, ["p1"])

    assert isinstance(err, TelosConnectionError)


# ----------------------------------------------------------------------
# Reconnecting after a refused credential
# ----------------------------------------------------------------------


async def _run_until(coordinator, connects: list, count: int) -> None:
    task = asyncio.ensure_future(coordinator.async_subscribe_updates())
    for _ in range(200):
        if len(connects) >= count:
            break
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(("uptime", "waits"), [(600.0, []), (1.0, [30])])
async def test_auth_refusal_reconnect(
    hass, mock_api_client, mock_config_entry, uptime, waits
) -> None:
    """A stream refused after running a while (expired access token) reconnects
    at once; one refused straight away waits like any other error."""
    coordinator = TelosCoordinator(hass, mock_config_entry, mock_api_client)
    coordinator.data = {"projects": [{"id": "p1"}], "todos_by_project": {}}
    connects: list[int] = []
    clock = [0.0]

    async def _subscribe(_ids):
        connects.append(1)
        await asyncio.sleep(0)
        clock[0] += uptime
        raise TelosAuthError("Your access token expired")
        yield  # unreachable; makes this an async generator

    slept: list[int] = []

    async def _sleep():
        slept.append(30)
        await asyncio.sleep(0)
        return True

    mock_api_client.subscribe_board_changes = _subscribe
    with patch.object(coordinator, "_now", side_effect=lambda: clock[0]), patch.object(
        coordinator, "_sleep", side_effect=_sleep
    ):
        await _run_until(coordinator, connects, 2)

    assert len(connects) >= 2
    assert slept[: len(waits)] == waits
    if not waits:
        assert slept == []
