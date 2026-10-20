DOMAIN = "telos"

CONF_URL = "url"
CONF_EMAIL = "email"
CONF_TOKEN = "token"
CONF_USER_ID = "user_id"

DEFAULT_SCAN_INTERVAL = 15  # minutes

PLATFORMS = ["todo"]

# How the entry signs in. Entries from before 1.2 have no key: a session.
CONF_AUTH_METHOD = "auth_method"
AUTH_SESSION = "session"  # a better-auth session token in CONF_TOKEN
AUTH_OIDC = "oidc"  # an access token from Telos's OIDC provider (device flow)

# For AUTH_OIDC entries.
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_EXPIRES_AT = "expires_at"
CONF_ISSUER = "issuer"
CONF_AUDIENCE = "audience"
CONF_CLIENT_ID = "client_id"
CONF_TOKEN_ENDPOINT = "token_endpoint"
