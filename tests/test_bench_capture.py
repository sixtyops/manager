"""Tests for scripts/bench/capture_lab_fixtures.py. No network access."""

import datetime
import hashlib
import json

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from scripts.bench import capture_lab_fixtures as cap

AP_IP = "192.0.2.10"
SWITCH_IP = "198.51.100.20"
OTHER_IP = "203.0.113.7"
USERNAME = "bench-user-x7"
PASSWORD = "S3cret-Pa55-q9"
TOKEN = "tok-abcdef0123456789"
NOT_AFTER = datetime.datetime(2035, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)


def _cert_der() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tachyon")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc))
        .not_valid_after(NOT_AFTER)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


CERT_DER = _cert_der()

BODIES = {
    "/cgi.lua/status?type=system": {
        "system": {
            "serial": "SN-0001",
            "hostname": "Customer Tower 5",
            "general": {"name": "Customer Tower 5", "latitude": 44.98, "longitude": -93.27},
            "ip": AP_IP,
            "gateway": OTHER_IP,
            "admin_password": PASSWORD,
            "api_key": "k-123",
        }
    },
    "/cgi.lua/status?type=interfaces": {
        "interfaces": {
            "eth0": {"name": "eth0", "mac_address": "aa:bb:cc:dd:ee:01", "ipv6": "fe80::1"},
            "wlan0": {"mac_address": "aa:bb:cc:dd:ee:02", "ipv4": f"{OTHER_IP}/24"},
        }
    },
    "/cgi.lua/status?type=wireless,zones": {
        "wireless": {"peers": [{"ipv4": OTHER_IP, "mac": "aa:bb:cc:dd:ee:09",
                                "system_name": "Smith Residence"}]}
    },
    "/cgi.lua/discovery": {"neighbors": [{"ip": AP_IP, "note": f"seen via {SWITCH_IP}"}]},
}


class FakeDevice:
    """Records every request and answers like a Tachyon device."""

    def __init__(self, login_ok=True):
        self.calls = []
        self.login_ok = login_ok

    def __call__(self, method, host, path, headers, body):
        self.calls.append((method, host, path, dict(headers), body))
        if path == cap.LOGIN_PATH:
            if not self.login_ok:
                return cap.Response(200, [], json.dumps({"statusCode": 401}).encode(), CERT_DER)
            return cap.Response(
                200,
                [("Set-Cookie", f"token={TOKEN}; Path=/; HttpOnly")],
                b'{"auth": true}',
                CERT_DER,
            )
        body = BODIES.get(path, {"echo_token": TOKEN, "cookie": f"token={TOKEN}"})
        return cap.Response(200, [], json.dumps(body).encode(), CERT_DER)


@pytest.fixture
def access_env(tmp_path):
    access = tmp_path / "access.txt"
    access.write_text(
        "# lab access\n"
        f"AP IP: {AP_IP}\n"
        f"TNS-100 IP: {SWITCH_IP}\n"
        f"Shared device username: {USERNAME}\n"
        f"Shared device password: {PASSWORD}\n"
        "Unrelated: ignored\n"
    )
    return {cap.ACCESS_FILE_ENV: str(access)}


def _run(tmp_path, env, device, capsys, include_ap=True):
    out = tmp_path / "out"
    lines = []
    rc = cap.run(out, env=env, transport=device, log=lines.append, include_ap=include_ap)
    printed = "\n".join(lines) + capsys.readouterr().out
    return rc, out, printed


# --- Allowlist ---------------------------------------------------------------


@pytest.mark.parametrize("method,path", [
    ("POST", "/cgi.lua/config"),
    ("PUT", "/cgi.lua/update"),
    ("POST", "/cgi.lua/update"),
    ("POST", "/cgi.lua/reboot"),
    ("DELETE", "/cgi.lua/status?type=system"),
    ("POST", "/cgi.lua/status?type=system"),
    ("GET", "/cgi.lua/config"),
    ("GET", "/cgi.lua/status?type=system&x=1"),
    ("POST", "/cgi.lua/logout"),
])
def test_allowlist_refuses_writes_and_unknown_reads(method, path):
    device = FakeDevice()
    client = cap.GuardedClient(AP_IP, device)
    with pytest.raises(cap.RequestRefused):
        client.request(method, path, body=b"{}")
    assert device.calls == []


