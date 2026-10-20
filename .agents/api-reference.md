# Telos API used by this integration

Endpoint: `POST {url}/graphql` (queries and mutations); `ws(s)://{url}/graphql` with the `graphql-transport-ws` subprotocol (subscriptions).
The schema is generated from Telos's Drizzle schema. Its SDL is `apps/telos/server/__generated__/schema.graphql` (run `npm run codegen` in apps/telos).

## Auth

- HTTP: `Authorization: Bearer <session token>`.
- Socket: `connection_init` with `payload: {"authorization": "Bearer <token>"}`.
- An expired or missing session gets a GraphQL error with `extensions.code == "UNAUTHENTICATED"` on HTTP 200.
- Errors otherwise say `NOT_FOUND` rather than `FORBIDDEN`.

### Discovery (no token)

```graphql
query AuthConfig {
  authConfig { magicLink secureLocalNet sso { name deviceClientId } }
}
```
`sso` is null when no provider is set up. `deviceClientId` is null until the admin sets `OIDC_DEVICE_CLIENT_ID`.

`GET {url}/.well-known/oauth-protected-resource` (RFC 9728): `{ "resource": <audience>, "authorization_servers": [<issuer>], "bearer_methods_supported": ["header"] }`.

### Provider device flow (RFC 8628, at the issuer)

- `GET {issuer}/.well-known/openid-configuration`, then check that `issuer` matches, and read `device_authorization_endpoint` and `token_endpoint`.
- `POST device_authorization_endpoint` form `client_id`, `scope=openid email offline_access`, `resource=<audience>`. The answer has `device_code user_code verification_uri verification_uri_complete expires_in interval`.
- `POST token_endpoint` form `grant_type=urn:ietf:params:oauth:grant-type:device_code`, `device_code`, `client_id`. The answer is `authorization_pending` / `slow_down` (add 5 s) / `access_denied` / `expired_token`, or the tokens.
- Refresh: `POST token_endpoint` form `grant_type=refresh_token`, `refresh_token`, `client_id`, `resource=<audience>`.

### Who am I

```graphql
query Me { users { id } }
```
`users` lists only the caller. It is used after the device flow, to find the Telos user id behind the provider token.

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
One per project, multiplexed on one socket, with the project id as the operation id. Fires on every committed change to the project's rows, whoever made it. Only a `user` actor (a session) may subscribe. An API key gets `FORBIDDEN`.
