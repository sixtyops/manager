"""Shared config management utilities used by app.py and poller.py."""

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
