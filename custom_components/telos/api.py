"""Telos API client."""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterable
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

GRAPHQL_TIMEOUT = aiohttp.ClientTimeout(total=30)
_WS_TIMEOUT = aiohttp.ClientWSTimeout(ws_close=10)
_WS_SUBPROTOCOL = "graphql-transport-ws"

# Selected on every todo read and write, so the coordinator's rows and a
# mutation's answer have the same shape.
_TODO_FIELDS = "id title notes dueAt completedAt"


class TelosApiError(Exception):
    """Base exception for Telos API errors."""


class TelosConnectionError(TelosApiError):
    """Failed to connect to the Telos server."""


class TelosAuthError(TelosApiError):
    """The session token is missing, invalid or expired."""


class TelosApiClient:
    """Async client for the Telos GraphQL API.

    Telos's ``telos_`` API keys are its AI/MCP door, so this client signs in
    as a person: ``token`` is a better-auth session token, sent as
    ``Authorization: Bearer <token>``. It is None only for the sign-in calls.
    """

    def __init__(
        self,
        url: str,
        token: str | None,
        session: aiohttp.ClientSession,
    ) -> None:
        self._graphql_url = f"{url.rstrip('/')}/graphql"
        self._token = token
        self._session = session

    # ------------------------------------------------------------------
    # Sign-in (no session needed)
    # ------------------------------------------------------------------

    async def request_magic_link(self, email: str) -> dict[str, Any]:
        """Ask for a magic link.

        With ``AUTH_MAGIC_LINK=false`` on the server the answer already holds
        a session (``token``/``userId``). Otherwise ``magicLink`` is set only
        when the server exposes links; it is printed to the server log either way.
        """
        result = await self._graphql(
            """
            mutation RequestMagicLink($email: String!) {
              requestMagicLink(email: $email) { ok magicLink token userId }
            }
            """,
            variables={"email": email},
        )
        return result["data"]["requestMagicLink"]

    async def verify_magic_link(self, token: str) -> dict[str, Any]:
        """Trade a magic-link token for a session (``token``/``userId``)."""
        result = await self._graphql(
            """
            mutation VerifyMagicLink($token: String!) {
              verifyMagicLink(token: $token) { token userId }
            }
            """,
            variables={"token": token},
        )
        return result["data"]["verifyMagicLink"]

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_projects(self) -> list[dict[str, Any]]:
        """Open projects, each with its open (non-archived) todos in board order.

        Soft-delete filtering only applies to root queries, so the nested
        ``todos`` field has to leave archived todos out itself.
        """
        result = await self._graphql(
            f"""
            query Projects {{
              projects(
                where: {{ archivedAt: {{ isNull: true }} }}
                orderBy: {{ name: {{ direction: asc, priority: 1 }} }}
              ) {{
                id name url
                todos(
                  where: {{ archivedAt: {{ isNull: true }} }}
                  orderBy: {{ position: {{ direction: asc, priority: 1 }} }}
                ) {{ {_TODO_FIELDS} }}
              }}
            }}
            """
        )
        return result["data"]["projects"]

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def create_todo(
        self,
        project_id: str,
        title: str,
        notes: str | None = None,
        due_at: str | None = None,
    ) -> dict[str, Any]:
        """Create a todo. The server puts it in the project's first open lane."""
        values: dict[str, Any] = {"projectId": project_id, "title": title}
        if notes is not None:
            values["notes"] = notes
        if due_at is not None:
            values["dueAt"] = due_at
        result = await self._graphql(
            f"""
            mutation CreateTodo($values: CreateTodoInput!) {{
              createTodo(values: $values) {{ {_TODO_FIELDS} }}
            }}
            """,
            variables={"values": values},
        )
        return result["data"]["createTodo"]

    async def update_todo(self, todo_id: str, **fields: Any) -> dict[str, Any]:
        """Set plain fields (``title``, ``notes``, ``dueAt``). None clears one.

        Completion is not one of them: use complete_todo / reopen_todo, which
        keep the todo's lane in step with it.
        """
        result = await self._graphql(
            f"""
            mutation UpdateTodo($id: UUID!, $set: UpdateTodoInput!) {{
              updateTodo(where: {{ id: {{ eq: $id }} }}, set: $set) {{ {_TODO_FIELDS} }}
            }}
            """,
            variables={"id": todo_id, "set": fields},
        )
        return result["data"]["updateTodo"]

    async def complete_todo(self, todo_id: str) -> dict[str, Any]:
        """Mark a todo done. Telos refuses while a todo it depends on is open."""
        result = await self._graphql(
            f"""
            mutation CompleteTodo($id: ID!) {{
              completeTodo(id: $id, reason: "Home Assistant") {{ {_TODO_FIELDS} }}
            }}
            """,
            variables={"id": todo_id},
        )
        return result["data"]["completeTodo"]

    async def reopen_todo(self, todo_id: str) -> dict[str, Any]:
        result = await self._graphql(
            f"""
            mutation ReopenTodo($id: ID!) {{
              reopenTodo(id: $id, reason: "Home Assistant") {{ {_TODO_FIELDS} }}
            }}
            """,
            variables={"id": todo_id},
        )
        return result["data"]["reopenTodo"]

    async def archive_todo(self, todo_id: str) -> dict[str, Any] | None:
        """Archive a todo (Telos's soft delete; it can be restored in the app)."""
        result = await self._graphql(
            """
            mutation ArchiveTodo($id: UUID!) {
              deleteTodo(where: { id: { eq: $id } }) { id }
            }
            """,
            variables={"id": todo_id},
        )
        return result["data"]["deleteTodo"]

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    async def subscribe_board_changes(
        self, project_ids: Iterable[str]
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield a graphql-ws ``next`` message for each change to the given projects.

        ``boardChanged`` takes one project, so one subscription per project is
        multiplexed over a single socket. Only a session may watch a board.
        Raises TelosConnectionError on network failure.
        """
        ws_url = (
            self._graphql_url
            .replace("http://", "ws://", 1)
            .replace("https://", "wss://", 1)
        )
        try:
            async with self._session.ws_connect(
                ws_url,
                protocols=[_WS_SUBPROTOCOL],
                timeout=_WS_TIMEOUT,
            ) as ws:
                await ws.send_json({
                    "type": "connection_init",
                    "payload": {"authorization": f"Bearer {self._token}"},
                })
                raw = await ws.receive()
                if raw.type != aiohttp.WSMsgType.TEXT:
                    raise TelosConnectionError("WS handshake failed: no text response")
                ack = json.loads(raw.data)
                if ack.get("type") != "connection_ack":
                    raise TelosConnectionError(
                        f"WS handshake failed: expected connection_ack, got {ack.get('type')!r}"
                    )

                for project_id in project_ids:
                    await ws.send_json({
                        "type": "subscribe",
                        "id": project_id,
                        "payload": {
                            "query": (
                                "subscription BoardChanged($projectId: ID!) "
                                "{ boardChanged(projectId: $projectId) { projectId table } }"
                            ),
                            "variables": {"projectId": project_id},
                        },
                    })

                async for raw in ws:
                    if raw.type == aiohttp.WSMsgType.TEXT:
                        msg = json.loads(raw.data)
                        msg_type = msg.get("type")
                        if msg_type == "next":
                            yield msg
                        elif msg_type == "ping":
                            await ws.send_json({"type": "pong"})
                        elif msg_type == "error":
                            # One project's subscription failed (e.g. it was
                            # deleted); the others keep running.
                            _LOGGER.warning(
                                "Telos subscription %s failed: %s",
                                msg.get("id"),
                                msg.get("payload"),
                            )
                    elif raw.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        raise TelosConnectionError(f"WebSocket closed: {raw.type.name}")
        except TelosApiError:
            raise
        except aiohttp.ClientConnectionError as err:
            raise TelosConnectionError(f"Cannot connect to {ws_url}") from err
        except (TimeoutError, aiohttp.ClientError) as err:
            raise TelosConnectionError(f"WebSocket error: {err}") from err

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}

        try:
            async with self._session.post(
                self._graphql_url,
                json=payload,
                headers=headers,
                timeout=GRAPHQL_TIMEOUT,
            ) as resp:
                if resp.status in (401, 403):
                    raise TelosAuthError(f"GraphQL returned {resp.status}")
                resp.raise_for_status()
                data = await resp.json()
        except TelosApiError:
            raise
        except aiohttp.ClientConnectionError as err:
            raise TelosConnectionError(
                f"Cannot connect to {self._graphql_url}"
            ) from err
        except (TimeoutError, aiohttp.ClientError) as err:
            raise TelosConnectionError(f"Request failed: {err}") from err

        errors = data.get("errors")
        if errors:
            message = "; ".join(str(e.get("message", e)) for e in errors)
            # Telos answers an expired or missing session with UNAUTHENTICATED
            # on HTTP 200, as GraphQL servers do.
            if any(
                (e.get("extensions") or {}).get("code") == "UNAUTHENTICATED"
                for e in errors
            ):
                raise TelosAuthError(message)
            raise TelosApiError(message)

        return data