def test_allowlist_has_one_write_and_only_gets():
    assert cap.WRITE_ALLOWLIST == {("POST", "/cgi.lua/login")}
    assert {m for m, _ in cap.READ_ALLOWLIST} == {"GET"}
    assert ("GET", "/cgi.lua/config") not in cap.READ_ALLOWLIST


def test_run_sends_only_login_and_allowlisted_gets(tmp_path, access_env, capsys):
    device = FakeDevice()
    rc, _, _ = _run(tmp_path, access_env, device, capsys)
    assert rc == 0
    for method, _, path, _, _ in device.calls:
        if method == "GET":
            assert ("GET", path) in cap.READ_ALLOWLIST
        else:
            assert (method, path) == ("POST", cap.LOGIN_PATH)
    ap_paths = [c[2] for c in device.calls if c[1] == AP_IP and c[0] == "GET"]
    switch_paths = [c[2] for c in device.calls if c[1] == SWITCH_IP and c[0] == "GET"]
    assert ap_paths == list(cap.AP_READS)
    assert switch_paths == list(cap.SWITCH_READS)


def test_ap_is_skipped_by_default(tmp_path, access_env, capsys):
    device = FakeDevice()
    rc, out, printed = _run(tmp_path, access_env, device, capsys, include_ap=False)
    assert rc == 0
    assert device.calls and all(c[1] == SWITCH_IP for c in device.calls)
    assert sorted(p.name for p in out.iterdir()) == ["switch.json", "tls.json"]
    assert "ap: skipped" in printed and "--include-ap" in printed


def test_main_passes_include_ap_flag(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(cap.socket, "setdefaulttimeout", lambda _: None)
    monkeypatch.setattr(cap, "run", lambda out, include_ap=False: seen.append(include_ap) or 0)
    assert cap.main(["--out", str(tmp_path)]) == 0
    assert cap.main(["--out", str(tmp_path), "--include-ap"]) == 0
    assert seen == [False, True]


def test_login_matches_tachyon_client_and_reads_send_token(tmp_path, access_env, capsys):
    device = FakeDevice()
    _run(tmp_path, access_env, device, capsys)
    login = device.calls[0]
    assert login[0] == "POST" and login[2] == "/cgi.lua/login"
    assert login[3]["Content-Type"] == "application/json"
    assert json.loads(login[4]) == {"username": USERNAME, "password": PASSWORD}
    assert device.calls[1][3]["Cookie"] == f"token={TOKEN}"


def test_failed_login_sends_no_reads_but_records_certificate(tmp_path, access_env, capsys):
    device = FakeDevice(login_ok=False)
    rc, out, printed = _run(tmp_path, access_env, device, capsys)
    assert rc == 0
    assert all(c[0] == "POST" for c in device.calls)
    assert "Invalid credentials" in printed
    tls = json.loads((out / "tls.json").read_text())
    assert tls["targets"][0]["leaf_certificates"]


# --- Redaction ---------------------------------------------------------------


def test_output_has_no_secrets_or_ips(tmp_path, access_env, capsys):
    rc, out, _ = _run(tmp_path, access_env, FakeDevice(), capsys)
    assert rc == 0
    files = sorted(p.name for p in out.iterdir())
    assert files == ["ap.json", "switch.json", "tls.json"]
    text = "".join(p.read_text() for p in out.iterdir())
    for value in (PASSWORD, USERNAME, TOKEN, "k-123", AP_IP, SWITCH_IP, OTHER_IP,
                  "fe80::1", "Customer Tower 5", "Smith Residence", "44.98", "-93.27"):
        assert value not in text


def test_redaction_keeps_identity_fields_and_uses_stable_placeholders(tmp_path, access_env, capsys):
    _, out, _ = _run(tmp_path, access_env, FakeDevice(), capsys)
    ap = json.loads((out / "ap.json").read_text())
    switch = json.loads((out / "switch.json").read_text())
    bodies = {r["path"]: r["body"] for r in ap["responses"]}
    system = bodies["/cgi.lua/status?type=system"]["system"]
    assert system["serial"] == "SN-0001"
    assert system["ip"] == "<ap-ip>"
    assert system["admin_password"] == cap.REDACTED
    assert system["api_key"] == cap.REDACTED
    assert system["hostname"].startswith("<label-")
    assert system["general"]["name"] == system["hostname"]
    assert system["general"]["latitude"] == cap.REDACTED
    peer = bodies["/cgi.lua/status?type=wireless,zones"]["wireless"]["peers"][0]
    assert peer["mac"] == "aa:bb:cc:dd:ee:09"
    assert peer["system_name"].startswith("<label-")
    assert peer["system_name"] != system["hostname"]
    ifaces = bodies["/cgi.lua/status?type=interfaces"]["interfaces"]
    assert ifaces["eth0"]["name"] == "eth0"
    assert ifaces["eth0"]["mac_address"] == "aa:bb:cc:dd:ee:01"
    assert ifaces["wlan0"]["mac_address"] == "aa:bb:cc:dd:ee:02"
    assert ifaces["wlan0"]["ipv4"].endswith("/24")
    gateway = system["gateway"]
    assert ifaces["wlan0"]["ipv4"] == f"{gateway}/24"
    discovery = {r["path"]: r["body"] for r in switch["responses"]}["/cgi.lua/discovery"]
    assert discovery["neighbors"][0]["ip"] == "<ap-ip>"
    assert discovery["neighbors"][0]["note"] == "seen via <switch-ip>"


def test_redactor_leaves_macs_and_versions():
    r = cap.Redactor()
    assert r.text("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"
    assert r.text("1.12.3") == "1.12.3"
    first = r.text("2001:db8::5 and 10.1.2.3")
    assert first in ("<ip-1> and <ip-2>", "<ip-2> and <ip-1>")
    assert r.text("10.1.2.3 2001:DB8::5") == " ".join(reversed(first.split(" and ")))
    assert r.text("::ffff:10.1.2.3") == "::ffff:" + first.split(" and ")[1]


# --- TLS ---------------------------------------------------------------------


def test_tls_records_fingerprint_and_not_after(tmp_path, access_env, capsys):
    _, out, _ = _run(tmp_path, access_env, FakeDevice(), capsys)
    tls = json.loads((out / "tls.json").read_text())
    digest = hashlib.sha256(CERT_DER).hexdigest().upper()
    expected = ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))
    for row in tls["targets"]:
        assert row["leaf_certificates"] == [
            {"sha256": expected, "not_after": NOT_AFTER.isoformat()}
        ]
        assert row["connections"] == 1 + len(
            cap.AP_READS if row["role"] == "ap" else cap.SWITCH_READS)


