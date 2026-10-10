"""Build managed test fleets in the caller's mock_db, using production APIs.

Helper         | Effect
make_fleet     | Create sites and managed AP/switch rows; return IDs and IPs.
set_version    | Set an observed version through the role's status API.
set_health     | Set a poll timestamp and error through the role's status API.
confirm        | Record a working-version confirmation through the database API.
make_artifacts | Register one firmware file per model family; return the names.

make_fleet also writes CPEs (cpe_cache), device MACs, extra MACs and switch
topology links (switch_bridge_entries), and registers artifacts.

Only the current schema is covered: devices, tower_sites, cpe_cache,
switch_bridge_entries, firmware_registry and firmware_confirmations.
Not covered: archived devices, offline flags and artifact selection. The
current schema has no such state. Add helpers when the schema adds it.
Credentials, MACs and addresses are synthetic. No transport runs.
"""

import hashlib

from updater import database as db

# One synthetic firmware file per model family. The date in the name makes
# the release-date Firmware Hold apply to it.
FAMILY_FILES = {
    "tna-30x": "tna-30x-1.12.2-r54944-20250828-fixture.bin",
    "tna-303l": "tna-303l-1.12.2-r54944-20250828-fixture.bin",
    "tns-100": "tns-100-1.12.2-r54944-20250828-fixture.bin",
}


def make_artifacts(families: tuple[str, ...] = tuple(FAMILY_FILES)) -> dict:
    """Register one firmware file per family; return {family: filename}."""
    artifacts = {}
    for family in families:
        filename = FAMILY_FILES[family]
        db.register_firmware(filename, source="fixture",
                             sha256=hashlib.sha256(filename.encode()).hexdigest())
        artifacts[family] = filename
    return artifacts


def _mac(kind: int, index: int, extra: int = 0) -> str:
    """Return a locally administered MAC, unique per kind, index and extra."""
    return f"02:00:{kind:02X}:{index // 256:02X}:{index % 256:02X}:{extra:02X}"


def make_fleet(*, sites: int = 2, aps_per_site: int = 5, cpes_per_ap: int = 4,
               switches_per_site: int = 1, macs_per_device: int = 1,
               ap_models: tuple[str, ...] = ("TNA-301", "TNA-303L"),
               families: tuple[str, ...] = tuple(FAMILY_FILES)) -> dict:
    """Return site IDs, devices, CPEs, links and artifacts from an isolated DB.

    Each AP and switch gets a primary MAC in devices.mac. Extra MACs (when
    macs_per_device is above 1) are only in the switch bridge table. Each AP
    links to the first switch of its site on port ``eth<n>``. A site with no
    switch has no links. ``fleet["macs"]`` maps device IP to all its MACs.
    """
    fleet = {"sites": [], "aps": [], "switches": [], "cpes": [], "links": [],
             "macs": {}, "index": {},
             "artifacts": make_artifacts(families)}
    counters = {"ap": 0, "switch": 0}
    for site_index in range(sites):
        site_id = db.create_tower_site(f"Fixture site {site_index + 1}")
        fleet["sites"].append(site_id)
        for role, count, bucket in (("ap", aps_per_site, "aps"),
                                    ("switch", switches_per_site, "switches")):
            upsert = db.upsert_access_point if role == "ap" else db.upsert_switch
            for index in range(count):
                # Use documentation addresses with separate site offsets.
                prefix = "192.0.2" if role == "ap" else "198.51.100"
                ip = f"{prefix}.{10 + site_index * 50 + index}"
                model = ap_models[index % len(ap_models)] if role == "ap" else "TNS-100"
                counters[role] += 1
                n = counters[role]
                macs = [_mac(1 if role == "ap" else 2, n, k)
                        for k in range(max(1, macs_per_device))]
                device_id = upsert(ip, "fixture-user", "synthetic-password",
                                   tower_site_id=site_id, model=model,
                                   firmware_version="1.0.0", mac=macs[0])
                fleet[bucket].append({"id": device_id, "ip": ip, "role": role,
                                      "model": model, "tower_site_id": site_id,
                                      "mac": macs[0]})
                fleet["macs"][ip] = macs
                fleet["index"][ip] = n
        _link_site(fleet, site_id)
        _add_cpes(fleet, site_id, cpes_per_ap)
    return fleet


def _link_site(fleet: dict, site_id: int) -> None:
    """Put each AP's MACs in the bridge table of the site's first switch."""
    switches = [d for d in fleet["switches"] if d["tower_site_id"] == site_id]
    if not switches:
        return
    entries = []
    for port, ap in enumerate((d for d in fleet["aps"]
                               if d["tower_site_id"] == site_id), start=1):
        for mac in fleet["macs"][ap["ip"]]:
            entries.append({"mac": mac, "port": f"eth{port}"})
            fleet["links"].append({"switch_ip": switches[0]["ip"], "mac": mac,
                                   "port": f"eth{port}", "ap_ip": ap["ip"]})
    db.replace_switch_bridge_entries(switches[0]["ip"], entries)


def _add_cpes(fleet: dict, site_id: int, cpes_per_ap: int) -> None:
    """Attach cpes_per_ap CPEs to each AP of the site."""
    for ap in (d for d in fleet["aps"] if d["tower_site_id"] == site_id):
        for i in range(cpes_per_ap):
            cpe = {"ip": f"198.18.{fleet['index'][ap['ip']]}.{i + 1}",
                   "mac": _mac(3, fleet["index"][ap["ip"]] * 100 + i), "model": "TNA-30x-CPE",
                   "firmware_version": "1.0.0",
                   "system_name": f"cpe-{fleet['index'][ap['ip']]}-{i + 1}"}
            db.upsert_cpe(ap["ip"], cpe)
            fleet["cpes"].append({**cpe, "ap_ip": ap["ip"]})


def set_version(device: dict, version: str) -> None:
    """Set the observed version without recording a confirmation."""
    update = db.update_ap_status if device["role"] == "ap" else db.update_switch_status
    update(device["ip"], firmware_version=version)


def set_health(device: dict, last_seen: str, *, error: str | None = None) -> None:
    """Record a synthetic poll result; omit this call to leave last_seen missing."""
    update = db.update_ap_status if device["role"] == "ap" else db.update_switch_status
    update(device["ip"], last_seen=last_seen, last_error=error)


def confirm(device: dict, version: str) -> None:
    """Record proof separately from the observed version and current health."""
    db.mark_device_firmware_confirmed(device["ip"], version)
