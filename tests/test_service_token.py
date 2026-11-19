import hashlib
import io
import json
import logging
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from threading import Barrier, Event
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from updater import app as app_mod
from updater.database import get_db
from updater.poller import NetworkPoller


def create_token(client):
    response = client.post("/api/service-token")
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()["token"]


def test_service_token_is_disclosed_once_and_restricted_to_inventory(authed_client, mock_db):
    token = create_token(authed_client)
    assert re.fullmatch(r"mgr_ro_[A-Za-z0-9_-]{43}", token)
    assert authed_client.post("/api/service-token").status_code == 409
    record = json.loads(mock_db.execute("SELECT value FROM settings WHERE key='readonly_service_token'").fetchone()[0])
    assert record["sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in record.values()
    assert "readonly_service_token" not in authed_client.get("/api/settings").json()["settings"]
    assert authed_client.get("/api/service-token").json() == {
        "configured": True,
        "created_at": record["created_at"],
        "created_by": "admin",
        "last_used_at": None,
    }
    token_client = authed_client
    token_client.cookies.clear()
    headers = {"Authorization": f"Bearer {token}"}
    for route in ("/api/aps", "/api/cpes", "/api/topology"):
        assert token_client.get(route, headers=headers).status_code == 200
    for route in ("/api/sites", "/api/users/me", "/api/aps/192.0.2.1/poll"):
        method = "POST" if route.endswith("/poll") else "GET"
        assert token_client.request(method, route, headers=headers).status_code == 401
    assert app_mod.SERVICE_TOKEN_KEY not in app_mod._SETTINGS_WRITABLE


@pytest.mark.parametrize("authorization", [
    [""],
    ["Bearer"],
    ["Basic abc"],
    ["Bearer wrong_" + "A" * 43],
    ["Bearer mgr_ro_" + "A" * 42],
    ["Bearer {token}, Bearer {token}"],
    ["Bearer {token}", "Bearer {token}"],
    ["Bearer mgr_ro_" + "B" * 43],
])
def test_malformed_or_invalid_authorization_does_not_use_cookie(authed_client, authorization):
    token = create_token(authed_client)
    headers = [("Authorization", value.format(token=token)) for value in authorization]
    assert authed_client.get("/api/aps", headers=headers).status_code == 401
    assert authed_client.get("/api/aps").status_code == 200


def test_legacy_token_row_is_ignored(authed_client, mock_db):
    legacy_token = "mgr_ro_" + "B" * 43
    mock_db.executescript("""
        CREATE TABLE api_tokens (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            token_prefix TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id),
            scopes TEXT DEFAULT 'read',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_used_at TEXT,
            expires_at TEXT
        );
    """)
    mock_db.execute(
        """INSERT INTO api_tokens (name, token_hash, token_prefix, user_id)
           SELECT ?, ?, ?, id FROM users WHERE username = 'admin'""",
        ("legacy", hashlib.sha256(legacy_token.encode()).hexdigest(), legacy_token[:8]),
    )
    mock_db.commit()
    assert authed_client.get("/api/aps", headers=bearer(legacy_token)).status_code == 401


def test_mixed_credentials_and_constant_time_comparison(authed_client, monkeypatch):
    token = create_token(authed_client)
    # Other protected routes keep their cookie-only behavior.
    assert authed_client.get("/api/sites", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    calls = []
    original = app_mod.hmac.compare_digest

    def compare(left, right):
        calls.append((len(left), len(right)))
        return original(left, right)

    monkeypatch.setattr(app_mod.hmac, "compare_digest", compare)
    assert authed_client.get("/api/aps", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert calls == [(32, 32)]


def test_service_token_audit_has_no_credential_material(authed_client, mock_db):
    token = create_token(authed_client)
    response = authed_client.get("/api/aps", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    rows = mock_db.execute("SELECT username, action, target_id, details FROM audit_log WHERE action LIKE 'service_token.%' ORDER BY id").fetchall()
    assert [row[1] for row in rows] == ["service_token.create", "service_token.use"]
    for row in rows:
        assert token not in str(tuple(row))
        assert hashlib.sha256(token.encode()).hexdigest() not in str(tuple(row))


def test_malformed_record_denies_auth_and_settings_write_cannot_replace_it(authed_client, mock_db):
    mock_db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("readonly_service_token", "not-json"))
    mock_db.commit()
    assert authed_client.get("/api/aps", headers={"Authorization": "Bearer mgr_ro_" + "A" * 43}).status_code == 401
    assert authed_client.post("/api/service-token").status_code == 409


@pytest.fixture(autouse=True)
def isolated_rate_limits(monkeypatch):
    monkeypatch.setattr(app_mod, "_auth_rate_attempts", {})


@pytest.fixture
def file_db(authed_client, mock_db, tmp_path, monkeypatch):
    path = tmp_path / "manager.db"
    with monkeypatch.context() as patches:
        patches.setattr(app_mod.db, "DB_PATH", path)
        with get_db() as conn:
            mock_db.backup(conn)
        patches.setattr(app_mod.db, "get_db", get_db)
        yield path


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_service_token_management_requires_admin(client, viewer_client, method):
    assert viewer_client.request(method, "/api/service-token").status_code == 403
    client.cookies.clear()
    assert client.request(method, "/api/service-token").status_code == 401


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_service_token_writes_require_csrf(authed_client, method):
    if method == "DELETE":
        create_token(authed_client)
    response = authed_client.request(
        method, "/api/service-token", headers={"Origin": "http://other.example"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF: origin mismatch"
    status = authed_client.get("/api/service-token").json()
    assert status["configured"] is (method == "DELETE")


def token_state(conn):
    record = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (app_mod.SERVICE_TOKEN_KEY,)
    ).fetchone()
    audit = conn.execute(
        "SELECT id, action FROM audit_log WHERE action LIKE 'service_token.%' ORDER BY id"
    ).fetchall()
    return record, audit


@pytest.mark.parametrize("operation", ["create", "use", "revoke"])
def test_service_token_commit_failure_rolls_back_state_and_audit(
    authed_client, file_db, monkeypatch, operation
):
    operations = {"create": ("POST", "/api/service-token"), "use": ("GET", "/api/aps"),
                  "revoke": ("DELETE", "/api/service-token")}
    headers = bearer(create_token(authed_client)) if operation != "create" else {}
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE commit_parent (id INTEGER PRIMARY KEY);
            CREATE TABLE commit_failure (
                parent_id INTEGER REFERENCES commit_parent(id) DEFERRABLE INITIALLY DEFERRED
            );
        """)
        before = token_state(conn)

    @contextmanager
    def db_with_foreign_keys():
        with get_db() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn

    original_audit = app_mod._service_token_audit
    before_commit = []

    def audit_with_deferred_failure(conn, *args):
        original_audit(conn, *args)
        conn.execute("INSERT INTO commit_failure VALUES (1)")
        before_commit.append(token_state(conn))

    with monkeypatch.context() as patches:
        patches.setattr(app_mod.db, "get_db", db_with_foreign_keys)
        patches.setattr(app_mod, "_service_token_audit", audit_with_deferred_failure)
        method, route = operations[operation]
        response = authed_client.request(method, route, headers=headers)
    assert response.status_code == 503
    detail = "Authentication service unavailable" if operation == "use" else "Service unavailable"
    assert response.json()["detail"] == detail
    [(record, audit)] = before_commit
    assert len(audit) == len(before[1]) + 1
    assert audit[-1]["action"] == f"service_token.{operation}"
    if operation == "use":
        assert json.loads(record[0])["last_used_at"] is not None
    else:
        assert (record is None) is (operation == "revoke")
    with get_db() as conn:
        assert token_state(conn) == before


def test_concurrent_creates_publish_one_token(authed_client, file_db):
    ready = Barrier(2)

    def create():
        client = TestClient(app_mod.app, headers=authed_client.headers, cookies=authed_client.cookies)
        ready.wait(timeout=5)
        return client.post("/api/service-token").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(create)
        second = pool.submit(create)
        assert sorted([first.result(timeout=10), second.result(timeout=10)]) == [201, 409]
    with get_db() as conn:
        record, audit = token_state(conn)
    assert record is not None
    assert [row["action"] for row in audit] == ["service_token.create"]


def test_use_waiting_for_revoke_cannot_resurrect_token(authed_client, file_db, monkeypatch):
    token = create_token(authed_client)
    deleting = Event()
    using = Event()
    original_audit = app_mod._service_token_audit

    def hold_revoke(conn, username, action, route, ip):
        original_audit(conn, username, action, route, ip)
        if action == "service_token.revoke":
            deleting.set()
            assert using.wait(timeout=5)

    @contextmanager
    def observe_use_transaction():
        with get_db() as conn:
            if deleting.is_set():
                def trace(statement):
                    if statement == "BEGIN IMMEDIATE":
                        using.set()

                conn.set_trace_callback(trace)
            yield conn

    with monkeypatch.context() as patches, ThreadPoolExecutor(max_workers=2) as pool:
        patches.setattr(app_mod, "_service_token_audit", hold_revoke)
        patches.setattr(app_mod.db, "get_db", observe_use_transaction)
        token_client = TestClient(app_mod.app)
        revoke = pool.submit(authed_client.delete, "/api/service-token")
        assert deleting.wait(timeout=5)
        use = pool.submit(token_client.get, "/api/aps", headers=bearer(token))
        assert revoke.result(timeout=10).status_code == 200
        assert use.result(timeout=10).status_code == 401

    assert authed_client.get("/api/aps", headers=bearer(token)).status_code == 401
    with get_db() as conn:
        record, audit = token_state(conn)
    assert record is None
    assert [row["action"] for row in audit] == ["service_token.create", "service_token.revoke"]


def test_create_and_revoke_persist_across_app_restart(authed_client, file_db):
    token = create_token(authed_client)
    probe = """
import sys
from fastapi.testclient import TestClient
from updater.app import app

response = TestClient(app).get(
    "/api/aps", headers={"Authorization": "Bearer " + sys.stdin.read()}
)
print(response.status_code)
"""

    def status_after_restart():
        result = subprocess.run(
            [sys.executable, "-c", probe],
            env={**os.environ, "SIXTYOPS_DB_PATH": str(file_db)},
            input=token, text=True, capture_output=True, timeout=30, check=True,
        )
        return int(result.stdout.splitlines()[-1])

    assert status_after_restart() == 200
    assert authed_client.delete("/api/service-token").status_code == 200
    assert status_after_restart() == 401


@pytest.fixture
def rate_clock(monkeypatch):
    class Clock(datetime):
        offset = timedelta()

        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, tzinfo=tz) + cls.offset

    monkeypatch.setattr(app_mod, "datetime", Clock)
    return Clock


def test_service_token_attempt_limit_is_per_ip_and_separate_from_login(
    authed_client, monkeypatch, rate_clock
):
    monkeypatch.setattr(app_mod, "authenticate", lambda *_: None)
    monkeypatch.setattr(app_mod, "_login_failures", {})
    monkeypatch.setattr(app_mod, "_login_locks", {})
    monkeypatch.setattr(app_mod, "LOGIN_RATE_LIMIT", 1)
    headers = bearer("invalid")
    other_client = TestClient(app_mod.app, client=("192.0.2.2", 50000))
    for _ in range(60):
        assert authed_client.get("/api/aps", headers=headers).status_code == 401
    limited = authed_client.get("/api/aps", headers=headers)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "300"
    assert other_client.get("/api/aps", headers=headers).status_code == 401
    assert authed_client.post(
        "/login", data={"username": "service-limit-test", "password": "invalid"}
    ).status_code == 401
    login_limited = authed_client.post(
        "/login", data={"username": "service-limit-test", "password": "invalid"}
    )
    assert login_limited.status_code == 429
    rate_clock.offset = timedelta(seconds=299)
    assert authed_client.get("/api/aps", headers=headers).status_code == 429
    rate_clock.offset = timedelta(seconds=300)
    assert authed_client.get("/api/aps", headers=headers).status_code == 401


def test_service_token_credential_limit_spans_clients(authed_client, rate_clock):
    token = create_token(authed_client)
    headers = bearer(token)
    other_client = TestClient(app_mod.app, client=("192.0.2.2", 50000))
    for _ in range(15):
        assert authed_client.get("/api/aps", headers=headers).status_code == 200
    limited = other_client.get("/api/aps", headers=headers)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    rate_clock.offset = timedelta(seconds=59)
    assert other_client.get("/api/aps", headers=headers).status_code == 429
    rate_clock.offset = timedelta(seconds=60)
    assert other_client.get("/api/aps", headers=headers).status_code == 200


def test_token_topology_uses_cached_data_and_requires_poller(authed_client, monkeypatch):
    headers = bearer(create_token(authed_client))
    poller = NetworkPoller()
    forbidden = AsyncMock(side_effect=AssertionError("Unexpected topology network work"))
    for method in ("poll_ap_now", "_poll_ap", "_poll_all_aps", "_poll_switch", "_poll_all_switches"):
        monkeypatch.setattr(poller, method, forbidden)
    monkeypatch.setattr(app_mod, "refresh_topology", forbidden)
    monkeypatch.setattr(app_mod.services, "_cache_get", lambda _: None)
    monkeypatch.setattr(app_mod.services, "get_default_rain_climate", forbidden)
    cached = Mock(return_value={"zone": "cached"})
    monkeypatch.setattr(app_mod.services, "get_default_rain_climate_cached", cached)
    monkeypatch.setattr(app_mod, "get_poller", lambda: poller)

    response = authed_client.get("/api/topology", headers=headers)
    assert response.status_code == 200
    assert response.json()["climate"] == cached.return_value
    cached.assert_called_once()
    forbidden.assert_not_called()

    monkeypatch.setattr(app_mod, "get_poller", lambda: None)
    assert authed_client.get("/api/topology", headers=headers).status_code == 503
    forbidden.assert_not_called()


def test_token_redactor_covers_messages_exceptions_and_formatter_errors(capsys, monkeypatch):
    from updater import logging_filter

    monkeypatch.setattr(logging_filter, "_pattern", None)
    token = "mgr_ro_" + "A" * 43
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.Logger("service-token-redaction")
    logger.addHandler(handler)
    logging_filter.install_sanitizer(handler)

    logger.error("Presented %s", token)
    try:
        raise ValueError(token)
    except ValueError:
        logger.exception("Request failed")

    output = stream.getvalue()
    assert token not in output
    assert "Presented [REDACTED]" in output
    assert "ValueError: [REDACTED]" in output

    class BrokenFormatter(logging.Formatter):
        def format(self, record):
            raise ValueError(token)

    handler.setFormatter(BrokenFormatter())
    logging_filter.install_sanitizer(handler)
    monkeypatch.setattr(logging, "raiseExceptions", True)
    logger.error("Presented %s", token)
    diagnostic = capsys.readouterr().err
    assert token not in diagnostic
    assert "ValueError: [REDACTED]" in diagnostic
    assert "Message: Presented [REDACTED]" in diagnostic
