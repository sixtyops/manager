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
