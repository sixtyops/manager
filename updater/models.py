"""Pydantic models for SixtyOps."""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class DeviceType(str, Enum):
    """Type of device in the network."""
    AP = "ap"
    SM = "sm"  # Subscriber Module (CPE)
    SWITCH = "switch"  # TNS-100 series


class SignalHealth(str, Enum):
    """Signal health classification based on dBm thresholds.

    Frontend bucket labels: Strong / Low / Marginal. Boundaries are kept
    consistent across the API model, the legend on the Signal vs Distance
    chart, the chart's reference dotted lines, and the per-row signal
    coloring helper in monitor.html.
    """
    GREEN = "green"   # >= -60 dBm (strong)
    YELLOW = "yellow"  # -65 to -61 dBm (low)
    RED = "red"       # < -65 dBm (marginal)

    @classmethod
    def from_signal(cls, signal_dbm: Optional[float]) -> "SignalHealth":
        """Determine health classification from signal strength."""
        if signal_dbm is None:
            return cls.RED
        if signal_dbm >= -60:
            return cls.GREEN
        if signal_dbm >= -65:
            return cls.YELLOW
        return cls.RED



class CPEInfo(BaseModel):
    """CPE (SM) with signal and distance data."""
    ip: str
    mac: Optional[str] = None
    system_name: Optional[str] = None
    model: Optional[str] = None
    firmware_version: Optional[str] = None

    # Distance metrics
    link_distance: Optional[float] = Field(None, description="Distance in meters")

    # Signal metrics (dBm)
    rx_power: Optional[float] = Field(None, description="Receive power in dBm")
    combined_signal: Optional[float] = Field(None, description="Combined signal in dBm")
    last_local_rssi: Optional[float] = Field(None, description="Last local RSSI in dBm")

    # Link performance
    tx_rate: Optional[float] = Field(None, description="Transmit rate in Mbps")
    rx_rate: Optional[float] = Field(None, description="Receive rate in Mbps")
    mcs: Optional[int] = Field(None, description="MCS index")

    # Connection info
    link_uptime: Optional[int] = Field(None, description="Link uptime in seconds")

    # Link-budget telemetry the AP already reports (used by the dynamic
    # signal-health UI; all optional so legacy rows + tests keep working).
    target_rssi_dbm: Optional[float] = Field(None, description="AP's expected RSSI in dBm")
    snr_db: Optional[float] = Field(None, description="Last-data RX SNR in dB")
    sector_tx: Optional[int] = Field(None, description="TX beam sector index")
    sector_rx: Optional[int] = Field(None, description="RX beam sector index")
    antenna_kit: Optional[str] = Field(None, description="CPE-side antenna kit (e.g. 'AK-150', 'none')")

    # Derived in the poller from rssi + distance + channel + bandwidth; cached
    # on the row so the frontend tooltip doesn't need to recompute.
    max_rain_mm_hr: Optional[float] = Field(None, description="Max rain rate this link survives (mm/hr)")

    @property
    def signal_health(self) -> SignalHealth:
        """Bucket this link's health.

        When `max_rain_mm_hr` is available we use it against the auto-detected
        climate's rain rates (GREEN survives 99.99 %, YELLOW survives 99.9 %,
        RED neither). Otherwise we fall back to the legacy bare-dBm classifier
        for backwards compatibility with pre-migration rows and bare-CPEInfo
        constructions in tests.
        """
        if self.max_rain_mm_hr is not None:
            # Lazy import to avoid a circular dependency at module load.
            from . import services
            rates = (services.get_default_rain_climate_cached() or {}).get("rates_mm_hr") or {}
            r9999 = rates.get("0.01%")
            r999 = rates.get("0.1%")
            if r9999 is not None and self.max_rain_mm_hr >= r9999:
                return SignalHealth.GREEN
            if r999 is not None and self.max_rain_mm_hr >= r999:
                return SignalHealth.YELLOW
            return SignalHealth.RED
        signal = self.rx_power or self.combined_signal
        return SignalHealth.from_signal(signal)

    @property
    def primary_signal(self) -> Optional[float]:
        """Get the primary signal value for display.

        Prefer `rx_power` (Tachyon `rxPower`): it is the value the device's own
        Alignment and AP Reporting pages headline, so matching it keeps our
        numbers consistent with what the operator sees on the device.
        `combinedSignal` can read several dB low on some links, so it is only a
        fallback.
        """
        return self.rx_power or self.combined_signal or self.last_local_rssi

    def to_dict(self) -> dict:
        """Convert to dictionary with computed properties."""
        data = self.model_dump()
        data["signal_health"] = self.signal_health.value
        data["primary_signal"] = self.primary_signal
        return data


class APWithCPEs(BaseModel):
    """Access Point with its connected CPEs."""
    ip: str
    mac: Optional[str] = None
    system_name: Optional[str] = None
    model: Optional[str] = None
    firmware_version: Optional[str] = None
    device_type: DeviceType = DeviceType.AP
    cpes: list[CPEInfo] = Field(default_factory=list)
    error: Optional[str] = None  # If discovery failed

    # Location data
    location: Optional[str] = Field(None, description="Location/site name")
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    zone: Optional[str] = Field(None, description="Zone or region")

    @property
    def cpe_count(self) -> int:
        """Number of connected CPEs."""
        return len(self.cpes)

    @property
    def health_summary(self) -> dict[str, int]:
        """Count of CPEs by signal health."""
        summary = {"green": 0, "yellow": 0, "red": 0}
        for cpe in self.cpes:
            summary[cpe.signal_health.value] += 1
        return summary

    def to_dict(self) -> dict:
        """Convert to dictionary with computed properties."""
        data = self.model_dump()
        data["device_type"] = self.device_type.value
        data["cpe_count"] = self.cpe_count
        data["health_summary"] = self.health_summary
        data["cpes"] = [cpe.to_dict() for cpe in self.cpes]
        return data


class NetworkTopology(BaseModel):
    """Full network topology tree."""
    aps: list[APWithCPEs] = Field(default_factory=list)
    discovered_at: Optional[str] = None

    @property
    def total_aps(self) -> int:
        """Total number of APs."""
        return len(self.aps)

    @property
    def total_cpes(self) -> int:
        """Total number of CPEs across all APs."""
        return sum(ap.cpe_count for ap in self.aps)

    @property
    def overall_health(self) -> dict[str, int]:
        """Aggregate health summary across all CPEs."""
        summary = {"green": 0, "yellow": 0, "red": 0}
        for ap in self.aps:
            ap_health = ap.health_summary
            for key in summary:
                summary[key] += ap_health[key]
        return summary

    def to_dict(self) -> dict:
        """Convert to dictionary with computed properties."""
        return {
            "aps": [ap.to_dict() for ap in self.aps],
            "discovered_at": self.discovered_at,
            "total_aps": self.total_aps,
            "total_cpes": self.total_cpes,
            "overall_health": self.overall_health,
        }
