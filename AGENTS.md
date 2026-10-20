# Project: Telos — Home Assistant Integration

A Home Assistant custom integration that connects to a [Telos](../../apps/telos) instance to surface:
- **Todo lists** — one HA todo list per open Telos project, read and written over the GraphQL API and kept live by the `boardChanged` subscription

The todo-list half of [auto-cal-ha-integration](../auto-cal-ha-integration), rebuilt for Telos's API. Users configure a server URL. The integration then signs in as them, through Telos's OIDC provider (device flow) or with a magic link, whichever the instance offers.

## Commands

```bash
# Install test dependencies
pip install -e ".[test]"

# Run tests
pytest

# Type checking
mypy custom_components/telos/

# Lint
ruff check custom_components/ tests/
```

## Tech Stack

| Choice | Why |
|--------|-----|
| **aiohttp** | HA's standard async HTTP client; reuse HA's session via `async_get_clientsession` |
| **DataUpdateCoordinator** | Single shared poller for all entities; 15-min default interval, plus a refresh per `boardChanged` event |

## Key Conventions

- All config stored in `entry.data`: `url`, `user_id`, `auth_method`, plus `email`/`token` for a session or the OIDC tokens (see `.agents/integration-structure.md` → Sign-in)
- `entry.runtime_data` holds the typed `TelosData(coordinator=...)` dataclass
- `TelosApiClient` owns all HTTP — coordinator calls client methods, entities call coordinator
- Auth: `Authorization: Bearer <token>` on HTTP; `connection_init` payload `{"authorization": "Bearer <token>"}` on the socket. The token is a better-auth session, or an access token from Telos's OIDC provider with Telos as its audience

## Why a person, not an API key

Telos's `telos_` API keys are its **AI/MCP door**: they resolve only while AI is on for the instance and the account, see only AI-enabled projects (and not `aiIgnored` todos), and may make only the writes in `AI_MUTATIONS`. `boardChanged` refuses anything but a person. So the integration signs in as the user, one of two ways:

- **Through the OIDC provider (heautou), by device flow** (`oidc.py`, RFC 8628). Telos's `/.well-known/oauth-protected-resource` (RFC 9728) names the provider and Telos's audience; `authConfig.sso.deviceClientId` names the public client. Home Assistant shows a link and code, you approve at the provider, and it gets an access token with `aud` = Telos (RFC 8707) plus a refresh token. Telos accepts that token as you. The provider account must already be linked to a Telos account (sign in to Telos with the provider once).
- **By magic link** (`signInMagicLink` → `magicLinkVerify`), when the instance emails links (or under `SECURE_LOCAL_NET`, where the address alone opens a session). That gives a 30-day better-auth session, refreshed by polling.

When either lapses (a refused refresh, or an expired session), Telos answers `UNAUTHENTICATED`, the coordinator raises `ConfigEntryAuthFailed`, and HA starts the reauth flow, which offers the same choice.

## Guard clauses

Raise `TelosConnectionError` for network failures, and `TelosAuthError` for 401/403 or a GraphQL `UNAUTHENTICATED` error. Any other GraphQL error is a `TelosApiError` carrying the server's messages. The config flow maps these to `cannot_connect` / `invalid_link` / `request_refused` / `rate_limited` (from the error's `code`, e.g. `TOO_MANY_REQUESTS`). Todo writes turn `TelosApiError` into `HomeAssistantError`, so a refusal such as "blocked by an open dependency" reaches the user.

## Telos rules the todo entity respects

- **Completion goes through `completeTodo` / `reopenTodo`**, never by writing `completedAt`, so the todo's lane follows it. Telos refuses to complete a blocked todo.
- **Only changed fields are sent** to `updateTodo`, so the todo's history in Telos records only real edits.
- **Remove = archive.** `deleteTodo` without `hard` is Telos's soft delete. The todo can be restored in the app.
- **New todos** go in with `projectId` + `title` (+ `notes`, `dueAt`). The server puts them in the project's first open lane.

## Agent File Convention

All planning, structure, and API reference docs live in `.agents/`. Always add new files to the reference list below.

## Agent Reference Files

- [`.agents/integration-structure.md`](.agents/integration-structure.md) — File layout, platform map, entity hierarchy, data flow
- [`.agents/api-reference.md`](.agents/api-reference.md) — GraphQL queries, mutations and the subscription used by this integration
