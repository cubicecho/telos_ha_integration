# Integration structure

```
custom_components/telos/
├── __init__.py       # async_setup_entry: client → coordinator → first refresh → todo platform → subscription task
├── api.py            # TelosApiClient: all Telos HTTP + WebSocket; TokenSource / StaticToken; TelosApiError (with .code) / TelosConnectionError / TelosAuthError
├── config_flow.py    # user (URL) → authConfig → method menu → browser (device flow progress) | email → link → entry; reauth_confirm → same menu
├── const.py          # DOMAIN, CONF_* keys, AUTH_SESSION / AUTH_OIDC, scan interval, PLATFORMS
├── oidc.py           # provider side: RFC 9728 metadata, discovery, device flow (RFC 8628), refresh; OidcTokenSource
├── coordinator.py    # TelosCoordinator: projects + todos, boardChanged subscription loop
├── todo.py           # TelosTodoListEntity, one per open project
├── manifest.json
├── strings.json      # copied verbatim to translations/en.json
└── translations/en.json
tests/
├── conftest.py       # MOCK_PROJECTS (with nested todos), mock_api_client, mock_config_entry
├── test_config_flow.py  # menu, device flow (gated polling), magic link, reauth
├── test_oidc.py      # device flow + refresh over HTTP, token source, retry on UNAUTHENTICATED, migration
├── test_coordinator.py  # data shape, reauth/retry on setup, subscription reconnect, GraphQL error mapping
└── test_todo.py
```

## Platforms

| Platform | Entity | One per |
|----------|--------|---------|
| `todo` | `TelosTodoListEntity` | open (non-archived) project |

All entities hang off one service device per config entry (`identifiers={(DOMAIN, entry_id)}`).
Entity unique id: `{entry_id}_todo_{project_id}`. The entity's name follows the project's name.
`extra_state_attributes.url` is the project's link back into the Telos app (`Project.url`).

## Data flow

`coordinator.data`:

```python
{
    "projects": [{"id", "name", "url"}, ...],          # sorted by name
    "todos_by_project": {project_id: [{"id", "title", "notes", "dueAt", "completedAt"}, ...]},  # board order
}
```

- **Polling:** every 15 min (`DEFAULT_SCAN_INTERVAL`).
- **Push:** `async_subscribe_updates` holds one socket with a `boardChanged(projectId)` subscription per project, and calls `async_request_refresh()` (debounced) on each event. The socket runs inside an `asyncio.timeout(None)` block. When a refresh finds a different set of projects, it moves that deadline to now, and the loop reconnects with the new set. Other errors reconnect after 30 s.
- **New projects** get entities straight away: `todo.async_setup_entry` adds a coordinator listener that creates entities for unseen project ids. A project that is archived or deleted leaves its entity unavailable.
- **Writes** call the client, then `coordinator.async_refresh()`. The board event that follows is debounced into the same refresh.

## Field mapping

| HA `TodoItem` | Telos `Todo` |
|---------------|--------------|
| `uid` | `id` |
| `summary` | `title` |
| `description` | `notes` |
| `due` (datetime, UTC) | `dueAt` |
| `status` | `completedAt` set → `COMPLETED` |

Supported features: create, update, delete (archive), set due datetime, set description. Due *date* (no time) is not offered, since `dueAt` is a timestamp. Moving items is not offered either, because Telos's `position` is project-wide and lane-aware.

## Sign-in

`entry.data[CONF_AUTH_METHOD]` says how the entry signs in (entries from before 1.2 are migrated to `session`):

| `auth_method` | Data | Bearer |
|---------------|------|--------|
| `session` | `email`, `token` | the better-auth session token, as is (`StaticToken`) |
| `oidc` | `access_token`, `refresh_token`, `expires_at`, `issuer`, `audience`, `client_id`, `token_endpoint` | `OidcTokenSource`: refreshes within 60 s of expiry (one at a time, behind a lock) and saves new tokens to the entry |

`TelosApiClient._graphql` asks the token source for a bearer on every call. On `UNAUTHENTICATED` it asks the source to refresh, and retries once (a session can't refresh, so it raises at once). A refused refresh (`invalid_grant`) is a `TelosAuthError`, so the coordinator starts reauth. The socket takes a fresh token on every connect. Telos closes a socket whose provider token has expired, and the reconnect loop opens a new one.
