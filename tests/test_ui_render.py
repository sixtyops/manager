"""Check that the Manager pages render with seeded data and key elements."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Panel and control ids that the UI scripts look up by id.
MONITOR_IDS = (
    "modeTabFw",
    "modeTabCfg",
    "deviceFilterSummary",
    "drawerPanel",
    "settingsPanel-system",
    "settingsPanel-notifications",
    "settingsPanel-access",
    "settingsPanel-radius",
    "settingsPanel-about",
)

STATIC_REF = re.compile(r"""<(?:script|link)\b[^>]*\b(?:src|href)=["'](/static/[^"'?#]+)""")
TEMPLATE_ERROR_MARKERS = ("Traceback", "UndefinedError", "TemplateSyntaxError", "{{", "{%")


def _seed(db):
    db.execute("INSERT INTO tower_sites (id, name) VALUES (1, 'Tower-Alpha')")
    db.execute(
        "INSERT INTO access_points (ip, tower_site_id, username, password, model, firmware_version, enabled) "
        "VALUES ('10.0.0.1', 1, 'admin', 'x', 'TNA-301', '1.0.0', 1)"
    )
    db.commit()


def test_monitor_page_renders_with_seeded_data(authed_client, mock_db):
    _seed(mock_db)
    response = authed_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    for marker in TEMPLATE_ERROR_MARKERS[:3]:
        assert marker not in html
    for element_id in MONITOR_IDS:
        assert f'id="{element_id}"' in html, f"missing element id {element_id}"


def test_monitor_static_references_resolve(authed_client):
    html = authed_client.get("/").text
    refs = set(STATIC_REF.findall(html))
    assert refs, "monitor page lists no static assets"
    for ref in sorted(refs):
        response = authed_client.get(ref)
        assert response.status_code == 200, f"{ref} returned {response.status_code}"


def test_login_page_renders(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert 'name="username"' in response.text
    assert 'name="password"' in response.text
    for marker in TEMPLATE_ERROR_MARKERS[:3]:
        assert marker not in response.text


def test_unauthenticated_monitor_page_does_not_render(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 303, 307, 401)
