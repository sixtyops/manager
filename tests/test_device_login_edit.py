"""Edit login: save a new switch or AP login, then poll with it."""

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from updater import database as db
from updater.poller import NetworkPoller

TEMPLATE = Path(__file__).resolve().parents[1] / "updater/templates/monitor.html"
SWITCH = "192.0.2.40"
AP = "192.0.2.41"


def recording_driver(logins, connect_result=True):
    """Return a driver factory that records the login each client uses."""
    def make(ip, username, password):
        logins.append((ip, username, password))
        return SimpleNamespace(
            connect=AsyncMock(return_value=connect_result),
            get_ap_info=AsyncMock(return_value={"model": "TNS-100", "firmware_version": "1.0"}),
            get_device_info=AsyncMock(return_value=SimpleNamespace(
                bank1_version=None, bank2_version=None, active_bank=None,
            )),
            get_bootbank_info=AsyncMock(return_value=SimpleNamespace(
                bank1_version=None, bank2_version=None, active_bank=None, warning=None,
            )),
            get_bridge_table=AsyncMock(return_value=[]),
            get_connected_cpes=AsyncMock(return_value=[]),
            last_radio_params=None,
        )
    return MagicMock(side_effect=make)


class TestSwitchLoginEdit:
    def test_save_clears_cached_client_and_next_poll_uses_new_login(self, authed_client):
        db.upsert_switch(SWITCH, "old-user", "old-pass")
        poller = NetworkPoller()
        poller._cache_client(SWITCH, MagicMock())
        logins = []

        with patch("updater.app.get_poller", return_value=poller), \
             patch("updater.poller.get_driver", return_value=recording_driver(logins)):
            saved = authed_client.put(f"/api/switches/{SWITCH}",
                                      data={"username": "new-user", "password": "new-pass"})
            assert saved.status_code == 200
            assert SWITCH not in poller._clients
            polled = authed_client.post(f"/api/switches/{SWITCH}/poll")

        assert polled.status_code == 200
        assert logins == [(SWITCH, "new-user", "new-pass")]
        stored = db.get_switch(SWITCH)
        assert (stored["username"], stored["password"]) == ("new-user", "new-pass")
        assert stored["last_error"] is None
        assert "new-pass" not in saved.text + polled.text
        audit = db.get_audit_log(action="device.login_update")
        assert [(a["target_type"], a["target_id"]) for a in audit] == [("switch", SWITCH)]

    def test_poll_reports_rejected_login(self, authed_client):
        db.upsert_switch(SWITCH, "old-user", "old-pass")
        poller = NetworkPoller()
        logins = []

        with patch("updater.app.get_poller", return_value=poller), \
             patch("updater.poller.get_driver",
                   return_value=recording_driver(logins, "Invalid credentials")):
            saved = authed_client.put(f"/api/switches/{SWITCH}",
                                      data={"username": "new-user", "password": "wrong-pass"})
            polled = authed_client.post(f"/api/switches/{SWITCH}/poll")

        assert saved.status_code == 200
        assert polled.status_code == 502
        assert polled.json()["detail"] == "Invalid credentials"
        assert logins == [(SWITCH, "new-user", "wrong-pass")]

    def test_notes_save_does_not_audit_login(self, authed_client):
        db.upsert_switch(SWITCH, "old-user", "old-pass")

        assert authed_client.put(f"/api/switches/{SWITCH}", data={"notes": "rack 2"}).status_code == 200
        assert db.get_audit_log(action="device.login_update") == []


class TestAPLoginEdit:
    def test_save_clears_cached_client_and_next_poll_uses_new_login(self, authed_client):
        db.upsert_access_point(AP, "old-user", "old-pass")
        poller = NetworkPoller()
        poller._cache_client(AP, MagicMock())
        logins = []
        probe = MagicMock(login=AsyncMock(return_value=True),
                          get_ap_info=AsyncMock(return_value={"model": "TNA-301"}))

        with patch("updater.app.get_poller", return_value=poller), \
             patch("updater.app.TachyonClient", return_value=probe), \
             patch("updater.poller.get_driver", return_value=recording_driver(logins)):
            saved = authed_client.put(f"/api/aps/{AP}",
                                      data={"username": "new-user", "password": "new-pass"})
            polled = authed_client.post(f"/api/aps/{AP}/poll")

        assert saved.status_code == 200
        assert polled.status_code == 200
        # The save polls with a fresh client; the next poll reuses that client.
        assert logins == [(AP, "new-user", "new-pass")]
        stored = db.get_access_point(AP)
        assert (stored["username"], stored["password"]) == ("new-user", "new-pass")
        assert "new-pass" not in saved.text + polled.text
        audit = db.get_audit_log(action="device.login_update")
        assert [(a["target_type"], a["target_id"]) for a in audit] == [("ap", AP)]


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = {}
        self.labels = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.elements[attrs["id"]] = (tag, attrs)
        if tag == "label" and "for" in attrs:
            self.labels[attrs["for"]] = True


def test_edit_login_dialog_wiring():
    source = TEMPLATE.read_text()
    parser = Markup()
    parser.feed(source)
    elements = parser.elements
    dialog = elements["deviceLoginDialog"][1]
    assert dialog["role"] == "dialog"
    assert dialog["aria-modal"] == "true"
    assert dialog["aria-labelledby"] in elements
    assert elements["deviceLoginStatus"][1]["aria-live"] == "polite"
    assert elements["deviceLoginPassword"][1]["type"] == "password"
    assert elements["deviceLoginPassword"][1]["autocomplete"] == "new-password"
    assert parser.labels.get("deviceLoginUsername") and parser.labels.get("deviceLoginPassword")
    assert "${editLoginActionButton('ap', id)}" in source
    assert "${editLoginActionButton('switch', id)}" in source
    assert "if (currentUserRole === 'viewer') return '';\n            return `<button type=\"button\" class=\"action-link\" data-edit-login" in source
