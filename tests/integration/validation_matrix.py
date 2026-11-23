"""Live-dev validation matrix keyed by product feature."""

DEV_BLOCKING = "dev_blocking"
DEV_SSO = "dev_sso"
UNIT_API_ONLY = "unit_api_only"
DEFERRED_EXTERNAL = "deferred_external"

VALIDATION_LANES = {
    DEV_BLOCKING,
    DEV_SSO,
    UNIT_API_ONLY,
    DEFERRED_EXTERNAL,
}

FEATURE_VALIDATION_MATRIX = {
    "update_single_device": DEV_BLOCKING,
    "sso_oidc": DEV_SSO,
    "radius_auth": DEV_BLOCKING,
    "config_backup": DEV_BLOCKING,
    "config_templates": DEV_BLOCKING,
    "config_compliance": DEV_BLOCKING,
    "config_push": DEV_BLOCKING,
    "slack_notifications": DEFERRED_EXTERNAL,
    "device_portal": DEV_BLOCKING,
    "device_history": DEV_BLOCKING,
    "tower_sites": DEV_BLOCKING,
    "beta_firmware": UNIT_API_ONLY,
    "firmware_hold_custom": UNIT_API_ONLY,
    "snmp_traps": DEFERRED_EXTERNAL,
    "webhooks": DEFERRED_EXTERNAL,
}
