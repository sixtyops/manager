"""Build managed test fleets in the caller's mock_db, using production APIs.

Helper         | Effect
make_fleet     | Create sites and managed AP/switch rows; return IDs and IPs.
set_version    | Set an observed version through the role's status API.
set_health     | Set a poll timestamp and error through the role's status API.
confirm        | Record a working-version confirmation through the database API.

Only current devices, tower_sites and firmware_confirmations are covered.
This does not create CPE topology, identity/MAC history, archives or artifacts.
Credentials and documentation-range addresses are synthetic. No transport runs.
"""

from updater import database as db


def make_fleet(*, sites: int = 2, aps_per_site: int = 5,
               switches_per_site: int = 1,
               ap_models: tuple[str, ...] = ("TNA-301", "TNA-303L")) -> dict:
    """Return site IDs and managed device references from an isolated test DB."""
    fleet = {"sites": [], "aps": [], "switches": []}
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
                device_id = upsert(ip, "fixture-user", "synthetic-password",
                                   tower_site_id=site_id, model=model,
                                   firmware_version="1.0.0")
                fleet[bucket].append({"id": device_id, "ip": ip, "role": role,
                                      "model": model, "tower_site_id": site_id})
    return fleet


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
