#!/usr/bin/env python3
"""Capture redacted, read-only Tachyon responses from the lab bench.

Part of #288 (identity fixtures) and #393 (TLS certificate stability).

What it does:
- Reads the bench access file named by SIXTYOPS_BENCH_ACCESS_FILE.
- Logs in to the lab TNS-100 switch with POST /cgi.lua/login, the same
  way updater/vendors/tachyon/client.py does.
- Skips the lab AP by default. The AP login returned 401 in #421. Do not log
  in to the AP again until the offline AP account check passes (#530). Then
  add --include-ap.
- Sends only the GET requests in READ_ALLOWLIST. Every other request is
  refused before it reaches the network.
- Records the SHA-256 fingerprint and notAfter of each TLS leaf certificate.
- Redacts tokens, cookies, passwords, secrets, keys, names, and IP addresses,
  then writes JSON files to an output directory outside the repository.

It never prints a credential value or a device address.

Usage:
    SIXTYOPS_BENCH_ACCESS_FILE=<path> \
        python3 scripts/bench/capture_lab_fixtures.py --out <dir outside repo>

    Add --include-ap only after the AP account check passes.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ACCESS_FILE_ENV = "SIXTYOPS_BENCH_ACCESS_FILE"

KEY_AP_IP = "AP IP"
KEY_SWITCH_IP = "TNS-100 IP"
KEY_USERNAME = "Shared device username"
KEY_PASSWORD = "Shared device password"
ACCESS_KEYS = (KEY_AP_IP, KEY_SWITCH_IP, KEY_USERNAME, KEY_PASSWORD)

LOGIN_PATH = "/cgi.lua/login"

# The only write the script may send. The Tachyon client has no logout call,
# so no logout endpoint is allowed here.
WRITE_ALLOWLIST = frozenset({("POST", LOGIN_PATH)})

# Read endpoints the Tachyon client already uses, plus the switch discovery
# endpoint observed in #421. /cgi.lua/config is excluded: it returns the full
# device config, including secrets.
AP_READS = (
    "/cgi.lua/status?type=system",
    "/cgi.lua/status?type=interfaces",
    "/cgi.lua/bootbank",
    "/cgi.lua/status?type=wireless,zones",
    "/cgi.lua/status?type=zones",
)
SWITCH_READS = (
    "/cgi.lua/status?type=system",
    "/cgi.lua/status?type=interfaces",
    "/cgi.lua/bootbank",
    "/cgi.lua/discovery",
    "/cgi.lua/bridge_table",
)
READ_ALLOWLIST = frozenset(("GET", path) for path in AP_READS + SWITCH_READS)

TIMEOUT_SECONDS = 30
REDACTED = "[REDACTED]"


class AccessFileError(Exception):
    """The access file is missing or incomplete. Messages name keys only."""


class RequestRefused(Exception):
    """The request is not in the allowlist."""


# --- Access file -----------------------------------------------------------


def parse_access_text(text: str) -> Dict[str, str]:
    """Parse 'Key: value' lines. Keep only the known keys."""
    values: Dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key in ACCESS_KEYS and value:
            values[key] = value
    return values


def load_access(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = os.environ if env is None else env
    path = env.get(ACCESS_FILE_ENV)
    if not path:
        raise AccessFileError(f"{ACCESS_FILE_ENV} is not set.")
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        raise AccessFileError(f"Cannot read the file named by {ACCESS_FILE_ENV}.") from None
    values = parse_access_text(text)
    missing = [k for k in (KEY_USERNAME, KEY_PASSWORD) if k not in values]
    if missing:
        raise AccessFileError("Access file is missing: " + ", ".join(f"'{k}'" for k in missing))
    if KEY_AP_IP not in values and KEY_SWITCH_IP not in values:
        raise AccessFileError(f"Access file has neither '{KEY_AP_IP}' nor '{KEY_SWITCH_IP}'.")
    return values


# --- Transport -------------------------------------------------------------


@dataclass
class Response:
    status: int
    headers: List[Tuple[str, str]]
    body: bytes
    peer_cert_der: Optional[bytes] = None


# (method, host, path, headers, body) -> Response
Transport = Callable[[str, str, str, Dict[str, str], Optional[bytes]], Response]


def https_transport(method: str, host: str, path: str, headers: Dict[str, str],
                    body: Optional[bytes]) -> Response:
    """Send one HTTPS request and keep the leaf certificate of that connection.

    Devices use self-signed certificates, so verification is off, as in the
    Tachyon client. The certificate is recorded for #393 instead.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(host, 443, timeout=TIMEOUT_SECONDS, context=context)
    try:
        conn.request(method, path, body=body, headers=headers)
        cert = conn.sock.getpeercert(binary_form=True) if conn.sock else None
        resp = conn.getresponse()
        return Response(resp.status, resp.getheaders(), resp.read(), cert)
    finally:
        conn.close()


