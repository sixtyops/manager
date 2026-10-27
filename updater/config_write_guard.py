"""Contain automatic config writes until the config rework is complete."""

CONFIG_WRITES_PAUSED_REASON = (
    "Phased config rollout and auto-enforce are paused for safety. "
    "Use manual push or restore for selected devices."
)


def automatic_config_writes_allowed() -> bool:
    """Keep automatic and phased config writes paused for safety."""
    return False
