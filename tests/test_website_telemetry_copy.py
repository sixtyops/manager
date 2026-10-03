"""Prove website enablement copy against telemetry's actual local gate, without sending."""

import importlib.util
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock

import pytest

from updater import database

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('setting,disabled,expected', [
    (None, None, False), ('false', None, False), ('true', None, True),
    ('TRUE', '', True), ('true', '0', True), ('false', '0', False),
    (None, '1', False), ('false', '1', False), ('true', '1', False),
    ('true', 'true', False), ('true', 'TRUE', False), ('true', 'yes', False),
])
def test_actual_telemetry_gate_without_send(monkeypatch, setting, disabled, expected):
    if disabled is None:
        monkeypatch.delenv('DISABLE_TELEMETRY', raising=False)
    else:
        monkeypatch.setenv('DISABLE_TELEMETRY', disabled)
    getter = Mock(side_effect=lambda key, default: default if setting is None else setting)
    monkeypatch.setattr(database, 'get_setting', getter)
    session = Mock(side_effect=AssertionError('No telemetry transport is allowed'))
    monkeypatch.setattr('aiohttp.ClientSession', session)
    spec = importlib.util.spec_from_file_location('updater.synthetic_telemetry_gate', ROOT / 'updater/telemetry.py')
    # Keep the real relative import while loading the startup environment in isolation.
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.is_telemetry_enabled() is expected
    session.assert_not_called()
    if disabled and disabled.lower() in ('1', 'true', 'yes'):
        getter.assert_not_called()
    else:
        getter.assert_called_once_with('telemetry_enabled', 'false')


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(source):
    parser = Text()
    parser.feed(source)
    return ' '.join(' '.join(parser.parts).split())


def test_website_disclosure_agrees_with_gate():
    home = (ROOT / 'website/index.html').read_text()
    faq = home.split('x-show="openFaq === 3"', 1)[1].split('</div>', 1)[0]
    privacy = (ROOT / 'website/privacy.html').read_text()
    section = privacy.split('2.3 Telemetry', 1)[1].split('2.4 Website Data', 1)[0]
    for text in [plain(faq), plain(section)]:
        assert 'off by default' in text
        assert 'administrator opt' in text
        assert 'DISABLE_TELEMETRY=1' in text
        assert 'restart the app' in text
        assert 'on by default' not in text
        assert 'By default, SixtyOps sends' not in text
    assert '(Opt-In)' in plain(section)
    assert 'even after opt-in' in plain(faq)
    assert 'even if an administrator opts in' in plain(section)
