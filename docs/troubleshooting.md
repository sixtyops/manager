# Troubleshooting

Top five failure modes operators hit during the design-partner window, with
the diagnostic and recovery path for each. If something here doesn't match
what you're seeing, email **support@sixtyops.net** with the symptom and a
copy of the logs.

## When in doubt, grab logs first

Most issues are diagnosable from container logs. Run these from the install
directory (typically `/opt/sixtyops`):

```bash
docker compose logs sixtyops-mgmt --tail=200
docker compose logs sixtyops-nginx --tail=200
docker compose logs sixtyops-certbot --tail=50
```

For a live tail, append `-f`. Errors are uppercased and include a Python
traceback when something blew up.

---

## 1. Device unreachable

### Symptom
A device shows as offline / red on the dashboard, `last_seen` is hours stale,
or update jobs report status `unreachable` for it.

### Diagnose
1. Confirm the device's IP and credentials in **Devices > row > Edit**. A
   DHCP renumber or password change on the device side is the most common
   cause. (MAC-based config-history rebind covers the snapshot side, but the
   poller still needs the current IP.)
2. From the Manager container, probe the device directly:
   ```bash
   docker compose exec sixtyops-mgmt curl -k -m 5 https://<device-ip>/
   ```
   If this hangs or returns "connection refused", the issue is network
   reachability, not the Manager. The Manager polls from inside its
   container's network namespace, so host-side reachability does not
   guarantee container-side reachability.
3. Tail `sixtyops-mgmt` and look for `Device not reachable` — emitted by the
   Tachyon client at `updater/vendors/tachyon/client.py` (lines 271 and 658)
   along with a retry count. Repeated retries followed by a final failure
   confirm it's a connectivity problem and not a transient blip.

### Recover
- **IP changed**: edit the device in the UI; the next poll cycle picks up
  the new address.
- **Credentials drifted**: re-enter the username / password in the same
  Edit modal. The save path calls `update_device_credentials` in
  `updater/database.py` and re-encrypts the stored secret.
- **Network**: confirm the Manager host can reach the device subnet
  (firewall, VPN, VLAN). Then re-test from inside the container with the
  `curl` command above.

---

## 2. RADIUS auth failing

### Symptom
Wireless clients see "auth reject" from APs configured against this
Manager's RADIUS server, or **Auth > Recent logins** in the UI shows
failed attempts (or nothing at all).

### Diagnose
1. **Shared secret**: open **Settings > RADIUS** and confirm the secret
   matches what each AP / client has configured. If `sixtyops-mgmt` logs
   show `Failed to decrypt RADIUS shared secret`
   (`updater/radius_server.py:82`), the encrypted value can't be read —
   typically because the encryption key changed (e.g., the DB was restored
   without the matching key). The server falls back to an auto-generated
   secret at startup (`updater/radius_server.py:89`), which won't match
   any configured client.
2. **LDAP backend**: in **Settings > RADIUS**, click **Test LDAP**
   (`POST /api/auth/radius/test-ldap` — `updater/app.py:2753`). The
   response reports the bind result and how many users the search
   returned. If logs contain
   `RADIUS: LDAP server unreachable` (`updater/radius_server.py:381`),
   the LDAP host is down or blocked by a firewall.
3. **URL scheme**: the configured LDAP URL must start with `ldap://` or
   `ldaps://`. The Settings form rejects anything else; if you suspect
   the validation is being bypassed, check the value in the DB.

### Recover
- **Secret mismatch**: regenerate the shared secret in **Settings >
  RADIUS** and copy it to each AP/client. See
  [docs/radius-secret-rotation-reminder.md](radius-secret-rotation-reminder.md)
  for the full rotation flow.
- **LDAP unreachable**: confirm DNS, port, and firewall to the LDAP host;
  re-run **Test LDAP** until it returns green.
- **Verify success**: use the Recent logins panel
  ([docs/radius.md](radius.md)) — it updates in near-real-time and is the
  fastest "did it work this time?" check.

---

## 3. Update job hung

### Symptom
A job on the **Jobs** page is stuck in `running` with no progress for
more than ~10 minutes, or a gradual rollout banner shows `paused` with
no obvious reason.

### Diagnose
1. **Job status**. An `UpdateJob` lives in one of
   `pending / running / completed / failed / cancelled`
   (`updater/app.py:3855`). "Hung" means `running` with no log progress.
   Open the job's row to see the per-device step log; if the last entry
   is "Device not reachable" or "Waiting for reboot", a device has dropped
   offline after its update (which halts the job — halt-on-first-failure).
2. **Rollout state**. Gradual rollouts move through
   `active / paused / completed / cancelled`
   ([docs/gradual-rollout.md](gradual-rollout.md)). The scheduler
   auto-pauses on device failure, and the reason is stored in the
   rollout's `pause_reason` field. Look for `pause_reason` in the
   rollout's API response (`GET /api/rollout/current`) or in the banner
   tooltip on the dashboard.
3. **Maintenance window**. A rollout that's mid-phase at the end of its
   window will defer until the next window opens — that looks like
   "stuck" but is correct behavior. Check the rollout's configured
   window against the current time.

### Recover
- **Cancel a stuck job**:
  ```bash
  curl -X POST -u admin:<pass> https://<manager>/api/job/<job-id>/cancel
  ```
  Or use the **Cancel** button on the Jobs page
  (`updater/app.py:5877`).
