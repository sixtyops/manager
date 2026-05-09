"""Shared config management utilities used by app.py and poller.py."""

import difflib
import json as _json
from copy import deepcopy

PROTECTED_CONFIG_KEYS = {"network", "ethernet"}


def _md5_crypt(plain: str) -> str:
    """Hash a plaintext password into Tachyon's `$1$<salt>$<hash>` format.

    34-char modular crypt MD5 — what the device stores in `system.users[*].password`
    and what the validator accepts on write. Mirrors the algorithm used by
    `updater.vendors.tachyon.client._normalize_user_passwords`; kept here as a
    shared utility so the at-rest hashing path doesn't pull in a vendor-specific
    import.
    """
    import os
    from passlib.hash import md5_crypt
    salt = os.urandom(4).hex()  # 8 hex chars; passlib clamps to 8 max
    return md5_crypt.using(salt=salt).hash(plain)


def hash_template_user_passwords(
    config_fragment: dict | None,
    form_data: dict | None,
    prior_fragment: dict | None = None,
) -> None:
    """Hash plaintext `system.users[*].password` and `form_data.users[*].password`
    values in place, *before* the template is persisted.

    Storing operator-entered plaintext for device users in the manager DB is a
    soft-secret leak: anyone with read access to `config_templates` (a SQL dump,
    a CSV backup, an SFTP backup) sees the credentials. Hashing at rest in the
    same `$1$<salt>$<hash>` format the device stores means worst-case exposure
    is a brute-forceable hash, not the password itself.

    Rules per user (matched by username):
      - `password` starts with `$1$`  → already hashed; keep as-is.
      - `password` is a non-empty plaintext string → hash it.
      - `password` is empty/missing AND `prior_fragment` has a matching username
        with a stored hash → copy the prior hash forward (operator left the
        field blank, indicating "no change").
      - `password` is empty/missing AND no prior → drop the field. The device
        treats an empty `password` as "no change" anyway, but a missing field
        is what the read API returns for users without a set password.

    Both `config_fragment.system.users` and `form_data.users` are walked so the
    two stay in sync. `prior_fragment` is used to look up the previous hash by
    username; pass `None` on creation, the existing row's `config_fragment` on
    update.
    """
    prior_users_by_name: dict[str, str] = {}
    if isinstance(prior_fragment, dict):
        prior_users = ((prior_fragment.get("system") or {}).get("users") or [])
        for u in prior_users:
            if isinstance(u, dict):
                name = u.get("username")
                pw = u.get("password")
                if isinstance(name, str) and isinstance(pw, str) and pw.startswith("$1$"):
                    prior_users_by_name[name] = pw

    def _resolve(user: dict) -> None:
        pw = user.get("password")
        if isinstance(pw, str) and pw.startswith("$1$"):
            return  # already hashed
        if isinstance(pw, str) and pw:
            user["password"] = _md5_crypt(pw)
            return
        # empty or missing → preserve prior hash if we have one
        name = user.get("username")
        if isinstance(name, str) and name in prior_users_by_name:
            user["password"] = prior_users_by_name[name]
        else:
            user.pop("password", None)

    if isinstance(config_fragment, dict):
        users = (config_fragment.get("system") or {}).get("users") or []
        for u in users:
            if isinstance(u, dict):
                _resolve(u)

    if isinstance(form_data, dict):
        users = form_data.get("users") or []
        for u in users:
            if isinstance(u, dict):
                _resolve(u)


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
