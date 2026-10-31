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
    straggler_candidates: list[dict] | None = None,
) -> bool:
    """Atomically reserve one non-empty wave before admitting its async job.

    The reservation consumes the window even when later job admission fails.
    """
    if (
        not isinstance(rollout, dict)
        or not isinstance(rollout.get("id"), int)
        or isinstance(rollout["id"], bool)
        or rollout["id"] <= 0
        or rollout.get("phase") not in ("pct10", "pct50", "pct100")
    ):
        return False
    if not isinstance(batch, dict):
        return False
    assignments = []
    for device_type, key in (("ap", "ap_ips"), ("switch", "switch_ips")):
        ips = batch.get(key, [])
        if not isinstance(ips, list) or any(
            not isinstance(ip, str) or not ip.strip() for ip in ips
        ):
            return False
        assignments.extend((ip, device_type, rollout["phase"]) for ip in ips)
    if not assignments:
        return False
    return db.reserve_rollout_wave(
        rollout_id=rollout["id"],
        phase=rollout["phase"],
        window_key=window_key,
        assignments=assignments,
        settings=settings,
        first_wave_held=first_wave_held,
        straggler_candidates=straggler_candidates,
    )
