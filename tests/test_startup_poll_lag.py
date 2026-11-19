"""Exercise startup polling with real parsing and file-backed SQLite."""

import asyncio
import cProfile
import hashlib
import io
import json
import logging
from pathlib import Path
import pstats
import time
from urllib.parse import urlsplit, parse_qs
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from updater import crypto, database as db, logging_filter
from updater.poller import NetworkPoller
from updater.vendors import init_vendors


class MockFleet:
    def __init__(self):
        self.requests = 0

    async def transport(self, args):
        self.requests += 1
        await asyncio.sleep(0.001)
        url = urlsplit(args[-1])
        index = int(url.hostname.split('.')[-1])
        octets = [int(part) for part in url.hostname.split('.')]
        mac = '02:00:' + ':'.join(f'{part:02x}' for part in octets)
        if url.path.endswith('/login'):
            # Synthetic per-device values exercise token registration at fleet size.
            token = hashlib.sha256(url.hostname.encode()).hexdigest()
            Path(args[args.index('-c') + 1]).write_text(
                f'{url.hostname}\tFALSE\t/\tFALSE\t0\ttoken\t{token}\n'
            )
            return 0, b'{"auth":true}'
        kind = parse_qs(url.query).get('type', [''])[0]
        if kind == 'system':
            body = {'system': {'model': 'TNA-301', 'serial': f'LAB-{url.hostname}',
                              'general': {'name': f'lab-{index}', 'location': 'Offline lab'},
                              'version': {'firmux': '1.12.3 rev 54970'}}}
        elif kind == 'interfaces':
            body = {'interfaces': {'eth0': {'mac_address': mac}}}
        elif kind == 'zones':
            body = {'zones': {'management': {'interface': 'eth0'}}}
        elif kind == 'wireless,zones':
            body = {'wireless': {'radios': {'wlan0': {'channel': 2, 'channelWidth': 2160,
                    'frequency': 60480}}, 'peers': [
                        {'ipv4': f'198.51.{index}.{j}', 'mac': f'02:01:00:00:{index:02x}:{j:02x}',
                         'system_name': f'cpe-{index}-{j}', 'model': 'TNA-301',
                         'fw': '1.12.3', 'linkDistance': 350, 'rxPower': -55,
                         'txRate': 1000, 'rxRate': 1000, 'mcs': 8, 'linkUptime': 3600}
                        for j in range(1, 5 + index % 7)]}}
        elif url.path.endswith('/bootbank'):
            body = {'active': {'bootbank': 1, 'firmux': '1.12.3 rev 54970'},
                    'backup': {'bootbank': 2, 'firmux': '1.12.3 rev 54970'}}
        elif url.path.endswith('/config'):
            body = {'system': {'name': f'lab-{index}', 'location': 'Offline lab'},
                    'network': {'interfaces': {'eth0': {'enabled': True, 'mtu': 1500}}}}
        else:
            raise AssertionError(f'Unexpected offline endpoint: {url.path}?{url.query}')
        return 0, json.dumps(body).encode() + b'\n200'


async def run_first_cycle(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'manager.db')
    monkeypatch.setattr(crypto, '_KEY_PATH', tmp_path / '.encryption_key')
    monkeypatch.setattr(crypto, '_fernet', None)
    monkeypatch.setattr(logging_filter, '_secrets', set())
    monkeypatch.setattr(logging_filter, '_device_tokens', {})
    monkeypatch.setattr(logging_filter, '_pattern', None)
    db._invalidate_settings_cache()
    db.init_db()
    db.set_setting('schedule_enabled', 'false')
    for i in range(1, 111):
        db.upsert_access_point(f'192.0.2.{i}', 'offline', 'offline-fixture')
    init_vendors()
    fleet = MockFleet()
    monkeypatch.setattr('updater.vendors.tachyon.client._run_curl', fleet.transport)
    poller = NetworkPoller(broadcast_func=None)
    done = asyncio.Event()
    real_maybe = poller._maybe_poll_configs

    async def finish_cycle():
        await real_maybe()
        done.set()

    monkeypatch.setattr(poller, '_maybe_poll_configs', finish_cycle)
    loop = asyncio.get_running_loop()
    old_debug, old_slow = loop.get_debug(), loop.slow_callback_duration
    loop.set_debug(True)
    loop.slow_callback_duration = 0.05
    lags = []
    from updater import app as app_module
    app = app_module.app
    monkeypatch.setattr(app_module, 'init_poller', lambda *a, **kw: poller)
    for name in ('init_scheduler', 'init_fetcher', 'init_checker'):
        service = MagicMock(start=AsyncMock(), stop=AsyncMock())
        monkeypatch.setattr(app_module, name, lambda *a, _service=service, **kw: _service)
    monkeypatch.setattr(app_module, 'ensure_admin_user', lambda: None)
    monkeypatch.setattr(app_module, 'verify_update_on_startup', AsyncMock())
    monkeypatch.setattr(app_module, 'init_radius_service', MagicMock())
    monkeypatch.setattr(app_module, '_supervised_task', AsyncMock())
    monkeypatch.setattr(app_module.syslog_forwarder, 'reload_config', lambda: None)
    monkeypatch.setattr(app_module, '_shutting_down', False)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver')
    blocks = []

    class SlowCallbacks(logging.Handler):
        def emit(self, record):
            if record.msg == 'Executing %s took %.3f seconds':
                blocks.append(record.args[1])

    handler = SlowCallbacks()
    logging.getLogger('asyncio').addHandler(handler)

    async def heartbeat():
        while not done.is_set():
            start = loop.time()
            await asyncio.sleep(0.1)
            lags.append(max(0, loop.time() - start - 0.1))
            response = await http.get('/login')
            assert response.status_code == 200

    pulse = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    profile = cProfile.Profile()
    started = time.perf_counter()
    try:
        profile.enable()
        async with app_module.lifespan(app):
            await asyncio.wait_for(done.wait(), 180)
            await pulse
    finally:
        profile.disable()
        logging.getLogger("asyncio").removeHandler(handler)
        await http.aclose()
        await poller.stop()
        loop.set_debug(old_debug)
        loop.slow_callback_duration = old_slow
    report = io.StringIO()
    pstats.Stats(profile, stream=report).strip_dirs().sort_stats('cumulative').print_stats(40)
    print(f'\nElapsed={time.perf_counter()-started:.3f}s requests={fleet.requests} '
          f'max_lag={max(lags):.3f}s total_lag={sum(lags):.3f}s '
          f'slow_callbacks={len(blocks)} total_blocked={sum(blocks):.3f}s '
          f'worst_callback={max(blocks, default=0):.3f}s')
    print(report.getvalue())
    assert len(db.get_access_points()) == 110
    assert len(db.get_all_latest_configs()) == 110
    assert len(db.get_all_cpes()) > 700
    assert all(not ap['last_error'] for ap in db.get_access_points())
    db._invalidate_settings_cache()
    return max(lags)


@pytest.mark.asyncio
async def test_first_poll_keeps_loop_responsive(monkeypatch, tmp_path):
    lag = await run_first_cycle(monkeypatch, tmp_path)
    assert lag < 0.5
