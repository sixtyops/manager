# Architecture

## Overview

Charlotte is a FastAPI application with an async-first architecture. The backend is Python, the frontend is server-rendered HTML with vanilla JavaScript, and real-time updates flow over WebSocket. Device-admin RADIUS authentication is provided by a bundled FreeRADIUS container that is managed by the app.

```
┌─────────────────────────────────────┐
│     Browser (HTML/JS Templates)     │
│  login.html │ monitor.html │ setup  │
└──────────────┬──────────────────────┘
               │ HTTP + WebSocket
┌──────────────▼──────────────────────┐
│       FastAPI App (app.py)          │
│  REST API │ WebSocket │ Templates   │
└──┬────┬───────┬───────┬─────┬───┬────┘
   │    │       │       │     │   │
┌──▼──┐ │ ┌────▼───┐ ┌─▼───┐ │ ┌─▼──────────────┐
│Tele-│ │ │Schedul-│ │Poll-│ │ │License Validator│
│metry│ │ │er      │ │er   │ │ │license.py       │
└──┬──┘ │ └───┬────┘ └──┬──┘ │ └───────┬─────────┘
   │    │     │          │    │         │ HTTPS (every 24h)
   ▼    ▼     │          │    │         ▼
 AWS  Slack   │          │    │   License Server
Lambda Webhook│          │    │   (cloud-hosted)
       ┌──────▼──────────▼──┐ │         │
       │  SQLite (database) │ │         ▼
       └──────┬─────────────┘ │       Stripe
              │               │   HTTPS/curl
              ▼               ▼
      FreeRADIUS Container  Network Devices
     (generated config, UDP 1812)
```

## Modules

### `app.py` - HTTP and WebSocket Server

The main FastAPI application. Handles all routing, serves HTML templates, manages WebSocket connections, and coordinates update jobs.

Key responsibilities:
- 30+ REST endpoints for sites, APs, CPEs, firmware, updates, settings, rollouts
- WebSocket broadcast to all connected clients
- Update job orchestration with phase-based device ordering
- App lifespan management (starts/stops poller, scheduler, release checks, and Radius log sync)
- Generates and reloads FreeRADIUS config for built-in device-admin authentication
- Monitors Radius container health and attempts automatic recovery when Docker marks it unhealthy

### `tachyon.py` - Device Communication

Low-level client for Tachyon hardware. Communicates over HTTPS using `curl` subprocesses (required for device SSL compatibility).

Update sequence per device:
1. Login and get session token
2. Fetch device info (model, firmware version, MAC, bank status)
3. Upload firmware file via multipart POST
4. Trigger installation with optional force/reset flags
5. Wait for reboot (poll until device responds)
6. Verify new firmware version

### `scheduler.py` - Automatic Update Scheduler

Background task that checks every 60 seconds whether to start an update. Enforces safety conditions before running:

- Schedule enabled and within configured day/time window
- System clock validated against NTP sources
- Weather conditions acceptable (optional temperature check)
- Firmware files selected
- Not already ran today

Manages gradual rollout progression across consecutive schedule windows.

### `poller.py` - Network Discovery

Background task that polls all APs every 60 seconds. Discovers connected CPEs, collects signal metrics, and broadcasts topology updates over WebSocket. Polls up to 5 APs concurrently.

### `database.py` - Data Layer

SQLite database with schema creation, migrations, and CRUD helpers for inventory, jobs, auth, config backups, and Radius state. Radius-related tables include `radius_users`, `radius_auth_log`, and `radius_client_overrides`.

### `auth.py` - Authentication

Web login authentication for the management UI. Supports local username/password and optional OIDC SSO. Sessions are stored in SQLite with a 24-hour TTL and validated for both HTTP and WebSocket requests.

### `builtin_radius.py` - Device Admin RADIUS Control Plane

Application-side management for the bundled FreeRADIUS service used by APs, switches, and other managed devices.

Key responsibilities:
- Stores built-in Radius users, auth history, and manual client overrides in SQLite
- Generates `clients.conf` and `mods-config/files/authorize` under `data/radius/`
- Reloads the `tachyon-radius` container when users or settings change
- Syncs FreeRADIUS auth results back into SQLite for stats and audit history
- Enforces reserved usernames (`admin` and `root`) for device-admin Radius accounts

### `models.py` - Data Models

Pydantic models for API validation: `Device`, `CPEInfo`, `APWithCPEs`, `NetworkTopology`. Enums for `DeviceType` and `SignalHealth`.

### `services.py` - External Integrations

Helpers for IP geolocation, weather forecasts (weather.gov), timezone detection, and NTP time validation.

### `telemetry.py` - Anonymous Usage Telemetry

Sends anonymized job statistics to an AWS Lambda endpoint after each update job completes. Runs as a fire-and-forget background task that never blocks the main flow.

**What is sent:** event type, timestamp, anonymous install ID (hashed), device counts (success/failed/skipped/cancelled), success rate, duration, bank mode, scheduled vs manual, device model distribution, categorized error counts (timeout, connection, auth, upload, install, reboot, verification), and per-role (AP/CPE/switch) breakdowns.

