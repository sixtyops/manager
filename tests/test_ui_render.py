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


def test_device_table_keeps_values_on_one_line_and_scrolls_horizontally():
    html = (ROOT / "updater/templates/monitor.html").read_text(encoding="utf-8")
    assert '<div class="u-overflow-x-auto" id="deviceTableScroll">' in html
    assert "#deviceTable th,\n        #deviceTable td {\n            white-space: nowrap;" in html
    assert "#deviceTableScroll {\n            min-width: 0;\n            overflow-x: auto;" in html
    assert re.search(r"\.tree-table\s*\{\s*width: 100%;", html)
    assert re.search(r"#deviceTable th:first-child\s*\{\s*width: 100%;", html)
    assert re.search(r"\.device-name-group\s*\{[^}]*contain: inline-size;", html)


@pytest.mark.parametrize("name_call", [
    "deviceNameHtml(ap.ip, apName)",
    "deviceNameHtml(sw.ip, swName)",
    "deviceNameHtml('', site.name)",
    "deviceNameHtml(cpeIp, cpeName)",
])
def test_device_rows_put_names_in_shrinking_group(name_call):
    html = (ROOT / "updater/templates/monitor.html").read_text(encoding="utf-8")
    assert re.search(
        r'<div class="device-name-group">\s*\$\{' + re.escape(name_call) + r"\}",
        html,
    )


def test_device_name_shows_full_text_on_keyboard_focus():
    html = (ROOT / "updater/templates/monitor.html").read_text(encoding="utf-8")
    assert re.search(r"\.device-name\s*\{[^}]*text-overflow: ellipsis;", html)
    focus = re.search(r"\.device-name:focus\s*\{([^}]*)\}", html).group(1)
    assert "overflow: visible;" in focus
    assert "white-space: normal;" in focus
    assert "overflow-wrap: anywhere;" in focus
    assert '<span class="device-name" tabindex="0">${safe}</span>' in html
    assert '<span class="name-static device-name" tabindex="0" data-ip="${ip}">${safe}</span>' in html
    assert '<a href="/api/device-portal/${ip}" target="_blank" class="name-link device-name"' in html


@pytest.mark.parametrize(("width", "class_name"), [(80, "device-notes"), (75, "device-date")])
def test_device_table_hides_secondary_values_at_narrow_widths(width, class_name):
    html = (ROOT / "updater/templates/monitor.html").read_text(encoding="utf-8")
    assert re.search(
        rf"@media \(max-width: {width}rem\)\s*\{{\s*"
        rf"#deviceTable \.{class_name}\s*\{{\s*display: none;",
        html,
    )


def test_monitor_weather_and_bank_labels_match_behavior(authed_client):
    html = authed_client.get("/").text
    assert "sampled ${sampleTime}" in html
    assert "currently ${tempDisplay}" not in html
    assert 'id="drawerParallel"' not in html
    assert "parallel_updates:" not in html
    assert "Manual Update Bank Mode" in html
    assert "Scheduled updates use one bank." in html
    assert "Updates run one device at a time." in html


def test_monitor_page_does_not_render_pre_update_reboot_switch(authed_client):
    html = authed_client.get("/").text
    assert "Pre-update Reboot" not in html
    assert "drawerPreRebootToggle" not in html
    assert "toggleDrawerPreReboot" not in html
    assert "Each device reboots before its firmware update." in html


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


def test_updates_bar_has_one_save_cluster_and_full_notice(authed_client):
    html = authed_client.get('/').text
    assert html.count('id="drawerSaveBtn"') == 1
    assert 'class="bar-save-cluster hidden"' in html
    assert 'aria-describedby="barNotice"' in html
    assert 'id="barNotice" role="status"' in html
    assert 'onclick="openFirmwareStopResolution()">Resolve stop</button>' in html
    for removed in ('drawerSaveRow', 'drawerUnsavedDot', 'barBlockReason', 'barDeviceCountSection', 'barBehindCount'):
        assert f'id="{removed}"' not in html
    for element in ('barQueueRemaining', 'barQueueNextWindow'):
        assert html.count(f'id="{element}"') == 1
    css = re.search(r'\.status-bar-notice \{(.*?)\}', html, re.S)[1]
    assert 'white-space: normal' in css
    assert 'text-overflow' not in css
    assert 'max-width' not in css
