"""Pydantic models for Tachyon Management System."""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class DeviceType(str, Enum):
    """Type of device in the network."""
    AP = "ap"
    SM = "sm"  # Subscriber Module (CPE)


class SignalHealth(str, Enum):
    """Signal health classification based on dBm thresholds."""
    GREEN = "green"   # > -65 dBm (strong)
    YELLOW = "yellow"  # -65 to -75 dBm (acceptable)
    RED = "red"       # < -75 dBm (weak)

    @classmethod
    def from_signal(cls, signal_dbm: Optional[float]) -> "SignalHealth":
        """Determine health classification from signal strength."""
        if signal_dbm is None:
            return cls.RED
        if signal_dbm > -65:
            return cls.GREEN
        if signal_dbm >= -75:
            return cls.YELLOW
        return cls.RED


class Device(BaseModel):
    """Base device information."""
    ip: str
    mac: Optional[str] = None
    system_name: Optional[str] = None
    model: Optional[str] = None
    firmware_version: Optional[str] = None
    device_type: DeviceType = DeviceType.SM


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

    @property
    def signal_health(self) -> SignalHealth:
        """Get signal health based on combined signal or rx_power."""
        signal = self.combined_signal or self.rx_power
        return SignalHealth.from_signal(signal)

    @property
    def primary_signal(self) -> Optional[float]:
        """Get the primary signal value for display."""
        return self.combined_signal or self.rx_power or self.last_local_rssi

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
