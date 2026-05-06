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
    can override BIND_IP / HOST_PORT / RADIUS_HOST_PORT without editing the
    file in place."""
    ports = compose_doc["services"]["sixtyops-mgmt"]["ports"]
    assert ports, "sixtyops-mgmt must declare published ports"
    for entry in ports:
        assert isinstance(entry, str), (
            f"Expected string-form ports for env-substitution support, got {entry!r}"
        )
        assert "${" in entry, (
            f"Port entry {entry!r} hardcodes its host bind. Use "
            "${BIND_IP:-0.0.0.0}:${HOST_PORT:-...}:<container_port> instead."
        )


def test_default_publishes_match_historical_behavior(compose_doc):
    """With no env vars set, publishes must still bind 0.0.0.0:8000 (TCP)
    and 0.0.0.0:1812/udp — the same shape as before this change, so existing
    operators with no custom env see no behavior change."""
    ports = compose_doc["services"]["sixtyops-mgmt"]["ports"]
    rendered = [_render_defaults(p) for p in ports]

    assert "0.0.0.0:8000:8000" in rendered, (
        f"Default TCP publish must be 0.0.0.0:8000:8000; rendered={rendered}"
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