**What is never sent:** IP addresses, MAC addresses, hostnames, credentials, location, or raw error messages.

**Disabling telemetry:** Set the `DISABLE_TELEMETRY=1` environment variable (e.g., in `docker-compose.yml`), then restart the app.

### `slack.py` - Slack Notifications

Sends rich webhook notifications on job completion with success/failure counts, failed device details, rollout phase progress, and next scheduled job info. Configured via `slack_webhook_url` in settings.

### `license.py` - Licensing and Feature Gating

Client-side license management. Validates license keys against a remote license server and gates PRO features based on subscription status.

Key concepts:
- **Two tiers**: FREE and PRO. All features in the `Feature` enum require PRO.
- **License key validation**: Posts `{ license_key, device_count, app_version }` to the license server's `/validate` endpoint. The server responds with `{ valid, customer_name, expires_at, device_limit, error }`.
- **Offline resilience**: 7-day grace period when the license server is unreachable. Previously-active licenses continue working during the grace window.
- **Background re-validation**: `LicenseValidator` re-checks the license every 24 hours (configurable via `LICENSE_CHECK_INTERVAL` env var) and broadcasts state changes over WebSocket.
- **Device counting**: Only enabled APs and switches are billable. CPEs are free. The billable count is reported to the license server on every validation call.
- **Free-tier nag**: A soft banner appears when free-tier users exceed 10 billable devices.
- **Migration**: Existing deployments that were set up before licensing was introduced receive a 30-day PRO trial.
- **Dev override**: `TACHYON_FORCE_PRO=1` bypasses all gating for development and testing.

FastAPI dependencies `require_pro()` and `require_feature(feature)` protect individual API endpoints, returning 403 with upgrade URLs when the license is insufficient.

### `release_checker.py` - Self-Update

Background service that checks GitHub Releases API for new versions. Compares the current `__version__` against the latest release tag. When a newer version is found, broadcasts a notification over WebSocket. Admins can apply the update from the Settings UI, which pulls the latest Docker image and recreates the container.

Respects appliance version compatibility: if a release's notes contain `<!-- min_appliance_version: X.Y -->`, the update is blocked on appliances running an older platform version.

## Licensing and Subscription Architecture

### Overview

The application uses a license-key model with a remote license server for validation and Stripe for payment processing. The three components are independent services:

```
┌────────────────────────┐     ┌──────────────────────────┐     ┌─────────────┐
│  Firmware Updater      │     │  License Server           │     │  Stripe     │
│  (on-prem, customer)   │────▶│  (cloud, your control)    │────▶│  (SaaS)     │
│                        │     │                           │     │             │
│  license.py            │     │  /api/v1/validate         │     │  Billing    │
│  - Stores key locally  │     │  /api/v1/stripe/webhook   │     │  Checkout   │
│  - Validates every 24h │     │  /checkout, /pricing      │     │  Portal     │
│  - 7-day grace period  │     │  /billing/portal          │     │  Webhooks   │
│  - Feature gating      │     │                           │     │             │
└────────────────────────┘     └──────────────────────────┘     └─────────────┘
       SQLite (settings)              PostgreSQL                   Hosted by
       stores license state           stores licenses,             Stripe
                                      customers, usage
```

### Tiers and Feature Gating

Two tiers: **FREE** and **PRO**. All gated features are defined in the `Feature` enum in `license.py`. Any feature not listed in the enum is free by default.

PRO-only features:
- `update_single_device` — Manual per-device updates
- `sso_oidc` — SSO/OIDC authentication
- `radius_auth` — Built-in device-admin Radius
- `config_backup`, `config_templates`, `config_compliance`, `config_push` — Configuration management suite
- `slack_notifications` — Slack integration
- `device_portal` — Per-device detail portal
- `device_history` — Update history tracking
- `tower_sites` — Site/location management
- `beta_firmware` — Beta firmware channel access
- `firmware_hold_custom` — Custom firmware hold periods

### Pricing Model

Per-device metered billing. Only enabled APs and switches are billable; CPEs are free. The firmware updater reports the current billable device count to the license server on every 24-hour validation call. The license server forwards this count to Stripe as a metered usage record.

Stripe aggregation uses `last_during_period` — the most recent device count reported during the billing cycle determines the charge. This avoids penalizing temporary spikes (e.g., test devices added and removed).

### License Key Lifecycle

```
Customer → Pricing Page → Stripe Checkout → Payment
                                                │
                              Stripe webhook ◀──┘
                              (checkout.session.completed)
                                                │
                              License server ◀──┘
                              - Creates customer record
                              - Generates license key (XXXX-XXXX-XXXX-XXXX)
                              - Links to Stripe subscription
                              - Emails key to customer
                                                │
                              Success page ◀────┘
                              - Displays license key
                              - Instructions to activate
                                                │
Customer pastes key in       ◀──────────────────┘
  Settings > License > Activate
       │
       ▼
Firmware updater calls POST /api/v1/validate
  { license_key, device_count, app_version }
       │
       ▼
License server checks DB + subscription status
  → { valid: true, customer_name, expires_at, device_limit }
       │
       ▼
PRO features unlocked
```

### Validation Protocol

