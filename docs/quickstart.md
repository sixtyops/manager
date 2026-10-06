# Quickstart

Install SixtyOps Manager and poll your first AP in about ten minutes. Aimed
at a WISP ops engineer who has never seen this system before. After you
finish here, run [docs/post-deploy-checklist.md](post-deploy-checklist.md)
to confirm the rest of the install is healthy.

## What you'll have at the end

A running Manager on `https://<your-server>` with one tower site, one AP
authenticated against your credentials, and a live signal-health row on the
dashboard. From there you can upload firmware and configure the auto-update
window the same evening.

## Prerequisites

- A fresh **Debian 12** VM with `sudo` access. This is the recommended easy
  path and the one that enables one-click app updates in **Settings →
  Updates** after install.
- Other Linux distributions with Docker may still work, but they are not the
  promoted path. If you use Ubuntu or another distro, follow
  [docs/deployment.md](deployment.md) and expect app updates to be manual
  unless you replicate the managed repo + Compose install shape.
- HTTPS reachability from the VM to each AP's management IP. The Manager
  polls devices from inside its container; if a `curl -k https://<ap-ip>/`
  from the VM hangs, the install won't help.
- One AP's IP, admin username, and admin password ready.
- (Optional) A name and rough location for the tower the AP lives at.

---

## Step 1 — Install (~2 min)

On the VM:

```bash
curl -sSL https://raw.githubusercontent.com/sixtyops/manager/main/scripts/install.sh | sudo bash
```

The installer installs Docker if it isn't already there, clones this repo
to `/opt/sixtyops`, builds the images, starts the standalone stack
(application + bundled nginx with self-signed HTTPS + certbot), and creates
a `sixtyops.service` systemd unit so the stack comes back after reboot. See
[docs/deployment.md](deployment.md) for what each piece does in detail.

The generated service sets `NoNewPrivileges=true` and `PrivateTmp=true` for
its Docker/Compose client processes. These settings do not sandbox the separate
Docker daemon or its containers. A repository update does not change an already
installed unit. Runtime enforcement and stack compatibility need a separate
authorized host check.

This install path is also what the in-app **Apply App Update** button expects:
the repo is mounted, Docker Compose is present, and the host can rebuild and
restart cleanly from the Settings page.

When it finishes you'll see roughly:

```
==========================================
  Installation complete!
==========================================

Installed to: /opt/sixtyops

Access: https://<server-ip>
        (Accept the self-signed certificate warning)

On first run, you'll be prompted to create an admin password.
```

<!-- screenshot: terminal showing "Installation complete!" -->

If the install command hangs at the "waiting for HTTPS" health check, jump
to [docs/troubleshooting.md](troubleshooting.md#when-in-doubt-grab-logs-first)
— `docker compose logs sixtyops-mgmt --tail=200` from `/opt/sixtyops` will
usually tell you why.

## Step 2 — First login + admin password (~1 min)

1. Open `https://<server-ip>` in a browser.
2. Accept the self-signed certificate warning. Private-network deployments
   keep self-signed by default; Let's Encrypt is offered in the next step.
3. The first-run screen prompts you to create an admin password (minimum
   12 characters). The username defaults to `admin` (configurable via the
   `ADMIN_USERNAME` env var — see
   [docs/deployment.md#authentication](deployment.md#authentication)).

<!-- screenshot: first-run password setup -->

## Step 3 — First-run settings (~1 min)

After login, App Settings opens at **System → HTTPS**.

Keep the self-signed certificate for a private network. To use Let's Encrypt,
configure a domain and email in this panel. The built-in flow uses HTTP-01
validation, which requires port 80 to be reachable from the internet. If you
need a trusted certificate on a private network, see
[docs/deployment.md#ssltls](deployment.md#ssltls) for the DNS-01 workflow.

Close App Settings to return to the dashboard. Use **Add APs & Switches** there
to add your first device. You can configure backups later under **System →
Backup & Restore** in App Settings.

## Step 4 — Add your first device (~1 min)

From the dashboard:

1. Find the **Add APs & Switches** card at the top-left.
2. Enter the device IP address. Enter one IP on each line if you are adding
   more than one device.
3. Enter the device username and password. The username defaults to `root`.
4. Select **Add APs & Switches**.

Manager identifies APs and switches from the device model. It assigns devices
to tower sites from the device's `location` field. The first poll starts when
you add the device.

<!-- screenshot: add AP form -->

## Step 5 — Watch it poll (~2 min)

Within ~60 seconds the AP appears in the main device table with:

- Model (e.g. `TNA-303x`)
- Current signal dBm and signal health (Strong / Low / Marginal)
- `last_seen` timestamp of "just now"
- Any CPEs attached to it, nested under the AP row

If the AP doesn't appear, or shows red/offline, see
[docs/troubleshooting.md §1 "Device unreachable"](troubleshooting.md#1-device-unreachable).
That section's container-side `curl` probe is the fastest way to
distinguish a credentials issue from a network-reachability issue.

<!-- screenshot: dashboard with one AP visible -->

---

## You're done — what next?

- **Verify the rest of the install** — run through
  [docs/post-deploy-checklist.md](post-deploy-checklist.md) (about
  5 minutes). It confirms notifications, audit logging, backups, HTTPS,
  and the update channel are wired correctly before you onboard real
  devices.
- **Upload firmware** — Firmware tab. The system auto-detects which device
  models each file applies to.
- **Configure the auto-update window** — Auto-Update tab. See the README's
  *Scheduled Automatic Updates* section and
  [docs/gradual-rollout.md](gradual-rollout.md) for how the 4-night
  rollout works.
- **Deployment options** — HTTPS / Let's Encrypt detail, env vars,
  reverse-proxy configurations, and RADIUS are in
  [docs/deployment.md](deployment.md).
- **If something breaks** — start with
  [docs/troubleshooting.md](troubleshooting.md); if it's not there, email
  **support@sixtyops.net** with the symptom and a copy of the relevant
  container logs.