class GuardedClient:
    """Sends only allowlisted requests to one device."""

    def __init__(self, host: str, transport: Transport):
        self._host = host
        self._transport = transport
        self._token: Optional[str] = None
        self.cert_ders: List[bytes] = []

    def _send(self, method: str, path: str, headers: Dict[str, str],
              body: Optional[bytes]) -> Response:
        method = method.upper()
        if (method, path) not in READ_ALLOWLIST | WRITE_ALLOWLIST:
            raise RequestRefused(f"Refused {method} {path}: not in the allowlist.")
        resp = self._transport(method, self._host, path, headers, body)
        if resp.peer_cert_der:
            self.cert_ders.append(resp.peer_cert_der)
        return resp

    def request(self, method: str, path: str, body: Optional[bytes] = None) -> Response:
        """Generic entry point. Refuses everything outside the allowlist."""
        headers: Dict[str, str] = {}
        if self._token:
            headers["Cookie"] = f"token={self._token}"
        return self._send(method, path, headers, body)

    def login(self, username: str, password: str) -> Tuple[bool, str]:
        """POST /cgi.lua/login with JSON, then read the token cookie.

        Uses the same failure checks as TachyonClient.login. The result text
        never contains the response body.
        """
        payload = json.dumps({"username": username, "password": password}).encode()
        try:
            resp = self._send("POST", LOGIN_PATH, {"Content-Type": "application/json"}, payload)
        except (OSError, http.client.HTTPException) as exc:
            return False, f"Device not reachable ({type(exc).__name__})"
        text = resp.body.decode("utf-8", errors="ignore")
        try:
            data = json.loads(text)
            if isinstance(data, dict) and (data.get("statusCode") == 401 or data.get("auth") is False):
                return False, f"Invalid credentials (HTTP {resp.status})"
        except json.JSONDecodeError:
            pass
        if "Authorization Failed" in text or "Invalid credentials" in text:
            return False, f"Invalid credentials (HTTP {resp.status})"
        token = _token_from_headers(resp.headers)
        if not token:
            return False, f"No token received (HTTP {resp.status})"
        self._token = token
        return True, f"Logged in (HTTP {resp.status})"

    @property
    def token(self) -> Optional[str]:
        return self._token


def _token_from_headers(headers: List[Tuple[str, str]]) -> Optional[str]:
    for name, value in headers:
        if name.lower() != "set-cookie":
            continue
        first = value.split(";", 1)[0].strip()
        cname, _, cvalue = first.partition("=")
        if cname.strip() == "token" and cvalue:
            return cvalue.strip()
    return None


# --- TLS certificate -------------------------------------------------------


def cert_summary(der: bytes) -> Dict[str, Any]:
    """Return the SHA-256 fingerprint and notAfter of a DER certificate."""
    from cryptography import x509

    digest = hashlib.sha256(der).hexdigest().upper()
    fingerprint = ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))
    try:
        cert = x509.load_der_x509_certificate(der)
        not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(
            tzinfo=timezone.utc)
        not_after_text = not_after.isoformat()
    except ValueError:
        not_after_text = "unparseable"
    return {"sha256": fingerprint, "not_after": not_after_text}