# --- Credentials are never printed ------------------------------------------


def test_run_never_prints_credentials_or_addresses(tmp_path, access_env, capsys):
    _, _, printed = _run(tmp_path, access_env, FakeDevice(), capsys)
    _, _, printed_fail = _run(tmp_path, access_env, FakeDevice(login_ok=False), capsys)
    for text in (printed, printed_fail):
        for value in (PASSWORD, USERNAME, TOKEN, AP_IP, SWITCH_IP):
            assert value not in text


def test_access_errors_name_keys_not_values(tmp_path, capsys):
    access = tmp_path / "access.txt"
    access.write_text(f"AP IP: {AP_IP}\nShared device password: {PASSWORD}\n")
    lines = []
    rc = cap.run(tmp_path / "out", env={cap.ACCESS_FILE_ENV: str(access)},
                 transport=FakeDevice(), log=lines.append)
    printed = "\n".join(lines) + capsys.readouterr().out
    assert rc == 2
    assert "'Shared device username'" in printed
    assert PASSWORD not in printed and AP_IP not in printed and str(access) not in printed


def test_missing_env_var_fails_without_network(tmp_path):
    device = FakeDevice()
    lines = []
    assert cap.run(tmp_path / "out", env={}, transport=device, log=lines.append) == 2
    assert device.calls == []
    assert cap.ACCESS_FILE_ENV in lines[0]


def test_output_dir_inside_repo_is_refused(access_env):
    device = FakeDevice()
    lines = []
    rc = cap.run(cap.repo_root() / "tmp-capture", env=access_env, transport=device,
                 log=lines.append)
    assert rc == 2
    assert device.calls == []
    assert not (cap.repo_root() / "tmp-capture").exists()
