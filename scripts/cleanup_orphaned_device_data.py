#!/usr/bin/env python3
"""One-time cleanup of audit/history rows whose device has already been deleted.

Before issue #56, deleting a device left orphaned rows in `config_enforce_log`,
`device_update_history`, and `device_uptime_events` keyed by the now-stale IP.
If an operator later reused that IP for a different device, the histories
blended together. This script purges any row in those tables whose IP is no
longer present in `devices`.

Safe to run repeatedly — does nothing on a clean DB.

Usage:
    python scripts/cleanup_orphaned_device_data.py [--db PATH] [--dry-run]
"""

import argparse
import sqlite3
import sys
from pathlib import Path


_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "sixtyops.db"

_TABLES = (
    "config_enforce_log",
    "device_update_history",
    "device_uptime_events",
)


def cleanup(db_path: Path, dry_run: bool = False) -> dict[str, int]:
    """Return per-table orphan counts.

    - `{}` means the database file was not found (main() exits 1).
    - `{table: 0, ...}` means the DB opened cleanly and had no orphans.
    - Any non-zero count is the number deleted (or that *would* be
      deleted under `--dry-run`).
    """
    if not db_path.exists():
        print(f"cleanup: database not found at {db_path}", file=sys.stderr)
        return {}

    db = sqlite3.connect(str(db_path), timeout=10)
    db.row_factory = sqlite3.Row
    counts: dict[str, int] = {}
    try:
        for table in _TABLES:
            row = db.execute(
                f"SELECT COUNT(*) AS n FROM {table} "
                "WHERE ip NOT IN (SELECT ip FROM devices)"
            ).fetchone()
            counts[table] = row["n"]
            if dry_run or row["n"] == 0:
                continue
            db.execute(
                f"DELETE FROM {table} WHERE ip NOT IN (SELECT ip FROM devices)"
            )
        if not dry_run:
            db.commit()
    finally:
        db.close()
    return counts


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", type=Path, default=_DEFAULT_DB,
                   help=f"Path to sixtyops.db (default: {_DEFAULT_DB})")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be deleted without modifying the DB")
    args = p.parse_args()

    counts = cleanup(args.db, dry_run=args.dry_run)
    if not counts:
        return 1
    verb = "would delete" if args.dry_run else "deleted"
    total = sum(counts.values())
    if total == 0:
        print("cleanup: no orphaned rows found")
    else:
        print(f"cleanup: {verb} {total} orphaned row(s):")
        for table, n in counts.items():
            print(f"  {table}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
