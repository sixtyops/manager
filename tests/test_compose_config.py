"""Tests for docker-compose.yml shape.

These guard the env-overridable port publishes so a deployment that needs a
specific bind IP / host port (e.g. a multi-tenant host where 8000 is taken)
can drive it via env vars instead of editing the file in place — which would
leave the working tree dirty and break the in-app self-update flow.
"""

import re
from pathlib import Path

import pytest
import yaml


COMPOSE_PATH = Path(__file__).resolve().parent.parent / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose_doc():
    with COMPOSE_PATH.open() as f:
        return yaml.safe_load(f)


def test_compose_yaml_parses(compose_doc):
    assert "services" in compose_doc
    assert "sixtyops-mgmt" in compose_doc["services"]


def test_publish_ports_use_env_substitution(compose_doc):
    """Each `ports:` entry must use ${VAR:-default} syntax so deployments
    can override the host bind IP / port without editing the file in
    place. Specifically guard the bind-IP knob (BIND_IP for the app port,
    RADIUS_BIND_IP for RADIUS) — that one is load-bearing for multi-tenant
    hosts (a refactor to ${PORT:-8000}:8000 that drops the bind-IP would
    still pass a generic ${ check)."""
    ports = compose_doc["services"]["sixtyops-mgmt"]["ports"]
    assert ports, "sixtyops-mgmt must declare published ports"
    for entry in ports:
        assert isinstance(entry, str), (
            f"Expected string-form ports for env-substitution support, got {entry!r}"
        )
        assert "${" in entry, (
            f"Port entry {entry!r} hardcodes its host bind. Use "
            "${BIND_IP:-...}:${HOST_PORT:-...}:<container_port> instead."
        )
        assert "${BIND_IP" in entry or "${RADIUS_BIND_IP" in entry, (
            f"Port entry {entry!r} dropped the bind-IP knob — operators on "
            "multi-tenant hosts need this to bind a specific IP."
        )


def test_default_publishes_are_secure(compose_doc):
    """With no env vars set, the app port (8000/tcp) must bind to loopback
    only — nginx (same compose stack, docker bridge) is the one that needs
    to reach it. RADIUS (1812/udp) still defaults to 0.0.0.0 because APs
    live on the LAN and have to hit it directly. Operators who terminate
    TLS elsewhere can set BIND_IP=0.0.0.0 (or a specific NIC)."""
    ports = compose_doc["services"]["sixtyops-mgmt"]["ports"]
    rendered = [_render_defaults(p) for p in ports]

    assert "127.0.0.1:8000:8000" in rendered, (
        f"Default TCP publish must be 127.0.0.1:8000:8000 (loopback); rendered={rendered}"
    )
    assert any(p.startswith("0.0.0.0:1812:1812") and p.endswith("/udp") for p in rendered), (
        f"Default UDP publish must be 0.0.0.0:1812:1812/udp; rendered={rendered}"
    )


def test_app_container_caps_drop_all_with_entrypoint_minimum(compose_doc):
    """cap_drop: [ALL] is the desired baseline. The entrypoint still runs
    briefly as root before gosu-dropping to appuser — it chowns the bind-
    mounted repo for self-update, manages the docker-socket group, and
    switches user. Those operations require a small set of capabilities;
    everything else (NET_RAW, NET_ADMIN, SYS_PTRACE, MKNOD, …) must stay
    dropped.
    """
    svc = compose_doc["services"]["sixtyops-mgmt"]
    assert svc.get("cap_drop") == ["ALL"], (
        f"cap_drop must be [ALL]; got {svc.get('cap_drop')!r}"
    )
    cap_add = set(svc.get("cap_add") or [])
    # If any of these go missing, the entrypoint silently fails inside the
    # 500ms-from-start crash window — see install-smoke run 78532580647.
    required = {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"}
    missing = required - cap_add
    assert not missing, (
        f"cap_add is missing entrypoint-required capabilities: {sorted(missing)}"
    )
    # Guard against re-adding the dangerous ones the audit asked us to drop.
    forbidden = {"NET_RAW", "NET_ADMIN", "SYS_ADMIN", "SYS_PTRACE", "MKNOD", "ALL"}
    over_granted = forbidden & cap_add
    assert not over_granted, (
        f"cap_add contains capabilities that should stay dropped: {sorted(over_granted)}"
    )


def test_app_container_no_new_privileges(compose_doc):
    """no-new-privileges prevents setuid-bit escalation inside the container.
    gosu (used by the entrypoint) uses syscalls, not the setuid bit, so it
    is not affected. This must stay on for both services."""
    for svc_name in ("sixtyops-mgmt", "nginx"):
        sec_opt = compose_doc["services"][svc_name].get("security_opt") or []
        assert "no-new-privileges:true" in sec_opt, (
            f"{svc_name} must set security_opt: no-new-privileges:true; got {sec_opt!r}"
        )


def test_app_container_port_unchanged(compose_doc):
    """The container-side port (right of the final colon) must stay 8000 for
    TCP and 1812/udp — those are baked into the app, healthcheck, and
    nginx upstream config and aren't environment-driven."""
    ports = compose_doc["services"]["sixtyops-mgmt"]["ports"]
    rendered = [_render_defaults(p) for p in ports]

    tcp_targets = [p.rsplit(":", 1)[1] for p in rendered if "/udp" not in p]
    assert tcp_targets == ["8000"], f"Unexpected TCP container ports: {tcp_targets}"

    udp_targets = [p.rsplit(":", 1)[1] for p in rendered if p.endswith("/udp")]
    assert udp_targets == ["1812/udp"], f"Unexpected UDP container ports: {udp_targets}"


def _render_defaults(entry: str) -> str:
    """Resolve ${VAR:-default} placeholders to their default value.

    Mirrors the subset of POSIX `${VAR:-default}` parameter expansion that
    Compose supports, so we can assert the as-shipped behavior without
    needing the docker CLI on the test runner.
    """
    return re.sub(r"\$\{[^}:]+:-([^}]*)\}", r"\1", entry)
