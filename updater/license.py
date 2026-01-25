"""Backward-compatibility shim — all functionality moved to features.py."""

from .features import *  # noqa: F401,F403
from .features import (  # explicit re-exports for type checkers
    Feature,
    DANGEROUS_FEATURES,
    is_feature_enabled,
    is_dangerous,
    get_feature_map,
    get_instance_id,
    require_pro,
    require_feature,
    get_license_state,
    get_nag_info,
    get_billable_device_count,
    validate_license,
    clear_license,
    init_license_validator,
    _FEATURE_DISPLAY_NAMES,
)