- **Resume / cancel / reset a rollout**: the dashboard rollout banner
  exposes these as buttons; the underlying endpoints are
  `/api/rollout/{id}/resume`, `/api/rollout/{id}/cancel`,
  `/api/rollout/{id}/reset`.
- **Test new firmware before the fleet rolls**: the firmware is held until its
  Firmware Hold elapses or a device is confirmed working on it. To clear the hold
  sooner, manually update one device (the **Update Firmware** button skips the
  hold); once it reboots healthy and passes smoke tests, that model's fleet wave
  proceeds at the next window.

---

## 4. SSL cert renewal failure

### Symptom
Browsers warn about an expired certificate, or **Settings > HTTPS** shows
`needs_renewal: true` (cert is within 30 days of expiry —
`updater/ssl_manager.py:90`).

### Diagnose
1. Check the certbot container's recent renewal attempts:
   ```bash
   docker compose logs sixtyops-certbot --tail=50
   ```
   The container runs `certbot renew` every 12 hours
   (`docker-compose.standalone.yml`).
2. Probe a dry-run manually:
   ```bash
   docker exec sixtyops-certbot certbot renew --non-interactive --dry-run
   ```
3. **DNS misconfig**: certbot output contains `DNS problem` or `NXDOMAIN`
   (`updater/ssl_manager.py:302`). The A record for the configured domain
   isn't pointing at this host — usually a DNS change that didn't fully
   propagate, or a typo in the host setting.
4. **Rate limit**: certbot mentions "too many certificates already issued
   for ...". Let's Encrypt enforces 50 certs per registered domain per
   week. Hitting this usually means a renewal loop ran in error.

### Recover
- **DNS**: fix the A record on your DNS provider, wait for TTL, then
  force a renewal:
  ```bash
  docker exec sixtyops-certbot certbot renew --force-renewal
  ```
- **Rate limit**: wait the printed reset window. Do not loop retries —
  every failed attempt counts against the limit.
- The active cert lives at
  `/etc/letsencrypt/live/<domain>/fullchain.pem` inside the certbot
  container (`updater/ssl_manager.py:79`); nginx reloads automatically
  on successful renewal.

---

## 5. SFTP backup failing

### Symptom
**Settings > Backups** shows `last_status: failed: <error>`
(`updater/sftp_backup.py:300`), or no recent backup files appear on the
remote SFTP server.

### Diagnose
1. Open **Settings > Backups** and click **Test connection**. This calls
   `test_backup_connection()` in `updater/sftp_backup.py:131` and
   surfaces the underlying `asyncssh` error verbatim — that error is
   nearly always sufficient to identify the cause.
2. Tail Manager logs for backup-related entries:
   ```bash
   docker compose logs sixtyops-mgmt --tail=200 | grep -i backup
   ```
3. Common causes, in rough order of frequency:
   - **Password or SSH key mismatch** — re-check the credentials block;
     keys must include the matching `BEGIN/END` lines.
   - **Remote path missing or not writable** — the configured path must
     already exist; the Manager will not `mkdir -p` for you.
   - **Remote disk full** — `asyncssh` surfaces this as an I/O error
     part-way through the upload.

### Recover
- Re-enter credentials in **Settings > Backups** (host / port / path /
  username / auth method are encrypted in the settings DB at
  `updater/sftp_backup.py`).
- Ensure the configured remote path exists and that the SFTP user can
  write to it. A 30-second `ssh user@host mkdir -p /path` from a
  workstation is the fastest way to verify both at once.
- Trigger a manual run from the **Run Now** button in Settings > Backups.
  Concurrent runs are prevented by `_backup_lock` in
  `updater/sftp_backup.py`, so you'll get a clean error rather than a
  half-run if one is already in flight.

---

## 6. Update button shows steps instead of updating

### Symptom
In **Settings → Updates**, the app shows update steps instead of restarting
itself, or the update banner says this install uses manual app updates.

### What it means
This is expected when the manager is not running in the managed install shape.
One-click app updates require the same ingredients `scripts/install.sh`
creates: a mounted repo, Docker Compose on the host, and Docker socket access.
The behavior is based on install shape, not whether the host is Debian or
Ubuntu.

### What to do
- If you want one-click app updates, move to the managed install path from
  [docs/deployment.md](deployment.md): Debian 12 + `install.sh`.
- If you prefer an image-based or custom install, use the manual commands
  shown in the update banner. That path is supported; it just does not
  self-apply from the UI.

---

## 7. App won't start after self-update (database error)

### Symptom
The `sixtyops-mgmt` container restarts on a loop after applying an in-app
update. Logs end in `sqlite3.OperationalError`, `no such column`, or
`Database integrity check failed`.

### Diagnose & recover
This is a database-migration issue. The full recovery procedure
(restart → restore from backup → manual repair) is in
[docs/migration-recovery.md](migration-recovery.md). Start with Path 1
(simple container restart) — `init_db()` is idempotent and resolves
most cases on its own.

---

## Still stuck?

Email **support@sixtyops.net** with:
- A description of what you were trying to do
- The relevant section above and which step failed
- A copy of the log output from the relevant container
- Manager version (visible in **Settings > About**)

<!-- TODO(#A2): add self-update auth failure entry once that feature ships
     (per acceptance criteria of issue #124). -->
