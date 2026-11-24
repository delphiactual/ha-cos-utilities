"""Constants for the Colorado Springs Utilities integration."""

DOMAIN = "csu"
CONF_ACCESS_TOKEN = "access_token"
CONF_TOKEN_EXPIRES_AT = "token_expires_at"
CONF_CUSTOMER_ID = "customer_id"
CONF_MFA_CODE = "mfa_code"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
TIME_ZONE = "America/Denver"
CONF_HOURLY_REBUILD_DONE = "hourly_rebuild_done"
# statistic id -> starts where the rebuild was refused or found no history
CONF_HOURLY_REBUILD_REFUSED = "hourly_rebuild_refused"
# Release that last touched the rebuild state, so a new release gets fresh tries
CONF_HOURLY_REBUILD_VERSION = "hourly_rebuild_version"