The firmware updater's `validate_license()` function (in `license.py`) sends a POST request to the license server:

**Request:** `POST {LICENSE_SERVER_URL}/validate`
```json
{
  "license_key": "A3K9-MN2P-X7HQ-R4WJ",
  "device_count": 47,
  "app_version": "1.2.0"
}
```

**Success response:**
```json
{
  "valid": true,
  "customer_name": "Acme Wireless ISP",
  "expires_at": "2026-03-15T00:00:00Z",
  "device_limit": null,
  "error": ""
}
```

**Failure response:**
```json
{
  "valid": false,
  "customer_name": "",
  "expires_at": "",
  "device_limit": 0,
  "error": "Subscription expired"
}
```

The `LICENSE_SERVER_URL` defaults to `https://license.sixtyops.net/api/v1` and is configurable via environment variable.

### Offline Resilience

The license state is persisted in the SQLite `settings` table so the application never blocks on network calls:

1. **Cache warm:** License state is loaded from DB on first access, then served from memory.
2. **Server unreachable:** If validation fails due to a network error and the license was previously `ACTIVE`, the status transitions to `GRACE` with a 7-day deadline.
3. **Grace period:** During grace, all PRO features remain available. If the server becomes reachable again within 7 days, the license is re-validated normally.
4. **Grace expired:** After 7 days without successful validation, the status transitions to `EXPIRED` and PRO features are disabled.

State transitions:
```
FREE ──(activate)──▶ ACTIVE ──(server down)──▶ GRACE ──(7 days)──▶ EXPIRED
                       ▲                         │
                       └──(server back up)────────┘
                     ACTIVE ──(invalid key)──▶ INVALID
                     ACTIVE ──(payment fail)──▶ past_due response ──▶ client GRACE
```

### Subscription Lifecycle (Stripe Webhooks)

The license server handles these Stripe webhook events:

| Event | Action |
|-------|--------|
| `checkout.session.completed` | Create customer + generate license key + link subscription |
| `invoice.payment_succeeded` | Confirm license active, update expiry |
| `invoice.payment_failed` | Set license to `past_due` (client's 7-day grace kicks in) |
| `customer.subscription.updated` | Update license status and expiry |
| `customer.subscription.deleted` | Mark license as cancelled |

### License Server Stack

The license server is a separate service (separate repository, cloud-deployed):

- **Framework:** FastAPI
- **Database:** PostgreSQL
- **ORM:** SQLAlchemy 2.0 + Alembic migrations
- **Payments:** Stripe Python SDK (checkout, webhooks, metered usage, customer portal)
- **Email:** Transactional email for license key delivery
- **Hosting:** Managed platform (Fly.io, Railway, or similar)

Database tables: `customers`, `licenses`, `usage_reports`, `validation_log`, `stripe_events`.

### License Server Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/validate` | POST | License validation (called by firmware updater every 24h) |
| `/api/v1/stripe/webhook` | POST | Stripe webhook receiver |
| `/api/v1/checkout/session` | POST | Create Stripe Checkout session |
| `/checkout/success` | GET | Post-purchase page displaying license key |
| `/pricing` | GET | Public pricing page |
| `/billing/portal` | GET | Redirect to Stripe Customer Portal |
| `/api/v1/admin/*` | GET/POST | Admin API for customer/license management |

### Billing Portal

Customers manage their subscription (update payment method, view invoices, cancel) through Stripe's hosted Customer Portal. The "Manage Billing" link in the firmware updater's Settings UI redirects to the license server's `/billing/portal` endpoint, which looks up the customer by license key and creates a Stripe portal session.

### Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `LICENSE_SERVER_URL` | `https://license.sixtyops.net/api/v1` | License server base URL |
| `LICENSE_CHECK_INTERVAL` | `86400` (24h) | Re-validation interval in seconds |
| `TACHYON_FORCE_PRO` | (unset) | Set to `1` to bypass all license gating (dev/test only) |

## Key Design Decisions

**Async everywhere** - All I/O uses asyncio. Device communication runs curl as async subprocesses. Database calls are synchronous but fast (local SQLite).

**WebSocket broadcast** - All connected clients receive every status update. The server maintains a set of active WebSocket connections and broadcasts to all on any state change.

**Phase-based updates** - Devices are grouped into phases (CPEs first, then APs, then second bank pass) to maintain network connectivity during updates.

**Gradual rollout** - Scheduled updates use a canary pattern. The first night updates 1 AP to learn the target firmware version. Subsequent nights scale to 10%, 50%, and 100%. Any failure pauses the rollout.

**curl for device communication** - Tachyon devices require specific TLS handling that is simplest to achieve with curl's `-k` flag for self-signed certs.

## Frontend

Six HTML templates rendered by Jinja2:
- `login.html` - Authentication form
- `monitor.html` - Main UI with firmware, update, auto-update, and network topology
- `setup.html` - Initial site configuration
- `setup_wizard.html` - Guided first-run setup
- `backup_setup.html` - Backup configuration
- `ssl_setup.html` - SSL certificate setup

JavaScript in `static/js/` handles WebSocket connections and real-time DOM updates. No build step or framework dependencies.