# --- Redaction -------------------------------------------------------------

_SECRET_KEY_RE = re.compile(
    r"token|cookie|passw|pwd|secret|psk|passphrase|credential|session|private|"
    r"community|auth.?key|api.?key|key$|keys$|^key|signature|bearer|authorization|"
    r"^lat$|^lon$|^lng$|latitude|longitude|coordinates|^gps",
    re.IGNORECASE,
)
# Operator-chosen labels. Replaced with stable placeholders, not dropped, so
# fixtures still show which records share a label. A "name" that looks like an
# interface (eth0, wlan0) is kept: #288 needs it.
_LABEL_KEY_RE = re.compile(
    r"^(name|hostname|host_name|device_name|system_name|sysname|sys_name|location|"
    r"contact|ssid|description|comment|label|remote_name|remote_hostname|"
    r"peer_name|site|site_name|customer)$",
    re.IGNORECASE,
)
_INTERFACE_NAME_RE = re.compile(
    r"^(eth|wlan|wifi|br|lo|ath|sfp|port|ge|xe|bond|vlan|mgmt)[\w.\-]*$", re.IGNORECASE)
_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6_CANDIDATE_RE = re.compile(r"(?<![0-9A-Za-z:])[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*(?![0-9A-Za-z:])")
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


class Redactor:
    """Removes secrets and replaces addresses and labels with placeholders.

    One instance per run, so a value maps to the same placeholder in every
    output file of that run.
    """

    def __init__(self, known_ips: Optional[Dict[str, str]] = None,
                 literal_secrets: Tuple[str, ...] = ()):
        self._ips: Dict[str, str] = {}
        for ip, name in (known_ips or {}).items():
            self._ips[ip] = f"<{name}-ip>"
        self._labels: Dict[str, str] = {}
        self._secrets = tuple(s for s in literal_secrets if s)

    def _ip(self, ip: str) -> str:
        if ip not in self._ips:
            self._ips[ip] = f"<ip-{len(self._ips) + 1}>"
        return self._ips[ip]

    def _label(self, value: str) -> str:
        if value not in self._labels:
            self._labels[value] = f"<label-{len(self._labels) + 1}>"
        return self._labels[value]

    def text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, REDACTED)

        def v4(match: re.Match) -> str:
            try:
                ipaddress.IPv4Address(match.group(0))
            except ValueError:
                return match.group(0)
            return self._ip(match.group(0))

        def v6(match: re.Match) -> str:
            candidate = match.group(0)
            if _MAC_RE.match(candidate):
                return candidate
            try:
                ipaddress.IPv6Address(candidate)
            except ValueError:
                return candidate
            return self._ip(candidate.lower())

        value = _IPV4_RE.sub(v4, value)
        return _IPV6_CANDIDATE_RE.sub(v6, value)

    def value(self, obj: Any, key: Optional[str] = None) -> Any:
        if key is not None and _SECRET_KEY_RE.search(key):
            return REDACTED
        if isinstance(obj, dict):
            return {self.text(str(k)): self.value(v, str(k)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.value(v, key) for v in obj]
        if isinstance(obj, str):
            if key is not None and _LABEL_KEY_RE.match(key) and obj:
                if key.lower() == "name" and _INTERFACE_NAME_RE.match(obj):
                    return obj
                return self._label(obj)
            return self.text(obj)
        return obj


# --- Capture ---------------------------------------------------------------


@dataclass
class TargetResult:
    role: str
    login: str = ""
    responses: List[Dict[str, Any]] = field(default_factory=list)
    certificates: List[Dict[str, Any]] = field(default_factory=list)
    connections: int = 0
    # Session token. Used only to strip it from output; never written.
    token: str = ""


def _decode_body(body: bytes) -> Any:
    text = body.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def capture_target(role: str, host: str, username: str, password: str,
                   paths: Tuple[str, ...], transport: Transport,
                   log: Callable[[str], None]) -> TargetResult:
    result = TargetResult(role=role)
    client = GuardedClient(host, transport)
    ok, message = client.login(username, password)
    result.login = message
    log(f"{role}: login: {message}")
    if ok:
        for path in paths:
            try:
                resp = client.request("GET", path)
            except (OSError, http.client.HTTPException) as exc:
                result.responses.append({"method": "GET", "path": path,
                                         "error": type(exc).__name__})
                log(f"{role}: GET {path}: {type(exc).__name__}")
                continue
            result.responses.append({"method": "GET", "path": path, "status": resp.status,
                                     "body": _decode_body(resp.body)})
            log(f"{role}: GET {path}: HTTP {resp.status}")
    seen = set()
    for der in client.cert_ders:
        summary = cert_summary(der)
        if summary["sha256"] not in seen:
            seen.add(summary["sha256"])
            result.certificates.append(summary)
    result.connections = len(client.cert_ders)
    result.token = client.token or ""
    return result


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def check_out_dir(out_dir: Path) -> Path:
    resolved = out_dir.expanduser().resolve()
    root = repo_root()
    if resolved == root or root in resolved.parents:
        raise ValueError("Output directory must be outside the repository.")
    return resolved


def run(out_dir: Path, env: Optional[Dict[str, str]] = None,
        transport: Transport = https_transport,
        log: Callable[[str], None] = print,
        include_ap: bool = False) -> int:
    try:
        out = check_out_dir(out_dir)
        access = load_access(env)
    except (ValueError, AccessFileError) as exc:
        log(f"error: {exc}")
        return 2

    username = access[KEY_USERNAME]
    password = access[KEY_PASSWORD]
    targets = []
    if KEY_AP_IP in access and not include_ap:
        log("ap: skipped. Add --include-ap only after the AP account check passes.")
    elif KEY_AP_IP in access:
        targets.append(("ap", access[KEY_AP_IP], AP_READS))
    else:
        log(f"ap: skipped, no '{KEY_AP_IP}' in the access file")
    if KEY_SWITCH_IP in access:
        targets.append(("switch", access[KEY_SWITCH_IP], SWITCH_READS))
    else:
        log(f"switch: skipped, no '{KEY_SWITCH_IP}' in the access file")

    results = [capture_target(role, host, username, password, paths, transport, log)
               for role, host, paths in targets]

    tokens = tuple(r.token for r in results)
    redactor = Redactor(known_ips={host: role for role, host, _ in targets},
                        literal_secrets=(password, username) + tokens)
    captured_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    out.mkdir(parents=True, exist_ok=True)
    tls_rows = []
    for result in results:
        doc = {
            "role": result.role,
            "captured_at": captured_at,
            "login": result.login,
            "responses": result.responses,
        }
        _write_json(out / f"{result.role}.json", redactor.value(doc))
        tls_rows.append({
            "role": result.role,
            "connections": result.connections,
            "leaf_certificates": result.certificates,
        })
    _write_json(out / "tls.json", redactor.value({
        "issue": 393,
        "captured_at": captured_at,
        "targets": tls_rows,
    }))
    log(f"Wrote {len(results) + 1} files. Review them before you attach them anywhere.")
    return 0


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path,
                        help="Output directory. Must be outside the repository.")
    parser.add_argument("--include-ap", action="store_true",
                        help="Log in to the lab AP too. Use only after the AP "
                             "account check passes (#530).")
    args = parser.parse_args(argv)
    socket.setdefaulttimeout(TIMEOUT_SECONDS)
    return run(args.out, include_ap=args.include_ap)


if __name__ == "__main__":
    sys.exit(main())
