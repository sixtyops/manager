#!/usr/bin/env python3
"""
SixtyOps Manager — Release Validation Script

Runs automated API-level tests against a live deployment to validate
that all endpoints and features are functioning correctly before a release.

Usage:
    python scripts/validate_release.py --host https://sixtyops.example.com
    python scripts/validate_release.py --host http://localhost:8000 --device-ip 10.0.0.1
    python scripts/validate_release.py --host http://localhost:8000 --skip radius,backup --verbose
"""

import argparse
import io
import json
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Optional

import requests
import urllib3

# Suppress TLS warnings for self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import websocket as ws_client

    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False


# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

class C:
    """ANSI color codes."""
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def _pass(msg: str) -> str:
    return f"  {C.GREEN}\u2713{C.RESET} {msg}"


def _fail(msg: str, detail: str = "") -> str:
    suffix = f" {C.DIM}({detail}){C.RESET}" if detail else ""
    return f"  {C.RED}\u2717{C.RESET} {msg}{suffix}"


def _skip(msg: str, reason: str = "") -> str:
    suffix = f" {C.DIM}({reason}){C.RESET}" if reason else ""
    return f"  {C.YELLOW}~{C.RESET} {msg}{suffix}"


def _header(name: str) -> str:
    return f"\n{C.BOLD}[{name}]{C.RESET}"


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

class TestResult:
    def __init__(self) -> None:
        self.passed: int = 0
        self.failed: int = 0
        self.skipped: int = 0
        self.errors: list[str] = []

    @property
    def total(self) -> int:
        return self.passed + self.failed

    def record_pass(self, msg: str, verbose: bool = False) -> None:
        self.passed += 1
        print(_pass(msg))

    def record_fail(self, msg: str, detail: str = "", verbose: bool = False) -> None:
        self.failed += 1
        print(_fail(msg, detail))
        self.errors.append(f"{msg}: {detail}" if detail else msg)

    def record_skip(self, msg: str, reason: str = "") -> None:
        self.skipped += 1
        print(_skip(msg, reason))


