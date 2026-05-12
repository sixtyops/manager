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

- A fresh **Debian 12** VM with `sudo` access. Other Linux distributions
  with Docker should work but are untested — see
  [docs/deployment.md#prerequisites](deployment.md#prerequisites).
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
   8 characters). The username defaults to `admin` (configurable via the
   `ADMIN_USERNAME` env var — see
   [docs/deployment.md#authentication](deployment.md#authentication)).

<!-- screenshot: first-run password setup -->

## Step 3 — Setup wizard (~1 min)

After login the setup wizard opens automatically. Two decisions:

- **HTTPS certificate** — keep the self-signed cert (fine for private
  networks) or switch to Let's Encrypt now. The built-in flow uses HTTP-01
  validation, which requires port 80 reachable from the internet. If
  you're on a private network and want a trusted cert without inbound
  internet, see
  [docs/deployment.md#ssltls](deployment.md#ssltls) for the DNS-01
  workflow.
- **SFTP backups** — skip for now if you don't have an SFTP server ready.
  You can return to Settings → Backups later. (Decide on backups
  explicitly before you onboard real devices — the post-deploy checklist
  forces this question.)

<!-- screenshot: setup wizard step 1 -->

## Step 4 — Add your first tower site (~1 min)

From the dashboard:

1. Find the **Add APs & Switches** card at the top-left.
2. In the site picker, click **+ New site**.
3. Enter a name (required). Location and lat/lng are optional but help the
   weather-guard temperature checks and signal-map rendering later.
4. Save. The site appears in the picker.

(Underlying API: `POST /api/sites`, `updater/app.py:1240`.)

<!-- screenshot: add tower site modal -->

## Step 5 — Add your first AP (~1 min)

In the same **Add APs & Switches** card:

1. Enter the AP's IP, admin username, and admin password.
2. Pick the tower site you just created.
3. Save. The first poll fires immediately — no need to wait for the
   scheduled cycle.

(Underlying API: `POST /api/aps`, `updater/app.py:1300`.)

<!-- screenshot: add AP form -->

## Step 6 — Watch it poll (~2 min)

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
