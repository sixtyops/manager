# Built-in RADIUS

The application includes a built-in RADIUS server (pyrad) for device-admin authentication. This is intended for managed devices such as APs and switches authenticating to this system over UDP `1812`.

This feature is separate from management UI login:

- Web login uses local username/password and optional OIDC SSO
- Built-in RADIUS is for end devices authenticating against this appliance

## Architecture

The RADIUS server runs in-process alongside the FastAPI app — no separate container needed.

- `updater/radius_server.py` — pyrad-based RADIUS server (`SixtyOpsRadiusServer`)
- `updater/radius_config.py` — configuration and auth config summary
- `updater/radius_rollout.py` — staged device migration to RADIUS

Users, settings, client overrides, and auth history are stored in SQLite. The server reads config directly from the database — no generated config files.

### Client Modes

- **Open** (default) — accepts authentication from any device that presents the correct shared secret. A single wildcard NAS client (`0.0.0.0`) is registered.
- **Restricted** — only allows authentication from inventory IPs plus manual client overrides. Use this when you want IP-based access control in addition to the shared secret.

Because `open` mode accepts requests from any source IP with the shared secret, it is not a good fit for broad public internet exposure. Even in `restricted` mode, prefer upstream firewall or VPN controls and treat IP filtering as defense in depth rather than the primary security boundary.

## Defaults

New installs default built-in RADIUS to enabled in settings, but the server is not usable until you set a shared secret and create at least one RADIUS user.

The default listen port is UDP `1812`.

Shared secret review is manual. The app recommends reviewing the secret once per year, but it does not rotate or expire the secret automatically.

## Username Rules

Built-in RADIUS accounts are intentionally separate from the local web admin account.

Reserved usernames are blocked:

- `admin`
- `root`

The app rejects those usernames when you create or update a RADIUS user, and auth attempts for those usernames are also rejected.

## Allowed Clients

In **restricted** client mode, RADIUS clients are built from two sources:

1. Enabled inventory IPs for APs, switches, and cached CPEs
2. Manual client overrides configured in `Settings > Authentication`

Manual client overrides accept either:

- A single IP address such as `10.0.10.5`
- A CIDR such as `10.0.10.0/24`

In **open** client mode (default), all devices are accepted — only the shared secret matters.

## Auth History and Stats

The RADIUS server logs authentication results directly to SQLite. The Authentication UI shows:

- Recent admin logins
- Login counts
- Success rate
- Active devices over the last 24 hours

## Setup

1. Open `Settings > Authentication`
2. In the built-in RADIUS section, set a shared secret
3. Set the device-facing RADIUS host that APs/CPEs/switches should use
4. Create named RADIUS users for device-admin login
5. If using restricted client mode, add manual client overrides for devices or subnets not yet in inventory
6. Configure your firewall or load balancer to allow UDP `1812` to the appliance only from trusted device networks, VPN ranges, or explicit source-IP allowlists

For most internet-facing deployments, publish only the web UI on `80/443` and keep RADIUS off the public internet. If remote devices must reach built-in RADIUS, expose UDP `1812` only behind network ACLs and use a strong random shared secret.

## Device Rollout Workflow

A common rollout path is:

1. Add devices to the system using their current manual credentials
2. Verify they are present in inventory and manageable
3. Enable built-in RADIUS and create RADIUS users
4. Save a RADIUS device config template under the Config UI
5. Start the staged RADIUS rollout from `Settings > Authentication`

The device RADIUS configuration drawer includes a `Use this appliance` helper that prefills the configured device host and UDP port from the built-in RADIUS settings. You still need to enter the shared secret when saving the RADIUS config template used for rollout.

Current rollout behavior:

- Scope is enabled APs, switches, and CPEs that currently authenticate with inherited parent-AP credentials
- Phases are `canary`, `pct10`, `pct50`, and `pct100`
- Rollout start forces a fresh AP poll first so attached CPE inventory and inherited-auth state are current
- The saved RADIUS config template must match the built-in RADIUS device host, port, and shared secret before rollout can start
- Each device is migrated using its currently stored manual management credentials
- CPEs inherit the parent AP credentials during migration; if that inherited login fails, the rollout pauses and the operator must update the AP credentials inline before resuming
- Saving new AP credentials triggers an immediate AP reprobe, which rediscovers attached CPEs and rechecks inherited CPE authentication before rollout resumes
- After the RADIUS config is applied, the app immediately verifies the cutover by logging back into the device through RADIUS with a hidden automation account hosted on this appliance
- Remaining rollout devices are resolved from current inventory state as each phase runs, so later CPE phases use the latest parent AP credentials instead of a stale snapshot
- On successful verification, the app updates stored credentials for APs and switches to that RADIUS automation account. CPEs continue to inherit the parent AP credentials because they are not stored separately.
- Any failure pauses the rollout

## Operational Notes

- Built-in RADIUS is classified as a dangerous feature in the UI
- The shared secret is not returned by the API after it is stored
- The app tracks when the RADIUS shared secret was last changed and shows a yearly manual review reminder
- Older installs that had a secret before tracking was added will show `Age Unknown` until an operator either changes the secret or explicitly marks the current secret as reviewed today
- Device RADIUS migration is staged and persistent; rollout state survives app restarts
- The RADIUS server runs in-process using pyrad — no separate container or config file generation
- The Authentication UI exposes server health so you can distinguish inactive, healthy, and degraded RADIUS states
- In restricted client mode, devices authenticating from unrecognized IPs without a manual override will be rejected

## Design Notes

- Yearly manual shared-secret review reminder design: see `docs/radius-secret-rotation-reminder.md`
