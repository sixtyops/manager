"""Identity observations for devices; no observation verifies a connection."""

import json
import re
import sqlite3
from datetime import datetime, timezone


def _serial(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value.lower() in {"unknown", "none", "null", "n/a", "na", "serial"}:
        return None
    if re.fullmatch(r"<[^>]+>", value):
        return None
    return value


def _mac(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{12}", value):
        parts = [value[i:i + 2] for i in range(0, 12, 2)]
    elif re.fullmatch(r"(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}", value):
        parts = re.split("[:-]", value)
    else:
        return None
    return ":".join(part.upper() for part in parts)


def _mac_observations(macs):
    if isinstance(macs, dict):
        items = macs.items()
    else:
        items = (("radio", mac) for mac in (macs or ()))
    result = {}
    for interface, value in items:
        normalized = _mac(value)
        if normalized:
            result.setdefault(normalized, set()).add(str(interface or "unknown"))
    return result


def _open_conflicts(db):
    return db.execute(
        "SELECT kind, value, device_ids FROM identity_conflicts WHERE state='open'"
    ).fetchall()


def match_observation(db, serial=None, macs=None):
    """Return candidates and an unverified/conflict/no_match status."""
    try:
        serial = _serial(serial)
        aliases = _mac_observations(macs)
        candidates = set()
        if serial:
            candidates.update(row[0] for row in db.execute(
                "SELECT id FROM devices WHERE uid=? AND uid_source='serial'", (serial,)
            ))
        if aliases:
            marks = ",".join("?" for _ in aliases)
            candidates.update(row[0] for row in db.execute(
                f"SELECT DISTINCT device_id FROM device_macs WHERE mac IN ({marks})",
                tuple(aliases),
            ))
        conflict = False
        for kind, value, encoded_ids in _open_conflicts(db):
            involved = set(json.loads(encoded_ids))
            if (kind == "serial" and serial == value) or (kind == "mac" and value in aliases):
                candidates.update(involved)
                conflict = True
            if candidates.intersection(involved):
                candidates.update(involved)
                conflict = True
        status = "conflict" if conflict or len(candidates) > 1 else (
            "unverified" if candidates else "no_match"
        )
        return {"device_ids": sorted(candidates), "status": status, "verified": False}
    except Exception:
        return {"device_ids": [], "status": "failure", "verified": False}


def _record_conflict(db, kind, value, device_ids, timestamp):
    rows = db.execute(
        "SELECT id, device_ids FROM identity_conflicts "
        "WHERE kind=? AND value=? AND state='open'", (kind, value)
    ).fetchall()
    involved = set(device_ids)
    for row in rows:
        involved.update(json.loads(row[1]))
    encoded = json.dumps(sorted(involved))
    if rows:
        db.execute(
            "UPDATE identity_conflicts SET device_ids=?, updated_at=? WHERE id=?",
            (encoded, timestamp, rows[0][0]),
        )
    else:
        db.execute(
            "INSERT INTO identity_conflicts(kind,value,device_ids,created_at,updated_at) "
            "VALUES(?,?,?,?,?)", (kind, value, encoded, timestamp, timestamp)
        )
    return involved


def record_identity(db, device_id, serial=None, macs=None, *, now=None):
    """Atomically store claims, aliases, and open holds for competing claims."""
    serial = _serial(serial)
    aliases = _mac_observations(macs)
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    if db.in_transaction:
        return {"device_ids": [], "status": "failure", "verified": False}
    started = False
    try:
        db.execute("BEGIN IMMEDIATE")
        started = True
        row = db.execute("SELECT uid, uid_source FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise ValueError("unknown device")
        held = set()
        for mac, interfaces in aliases.items():
            for interface in interfaces:
                existing = db.execute(
                    "SELECT id FROM device_macs WHERE device_id=? AND mac=? AND interface=?",
                    (device_id, mac, interface),
                ).fetchone()
                if existing:
                    db.execute("UPDATE device_macs SET last_seen=? WHERE id=?", (timestamp, existing[0]))
                else:
                    db.execute(
                        "INSERT INTO device_macs(device_id,mac,interface,first_seen,last_seen) "
                        "VALUES(?,?,?,?,?)", (device_id, mac, interface, timestamp, timestamp)
                    )
            owners = {r[0] for r in db.execute(
                "SELECT DISTINCT device_id FROM device_macs WHERE mac=?", (mac,)
            )}
            if len(owners) > 1:
                held.update(_record_conflict(db, "mac", mac, owners, timestamp))

        if serial:
            owners = {r[0] for r in db.execute(
                "SELECT id FROM devices WHERE uid=? AND uid_source='serial'", (serial,)
            )}
            for kind, value, encoded in _open_conflicts(db):
                ids = set(json.loads(encoded))
                if kind == "serial" and value == serial:
                    owners.update(ids)
            if owners - {device_id}:
                held.update(_record_conflict(db, "serial", serial, owners | {device_id}, timestamp))
            elif row[1] in (None, "ip_legacy", "mac") or row[0] == serial:
                db.execute("UPDATE devices SET uid=?, uid_source='serial' WHERE id=?", (serial, device_id))

        if not serial and not held and aliases and row[1] in (None, "ip_legacy"):
            first_mac = next(iter(aliases))
            owner = db.execute("SELECT id FROM devices WHERE uid=?", (first_mac,)).fetchone()
            if owner is None:
                db.execute("UPDATE devices SET uid=?, uid_source='mac' WHERE id=?", (first_mac, device_id))
        result = {"device_ids": sorted(held or {device_id}),
                  "status": "conflict" if held else ("unverified" if serial or aliases else "no_match"),
                  "verified": False}
        db.commit()
        return result
    except Exception:
        if started:
            db.rollback()
        return {"device_ids": [], "status": "failure", "verified": False}
