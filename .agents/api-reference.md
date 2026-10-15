# Telos API used by this integration

Endpoint: `POST {url}/graphql` (queries and mutations); `ws(s)://{url}/graphql` with the `graphql-transport-ws` subprotocol (subscriptions).
The schema is generated from Telos's Drizzle schema. Its SDL is `apps/telos/server/__generated__/schema.graphql` (run `npm run codegen` in apps/telos).

## Auth

- HTTP: `Authorization: Bearer <session token>`.
- Socket: `connection_init` with `payload: {"authorization": "Bearer <token>"}`.
- An expired or missing session gets a GraphQL error with `extensions.code == "UNAUTHENTICATED"` on HTTP 200.
- Errors otherwise say `NOT_FOUND` rather than `FORBIDDEN`.

### Sign-in (no token)

```graphql
mutation RequestMagicLink($email: String!) {
  requestMagicLink(email: $email) { ok magicLink token userId }
}
```
- `AUTH_MAGIC_LINK=false` → `token` / `userId` are a live session, so no link step is needed.
- Otherwise the link is printed to the server log, and `magicLink` is set only when the server exposes it (outside production, or with `EXPOSE_MAGIC_LINK`). Links last 15 min. Five requests per address per 15 min, then it errors.

```graphql
mutation VerifyMagicLink($token: String!) {
  verifyMagicLink(token: $token) { token userId }
}
```
Link format: `{APP_URL}/auth/verify?token=<token>`. A bad token gives `BAD_USER_INPUT`.

## Query

```graphql
query Projects {
  projects(
    where: { archivedAt: { isNull: true } }
    orderBy: { name: { direction: asc, priority: 1 } }
  ) {
    id name url
    todos(
      where: { archivedAt: { isNull: true } }
      orderBy: { position: { direction: asc, priority: 1 } }
    ) { id title notes dueAt completedAt }
  }
}
```
Soft-delete filtering (`archivedAt`) applies only to root queries (`softDelete.scope: 'root'`), so the nested `todos` filter is required.

## Mutations

| Mutation | Used for |
|----------|----------|
| `createTodo(values: {projectId, title, notes?, dueAt?})` | add item; the server picks the lane |
| `updateTodo(where: {id: {eq}}, set: {title?, notes?, dueAt?})` | edit item; `null` clears a field |
| `completeTodo(id, reason: "Home Assistant")` | check off; refused while a dependency is open |
| `reopenTodo(id, reason: "Home Assistant")` | uncheck |
| `deleteTodo(where: {id: {eq}})` | remove item = archive (no `hard: true`) |

Every todo mutation selects `id title notes dueAt completedAt`.

## Subscription

```graphql
subscription BoardChanged($projectId: ID!) {
  boardChanged(projectId: $projectId) { projectId table }
}
```
One per project, multiplexed on one socket, with the project id as the operation id. Fires on every committed change to the project's rows, whoever made it. Only a `user` actor (a session) may subscribe. An API key gets `FORBIDDEN`.
