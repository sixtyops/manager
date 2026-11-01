"""Read and enforce rollout data format compatibility."""

import sqlite3
from dataclasses import dataclass
from functools import wraps
from pathlib import Path


ROLLOUT_FORMAT_KEY = "rollout_format_version"
CURRENT_ROLLOUT_FORMAT = 2


class RolloutCompatibilityError(RuntimeError):
    """The database cannot be safely read or migrated by this image."""


@dataclass(frozen=True)
class CompatibilityResult:
    allowed: bool
    reason: str | None = None
    format_version: int | None = None
    empty: bool = False
    legacy: bool = False


def inspect_connection(
    db: sqlite3.Connection, max_supported: int = CURRENT_ROLLOUT_FORMAT
) -> CompatibilityResult:
    """Read the marker and held-row state from one SQLite snapshot."""
    owns_transaction = not db.in_transaction
    try:
        if owns_transaction:
            db.execute("BEGIN")
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not tables:
            return CompatibilityResult(True, empty=True)
        if "settings" not in tables:
            return CompatibilityResult(False, "Rollout compatibility marker is missing")
        marker = db.execute(
            "SELECT value FROM settings WHERE key = ?", (ROLLOUT_FORMAT_KEY,)
        ).fetchone()
        if marker is None:
            has_saved_members = False
            if "rollout_devices" in tables:
                has_saved_members = db.execute(
                    "SELECT 1 FROM rollout_devices LIMIT 1"
                ).fetchone() is not None
            has_held_rollout = "rollouts" in tables and db.execute(
                "SELECT 1 FROM rollouts WHERE status = 'waiting_hold' LIMIT 1"
            ).fetchone() is not None
            if has_saved_members or has_held_rollout:
                return CompatibilityResult(
                    False,
                    "Rollout compatibility marker is missing while saved rollout data is present",
                )
            return CompatibilityResult(True, format_version=1, legacy=True)
        raw = marker[0]
        if not isinstance(raw, str) or not raw.isdecimal() or str(int(raw)) != raw:
            return CompatibilityResult(False, "Rollout compatibility marker is malformed")
        version = int(raw)
        if version < 1:
            return CompatibilityResult(False, "Rollout compatibility marker is malformed")
        if version > max_supported:
            return CompatibilityResult(
                False,
                f"Rollout format {version} exceeds this image's supported format {max_supported}",
                version,
            )
        if "rollouts" not in tables:
            return CompatibilityResult(False, "Rollout compatibility could not be read", version)
        held = db.execute(
            "SELECT 1 FROM rollouts WHERE status = 'waiting_hold' LIMIT 1"
        ).fetchone()
        if held and version < 2:
            return CompatibilityResult(
                False,
                "Saved waiting_hold rollout requires format 2 support",
                version,
            )
        return CompatibilityResult(True, format_version=version)
    except (sqlite3.Error, ValueError, TypeError, OverflowError):
        return CompatibilityResult(False, "Rollout compatibility could not be read")
    finally:
        if owns_transaction and db.in_transaction:
            db.rollback()


def require_compatible_connection(
    db: sqlite3.Connection,
    max_supported: int = CURRENT_ROLLOUT_FORMAT,
    *,
    allow_empty: bool = False,
) -> CompatibilityResult:
    """Raise before a write boundary when the database is incompatible."""
    result = inspect_connection(db, max_supported)
    if not result.allowed or (result.empty and not allow_empty):
        reason = result.reason or "Rollout compatibility marker is missing"
        raise RolloutCompatibilityError(reason)
    return result


def inspect_database(
    path: str | Path, max_supported: int = CURRENT_ROLLOUT_FORMAT
) -> CompatibilityResult:
    """Inspect a database through a read-only URI before initialization writes."""
    connection = None
    try:
        uri = f"{Path(path).resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        connection.execute("BEGIN")
        return inspect_connection(connection, max_supported)
    except (sqlite3.Error, OSError, ValueError):
        return CompatibilityResult(False, "Rollout compatibility could not be read")
    finally:
        if connection is not None:
            connection.close()


def require_compatible_database(
    path: str | Path, max_supported: int = CURRENT_ROLLOUT_FORMAT
) -> CompatibilityResult:
    """Refuse initialization if an existing database is not compatible."""
    result = inspect_database(path, max_supported)
    if not result.allowed:
        raise RolloutCompatibilityError(
            result.reason or "Rollout compatibility could not be read"
        )
    return result


def initialize_empty_database(db: sqlite3.Connection) -> None:
    """Stamp a new database and keep scheduled rollouts disabled."""
    db.executemany(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (
            (ROLLOUT_FORMAT_KEY, str(CURRENT_ROLLOUT_FORMAT)),
            ("schedule_enabled", "false"),
            ("scheduled_rollout_safety_ready", "false"),
        ),
    )


def stamp_legacy_database(db: sqlite3.Connection) -> None:
    """Stamp confirmed legacy data in the migration transaction."""
    result = inspect_connection(db)
    if not result.allowed or not result.legacy:
        raise RolloutCompatibilityError(
            result.reason or "Rollout compatibility marker is no longer missing"
        )
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, '1')",
        (ROLLOUT_FORMAT_KEY,),
    )


def raise_rollout_format_to_2(db: sqlite3.Connection) -> None:
    """Raise a valid format-1 marker in the caller's active transaction."""
    result = require_compatible_connection(db)
    if result.format_version == 2:
        return
    if result.format_version != 1:
        raise RolloutCompatibilityError("Rollout compatibility marker is malformed")
    cursor = db.execute(
        "UPDATE settings SET value = '2' WHERE key = ? AND value = '1'",
        (ROLLOUT_FORMAT_KEY,),
    )
    if cursor.rowcount != 1:
        raise RolloutCompatibilityError("Rollout compatibility marker changed during creation")


def migration_guard(function):
    """Guard directly callable migration helpers before their first write."""
    @wraps(function)
    def guarded(db, *args, **kwargs):
        require_compatible_connection(db)
        return function(db, *args, **kwargs)

    return guarded
