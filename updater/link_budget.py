"""Link-budget math for 60 GHz Tachyon radios.

Reverse-engineered from Tachyon's public link-budget calculator
(`https://tachyon-networks.com/calc.html`). The full calculator computes path
loss from scratch given model + antenna kit + channel + distance; we don't
need that because the AP already reports the live `target_rssi`. We only need
to derive how much *rain* the current link can tolerate before signal drops
below the MCS1 floor — that's the headline metric.

Formula (ITU-R P.838):
    fade_margin_dB  = current_rssi - MCS1_floor
    A_rain (dB/km)  = k · R^a
    max_rain (mm/hr) solved from  fade_margin = A_rain · distance_km
                                = k · R^a · (distance_m / 1000)
                  => R = (fade_margin / (k · distance_km)) ^ (1/a)
"""

from typing import Optional

# ---- Bandwidth bucketing ----------------------------------------------------

# Tachyon TNA-300 series 802.11ad channels are 2.16 GHz wide ("full BW") or
# 1.08 GHz wide ("half BW"). Treat anything ≥ 1.5 GHz as full.
_FULL_BW_THRESHOLD_MHZ = 1500


def bandwidth_bucket(channel_width_mhz: Optional[int]) -> Optional[str]:
    """Map a reported `channelWidth` to "full" or "half"."""
    if channel_width_mhz is None or channel_width_mhz <= 0:
        return None
    return "full" if channel_width_mhz >= _FULL_BW_THRESHOLD_MHZ else "half"


# ---- O2 absorption ----------------------------------------------------------
# dB/m, indexed [bucket][channel]. Channel index is 1-based to match Tachyon.
O2_LOSS_DB_PER_M: dict[str, dict[int, float]] = {
    "full": {1: 0.013, 2: 0.015, 3: 0.014, 4: 0.009, 5: 0.002, 6: 0.002},
    "half": {
        1: 0.013, 2: 0.014, 3: 0.015, 4: 0.015, 5: 0.014, 6: 0.011,
        7: 0.009, 8: 0.005, 9: 0.002, 10: 0.002, 11: 0.002, 12: 0.002,
    },
}


# ---- RX sensitivity (MCS1 floor) -------------------------------------------
# dBm at the link-drop floor. We always size against MCS1: "keep the link up"
# beats "preserve current modulation" per uptime-first policy.
RX_SENS_DBM: dict[str, dict[str, float]] = {
    "full": {"MCS1": -70, "MCS4": -68, "MCS8": -64, "MCS10": -58},
    "half": {"MCS1": -76, "MCS4": -73, "MCS8": -69, "MCS10": -57},
}


def mcs1_floor_dbm(bucket: str) -> Optional[float]:
    """Return the MCS1 RX sensitivity for the bandwidth bucket."""
    return RX_SENS_DBM.get(bucket, {}).get("MCS1")


# ---- Rain attenuation (ITU-R P.838) ----------------------------------------
# k, a coefficients per Tachyon channel (vertical polarization, 60 GHz band).
RAIN_ATT_K: dict[int, float] = {
    1: 0.8129, 2: 0.8515, 3: 0.9071, 4: 0.9425, 5: 0.9767, 6: 1.0094,
}
RAIN_ATT_A: dict[int, float] = {
    1: 0.7552, 2: 0.7486, 3: 0.7395, 4: 0.7339, 5: 0.7287, 6: 0.7238,
}


def _coeffs_for_channel(channel: int) -> tuple[Optional[float], Optional[float]]:
    """Return (k, a) coefficients for a channel. Half-BW channels >6 reuse the
    closest full-BW coefficients (the spectrum overlap is similar enough at
    this granularity for a survival estimate)."""
    if channel in RAIN_ATT_K:
        return RAIN_ATT_K[channel], RAIN_ATT_A[channel]
    # Half-BW channels 7-12 sit between the full-BW channels — clamp to the
    # nearest channel we have coefficients for.
    if channel >= 7:
        return RAIN_ATT_K[6], RAIN_ATT_A[6]
    return None, None


# ---- Survivability ---------------------------------------------------------


def max_survivable_rain_mm_hr(
    *,
    rssi_dbm: Optional[float],
    distance_m: Optional[float],
    channel: Optional[int],
    channel_width_mhz: Optional[int],
) -> Optional[float]:
    """How heavy a rain (mm/hr) the link can survive before signal drops below
    the MCS1 floor.

    Returns None when any input is missing or the link is already below floor.
    """
    if rssi_dbm is None or distance_m is None or channel is None:
        return None
    if distance_m <= 0:
        return None
    bucket = bandwidth_bucket(channel_width_mhz)
    if bucket is None:
        return None
    floor = mcs1_floor_dbm(bucket)
    if floor is None:
        return None
    fade_margin = rssi_dbm - floor
    if fade_margin <= 0:
        return 0.0
    k, a = _coeffs_for_channel(channel)
    if k is None or a is None:
        return None
    distance_km = distance_m / 1000.0
    # Solve fade_margin = k * R^a * distance_km for R
    return (fade_margin / (k * distance_km)) ** (1.0 / a)