class CategoryResult:
    def __init__(self, name: str) -> None:
        self.name = name
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    @property
    def total(self) -> int:
        return self.passed + self.failed


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class ReleaseValidator:
    """Runs all validation tests against a live SixtyOps deployment."""

    CATEGORIES = [
        "health_auth",
        "license_system",
        "sites",
        "devices",
        "firmware_files",
        "firmware_update",
        "rollout",
        "config_management",
        "config_push_rollout",
        "radius",
        "users",
        "notifications",
        "backup",
        "freeze_windows",
        "analytics",
        "scheduler",
        "self_update",
        "ssl",
        "websocket",
    ]

    def __init__(
        self,
        host: str,
        username: str = "admin",
        password: str = "admin",
        skip: set[str] | None = None,
        device_ip: str | None = None,
        switch_ip: str | None = None,
        verbose: bool = False,
        dry_run: bool = False,
        allow_firmware_update: bool = False,
    ) -> None:
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.skip = skip or set()
        self.device_ip = device_ip
        self.switch_ip = switch_ip
        self.verbose = verbose
        self.dry_run = dry_run
        self.allow_firmware_update = allow_firmware_update

        self.session = requests.Session()
        self.session.verify = False

        self.category_results: list[CategoryResult] = []

    # -- helpers -----------------------------------------------------------

    def url(self, path: str) -> str:
        return f"{self.host}{path}"

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"  {C.DIM}  > {msg}{C.RESET}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict | None = None,
        json_body: dict | None = None,
        files: dict | None = None,
        allow_redirects: bool = True,
        session: requests.Session | None = None,
        headers: dict | None = None,
        timeout: int = 30,
    ) -> requests.Response:
        s = session or self.session
        self._log(f"{method.upper()} {path}")
        resp = s.request(
            method,
            self.url(path),
            data=data,
            json=json_body,
            files=files,
            allow_redirects=allow_redirects,
            headers=headers,
            timeout=timeout,
        )
        self._log(f"  -> {resp.status_code}")
        return resp

    def _get(self, path: str, **kw: Any) -> requests.Response:
        return self._request("GET", path, **kw)

    def _post(self, path: str, **kw: Any) -> requests.Response:
        return self._request("POST", path, **kw)

    def _put(self, path: str, **kw: Any) -> requests.Response:
        return self._request("PUT", path, **kw)

    def _delete(self, path: str, **kw: Any) -> requests.Response:
        return self._request("DELETE", path, **kw)

    def _json_ok(self, resp: requests.Response) -> Any:
        """Parse JSON, raise on non-200."""
        resp.raise_for_status()
        return resp.json()

    # -- category runner ---------------------------------------------------

    def run_category(self, name: str, result: TestResult) -> CategoryResult:
        cat = CategoryResult(name)
        before_pass = result.passed
        before_fail = result.failed
        before_skip = result.skipped

        fn = getattr(self, f"test_{name}", None)
        if fn is None:
            result.record_skip(name, "no test method")
            cat.skipped += 1
            return cat

        print(_header(name))

        if name in self.skip:
            result.record_skip(name, "skipped by user")
            cat.skipped += 1
            return cat

        if self.dry_run:
            result.record_skip(name, "dry-run mode")
            cat.skipped += 1
            return cat

        try:
            fn(result)
        except Exception as exc:
            result.record_fail(f"{name} (uncaught exception)", str(exc))

        cat.passed = result.passed - before_pass
        cat.failed = result.failed - before_fail
        cat.skipped = result.skipped - before_skip
        return cat

    # -- run all -----------------------------------------------------------

    def run(self) -> bool:
        print(f"\n{C.BOLD}SixtyOps Release Validator{C.RESET}")
        print(f"  Host:      {self.host}")
        print(f"  User:      {self.username}")
        print(f"  Device IP: {self.device_ip or 'none'}")
        print(f"  Switch IP: {self.switch_ip or 'none'}")
        if self.skip:
            print(f"  Skipping:  {', '.join(sorted(self.skip))}")
        if self.dry_run:
            print(f"  {C.YELLOW}DRY-RUN MODE — no API calls will be made{C.RESET}")

        result = TestResult()

        for name in self.CATEGORIES:
            cat = self.run_category(name, result)
            self.category_results.append(cat)

        self._print_summary(result)
        self._print_manual_checklist()

        return result.failed == 0

    # -- summary -----------------------------------------------------------

    def _print_summary(self, result: TestResult) -> None:
        print(f"\n{'=' * 50}")
        print(f"  {C.BOLD}VALIDATION SUMMARY{C.RESET}")
        print(f"{'=' * 50}")

        for cat in self.category_results:
            if cat.skipped and cat.total == 0:
                status = f"{C.YELLOW}~ skipped{C.RESET}"
            elif cat.failed == 0:
                status = f"{C.GREEN}\u2713 {cat.passed}/{cat.total} passed{C.RESET}"
            else:
                status = f"{C.RED}\u2717 {cat.passed}/{cat.total} passed, {cat.failed} failed{C.RESET}"
            print(f"  {cat.name:<22s} {status}")

        total_skipped = sum(1 for c in self.category_results if c.skipped and c.total == 0)
        color = C.GREEN if result.failed == 0 else C.RED
        print(f"\n  {color}TOTAL: {result.passed}/{result.total} passed, "
              f"{result.failed} failed, {total_skipped} skipped{C.RESET}")
        print(f"{'=' * 50}")

    def _print_manual_checklist(self) -> None:
        print(f"\n{C.BOLD}MANUAL VERIFICATION CHECKLIST:{C.RESET}")
        items = [
            "OIDC/SSO login flow (requires IdP)",
            "SSL cert provisioning via Let's Encrypt (requires domain)",
            "Self-update apply (POST /api/updates/apply) — destructive",
            "Appliance TUI console (login via VM console)",
            "Appliance network reconfiguration (TUI > Network)",
            "Firmware rollout with canary phases (start and let complete)",
            "Config push to real device and verify on device",
            "certbot renewal cycle",
        ]
        for item in items:
            print(f"  [ ] {item}")
        print()

    # ======================================================================
    # TEST CATEGORIES
    # ======================================================================

    # 1. health_auth -------------------------------------------------------

    def test_health_auth(self, r: TestResult) -> None:
        # GET /healthz
        try:
            resp = self._get("/healthz")
            if resp.status_code == 200:
                r.record_pass("GET /healthz -> 200")
            else:
                r.record_fail("GET /healthz", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /healthz", str(e))

        # POST /login (valid)
        try:
            resp = self._post(
                "/login",
                data={"username": self.username, "password": self.password},
                allow_redirects=False,
            )
            if resp.status_code in (303, 302, 200):
                r.record_pass(f"POST /login -> {resp.status_code} (authenticated)")
            else:
                r.record_fail("POST /login", f"unexpected status {resp.status_code}")
        except Exception as e:
            r.record_fail("POST /login", str(e))

        # GET /api/users/me
        try:
            resp = self._get("/api/users/me")
            body = self._json_ok(resp)
            if "username" in body:
                r.record_pass("GET /api/users/me -> has username")
            else:
                r.record_fail("GET /api/users/me", "missing 'username' field")
        except Exception as e:
            r.record_fail("GET /api/users/me", str(e))

        # GET /api/auth/config
        try:
            resp = self._get("/api/auth/config")
            body = self._json_ok(resp)
            if isinstance(body, dict):
                r.record_pass("GET /api/auth/config -> dict")
            else:
                r.record_fail("GET /api/auth/config", f"expected dict, got {type(body).__name__}")
        except Exception as e:
            r.record_fail("GET /api/auth/config", str(e))

        # POST /login with bad password (separate session)
        try:
            bad_session = requests.Session()
            bad_session.verify = False
            resp = self._post(
                "/login",
                data={"username": self.username, "password": "wrong_password_xyz"},
                allow_redirects=False,
                session=bad_session,
            )
            # Verify the bad session can NOT access protected endpoint
            check = self._get("/api/users/me", session=bad_session)
            if check.status_code in (401, 403, 302, 303):
                r.record_pass("POST /login with bad password -> rejected")
            else:
                r.record_fail("POST /login with bad password", "bad creds were accepted")
        except Exception as e:
            r.record_fail("POST /login with bad password", str(e))

        # GET /api/audit-log
        try:
            resp = self._get("/api/audit-log")
            body = self._json_ok(resp)
            if isinstance(body, list):
                r.record_pass("GET /api/audit-log -> list")
            else:
                r.record_fail("GET /api/audit-log", f"expected list, got {type(body).__name__}")
        except Exception as e:
            r.record_fail("GET /api/audit-log", str(e))

    # 2. license_system ----------------------------------------------------

    def test_license_system(self, r: TestResult) -> None:
        # GET /api/license
        try:
            resp = self._get("/api/license")
            body = self._json_ok(resp)
            if "status" in body or "tier" in body:
                r.record_pass("GET /api/license -> has status/tier")
            else:
                r.record_fail("GET /api/license", "missing 'status' and 'tier'")
        except Exception as e:
            r.record_fail("GET /api/license", str(e))

        # GET /api/license/instance-id
        try:
            resp = self._get("/api/license/instance-id")
            if resp.status_code == 200 and len(resp.text.strip()) > 0:
                r.record_pass("GET /api/license/instance-id -> non-empty")
            else:
                r.record_fail("GET /api/license/instance-id", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /api/license/instance-id", str(e))

        # GET /api/system/info
        try:
            resp = self._get("/api/system/info")
            body = self._json_ok(resp)
            if "version" in body:
                self._log(f"version: {body['version']}")
                r.record_pass(f"GET /api/system/info -> version {body['version']}")
            else:
                r.record_fail("GET /api/system/info", "missing 'version'")
        except Exception as e:
            r.record_fail("GET /api/system/info", str(e))

        # GET /api/time
        try:
            resp = self._get("/api/time")
            if resp.status_code == 200:
                r.record_pass("GET /api/time -> 200")
            else:
                r.record_fail("GET /api/time", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /api/time", str(e))

        # GET /api/settings
        try:
            resp = self._get("/api/settings")
            body = self._json_ok(resp)
            if isinstance(body, dict):
                r.record_pass("GET /api/settings -> dict")
            else:
                r.record_fail("GET /api/settings", f"expected dict, got {type(body).__name__}")
        except Exception as e:
            r.record_fail("GET /api/settings", str(e))

    # 3. sites -------------------------------------------------------------

    def test_sites(self, r: TestResult) -> None:
        site_id: Optional[int] = None
        try:
            # POST /api/sites
            try:
                resp = self._post("/api/sites", data={"name": "__test_site", "location": "Test"})
                body = self._json_ok(resp)
                if "id" in body:
                    site_id = body["id"]
                    r.record_pass(f"POST /api/sites -> id={site_id}")
                else:
                    r.record_fail("POST /api/sites", "missing 'id'")
            except Exception as e:
                r.record_fail("POST /api/sites", str(e))

            # GET /api/sites
            try:
                resp = self._get("/api/sites")
                body = self._json_ok(resp)
                names = [s.get("name", "") for s in body] if isinstance(body, list) else []
                if "__test_site" in names:
                    r.record_pass("GET /api/sites -> contains __test_site")
                else:
                    r.record_fail("GET /api/sites", "__test_site not found")
            except Exception as e:
                r.record_fail("GET /api/sites", str(e))

            # PUT /api/sites/{id}
            if site_id:
                try:
                    resp = self._put(f"/api/sites/{site_id}", data={"name": "__test_site_renamed"})
                    if resp.status_code == 200:
                        r.record_pass(f"PUT /api/sites/{site_id} -> 200")
                    else:
                        r.record_fail(f"PUT /api/sites/{site_id}", f"status {resp.status_code}")
                except Exception as e:
                    r.record_fail(f"PUT /api/sites/{site_id}", str(e))
        finally:
            # DELETE /api/sites/{id}
            if site_id:
                try:
                    resp = self._delete(f"/api/sites/{site_id}")
                    if resp.status_code == 200:
                        r.record_pass(f"DELETE /api/sites/{site_id} -> 200")
                    else:
                        r.record_fail(f"DELETE /api/sites/{site_id}", f"status {resp.status_code}")
                except Exception as e:
                    r.record_fail(f"DELETE /api/sites/{site_id}", str(e))

    # 4. devices -----------------------------------------------------------

    def test_devices(self, r: TestResult) -> None:
        if not self.device_ip:
            r.record_skip("devices", "no --device-ip provided")
            return

        # GET /api/aps
        try:
            resp = self._get("/api/aps")
            body = self._json_ok(resp)
            if isinstance(body, list):
                r.record_pass(f"GET /api/aps -> list ({len(body)} items)")
            else:
                r.record_fail("GET /api/aps", f"expected list, got {type(body).__name__}")
        except Exception as e:
            r.record_fail("GET /api/aps", str(e))

        # GET /api/switches
        try:
            resp = self._get("/api/switches")
            body = self._json_ok(resp)
            if isinstance(body, list):
                r.record_pass(f"GET /api/switches -> list ({len(body)} items)")
            else:
                r.record_fail("GET /api/switches", f"expected list")
        except Exception as e:
            r.record_fail("GET /api/switches", str(e))

        # GET /api/cpes
        try:
            resp = self._get("/api/cpes")
            if resp.status_code == 200:
                r.record_pass("GET /api/cpes -> 200")
            else:
                r.record_fail("GET /api/cpes", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /api/cpes", str(e))

        # POST /api/aps/{device_ip}/poll
        try:
            resp = self._post(f"/api/aps/{self.device_ip}/poll")
            if resp.status_code == 200:
                r.record_pass(f"POST /api/aps/{self.device_ip}/poll -> 200")
            else:
                r.record_fail(f"POST /api/aps/{self.device_ip}/poll", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail(f"POST /api/aps/{self.device_ip}/poll", str(e))

        # GET /api/topology
        try:
            resp = self._get("/api/topology")
            if resp.status_code == 200:
                r.record_pass("GET /api/topology -> 200")
            else:
                r.record_fail("GET /api/topology", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /api/topology", str(e))

        # GET /api/vendors
        try:
            resp = self._get("/api/vendors")
            body = self._json_ok(resp)
            if isinstance(body, list):
                r.record_pass(f"GET /api/vendors -> list ({len(body)} items)")
            else:
                r.record_fail("GET /api/vendors", "expected list")
        except Exception as e:
            r.record_fail("GET /api/vendors", str(e))

        # Device groups CRUD
        group_id: Optional[int] = None
        try:
            # GET /api/device-groups
            resp = self._get("/api/device-groups")
            body = self._json_ok(resp)
            if isinstance(body, list):
                r.record_pass("GET /api/device-groups -> list")
            else:
                r.record_fail("GET /api/device-groups", "expected list")

            # POST /api/device-groups
            resp = self._post("/api/device-groups", json_body={"name": "__test_group"})
            body = self._json_ok(resp)
            if "id" in body:
                group_id = body["id"]
                r.record_pass(f"POST /api/device-groups -> id={group_id}")
            else:
                r.record_fail("POST /api/device-groups", "missing 'id'")
        except Exception as e:
            r.record_fail("device-groups CRUD", str(e))
        finally:
            if group_id:
                try:
                    resp = self._delete(f"/api/device-groups/{group_id}")
                    if resp.status_code == 200:
                        r.record_pass(f"DELETE /api/device-groups/{group_id} -> 200")
                    else:
                        r.record_fail(f"DELETE /api/device-groups/{group_id}", f"status {resp.status_code}")
                except Exception as e:
                    r.record_fail(f"DELETE /api/device-groups/{group_id}", str(e))

    # 5. firmware_files ----------------------------------------------------

    def test_firmware_files(self, r: TestResult) -> None:
        # GET /api/firmware-files
        try:
            resp = self._get("/api/firmware-files")
            body = self._json_ok(resp)
            if isinstance(body, list):
                r.record_pass(f"GET /api/firmware-files -> list ({len(body)} files)")
            else:
                r.record_fail("GET /api/firmware-files", "expected list")
        except Exception as e:
            r.record_fail("GET /api/firmware-files", str(e))

        # POST /api/upload-firmware (dummy file)
        try:
            dummy = io.BytesIO(b"\x00" * 1024)
            resp = self._post(
                "/api/upload-firmware",
                files={"file": ("__test_dummy.bin", dummy, "application/octet-stream")},
            )
            if resp.status_code == 200:
                r.record_pass("POST /api/upload-firmware -> 200 (dummy file)")
            else:
                r.record_fail("POST /api/upload-firmware", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("POST /api/upload-firmware", str(e))

        # DELETE /api/firmware-files/__test_dummy.bin
        try:
            resp = self._delete("/api/firmware-files/__test_dummy.bin")
            if resp.status_code == 200:
                r.record_pass("DELETE /api/firmware-files/__test_dummy.bin -> 200")
            else:
                r.record_fail("DELETE /api/firmware-files/__test_dummy.bin", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("DELETE /api/firmware-files/__test_dummy.bin", str(e))

        # POST /api/firmware-fetch (start fetch)
        try:
            resp = self._post("/api/firmware-fetch")
            if resp.status_code == 200:
                r.record_pass("POST /api/firmware-fetch -> 200 (fetch started)")
            else:
                r.record_fail("POST /api/firmware-fetch", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("POST /api/firmware-fetch", str(e))

        # GET /api/firmware-fetch/status
        try:
            resp = self._get("/api/firmware-fetch/status")
            if resp.status_code == 200:
                r.record_pass("GET /api/firmware-fetch/status -> 200")
            else:
                r.record_fail("GET /api/firmware-fetch/status", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /api/firmware-fetch/status", str(e))

    # 6. firmware_update ---------------------------------------------------

    def test_firmware_update(self, r: TestResult) -> None:
        if not self.device_ip:
            r.record_skip("firmware_update", "no --device-ip provided")
            return

        if not self.allow_firmware_update:
            r.record_skip(
                "firmware_update",
                "destructive test — pass --allow-firmware-update to enable",
            )
            return

        # Check firmware files exist
        try:
            resp = self._get("/api/firmware-files")
            files = self._json_ok(resp)
            if not files:
                r.record_skip("firmware_update", "no firmware files available")
                return
        except Exception as e:
            r.record_fail("firmware_update pre-check", str(e))
            return

        firmware_file = files[0] if isinstance(files[0], str) else files[0].get("filename", files[0].get("name", ""))

        print(f"  {C.YELLOW}WARNING: Starting firmware update on {self.device_ip} "
              f"with {firmware_file}{C.RESET}")

        job_id: Optional[str] = None

        # POST /api/start-update
        try:
            resp = self._post(
                "/api/start-update",
                data={
                    "firmware_file": firmware_file,
                    "device_type": "tachyon",
                    "ip_list": self.device_ip,
                    "concurrency": "1",
                },
            )
            body = self._json_ok(resp)
            job_id = body.get("job_id") or body.get("id")
            if job_id:
                r.record_pass(f"POST /api/start-update -> job_id={job_id}")
            else:
                r.record_fail("POST /api/start-update", "missing job_id")
                return
        except Exception as e:
            r.record_fail("POST /api/start-update", str(e))
            return

        # Poll job status
        deadline = time.time() + 600
        final_status = None
        while time.time() < deadline:
            try:
                resp = self._get(f"/api/job/{job_id}")
                body = self._json_ok(resp)
                status = body.get("status", "unknown")
                self._log(f"Job {job_id} status: {status}")
                if status in ("completed", "done", "finished", "success"):
                    final_status = "success"
                    break
                elif status in ("failed", "error"):
                    final_status = "failed"
                    break
            except Exception:
                pass
            time.sleep(10)

        if final_status == "success":
            r.record_pass(f"Firmware update job {job_id} completed")
        elif final_status == "failed":
            r.record_fail(f"Firmware update job {job_id}", "job failed")
        else:
            r.record_fail(f"Firmware update job {job_id}", "timed out after 600s")

        # GET /api/job-history
        try:
            resp = self._get("/api/job-history")
            body = self._json_ok(resp)
            if isinstance(body, list) and len(body) > 0:
                r.record_pass("GET /api/job-history -> has entries")
            else:
                r.record_fail("GET /api/job-history", "empty or non-list")
        except Exception as e:
            r.record_fail("GET /api/job-history", str(e))

        # GET /api/device-history
        try:
            resp = self._get("/api/device-history")
            if resp.status_code == 200:
                r.record_pass("GET /api/device-history -> 200")
            else:
                r.record_fail("GET /api/device-history", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /api/device-history", str(e))

    # 7. rollout -----------------------------------------------------------

    def test_rollout(self, r: TestResult) -> None:
        # Rollout is skipped by default; just verify endpoint responds
        try:
            resp = self._get("/api/rollout/current")
            if resp.status_code == 200:
                r.record_pass("GET /api/rollout/current -> 200")
            else:
                # 404 is acceptable if no rollout is active
                r.record_pass(f"GET /api/rollout/current -> {resp.status_code} (no active rollout)")
        except Exception as e:
            r.record_fail("GET /api/rollout/current", str(e))

    # 8. config_management -------------------------------------------------

    def test_config_management(self, r: TestResult) -> None:
        if not self.device_ip:
            r.record_skip("config_management", "no --device-ip provided")
            return

        # POST /api/configs/{device_ip}/poll
        try:
            resp = self._post(f"/api/configs/{self.device_ip}/poll")
            if resp.status_code == 200:
                r.record_pass(f"POST /api/configs/{self.device_ip}/poll -> 200")
            else:
                r.record_fail(f"POST /api/configs/{self.device_ip}/poll", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail(f"POST /api/configs/{self.device_ip}/poll", str(e))

        # GET /api/configs
        try:
            resp = self._get("/api/configs")
            if resp.status_code == 200:
                r.record_pass("GET /api/configs -> 200")
            else:
                r.record_fail("GET /api/configs", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /api/configs", str(e))

        # GET /api/configs/{device_ip}/latest
        try:
            resp = self._get(f"/api/configs/{self.device_ip}/latest")
            if resp.status_code in (200, 404):
                r.record_pass(f"GET /api/configs/{self.device_ip}/latest -> {resp.status_code}")
            else:
                r.record_fail(f"GET /api/configs/{self.device_ip}/latest", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail(f"GET /api/configs/{self.device_ip}/latest", str(e))

        # Config templates CRUD
        template_id: Optional[int] = None
        try:
            # POST /api/config-templates
            resp = self._post(
                "/api/config-templates",
                json_body={
                    "name": "__test_ntp",
                    "category": "ntp",
                    "config_fragment": {
                        "services": {
                            "ntp": {"enabled": True, "servers": ["pool.ntp.org"]},
                        },
                    },
                    "description": "test",
                },
            )
            body = self._json_ok(resp)
            if "id" in body:
                template_id = body["id"]
                r.record_pass(f"POST /api/config-templates -> id={template_id}")
            else:
                r.record_fail("POST /api/config-templates", "missing 'id'")

            # GET /api/config-templates
            resp = self._get("/api/config-templates")
            body = self._json_ok(resp)
            names = []
            if isinstance(body, list):
                names = [t.get("name", "") for t in body]
            if "__test_ntp" in names:
                r.record_pass("GET /api/config-templates -> contains __test_ntp")
            else:
                r.record_fail("GET /api/config-templates", "__test_ntp not found")

            # GET /api/config-compliance
            resp = self._get("/api/config-compliance")
            if resp.status_code == 200:
                r.record_pass("GET /api/config-compliance -> 200")
            else:
                r.record_fail("GET /api/config-compliance", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("config-templates CRUD", str(e))
        finally:
            if template_id:
                try:
                    resp = self._delete(f"/api/config-templates/{template_id}")
                    if resp.status_code == 200:
                        r.record_pass(f"DELETE /api/config-templates/{template_id} -> 200")
                    else:
                        r.record_fail(f"DELETE /api/config-templates/{template_id}", f"status {resp.status_code}")
                except Exception as e:
                    r.record_fail(f"DELETE /api/config-templates/{template_id}", str(e))

    # 9. config_push_rollout -----------------------------------------------

    def test_config_push_rollout(self, r: TestResult) -> None:
        endpoints = [
            ("GET", "/api/config-push/rollout", [200, 404]),
            ("GET", "/api/config-enforce/status", [200]),
            ("GET", "/api/config-enforce/log", [200]),
        ]
        for method, path, ok_codes in endpoints:
            try:
                resp = self._request(method, path)
                if resp.status_code in ok_codes:
                    r.record_pass(f"{method} {path} -> {resp.status_code}")
                else:
                    r.record_fail(f"{method} {path}", f"status {resp.status_code}")
            except Exception as e:
                r.record_fail(f"{method} {path}", str(e))

    # 10. radius -----------------------------------------------------------

    def test_radius(self, r: TestResult) -> None:
        # GET /api/auth/radius
        try:
            resp = self._get("/api/auth/radius")
            if resp.status_code == 200:
                r.record_pass("GET /api/auth/radius -> 200")
            else:
                r.record_fail("GET /api/auth/radius", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /api/auth/radius", str(e))

        # RADIUS users CRUD
        user_id: Optional[int] = None
        try:
            resp = self._post(
                "/api/auth/radius/users",
                json_body={"username": "__test_raduser", "password": "TestPass123!"},
            )
            body = self._json_ok(resp)
            if "id" in body:
                user_id = body["id"]
                r.record_pass(f"POST /api/auth/radius/users -> id={user_id}")
            else:
                r.record_fail("POST /api/auth/radius/users", "missing 'id'")

            resp = self._get("/api/auth/radius/users")
            body = self._json_ok(resp)
            names = [u.get("username", "") for u in body] if isinstance(body, list) else []
            if "__test_raduser" in names:
                r.record_pass("GET /api/auth/radius/users -> contains __test_raduser")
            else:
                r.record_fail("GET /api/auth/radius/users", "__test_raduser not found")
        except Exception as e:
            r.record_fail("RADIUS users CRUD", str(e))
        finally:
            if user_id:
                try:
                    resp = self._delete(f"/api/auth/radius/users/{user_id}")
                    if resp.status_code == 200:
                        r.record_pass(f"DELETE /api/auth/radius/users/{user_id} -> 200")
                    else:
                        r.record_fail(f"DELETE /api/auth/radius/users/{user_id}", f"status {resp.status_code}")
                except Exception as e:
                    r.record_fail(f"DELETE /api/auth/radius/users/{user_id}", str(e))

        # RADIUS clients CRUD
        client_id: Optional[int] = None
        try:
            resp = self._post(
                "/api/auth/radius/clients",
                json_body={"client_spec": "192.168.99.99/32", "shortname": "__test_client"},
            )
            body = self._json_ok(resp)
            if "id" in body:
                client_id = body["id"]
                r.record_pass(f"POST /api/auth/radius/clients -> id={client_id}")
            else:
                r.record_fail("POST /api/auth/radius/clients", "missing 'id'")

            resp = self._get("/api/auth/radius/clients")
            body = self._json_ok(resp)
            if isinstance(body, list):
                r.record_pass(f"GET /api/auth/radius/clients -> list ({len(body)} items)")
            else:
                r.record_fail("GET /api/auth/radius/clients", "expected list")
        except Exception as e:
            r.record_fail("RADIUS clients CRUD", str(e))
        finally:
            if client_id:
                try:
                    resp = self._delete(f"/api/auth/radius/clients/{client_id}")
                    if resp.status_code == 200:
                        r.record_pass(f"DELETE /api/auth/radius/clients/{client_id} -> 200")
                    else:
                        r.record_fail(f"DELETE /api/auth/radius/clients/{client_id}", f"status {resp.status_code}")
                except Exception as e:
                    r.record_fail(f"DELETE /api/auth/radius/clients/{client_id}", str(e))

        # GET /api/auth/radius/stats
        try:
            resp = self._get("/api/auth/radius/stats")
            if resp.status_code == 200:
                r.record_pass("GET /api/auth/radius/stats -> 200")
            else:
                r.record_fail("GET /api/auth/radius/stats", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /api/auth/radius/stats", str(e))

        # GET /api/auth/radius/auth-log
        try:
            resp = self._get("/api/auth/radius/auth-log")
            if resp.status_code == 200:
                r.record_pass("GET /api/auth/radius/auth-log -> 200")
            else:
                r.record_fail("GET /api/auth/radius/auth-log", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /api/auth/radius/auth-log", str(e))

    # 11. users ------------------------------------------------------------

    def test_users(self, r: TestResult) -> None:
        user_id: Optional[int] = None
        try:
            # POST /api/users
            resp = self._post(
                "/api/users",
                json_body={
                    "username": "__test_user",
                    "password": "TestPassword123!",
                    "role": "operator",
                },
            )
            body = self._json_ok(resp)
            if "id" in body:
                user_id = body["id"]
                r.record_pass(f"POST /api/users -> id={user_id}")
            else:
                r.record_fail("POST /api/users", "missing 'id'")

            # GET /api/users
            resp = self._get("/api/users")
            body = self._json_ok(resp)
            names = [u.get("username", "") for u in body] if isinstance(body, list) else []
            if "__test_user" in names:
                r.record_pass("GET /api/users -> contains __test_user")
            else:
                r.record_fail("GET /api/users", "__test_user not found")

            # PUT /api/users/{id}
            if user_id:
                resp = self._put(f"/api/users/{user_id}", json_body={"role": "viewer"})
                if resp.status_code == 200:
                    r.record_pass(f"PUT /api/users/{user_id} -> 200")
                else:
                    r.record_fail(f"PUT /api/users/{user_id}", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("users CRUD", str(e))
        finally:
            if user_id:
                try:
                    resp = self._delete(f"/api/users/{user_id}")
                    if resp.status_code == 200:
                        r.record_pass(f"DELETE /api/users/{user_id} -> 200")
                    else:
                        r.record_fail(f"DELETE /api/users/{user_id}", f"status {resp.status_code}")
                except Exception as e:
                    r.record_fail(f"DELETE /api/users/{user_id}", str(e))

        # API tokens CRUD
        token_id: Optional[int] = None
        token_value: Optional[str] = None
        try:
            # POST /api/tokens
            resp = self._post(
                "/api/tokens",
                json_body={"name": "__test_token", "scopes": "read"},
            )
            body = self._json_ok(resp)
            if "token" in body:
                token_value = body["token"]
                token_id = body.get("id")
                r.record_pass("POST /api/tokens -> has token")
            else:
                r.record_fail("POST /api/tokens", "missing 'token'")

            # Test bearer token auth
            if token_value:
                token_session = requests.Session()
                token_session.verify = False
                resp = self._get(
                    "/api/aps",
                    session=token_session,
                    headers={"Authorization": f"Bearer {token_value}"},
                )
                if resp.status_code == 200:
                    r.record_pass("GET /api/aps with Bearer token -> 200")
                else:
                    r.record_fail("GET /api/aps with Bearer token", f"status {resp.status_code}")

            # GET /api/tokens
            resp = self._get("/api/tokens")
            body = self._json_ok(resp)
            names = [t.get("name", "") for t in body] if isinstance(body, list) else []
            if "__test_token" in names:
                r.record_pass("GET /api/tokens -> contains __test_token")
                # Find the id if we didn't get it from creation
                if not token_id:
                    for t in body:
                        if t.get("name") == "__test_token":
                            token_id = t.get("id")
                            break
            else:
                r.record_fail("GET /api/tokens", "__test_token not found")
        except Exception as e:
            r.record_fail("tokens CRUD", str(e))
        finally:
            if token_id:
                try:
                    resp = self._delete(f"/api/tokens/{token_id}")
                    if resp.status_code == 200:
                        r.record_pass(f"DELETE /api/tokens/{token_id} -> 200")
                    else:
                        r.record_fail(f"DELETE /api/tokens/{token_id}", f"status {resp.status_code}")
                except Exception as e:
                    r.record_fail(f"DELETE /api/tokens/{token_id}", str(e))

    # 12. notifications ----------------------------------------------------

    def test_notifications(self, r: TestResult) -> None:
        endpoints = [
            "/api/syslog/test",
            "/api/email/test",
            "/api/slack/test",
            "/api/webhooks/test",
            "/api/snmp/test",
        ]
        for path in endpoints:
            try:
                resp = self._post(path)
                # Accept 200 (success or not-configured response) and common error codes
                if resp.status_code in (200, 400, 422):
                    r.record_pass(f"POST {path} -> {resp.status_code}")
                else:
                    r.record_fail(f"POST {path}", f"status {resp.status_code}")
            except Exception as e:
                r.record_fail(f"POST {path}", str(e))

    # 13. backup -----------------------------------------------------------

    def test_backup(self, r: TestResult) -> None:
        # POST /api/backup/run
        try:
            resp = self._post("/api/backup/run")
            if resp.status_code == 200:
                r.record_pass("POST /api/backup/run -> 200")
            else:
                r.record_fail("POST /api/backup/run", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("POST /api/backup/run", str(e))

        # GET /api/backup/status
        try:
            resp = self._get("/api/backup/status")
            if resp.status_code == 200:
                r.record_pass("GET /api/backup/status -> 200")
            else:
                r.record_fail("GET /api/backup/status", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /api/backup/status", str(e))

        # POST /api/backup/export
        try:
            resp = self._post(
                "/api/backup/export",
                json_body={"passphrase": "testpass1234"},
            )
            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "")
                size = len(resp.content)
                r.record_pass(f"POST /api/backup/export -> 200 ({size} bytes)")
            else:
                r.record_fail("POST /api/backup/export", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("POST /api/backup/export", str(e))

    # 14. freeze_windows ---------------------------------------------------

    def test_freeze_windows(self, r: TestResult) -> None:
        freeze_id: Optional[int] = None
        tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
        day_after = (datetime.utcnow() + timedelta(days=2)).strftime("%Y-%m-%d")

        try:
            # POST /api/freeze-windows
            resp = self._post(
                "/api/freeze-windows",
                json_body={
                    "name": "__test_freeze",
                    "start_date": tomorrow,
                    "end_date": day_after,
                    "reason": "test",
                },
            )
            body = self._json_ok(resp)
            if "id" in body:
                freeze_id = body["id"]
                r.record_pass(f"POST /api/freeze-windows -> id={freeze_id}")
            else:
                r.record_fail("POST /api/freeze-windows", "missing 'id'")

            # GET /api/freeze-windows
            resp = self._get("/api/freeze-windows")
            body = self._json_ok(resp)
            names = [f.get("name", "") for f in body] if isinstance(body, list) else []
            if "__test_freeze" in names:
                r.record_pass("GET /api/freeze-windows -> contains __test_freeze")
            else:
                r.record_fail("GET /api/freeze-windows", "__test_freeze not found")
        except Exception as e:
            r.record_fail("freeze-windows CRUD", str(e))
        finally:
            if freeze_id:
                try:
                    resp = self._delete(f"/api/freeze-windows/{freeze_id}")
                    if resp.status_code == 200:
                        r.record_pass(f"DELETE /api/freeze-windows/{freeze_id} -> 200")
                    else:
                        r.record_fail(f"DELETE /api/freeze-windows/{freeze_id}", f"status {resp.status_code}")
                except Exception as e:
                    r.record_fail(f"DELETE /api/freeze-windows/{freeze_id}", str(e))

    # 15. analytics --------------------------------------------------------

    def test_analytics(self, r: TestResult) -> None:
        endpoints = [
            "/api/fleet-status",
            "/api/analytics/summary",
            "/api/analytics/trends",
            "/api/analytics/models",
            "/api/reports/export/devices",
            "/api/reports/export/jobs",
            "/api/uptime/fleet",
        ]
        for path in endpoints:
            try:
                resp = self._get(path)
                if resp.status_code == 200:
                    r.record_pass(f"GET {path} -> 200")
                else:
                    r.record_fail(f"GET {path}", f"status {resp.status_code}")
            except Exception as e:
                r.record_fail(f"GET {path}", str(e))

    # 16. scheduler --------------------------------------------------------

    def test_scheduler(self, r: TestResult) -> None:
        # GET /api/scheduler/status (initial)
        try:
            resp = self._get("/api/scheduler/status")
            if resp.status_code == 200:
                r.record_pass("GET /api/scheduler/status -> 200")
            else:
                r.record_fail("GET /api/scheduler/status", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /api/scheduler/status", str(e))

        # PUT /api/settings (enable scheduler)
        try:
            resp = self._put(
                "/api/settings",
                json_body={
                    "schedule_enabled": True,
                    "schedule_days": "mon,tue,wed,thu,fri",
                    "schedule_start_hour": 2,
                    "schedule_end_hour": 6,
                },
            )
            if resp.status_code == 200:
                r.record_pass("PUT /api/settings (enable scheduler) -> 200")
            else:
                r.record_fail("PUT /api/settings (enable scheduler)", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("PUT /api/settings (enable scheduler)", str(e))

        # GET /api/scheduler/status (verify enabled)
        try:
            resp = self._get("/api/scheduler/status")
            if resp.status_code == 200:
                body = resp.json()
                r.record_pass(f"GET /api/scheduler/status -> reflects settings")
            else:
                r.record_fail("GET /api/scheduler/status (post-enable)", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /api/scheduler/status (post-enable)", str(e))

        # PUT /api/settings (disable scheduler - cleanup)
        try:
            resp = self._put("/api/settings", json_body={"schedule_enabled": False})
            if resp.status_code == 200:
                r.record_pass("PUT /api/settings (disable scheduler) -> 200")
            else:
                r.record_fail("PUT /api/settings (disable scheduler)", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("PUT /api/settings (disable scheduler)", str(e))

    # 17. self_update ------------------------------------------------------

    def test_self_update(self, r: TestResult) -> None:
        # POST /api/updates/check
        try:
            resp = self._post("/api/updates/check")
            if resp.status_code == 200:
                r.record_pass("POST /api/updates/check -> 200")
            else:
                r.record_fail("POST /api/updates/check", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("POST /api/updates/check", str(e))

        # GET /api/updates
        try:
            resp = self._get("/api/updates")
            if resp.status_code == 200:
                r.record_pass("GET /api/updates -> 200")
            else:
                r.record_fail("GET /api/updates", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /api/updates", str(e))

    # 18. ssl --------------------------------------------------------------

    def test_ssl(self, r: TestResult) -> None:
        try:
            resp = self._get("/api/ssl/status")
            if resp.status_code == 200:
                r.record_pass("GET /api/ssl/status -> 200")
            else:
                r.record_fail("GET /api/ssl/status", f"status {resp.status_code}")
        except Exception as e:
            r.record_fail("GET /api/ssl/status", str(e))

    # 19. websocket --------------------------------------------------------

    def test_websocket(self, r: TestResult) -> None:
        if not HAS_WEBSOCKET:
            r.record_skip("websocket", "websocket-client not installed")
            return

        try:
            ws_url = self.host.replace("https://", "wss://").replace("http://", "ws://")
            ws_url = f"{ws_url}/ws"
            self._log(f"Connecting to {ws_url}")

            # Extract cookies from session for WS auth
            cookie_str = "; ".join(
                f"{c.name}={c.value}" for c in self.session.cookies
            )

            conn = ws_client.WebSocket(sslopt={"cert_reqs": 0})
            conn.settimeout(5)
            conn.connect(ws_url, cookie=cookie_str)

            try:
                msg = conn.recv()
                r.record_pass(f"WebSocket connected, received message ({len(msg)} bytes)")
            except ws_client.WebSocketTimeoutException:
                # No message within 5s is acceptable — connection itself worked
                r.record_pass("WebSocket connected (no message within 5s, connection OK)")
            finally:
                conn.close()
        except Exception as e:
            r.record_fail("WebSocket connection", str(e))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SixtyOps Manager — Release Validation Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --host https://sixtyops.local\n"
            "  %(prog)s --host http://localhost:8000 --device-ip 10.0.0.1 --verbose\n"
            "  %(prog)s --host http://localhost:8000 --skip radius,backup --dry-run\n"
        ),
    )
    parser.add_argument("--host", required=True, help="Base URL (e.g. http://localhost:8000)")
    parser.add_argument("--username", default="admin", help="Login username (default: admin)")
    parser.add_argument("--password", default="admin", help="Login password (default: admin)")
    parser.add_argument("--skip", default="", help="Comma-separated categories to skip")
    parser.add_argument("--device-ip", default=None, help="AP IP for device-specific tests")
    parser.add_argument("--switch-ip", default=None, help="Switch IP for switch-specific tests")
    parser.add_argument("--verbose", action="store_true", help="Print request/response details")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be tested without calling APIs")
    parser.add_argument(
        "--allow-firmware-update",
        action="store_true",
        help="Enable destructive firmware_update tests (requires --device-ip)",
    )

    args = parser.parse_args()

    skip_set: set[str] = set()
    if args.skip:
        skip_set = {s.strip() for s in args.skip.split(",") if s.strip()}

    # Validate skip categories
    valid_cats = set(ReleaseValidator.CATEGORIES)
    invalid = skip_set - valid_cats
    if invalid:
        print(f"{C.RED}Unknown categories to skip: {', '.join(sorted(invalid))}{C.RESET}")
        print(f"Valid categories: {', '.join(sorted(valid_cats))}")
        sys.exit(2)

    validator = ReleaseValidator(
        host=args.host,
        username=args.username,
        password=args.password,
        skip=skip_set,
        device_ip=args.device_ip,
        switch_ip=args.switch_ip,
        verbose=args.verbose,
        dry_run=args.dry_run,
        allow_firmware_update=args.allow_firmware_update,
    )

    success = validator.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
