import hashlib
import json
import re

import pytest


def create_token(client):
    response = client.post("/api/service-token")
    assert response.status_code == 200
    return response.json()["token"]


def test_service_token_is_disclosed_once_and_restricted_to_inventory(authed_client, mock_db):
    from updater import app as app_mod

    token = create_token(authed_client)
    assert re.fullmatch(r"mgr_ro_[A-Za-z0-9_-]{43}", token)
    assert authed_client.post("/api/service-token").status_code == 409
    record = json.loads(mock_db.execute("SELECT value FROM settings WHERE key='readonly_service_token'").fetchone()[0])
    assert record["sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in record.values()
    assert "readonly_service_token" not in authed_client.get("/api/settings").json()["settings"]
    token_client = authed_client
    token_client.cookies.clear()
    headers = {"Authorization": f"Bearer {token}"}
    for route in ("/api/aps", "/api/cpes", "/api/topology"):
        assert token_client.get(route, headers=headers).status_code == 200
    for route in ("/api/sites", "/api/users/me", "/api/aps/192.0.2.1/poll"):
        method = "POST" if route.endswith("/poll") else "GET"
        assert token_client.request(method, route, headers=headers).status_code == 401
    assert app_mod._service_token_record


def test_invalid_and_legacy_bearer_do_not_use_session_fallback(authed_client, mock_db):
    from updater import app as app_mod

    assert authed_client.get("/api/aps", headers={"Authorization": "Bearer legacy"}).status_code == 401
    assert authed_client.get("/api/aps", headers={"Authorization": "Basic abc"}).status_code == 401
    assert authed_client.get("/api/aps").status_code == 200
    assert app_mod.SERVICE_TOKEN_KEY not in app_mod._SETTINGS_WRITABLE


def test_mixed_credentials_and_constant_time_comparison(authed_client, monkeypatch):
    import updater.app as app_mod

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


def test_service_token_admin_management_and_revoke(authed_client, mock_db):
    from datetime import datetime, timedelta

    mock_db.execute("INSERT OR IGNORE INTO users (username, role, auth_method) VALUES (?, ?, ?)",
                    ("viewer-token-test", "viewer", "local"))
    mock_db.execute("INSERT INTO sessions (session_id, username, ip_address, expires_at) VALUES (?, ?, ?, ?)",
                    ("viewer-token-test-session", "viewer-token-test", "127.0.0.1",
                     (datetime.now() + timedelta(hours=1)).isoformat()))
    mock_db.commit()
    token = create_token(authed_client)
    assert authed_client.get("/api/service-token").json()["configured"] is True
    assert token not in authed_client.get("/api/service-token").text
    authed_client.cookies.set("session_id", "viewer-token-test-session")
    assert authed_client.get("/api/service-token").status_code == 403
    authed_client.cookies.set("session_id", "test-session-id-1234")
    assert authed_client.delete("/api/service-token").status_code == 200
    assert authed_client.get("/api/aps", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_service_token_rate_limit_and_topology_fails_closed(authed_client, monkeypatch):
    from updater import app as app_mod

    token = create_token(authed_client)
    client = authed_client
    client.cookies.clear()
    for _ in range(15):
        assert client.get("/api/aps", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    limited = client.get("/api/aps", headers={"Authorization": f"Bearer {token}"})
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
    monkeypatch.setattr(app_mod, "get_poller", lambda: None)
    app_mod._auth_rate_attempts.clear()
    assert client.get("/api/topology", headers={"Authorization": f"Bearer {token}"}).status_code == 503


def test_token_redactor_covers_messages_exceptions_and_formatter_errors():
    import logging
    from updater.logging_filter import _sanitize

    token = "mgr_ro_" + "A" * 43
    assert token not in _sanitize(f"message {token}")
    assert token not in _sanitize(f"ValueError: {token}")
    assert token not in _sanitize(f"Message: {token}\nLogging error")
