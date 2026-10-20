"""Telos API client."""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterable
from typing import Any, Protocol

import aiohttp

_LOGGER = logging.getLogger(__name__)

GRAPHQL_TIMEOUT = aiohttp.ClientTimeout(total=30)
_WS_TIMEOUT = aiohttp.ClientWSTimeout(ws_close=10)
_WS_SUBPROTOCOL = "graphql-transport-ws"

# Selected on every todo read and write, so the coordinator's rows and a
# mutation's answer have the same shape.
_TODO_FIELDS = "id title notes dueAt completedAt"


class TelosApiError(Exception):
    """Base exception for Telos API errors.

    ``code`` is the first GraphQL error's ``extensions.code`` (e.g.
    ``BAD_USER_INPUT``, ``TOO_MANY_REQUESTS``), when the server gave one.
    """

    def __init__(self, message: str = "", code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class TelosConnectionError(TelosApiError):
    """Failed to connect to the Telos server."""


class TelosAuthError(TelosApiError):
    """The session token is missing, invalid or expired."""


class TokenSource(Protocol):
    """Where the client gets the bearer it sends."""

    async def async_token(self) -> str | None:
        """The bearer to send now, refreshed first if it is about to expire."""

    async def async_refresh(self) -> bool:
        """Get a new bearer after Telos refused one. False when there is no way to."""


class StaticToken:
    """A bearer that never changes: a better-auth session, or none at all."""

    def __init__(self, token: str | None) -> None:
        self._token = token

    async def async_token(self) -> str | None:
        return self._token

    async def async_refresh(self) -> bool:
        return False


class TelosApiClient:
    """Async client for the Telos GraphQL API.

    Telos's ``telos_`` API keys are its AI/MCP door, so this client signs in
    as a person. ``token`` is either a better-auth session token, or a
    TokenSource handing out access tokens from Telos's OIDC provider (see
    oidc.py). Both are sent as ``Authorization: Bearer <token>``. It is None
    only for the sign-in calls.
    """

    def __init__(
        self,
        url: str,
        token: str | TokenSource | None,
        session: aiohttp.ClientSession,
    ) -> None:
        self._url = url.rstrip("/")
        self._graphql_url = f"{self._url}/graphql"
        self._auth: TokenSource = (
            token if token is not None and not isinstance(token, str) else StaticToken(token)
        )
        self._session = session

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    @property
    def url(self) -> str:
        return self._url

    # ------------------------------------------------------------------
    # Sign-in (no session needed)
    # ------------------------------------------------------------------

    async def sign_in_magic_link(self, email: str) -> dict[str, Any]:
        """Ask Telos to email a sign-in link.

        Under ``SECURE_LOCAL_NET`` no link is sent and the answer already holds
        a session (``token``/``userId``). Otherwise both are None and the link
        arrives by email. An instance with no mail server refuses with
        ``BAD_USER_INPUT``; the sign-in limiter answers ``TOO_MANY_REQUESTS``.
        """
        result = await self._graphql(
            """
            mutation SignInMagicLink($email: String!) {
              signInMagicLink(email: $email) { status token userId }
            }
            """,
            variables={"email": email},
        )
        return result["data"]["signInMagicLink"]

    async def magic_link_verify(self, token: str) -> dict[str, Any]:
        """Trade a sign-in link's token for a session: ``{"token", "userId"}``."""
        result = await self._graphql(
            """
            mutation MagicLinkVerify($token: String!) {
              magicLinkVerify(token: $token) { token user { id } }
            }
            """,
            variables={"token": token},
        )
        answer = result["data"]["magicLinkVerify"]
        return {"token": answer["token"], "userId": answer["user"]["id"]}

    async def get_auth_config(self) -> dict[str, Any]:
        """What the instance offers for signing in. Readable before signing in."""
        result = await self._graphql(
            """
            query AuthConfig {
              authConfig { magicLink secureLocalNet sso { name deviceClientId } }
            }
            """
        )
        return result["data"]["authConfig"]

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_user_id(self) -> str:
        """The signed-in person's Telos user id. ``users`` lists only the caller."""
        result = await self._graphql("query Me { users { id } }")
        users = result["data"]["users"]
        if not users:
            raise TelosAuthError("Telos does not know who this is")
        return users[0]["id"]

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
                token = await self._auth.async_token()
                await ws.send_json({
                    "type": "connection_init",
                    "payload": {"authorization": f"Bearer {token}"},
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
        try:
            return await self._post(payload, await self._auth.async_token())
        except TelosAuthError:
            # An access token can be refused before its own expiry (revoked,
            # or the clocks disagree): get a new one and try once more.
            if not await self._auth.async_refresh():
                raise
            return await self._post(payload, await self._auth.async_token())

    async def _post(self, payload: dict[str, Any], token: str | None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}

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
            codes = [(e.get("extensions") or {}).get("code") for e in errors]
            if "UNAUTHENTICATED" in codes:
                raise TelosAuthError(message, "UNAUTHENTICATED")
            raise TelosApiError(message, next((c for c in codes if c), None))

        return data
