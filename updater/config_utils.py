"""Shared config management utilities used by app.py and poller.py."""

import difflib
import json as _json
from copy import deepcopy

PROTECTED_CONFIG_KEYS = {"network", "ethernet"}


def validate_fragment_safety(fragment: dict):
    """Raise ValueError if fragment tries to modify protected config sections."""
    if not isinstance(fragment, dict):
        return
    for key in PROTECTED_CONFIG_KEYS:
        if key in fragment:
            raise ValueError(
                f"Config templates cannot modify the '{key}' section — "
                f"this could make devices unreachable"
            )


def deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base. Overlay values win for scalars.
    Lists in overlay replace lists in base entirely."""
    result = deepcopy(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def fragment_matches(config: dict, fragment: dict) -> bool:
    """Check if all keys in fragment match corresponding values in config."""
    for key, value in fragment.items():
        if key not in config:
            return False
        if isinstance(value, dict) and isinstance(config[key], dict):
            if not fragment_matches(config[key], value):
                return False
        elif config[key] != value:
            return False
    return True


def filter_templates_by_device_type(
    templates: list[dict], device_type: str
) -> tuple[list[dict], list[dict]]:
    """Split templates into (applicable, excluded) for a device of given type.

    A template with no `device_types` restriction (NULL/missing/empty) applies
    to every device type. A template whose `device_types` JSON array names the
    device's type applies; otherwise it goes to `excluded`. Malformed values
    are treated as unrestricted (applicable) — same forgiving behavior as the
    auto-enforce path so we don't surface noisy "skipped" reasons for bad data.
    """
    applicable: list[dict] = []
    excluded: list[dict] = []
    for t in templates:
        dt = t.get("device_types")
        if not dt:
            applicable.append(t)
            continue
        try:
            allowed_types = _json.loads(dt) if isinstance(dt, str) else dt
        except (_json.JSONDecodeError, TypeError):
            applicable.append(t)
            continue
        if not isinstance(allowed_types, list) or not allowed_types:
            applicable.append(t)
            continue
        if device_type in allowed_types:
            applicable.append(t)
        else:
            excluded.append(t)
    return applicable, excluded


def generate_config_diff(
    config_a: dict, config_b: dict, label_a: str = "before", label_b: str = "after"
) -> list[str]:
    """Return unified diff lines between two config dicts (pretty-printed JSON)."""
    a_lines = _json.dumps(config_a, indent=2, sort_keys=True).splitlines(keepends=True)
    b_lines = _json.dumps(config_b, indent=2, sort_keys=True).splitlines(keepends=True)
    return list(difflib.unified_diff(a_lines, b_lines, fromfile=label_a, tofile=label_b))


def check_config_compliance(device_config, templates: list[dict]) -> bool:
    """Check if a device config matches all enabled templates.

    For each template, extract the same key paths from the device config
    and compare. Returns True if all templates match.
    """
    import json

    if not templates:
        return True
    config = device_config
    if isinstance(config, str):
        config = json.loads(config)

    for template in templates:
        frag = template["config_fragment"]
        fragment = json.loads(frag) if isinstance(frag, str) else frag
        if not fragment_matches(config, fragment):
            return False
    return True
