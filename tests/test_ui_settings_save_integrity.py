from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_settings_save_preserves_scope_and_zero_temperature():
    source = (ROOT / "updater/templates/monitor.html").read_text()
    drawer = source.split("async function saveDrawerSettings()", 1)[1].split("// ── Drawer Firmware", 1)[0]
    assert "schedule_scope: 'all'" not in drawer
    assert "Number.isFinite(parseFloat" in drawer


def test_cooldown_is_writable_and_validated():
    source = (ROOT / "updater/app.py").read_text()
    assert '"firmware_update_cooldown_days"' in source
    assert "firmware_update_cooldown_days must be between 0 and 365" in source


def test_webhook_secret_is_not_loaded_or_sent_when_unchanged():
    source = (ROOT / "updater/templates/monitor.html").read_text()
    assert "document.getElementById('webhookSecret').value = '';" in source
    assert "if (secret) settings.webhook_secret = secret;" in source


def test_settings_saves_check_http_status_and_stop_tests_on_failure():
    source = (ROOT / "updater/templates/monitor.html").read_text()
    assert source.count("if (!resp.ok) throw new Error(await readSaveError(resp));") >= 3
    assert "if (!await saveWebhookSettings()) return;" in source
    assert "return false;" in source
