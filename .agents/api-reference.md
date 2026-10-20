# Telos API used by this integration

Endpoint: `POST {url}/graphql` for queries and mutations, and for subscriptions as GraphQL over SSE (graphql-sse's distinct-connections mode: the same POST with `Accept: text/event-stream`, one request per subscription). Telos has no WebSocket server.
The schema is generated from Telos's Drizzle schema. Its SDL is `apps/telos/server/__generated__/schema.graphql` (run `npm run codegen` in apps/telos).

## Auth

- HTTP: `Authorization: Bearer <session token>`.
- Event streams: the same `Authorization` header.
- An expired or missing session gets a GraphQL error with `extensions.code == "UNAUTHENTICATED"` on HTTP 200.
- Errors otherwise say `NOT_FOUND` rather than `FORBIDDEN`.

### Sign-in (no token)

```graphql
mutation SignInMagicLink($email: String!) {
  signInMagicLink(email: $email) { status token userId }
}
```
- `SECURE_LOCAL_NET` → `token` / `userId` are a live session, so no link step is needed.
- Otherwise the link is emailed (only when the instance has `SMTP_URL`; without it the mutation fails with `BAD_USER_INPUT`). Links last 15 min. Too many attempts give `TOO_MANY_REQUESTS`.

```graphql
mutation MagicLinkVerify($token: String!) {
  magicLinkVerify(token: $token) { token user { id } }
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
One SSE stream per project. Each change is `event: next` with `data: <ExecutionResult JSON>`, and the stream ends with `event: complete`. Lines starting `:` are keep-alives (about every 12 s). A refused credential is a `next` result with `extensions.code` `UNAUTHENTICATED`, followed by `complete`; Telos ends a provider-token stream that way when the token expires. Fires on every committed change to the project's rows, whoever made it. Only a `user` actor (a session) may subscribe. An API key gets `FORBIDDEN`.
