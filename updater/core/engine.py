"""Shared rollout wave reservation boundary."""

from typing import Any

from .. import database as db


def start_wave(
    rollout: dict,
    batch: dict[str, list[str]],
    settings: dict,
    window_key: Any,
    *,
    first_wave_held: bool = False,
) -> bool:
    """Atomically reserve one non-empty wave before admitting its async job.

    The reservation consumes the window even when later job admission fails.
    """
    if not isinstance(rollout, dict) or not rollout.get("id") or not rollout.get("phase"):
        return False
    assignments = [
        (ip, device_type, rollout.get("phase"))
        for device_type, key in (("ap", "ap_ips"), ("switch", "switch_ips"))
        for ip in batch.get(key, [])
    ]
    if not assignments:
        return False
    return db.reserve_rollout_wave(
        rollout_id=rollout["id"],
        phase=rollout["phase"],
        window_key=window_key,
        assignments=assignments,
        settings=settings,
        first_wave_held=first_wave_held,
    )
