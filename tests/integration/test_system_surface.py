"""Blocking live-dev coverage for shared read-heavy product surfaces."""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.dev_blocking]


def test_dashboard_page_loads(session):
    resp = session.get("/")
    assert resp.status_code == 200
    assert "SixtyOps" in resp.text
    assert "deviceTable" in resp.text


def test_feature_and_system_endpoints(session, request_with_retry):
    license_resp = request_with_retry("GET", "/api/license")
    assert license_resp.status_code == 200
    license_data = license_resp.json()
    assert license_data["is_pro"] is True

    features_resp = request_with_retry("GET", "/api/features")
    assert features_resp.status_code == 200
    feature_data = features_resp.json()
    assert all(feature_data["features"].values())

    system_resp = request_with_retry("GET", "/api/system/info")
    assert system_resp.status_code == 200
    assert system_resp.json().get("version")

    auth_resp = request_with_retry("GET", "/api/auth/config")
    assert auth_resp.status_code == 200
    auth_data = auth_resp.json()
    assert "radius" in auth_data
    assert "oidc" in auth_data
    assert "device_defaults" in auth_data


def test_device_portal_surfaces_ap_switch_and_cpe(session, test_ap, test_switch, test_cpe, portal_credentials):
    for device in (test_ap, test_switch, test_cpe):
        resp = session.get(f"/api/device-portal/{device['ip']}")
        assert resp.status_code == 200
        assert "cgi.lua/login" in resp.text
        username, password = portal_credentials(device["ip"])
        assert username
        assert password


def test_reporting_and_analytics_endpoints(session, test_ap, request_with_retry):
    ip = test_ap["ip"]

    history_resp = request_with_retry("GET", "/api/device-history", params={"ip": ip})
    assert history_resp.status_code == 200
    history_data = history_resp.json()
    assert "history" in history_data

    jobs_resp = request_with_retry("GET", "/api/job-history")
    assert jobs_resp.status_code == 200
    assert "jobs" in jobs_resp.json()

    update_summary = request_with_retry("GET", "/api/reports/update-summary")
    assert update_summary.status_code == 200

    fleet_status = request_with_retry("GET", "/api/reports/fleet-status")
    assert fleet_status.status_code == 200

    jobs_csv = request_with_retry("GET", "/api/reports/export/jobs")
    assert jobs_csv.status_code == 200
    assert "csv" in jobs_csv.headers.get("content-type", "").lower()

    devices_csv = request_with_retry("GET", "/api/reports/export/devices")
    assert devices_csv.status_code == 200
    assert "csv" in devices_csv.headers.get("content-type", "").lower()

    analytics_summary = request_with_retry("GET", "/api/analytics/summary", params={"days": 30})
    assert analytics_summary.status_code == 200

    analytics_trends = request_with_retry("GET", "/api/analytics/trends", params={"days": 30})
    assert analytics_trends.status_code == 200
    assert "trends" in analytics_trends.json()

    analytics_models = request_with_retry("GET", "/api/analytics/models", params={"days": 90})
    assert analytics_models.status_code == 200
    assert "models" in analytics_models.json()

    analytics_errors = request_with_retry("GET", "/api/analytics/errors", params={"days": 90, "limit": 10})
    assert analytics_errors.status_code == 200
    assert "errors" in analytics_errors.json()

    analytics_reliability = request_with_retry("GET", "/api/analytics/reliability", params={"days": 90, "limit": 20})
    assert analytics_reliability.status_code == 200
    assert "devices" in analytics_reliability.json()

    uptime_device = request_with_retry("GET", "/api/uptime/device", params={"ip": ip, "days": 30})
    assert uptime_device.status_code == 200

    uptime_fleet = request_with_retry("GET", "/api/uptime/fleet", params={"device_type": "ap", "days": 30})
    assert uptime_fleet.status_code == 200
    assert "devices" in uptime_fleet.json()

    uptime_events = request_with_retry("GET", "/api/uptime/events", params={"ip": ip, "days": 30, "limit": 25})
    assert uptime_events.status_code == 200
    assert "events" in uptime_events.json()
