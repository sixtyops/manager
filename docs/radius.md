# Built-in Radius

The application includes a built-in FreeRADIUS service for device-admin authentication. This is intended for managed devices such as APs and switches authenticating to this system over UDP `1812`.

This feature is separate from management UI login:

- Web login uses local username/password and optional OIDC SSO
- Built-in Radius is for end devices authenticating against this appliance

## Architecture

The built-in Radius feature is split across two components:

- `tachyon-mgmt` stores Radius users, settings, client overrides, and auth history in SQLite
- `tachyon-radius` runs FreeRADIUS and reads generated config files from `data/radius/`

The app generates these files:

- `data/radius/clients.conf`
- `data/radius/mods-config/files/authorize`

After Radius settings, users, or client overrides change, the app regenerates those files and reloads the FreeRADIUS container.

The container is supervised in two ways:

- Docker healthchecks validate that the generated config files exist, FreeRADIUS can parse them, and UDP `1812` is listening
- The app runs a background health monitor and attempts recovery if Docker marks the container unhealthy

## Defaults

New installs default built-in Radius to enabled in settings, but the server is not usable until you set a shared secret and create at least one Radius user.

The default listen port is UDP `1812`.

Shared secret review is manual. The app recommends reviewing the secret once per year, but it does not rotate or expire the secret automatically.

## Username Rules

Built-in Radius accounts are intentionally separate from the local web admin account.

Reserved usernames are blocked:

- `admin`
- `root`

The app rejects those usernames when you create or update a Radius user, and auth attempts for those usernames are also rejected.

## Allowed Clients

FreeRADIUS clients are generated from two sources:

1. Enabled inventory IPs for APs, switches, and cached CPEs
2. Manual client overrides configured in `Settings > Authentication`

Manual client overrides accept either:

- A single IP address such as `10.0.10.5`
- A CIDR such as `10.0.10.0/24`

Use manual overrides when:

- A device is not fully inventoried yet
- A group of devices should be allowed before onboarding
- Source IPs do not line up cleanly with one-device-per-IP inventory entries

## Auth History and Stats

FreeRADIUS writes auth results to container logs. The app periodically ingests those logs into SQLite so the Authentication UI can show:

- Recent admin logins
- Login counts
- Success rate
- Active devices over the last 24 hours

Because auth history is copied into SQLite, recent stats survive Radius container restarts after the logs have been synced.

## Setup

1. Open `Settings > Authentication`
2. In the built-in Radius section, set a shared secret
3. Set the device-facing Radius host that APs/CPEs/switches should use
4. Create named Radius users for device-admin login
5. If needed, add manual client overrides for devices or subnets not yet represented by inventory
6. Configure your firewall or load balancer to allow UDP `1812` to the appliance

## Device Rollout Workflow

A common rollout path is:

1. Add devices to the system using their current manual credentials
2. Verify they are present in inventory and manageable
3. Enable built-in Radius and create Radius users
4. Save a Radius device config template under the Config UI
5. Start the staged Radius rollout from `Settings > Authentication`

The device Radius configuration drawer includes a `Use this appliance` helper that prefills the configured device host and UDP port from the built-in Radius settings. You still need to enter the shared secret when saving the Radius config template used for rollout.

Current rollout behavior:

- Scope is enabled APs, switches, and CPEs that currently authenticate with inherited parent-AP credentials
- Phases are `canary`, `pct10`, `pct50`, and `pct100`
- Rollout start forces a fresh AP poll first so attached CPE inventory and inherited-auth state are current
- The saved Radius config template must match the built-in Radius device host, port, and shared secret before rollout can start
- Each device is migrated using its currently stored manual management credentials
- CPEs inherit the parent AP credentials during migration; if that inherited login fails, the rollout pauses and the operator must update the AP credentials inline before resuming
- Saving new AP credentials triggers an immediate AP reprobe, which rediscovers attached CPEs and rechecks inherited CPE authentication before rollout resumes
- After the Radius config is applied, the app immediately verifies the cutover by logging back into the device through Radius with a hidden automation account hosted on this appliance
- Remaining rollout devices are resolved from current inventory state as each phase runs, so later CPE phases use the latest parent AP credentials instead of a stale snapshot
- On successful verification, the app updates stored credentials for APs and switches to that Radius automation account. CPEs continue to inherit the parent AP credentials because they are not stored separately.
- Any failure pauses the rollout

## Operational Notes

- Built-in Radius is a PRO-gated feature
- The shared secret is not returned by the API after it is stored
- The app tracks when the Radius shared secret was last changed and shows a yearly manual review reminder
- Older installs that had a secret before tracking was added will show `Age Unknown` until an operator either changes the secret or explicitly marks the current secret as reviewed today
- Device Radius migration is staged and persistent; rollout state survives app restarts
- The app uses FreeRADIUS for protocol handling, but Radius policy remains app-managed
- The Authentication UI exposes container health so you can distinguish inactive, healthy, and degraded Radius states
- If devices authenticate from IPs the system does not recognize and you have not added a manual override, FreeRADIUS will reject them as unknown clients

## Design Notes

- Yearly manual shared-secret review reminder design: see `docs/radius-secret-rotation-reminder.md`
