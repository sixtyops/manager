# Post-Deploy Checklist

Run this after every install — your own and every design partner's. About
five minutes, ten items. Each step says **what to do** → **what to
confirm**. If a step fails, jump to [docs/troubleshooting.md](troubleshooting.md);
if it's not in troubleshooting, email **support@sixtyops.net** with the
symptom and a copy of the relevant container logs.

Prerequisite: you've completed [docs/quickstart.md](quickstart.md). The
checklist assumes the Manager is reachable, one AP is added, and at least
one poll has completed.

---

## 1. Log in as admin

**Do:** Open `https://<manager>` and log in.
**Confirm:** The header shows your username with an **admin** role badge.
If you see a **viewer** badge instead, write-operation UI is hidden — log
in with the admin account or promote your user via Settings → Users.

## 2. Add a tower site

**Do:** From the **Add APs & Switches** card (top-left), open the site
picker and click **+ New site**. Save with at least a name.
**Confirm:** The new site appears in the picker and in Settings → Sites.

## 3. Add an AP with credentials

**Do:** In the same card, enter an AP's IP, admin username, password,
and pick the site from step 2. Save.
**Confirm:** The row appears in the device table within ~60 seconds with
a status that progresses from "polling" to a model + signal dBm reading.

## 4. Verify the AP polled successfully

**Do:** Look at the AP's row in the main device table.
**Confirm:** Model, signal dBm, signal-health pill (Strong / Low /
Marginal), and `last_seen` of "just now". If the row stays empty or red,
see [troubleshooting §1 — Device unreachable](troubleshooting.md#1-device-unreachable).

## 5. Trigger a manual config poll

**Do:** Open the **Config** drawer and click **Check Compliance**. (This
fires `POST /api/configs/poll?refresh=true` — see
[docs/api.md](api.md) for the underlying API.)
**Confirm:** The AP's **Last Checked** timestamp updates to "just now"
and no `⚠` glyph appears in the row. If the row carries `⚠`, hover for
the per-device `last_poll_error` string and reconcile with
[troubleshooting §1](troubleshooting.md#1-device-unreachable).

## 6. Send a test notification

Pick the channel you actually plan to alert on; Slack is the most common
for design partners.

**Do:** Settings → Notifications → **Slack** subtab → paste an Incoming
Webhook URL → **Save** → **Test**. (Underlying API: `POST /api/slack/test`,
[docs/api.md](api.md).)
**Confirm:** A test message arrives in the Slack channel within a few
seconds.

Email, SNMP traps, and generic webhooks each have their own subtab with
a **Test** button and matching API (`/api/email/test`, `/api/snmp/test`,
`/api/webhooks/test`). Pick one and verify it. Don't skip this — silent
failures are the most expensive ones.

## 7. Confirm the audit log is recording

There's no in-UI audit-log panel yet (tracked in
[issue #136](https://github.com/sixtyops/manager/issues/136)). Verify via
the API for now.

**Do:** From any host that can reach the Manager:

```bash
curl -k -u <admin-user>:<admin-pass> https://<manager>/api/audit-log | head
```

**Confirm:** The most recent entries include your login from step 1 and
the create-site / add-AP actions from steps 2 and 3. If the list is
empty, the audit pipeline isn't writing — check
`docker compose logs sixtyops-mgmt --tail=200` from the install directory
for errors and escalate.

<!-- TODO(#136): replace this step with a UI walkthrough once the in-UI
     audit-log panel ships. -->

## 8. Decide on backups (don't leave this unanswered)

**Do:** Settings → Backups. Either configure an SFTP target (host, port,
path, username, auth) and click **Test connection**, or explicitly note
in your runbook that you're running without backups for now.
**Confirm:** If configured, **Test connection** returns green and
**Run Now** produces a fresh archive on the remote host. If skipped,
write down the conscious decision — leaving Backups in its default
unconfigured state silently means *no off-host disaster recovery*. SFTP
failure modes are in
[troubleshooting §5](troubleshooting.md#5-sftp-backup-failing).

## 9. Verify HTTPS

**Do:** Visit the Manager URL from a clean browser session (no cached
exceptions).
**Confirm:**

- If you configured Let's Encrypt during the setup wizard: the browser
  padlock shows a trusted certificate with no warning, and Settings →
  HTTPS shows `needs_renewal: false`.
- If you stayed self-signed: the certificate's Subject / SAN matches the
  hostname or IP you typed into the browser. The cert path is
  `nginx/ssl/` inside the install directory.

Renewal failures and DNS / rate-limit issues are in
[troubleshooting §4](troubleshooting.md#4-ssl-cert-renewal-failure).

## 10. Verify the update channel

**Do:** Settings → Updates.
**Confirm:**

- **Release channel** is set to **Stable** (or **Dev** if that's
  intentional — see the README for the channel distinction).
- Clicking **Check for updates** returns either *up to date* or a
  release banner with notes. No errors.
- **Auto-update** is enabled if you want the system to apply updates on
  its own (recommended for design partners). Updates auto-defer during
  firmware rollouts and maintenance windows; details in
  [docs/release-system.md](release-system.md).

---

## All ten pass?

You're ready to onboard the rest of the fleet. From here:

- Upload firmware in the **Firmware** tab and configure the auto-update
  window — see the README's *Scheduled Automatic Updates* section and
  [docs/gradual-rollout.md](gradual-rollout.md) for how the 4-night
  rollout actually runs.
- Bookmark [docs/troubleshooting.md](troubleshooting.md) for the on-call
  rotation.
- If you're an operator with feedback on this checklist, email
  **support@sixtyops.net** — every item here came from someone hitting
  a real wall during a real install, and we'd like to keep it that way.
