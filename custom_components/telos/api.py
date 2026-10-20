"""Telos API client."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterable
from typing import Any, Protocol

import aiohttp

_LOGGER = logging.getLogger(__name__)

GRAPHQL_TIMEOUT = aiohttp.ClientTimeout(total=30)
# A stream stays open for as long as it is watched, so no total timeout. Yoga
# sends a keep-alive comment every 12 s; a minute of silence is a dead stream.
_SSE_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60)
_BOARD_CHANGED = (
    "subscription BoardChanged($projectId: ID!) "
    "{ boardChanged(projectId: $projectId) { projectId table } }"
)

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
        """Yield ``{"id": project_id, "payload": result}`` for each change to the given projects.

        Telos serves subscriptions as GraphQL over SSE in distinct-connections
        mode: one ``POST /graphql`` with ``Accept: text/event-stream`` per
        subscription. ``boardChanged`` takes one project, so this opens one
        stream per project and merges them. Only a person may watch a board.

        A project whose stream fails on its own (deleted, say) is logged and
        dropped; the others keep running. Raises TelosAuthError when Telos
        refuses the credential (it ends a stream that way when an access token
        expires), and TelosConnectionError when any stream drops.
        """
        queue: asyncio.Queue[dict[str, Any] | BaseException] = asyncio.Queue()
        ids = list(project_ids)
        tasks = [
            asyncio.create_task(self._watch_board(project_id, queue)) for project_id in ids
        ]
        running = len(tasks)
        try:
            while running:
                item = await queue.get()
                if isinstance(item, _StreamDone):
                    running -= 1
                elif isinstance(item, BaseException):
                    raise item
                else:
                    yield item
            raise TelosConnectionError("every board stream ended")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch_board(
        self, project_id: str, queue: asyncio.Queue[dict[str, Any] | BaseException]
    ) -> None:
        """One project's stream, into the shared queue."""
        try:
            async for result in self._sse(
                {"query": _BOARD_CHANGED, "variables": {"projectId": project_id}}
            ):
                errors = result.get("errors")
                if errors:
                    _raise_for_errors(errors)
                await queue.put({"id": project_id, "payload": result})
        except (TelosAuthError, TelosConnectionError) as err:
            await queue.put(err)
            return
        except TelosApiError as err:
            # This project's subscription failed (e.g. it was deleted); the
            # others keep running.
            _LOGGER.warning("Telos subscription %s failed: %s", project_id, err)
        except Exception as err:  # noqa: BLE001
            # Anything else ends them all, rather than leaving the merge waiting.
            await queue.put(err)
            return
        await queue.put(_StreamDone())

    async def _sse(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """POST an operation and yield each ``next`` result of its event stream."""
        headers = {"Accept": "text/event-stream"}
        # Asked per stream, so a reconnect after expiry brings a fresh token.
        if token := await self._auth.async_token():
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with self._session.post(
                self._graphql_url, json=payload, headers=headers, timeout=_SSE_TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    raise TelosAuthError(f"GraphQL returned {resp.status}")
                resp.raise_for_status()
                if not (resp.content_type or "").startswith("text/event-stream"):
                    # Refused before the stream began: a plain GraphQL answer.
                    body = json.loads(await resp.text() or "{}")
                    _raise_for_errors(body.get("errors") or [{"message": "no event stream"}])
                    return
                event, data = "", []
                async for raw in resp.content:
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line.startswith(":"):
                        continue  # keep-alive
                    if line:
                        field, _, value = line.partition(":")
                        value = value.removeprefix(" ")
                        if field == "event":
                            event = value
                        elif field == "data":
                            data.append(value)
                        continue
                    # A blank line ends the event.
                    if event == "complete":
                        return
                    if data and event in ("", "next"):
                        yield json.loads("\n".join(data))
                    event, data = "", []
        except TelosApiError:
            raise
        except aiohttp.ClientConnectionError as err:
            raise TelosConnectionError(f"Cannot connect to {self._graphql_url}") from err
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            raise TelosConnectionError(f"Event stream failed: {err}") from err

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
            _raise_for_errors(errors)

        return data


class _StreamDone:
    """One board stream ended on its own."""


def _raise_for_errors(errors: list[dict[str, Any]]) -> None:
    """Raise the GraphQL errors as the matching TelosApiError."""
    message = "; ".join(str(e.get("message", e)) for e in errors)
    # Telos answers an expired or missing credential with UNAUTHENTICATED on
    # HTTP 200, as GraphQL servers do.
    codes = [(e.get("extensions") or {}).get("code") for e in errors]
    if "UNAUTHENTICATED" in codes:
        raise TelosAuthError(message, "UNAUTHENTICATED")
    raise TelosApiError(message, next((c for c in codes if c), None))
