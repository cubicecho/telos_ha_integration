# Integration structure

```
custom_components/telos/
├── __init__.py       # async_setup_entry: client → coordinator → first refresh → todo platform → subscription task
├── api.py            # TelosApiClient: all HTTP, incl. the SSE board streams; TelosApiError (with .code) / TelosConnectionError / TelosAuthError
├── config_flow.py    # user → link (magic link) → entry; reauth_confirm → link → update + reload
├── const.py          # DOMAIN, CONF_* keys, scan interval, PLATFORMS
├── coordinator.py    # TelosCoordinator: projects + todos, boardChanged subscription loop
├── todo.py           # TelosTodoListEntity, one per open project
├── manifest.json
├── strings.json      # copied verbatim to translations/en.json
└── translations/en.json
tests/
├── conftest.py       # MOCK_PROJECTS (with nested todos), mock_api_client, mock_config_entry
├── test_config_flow.py
├── test_coordinator.py  # data shape, reauth/retry on setup, subscription reconnect, GraphQL error mapping
├── test_sse.py       # boardChanged over SSE: merge, per-project failure, refusals, auth reconnect
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
- **Push:** `async_subscribe_updates` holds one SSE stream per project (`boardChanged(projectId)`), merged by `subscribe_board_changes`, and calls `async_request_refresh()` (debounced) on each event. The streams run inside an `asyncio.timeout(None)` block. When a refresh finds a different set of projects, it moves that deadline to now, and the loop reconnects with the new set. A project whose own stream fails is dropped and the rest keep running; a dropped connection (or a minute without even a keep-alive) reconnects after 30 s. A refused credential reconnects at once if the stream had been up longer than 30 s (an expired access token: the next connect brings a fresh one), and after 30 s otherwise.
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
