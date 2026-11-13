# Dev Hardware Validation

**Key points:** The operator or the Manager owner under [AGENTS.md authority](../AGENTS.md#host-access)
may run this procedure. A worker may run it only when the owner assigns the
worker a named run. The exception in [AGENTS.md](../AGENTS.md#must-not-do)
allows that worker to run only the integration tests named in the run brief,
against the dev host and dedicated lab devices. Workers must not run other
integration tests or touch live hardware, a dev host, or a production install
outside that assigned run.

**Detail:** Commands and reference tables below do not grant access. Follow
[Agent rules](../AGENTS.md#agent-rules). Use existing private credentials
without putting secret values on a command line.

This repo now has two live-dev validation lanes:

- `dev_blocking`: merge-gating validation against the shared dev host and dedicated lab devices
- `dev_sso`: separate non-blocking SSO/OIDC validation lane
- `dev_nonblocking`: live tests that do not gate a merge. The RADIUS rollout
  test uses this marker while RADIUS remains scheduled for removal.

Set `SIXTYOPS_TEST_URL` to the authorized dev host URL for the authorized dev
validation lane. Use this lane only with its authorized test devices.

## Dev host access

The owner host reaches the authorized dev host through a recorded SSH alias and
its approved network path. The alias, host details, and access record are on
the owner host's local disk and in the private system record. Check those
locations before reporting dev host access as missing. Keep the IP address,
alias, usernames and host paths in the private record. An agent assigned dev
host work has full authority on that host, including guest-local `sudo` and
Docker, within these boundaries:

- Read the repository instructions first.
- Work in a separate git worktree.
- Use only the known test devices.
- Reach the dev host only through the recorded SSH alias and its approved
  network path.
- If access is blocked, stop that operation and report it.
- Keep every change inside the dev host itself; shared infrastructure,
  production and firewall rules stay as they are.
- Leave releases and merges to the normal release and merge process.
- Keep secrets in the private files, out of logs and Git.

When the lab has fewer devices than test roles, one dedicated lab AP can fill
several roles; its write tests then run one at a time.

### Lab firmware authority

On 2026-11-03, the operator gave standing approval for firmware changes on the
dedicated lab devices and the dev host environment. This approval includes the
live firmware integration test (`tests/integration/test_firmware.py`) and the
Dev host run procedure below: back up the dev host, deploy an exact `main` or
pull request head, enable the lab inventory records, run the test, read back
the state, and restore the backup. The owner and a worker the owner assigns to a
named run may follow these steps without per-run operator approval. Use this
section as the approval source in the run brief.

Use only firmware images in the Manager store for that model. Change one
device at a time. Hold the shared dev hardware lock with a detached process on
the dev host. The owner's lock helper starts this holder over one SSH call and
prints its PID:

```sh
ssh <dev host alias> 'setsid nohup flock -w 300 /var/lock/sixtyops-dev-hardware.lock sleep <seconds> >/dev/null 2>&1 < /dev/null & echo $!'
```

Confirm that the lock is held with `flock -n /var/lock/sixtyops-dev-hardware.lock -c true`; it must fail while the holder runs. Release the lock by stopping the printed PID and its child on the dev host. Then confirm that `flock -n /var/lock/sixtyops-dev-hardware.lock -c true` succeeds. A local background holder does not stay alive across agent tool calls.

After each write, read back firmware, active bank, both bank versions,
connected CPE count, and config section names twice. Judge the write from
these read-backs. On 2026-11-01, two lab bank writes returned success and the
read-backs confirmed them, so the false failure in
[#670](https://github.com/sixtyops/manager/issues/670) is intermittent. Record
each change in the owner artifacts. This approval covers dedicated test lab
devices and the dev host environment. It does not cover production devices,
stable releases, dev releases, or other hosts.

### Power-cycle a lab AP through the switch

**Key points:** Use this procedure when a lab AP answers ping but its web
service hangs, or when a run needs a clean reboot without a firmware write.
The operator approved this method on 2026-11-09. Hold the shared hardware lock
before changing PoE.

**Detail:** Find the AP's switch port in Manager's `switch_bridge_entries`
table. Match the AP MAC address and the CPE MAC address behind it; both appear
on the same switch port. Record the switch and port before continuing.

The TNS-100 controls PoE with `ethernet.ports.<port>.poe`. The live Ethernet
status is available from `GET /cgi.lua/status?type=ethernet`. For the port,
check `poe_out`, `poe_fault`, and `link`.

Hold the shared dev hardware lock with a detached process on the dev host:

```sh
ssh <dev host alias> 'setsid nohup flock -w 300 /var/lock/sixtyops-dev-hardware.lock sleep <seconds> >/dev/null 2>&1 < /dev/null & echo $!'
```

Confirm that the lock is held with
`flock -n /var/lock/sixtyops-dev-hardware.lock -c true`; it must fail while the
holder runs. Release the lock by stopping the printed PID and its child, then
confirm that the same `flock -n` command succeeds.

Use `TachyonClient.get_config()` to read the full config. Change only
`ethernet.ports.<port>.poe` and pass the full config to
`TachyonClient.apply_config()`. First call it with `dry_run=True`; after the
dry run succeeds, apply the same config with `preserve_user_passwords=True`.
Pass `preserve_user_passwords=True` on the dry run as well, so stored user
password hashes remain unchanged through the full-config round trip.

Power-cycle the port in this order:

1. Set PoE to `false` and apply it. Read back the config value for `poe` and
   Ethernet status values `poe_out`, `poe_fault`, and `link` twice.
2. Wait 15 seconds.
3. Set PoE to `true` and apply it. Read back `poe` and status values
   `poe_out`, `poe_fault`, and `link` twice.

If any step after PoE is disabled fails, restore PoE to `true` in a cleanup
step. Run the same dry-run and apply sequence for the restore, then read back
`poe`, `poe_out`, `poe_fault`, and `link` twice. Do not release the shared lock
until PoE has been restored or the restore failure has been recorded.

A TNA-301 on firmware 1.15.1 answers about 75 to 85 seconds after PoE returns.
On firmware 1.15.0, it took about 4.5 minutes. Wait about 90 seconds after boot
before the first login, then probe every 30 seconds. Fast repeated logins
during boot preceded web server hangs. Its management address can change after
reboot. Find it through the CPE's wireless peer report, then confirm its serial;
see
[#777](https://github.com/sixtyops/manager/issues/777). Do not use the TNA-30x
1.15.0 image for lab runs. It has a known web server hang that 1.15.1 fixes.
Read the vendor release notes before any lab flash.

### Dev host run procedure

The operator or the Manager owner under [AGENTS.md authority](../AGENTS.md#host-access)
may run dev host procedures. A worker may run a procedure only when the owner
assigns that worker a named run. The brief names the lock holder and gives the
exact approved backup, verify, and restore command lines. Run procedures on the dev host over the
recorded SSH alias. Use the
approved backup, verify, and restore commands with `sudo -n`; do not run
`sudo` on the owner host. Read command arguments and paths from the approved
`sixtyops-manager-loop.env` file without copying its values into Git. Before a
deploy, list the backup and test-extract it into a temporary directory.
Record the backup name immediately after creation.

Use these command forms with the approved paths and flags:

```bash
sudo -n <approved-backup-command> <approved-paths-and-flags>
sudo -n <approved-backup-list-command> <backup-path> <approved-flags>
sudo -n <approved-backup-extract-command> <backup-path> <temporary-directory> <approved-flags>
sudo -n <approved-restore-command> <backup-path> <approved-paths-and-flags>
```

For AP and switch update routes, send `enabled=true` as form data, then read
the record back to confirm the value. Match each lab inventory record by
comparing the access file's `address` field with the API list's `ip` key. The
switch is optional for an AP-only run. Workers must request sandbox escalation
for every HTTPS call to the dev host or a lab device. A worker `URLError`
usually means the sandbox blocked the request.

Route credential-bearing Tachyon curl calls through `_run_curl`, which sends
credentials outside process arguments. Unauthenticated readiness probes may
use direct HTTPS calls. Request sandbox escalation for both call types.

On Tachyon TNA-30x firmware 1.15.1, each user record has `username`,
`password`, `level`, `enabled`, and `first_login` fields. The password field
stores a 34-character MD5-crypt hash. Dry-run rejects plaintext passwords.
Set `first_login=false` for a new user that must log in. The published API
documentation shows different field names. A Tachyon session stays valid
after a password change.

## Manual Commands

### Owner integration write lane

Run integration write tests from the owner host against the authorized dev host
through the owner's integration runner. The runner reads the private lab access
file, exports the required `SIXTYOPS_TEST_*` variables for pytest only, and
prints no variable value. The runner lives in the owner's private state folder.

The lab has one access point (AP). Use it for the AP, firmware AP, config AP,
and RADIUS AP roles. Run write tests on this lab one at a time. Before each
write run, confirm that the lab AP and switch are enabled in the dev host
inventory, keep autoupdate, schedule, and scheduled readiness settings off,
and verify a full private backup.

Blocking lane:

```bash
SIXTYOPS_TEST_URL=https://<your-dev-host> \
SIXTYOPS_TEST_USER=<local-admin-user> \
SIXTYOPS_TEST_PASS=<local-admin-pass> \
SIXTYOPS_TEST_AP_IP=<ap-with-cpes> \
SIXTYOPS_TEST_SWITCH_IP=<dedicated-switch> \
SIXTYOPS_TEST_FIRMWARE_AP_IP=<firmware-test-ap> \
SIXTYOPS_TEST_CONFIG_AP_IP=<config-test-ap> \
SIXTYOPS_TEST_RADIUS_AP_IP=<radius-test-ap> \
pytest -m "integration and dev_blocking" -v --tb=no -rN --timeout=900
```

SSO lane:

```bash
SIXTYOPS_TEST_URL=https://<your-dev-host> \
SIXTYOPS_TEST_USER=<local-admin-user> \
SIXTYOPS_TEST_PASS=<local-admin-pass> \
SIXTYOPS_TEST_OIDC_PROVIDER_URL=<provider-url> \
SIXTYOPS_TEST_OIDC_CLIENT_ID=<client-id> \
SIXTYOPS_TEST_OIDC_CLIENT_SECRET=<client-secret> \
SIXTYOPS_TEST_OIDC_REDIRECT_URI=<redirect-uri> \
pytest -m "integration and dev_sso" -v --tb=no -rN
```

The RADIUS rollout test is excluded from `dev_blocking`. Select it explicitly
with `pytest -m "integration and dev_nonblocking"` when you need to run it.
It is not part of the merge-gating hardware workflow.

These options print each test name with its `PASSED`, `FAILED`, or `SKIPPED`
status without printing tracebacks or a short summary that could include
failure reasons or response bodies. Investigate a failure by rerunning only
that test in a private terminal.

## Integration reference

Integration tests in `tests/integration/` run against a live SixtyOps instance
with real Tachyon devices. They are skipped when `SIXTYOPS_TEST_URL` is unset.

**What the tests cover:**

| Test file | What it validates |
|-----------|-------------------|
| `test_smoke.py` | Health check, devices seen recently, no persistent errors |
| `test_device_polling.py` | Dedicated AP/switch poll coverage and CPE presence |
| `test_config_backup.py` | Config poll, snapshot history, diff, tar download |
| `test_config_push.py` | Preview and no-op config push on the dedicated config AP |
| `test_config_backup_restore.py` | Round-trip: poll config → restore same → verify hash |
| `test_manager_backup.py` | Export CSV backup, verify it contains live device IPs |
| `test_firmware.py` | Upgrade dedicated firmware AP and roll back (**slow**) |
| `test_cpe_lifecycle.py` | Dedicated AP CPE signal/auth coverage |
| `test_system_surface.py` | Dashboard, portal, device history, reports, analytics |
| `test_live_crud.py` | Tower-site, device-group, and bulk-device CRUD with cleanup |
| `test_radius_live.py` | RADIUS CRUD, defaults, targeted rollout, and restore |
| `test_sso_live.py` | Separate non-blocking OIDC config and login affordance checks |

**Safety:** The blocking lane mutates only dedicated lab devices named by env
vars. Firmware tests restore the original version. Config restore tests push
back the same config. The RADIUS rollout lane is explicitly targetable so it
does not walk the whole enabled fleet.

**Environment variables:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `SIXTYOPS_TEST_URL` | *(none — tests skip)* | Base URL of the shared dev instance |
| `SIXTYOPS_TEST_USER` | *(required for live auth)* | Dedicated local admin username |
| `SIXTYOPS_TEST_PASS` | *(required for live auth)* | Dedicated local admin password |
| `SIXTYOPS_TEST_AP_IP` | *(required for `dev_blocking`)* | Dedicated AP with attached CPEs |
| `SIXTYOPS_TEST_SWITCH_IP` | *(required for `dev_blocking`)* | Dedicated switch |
| `SIXTYOPS_TEST_FIRMWARE_AP_IP` | *(required for `dev_blocking`)* | Dedicated firmware test AP |
| `SIXTYOPS_TEST_CONFIG_AP_IP` | *(required for `dev_blocking`)* | Dedicated config test AP |
| `SIXTYOPS_TEST_RADIUS_AP_IP` | *(required for `dev_blocking`)* | Dedicated RADIUS rollout AP |

## Workflow Contracts

- `.github/workflows/dev-hardware.yml` runs the merge-gating `dev_blocking`
  lane on `pull_request`, `workflow_dispatch`, and `schedule`. It does not run
  `dev_nonblocking` tests.
- `.github/workflows/dev-sso.yml` runs the separate `dev_sso` lane on `workflow_dispatch` and `schedule`.
- The Actions workflows accept repository variables or secrets for the non-sensitive host and device inputs, and secrets for passwords.
- On `pull_request`, `Dev Hardware Validation` soft-skips with a warning if the live-dev inputs are not configured yet.
- On `workflow_dispatch` and `schedule`, missing live-dev inputs are still treated as hard failures.

## UI checks

**Key points:** Three test files check the UI without a browser. They run in the
normal `pytest -v` unit lane. There is no browser harness.

**Detail:**

- `tests/test_ui_routes.py`: each `/api/` request in `monitor.html` matches an OpenAPI route and method.
- `tests/test_ui_render.py`: `/` and `/login` render with seeded data, show no template errors, list the expected panel ids, and every `/static/` asset resolves.
- `tests/test_ui_js.py`: `static/js/*.js` and the inline scripts parse with `node --check`. The tests skip with a clear reason when `node` is not installed.

## Branch Protection

GitHub branch protection is not repo-tracked. After merging the workflow, configure the `Dev Hardware Validation` check as a required status check in repository settings.

## Firmware engine and driver changes

**Key points:** Unit tests do not prove device behavior. Every engine or
driver change needs real Tachyon bench proof on the exact pull request head
before merge. Only the operator names bench devices.

**Detail:** Record the tested models, firmware, recovery evidence, and exact
commit in the pull request. Re-run bench proof after any change to the pull
request head. This is a target merge requirement. This documentation change
does not claim hardware proof. Use
[Expected Tachyon CPE and AP behavior](tachyon-expected-behavior.md) for the
read-only customer-router signals and their current limits.

## Read-only bench wrapper

**Key points:** The owner runs `scripts/bench/run-readonly-bench.sh` to get
read-only bench evidence on an exact commit. The wrapper reads the private
access file and never prints its values. The wrapper does not change device
firmware or configuration.

**Detail:**

Run it from a trusted checkout of `main`, with the checkout under test as
the current directory:

```bash
cd <checkout-under-test>
SIXTYOPS_BENCH_ACCESS_FILE=<private-file> \
  <main-checkout>/scripts/bench/run-readonly-bench.sh <full-commit-sha> \
  [--mode integration|local-poll|session-proof|local-switch|firmware-stop] \
  [--duration-min 35] [--stop-id N]
```

Guards, in this order:

1. `HEAD` must equal the given 40-character SHA, and the working tree must be
   clean. The wrapper checks this before it reads the access file.
2. The commit must not change `tests/integration/`, `tests/conftest.py`,
   `conftest.py`, or `pyproject.toml`.
3. The access file must be owned by the current user, have mode `600` or
   `400`, and be outside the repository. The wrapper parses its
   `Key: value` lines in a subshell. It does not `source` the file. It
   refuses the run if a value is shorter than 3 characters, because the
   filter does not replace such short values.
4. The integration and AP modes send output through `scripts/bench/redact.py`.
   The filter replaces every access-file value and IPv4 address. The switch
   mode emits fixed result fields only. The log and summary go to
   `SIXTYOPS_BENCH_SUMMARY_DIR`. The default is
   `sixtyops-bench` in `XDG_STATE_HOME`, or in `~/.local/state`.

Access file keys: `Manager URL`, `Manager CA file`, `Manager username`, `Manager password`,
`TNS-100 IP`, `AP IP`, `303L SM IP`, `303X SM IP`, `Shared device username`,
and `Shared device password`.

Modes:

- `integration` (default): runs the read-only integration tests against the
  dev host Manager: `test_smoke.py`, `test_device_polling.py`,
  `test_cpe_lifecycle.py`, `test_config_backup.py`, and
  `test_system_surface.py`, without the device portal test. The Manager URL
  must be the authorized dev host. This mode tests the deployed Manager, not
  the checked-out commit.
- `local-switch`: logs in once to the named TNS-100 switch with the exact
  commit's `TachyonClient`. It calls `get_device_info()` twice. After login,
  it permits only GETs for system status, bootbank information, and interface
  status. Both reads must identify a TNS-100 with a nonempty firmware version.
  The firmware and bank results must stay the same. It emits only fixed
  PASS/FAIL fields. It does not save model labels, versions, serials, MAC
  addresses, raw responses, or exception text.
- `firmware-stop`: pass `--stop-id N` to select one unresolved stop row. The
  wrapper creates the normal Manager auth session and audit entry, reads the
  expected serial from the stop list, then logs in to the AP and compares its
  serial. It verifies Manager with the supplied CA file and the Manager host
  name. It does not follow redirects. Login must return the expected local
  redirect and session cookie. It emits fixed status fields only. It does not
  change stop, inventory, schedule, or device data, and it does not resolve
  the stop.
- `local-poll`: logs in to the AP with the commit's driver and reads device
  information and the CPE list.
- `session-proof`: for the CPE session reuse proof
  ([PR 266](https://github.com/sixtyops/manager/pull/266)). It copies the
  commit to a temporary directory with a fresh database, adds only the AP,
  and runs the commit's poller for the set duration. The summary gives, per
  CPE, the count of `login()` calls, the count of reused sessions, and the
  time of each new login. It runs the poller only, not the web app, so it
  does not download firmware, check for self-updates, or open the RADIUS
  port. The proof logs out only sessions that its own login instrumentation
  opened.

For session-sensitive changes, run the exact-head session proof for at least
three minutes. Follow the session policy in that commit. Record login, reuse,
renewal, and logout recovery evidence in the sanitized summary.

For `local-switch` and `firmware-stop`, the wrapper also requires an owner-only
output directory with mode `700`. It creates a new log and summary with mode `600` before
login. Unsafe output permissions or a failed file creation stop the run before
device contact. `local-switch` uses the existing `TNS-100 IP`, `Shared device
username`, and `Shared device password` fields. `firmware-stop` uses `Manager
URL`, `Manager username`, `Manager password`, `AP IP`, `Shared device username`,
and `Shared device password`. It also requires `Manager CA file`, which must
give the absolute path to the trusted Manager CA certificate. Store the CA
file outside the repository in an owner-only directory. Provision the PEM from
the approved certificate source, then set the directory to mode `700` and the
file to mode `600`. For example, use `install -d -m 700` for the destination
directory and `install -m 600` to place the provisioned CA file there. Confirm
its fingerprint through a separate trusted channel. Do not paste certificate
or credential contents into commands, logs, or the repository. Keep the
directory and file owned by root or the user running the helper. The helper
rejects symlinks and group or world write access on the CA file and path
components. It reads the CA through verified file descriptors and uses only
that certificate for Manager TLS. The helper does not read the access file.
Missing or invalid CA files fail closed.
Missing fields fail the run. Inherited device inputs cannot replace missing
fields in the access file. A log write failure makes the lane fail even when
reads succeed.

The tool-only change can merge after the normal review and offline gates,
before a lab run. This makes the wrapper available from trusted `main`.
Keep route confirmation, account binding, and the exact-commit lab run as open
post-merge work. A credentialed run also requires a target driver that keeps
login secrets and tokens out of process arguments.

Run this mode only after the host operator confirms the intended switch route
and current account binding. Keep the output private. A successful read does
not prove firmware updates, configuration changes, or recovery. This mode does
not satisfy the other device and recovery gates for
[PR 558](https://github.com/sixtyops/manager/pull/558). A skipped or failed run
is not hardware proof.

`tests/test_bench_wrapper.py` checks the SHA refusal and that the allowlist
has no write test. The local modes run the commit's driver code with the
device login. Review the commit before you run them.

## Private access records

**Key points:** Keep the access manifest on the authorized private host. It
contains role names and references to existing credential fields. It does not
contain credential values or device inventory.

**Detail:** Store the manifest beside the existing private credential file.
Use an owner-only directory with mode `700` and a manifest with mode `600`.
Do not commit the manifest or its path. Do not copy usernames, passwords,
tokens, device addresses, or inventory into shared issues, pull requests, or
logs.

On the authorized dev host, the lab access manifest and credential files are
on local disk in owner-only folders in the operator's home directory. One
credential file covers the switch lanes; one covers the AP lanes. Check these
records before reporting access missing. Pass the correct file to the bench
wrapper with `SIXTYOPS_BENCH_ACCESS_FILE`. Do not ask the operator for access
already recorded there. Do not read, print, or copy these files. Only the
wrapper reads them.

Map each role to labels for fields that already exist in the private
credential file. Use references only. Do not copy secret values into the
manifest. This example shows the information to record. It does not define a
new manifest schema:

```yaml
role: <service-or-device-role>
credential_field_ref: <existing-field-label>
observation: <operator_reported|verified|failed|not_attempted>
observed_at_utc: <timestamp-or-unknown>
method: <method-or-unknown>
result: <result-or-unknown>
evidence_source: <source-or-unknown>
```

Append each new observation. Preserve earlier reports, successes, and failures.
Mark an operator report as `operator_reported`; it is not a verified login.
Mark a read or login as `verified` only after that operation succeeds for the
recorded role. Mark a failed operation as `failed`. Do not infer a cause from
an error code. Record unknown dates, methods, and sources as `unknown`.

For an update, write a temporary file in the same private directory. Set its
mode to `600`. Preserve the existing history. Validate the new record and its
field references without printing credential values. Keep the prior manifest
for rollback until the replacement passes read-back. Replace the manifest
atomically only after validation. Check its owner, modes, role names, field
references, and preserved history. Do not include manifest contents or its
path in shared output.

### Latest recorded lab access evidence

**Key points:** This dated record reports only the available evidence. It does
not establish access that was not verified.

**Detail:**

- On 2026-10-06 at 12:59 UTC, the switch discovery and bridge-table reads
  returned HTTP 200 and listed an AP neighbor. This does not verify direct AP
  access or account binding.
- On 2026-10-06 at 12:59 UTC, access-point and Manager login requests returned
  HTTP 401. Their causes are unknown. Account binding remains unresolved.
- The operator reports earlier successful access-point and Manager logins.
  Their dates and methods are unknown. These reports are not current
  verification.
- No connected subscriber module was contacted or identified. A Manager
  deployed-commit read was not verified.

## Deployment Reality

- Production install path is `/opt/sixtyops` via `scripts/install.sh` (defaults
  to `main`) and creates `sixtyops.service` systemd unit that runs compose up/down.
- Standard deployment command uses both compose files:
  `docker compose -f docker-compose.yml -f docker-compose.standalone.yml up -d --build`.
- Base compose publishes `8000/tcp` and `1812/udp`; standalone overlay adds
  nginx ports `80/443` and certbot renewal loop (12h). The base publishes
  are env-overridable: set `BIND_IP=<host-ip>`, `HOST_PORT=<port>`, and/or
  `RADIUS_HOST_PORT=<port>` to bind to a specific host IP / different host
  port without editing `docker-compose.yml` (which would leave the tree
  dirty and break the in-app self-update path). Set them in a `.env` file
  next to `docker-compose.yml` or export them in the shell that runs
  `docker compose up`: these are *Compose substitution* vars, not the
  service's own `environment:` block. If the deployment also has a
  `docker-compose.override.yml` re-publishing the same port, drop that
  override after switching to env vars or Compose's list-merge will append
  both entries.
- Persistent directories are bind mounts: `./data`, `./firmware`, `./backups`,
  plus nginx/certbot directories in standalone mode.
- Host directories must be writable by UID/GID `1500` (container `appuser`);
  installer/deploy scripts enforce this.
- Self-update requires `/var/run/docker.sock`, mounted repo at `/app/repo`,
  and compose files mounted into container.

## Local Docker development

**Docker stack**: there is no `dev-docker.sh`. Run the compose files from
[Deployment Reality](#deployment-reality) with `SEED_DATA=1` (development only, never on a
production host) for a production-like stack on the LAN.
Local seeded login: `admin / admin`. This local default does not apply to the
shared dev host or live integration tests.

Seed script (`scripts/seed_dev_data.py`) inserts sample sites, devices, CPEs,
config templates, and job history. It's idempotent: skips if data exists.
Runs automatically in the entrypoint when `SEED_DATA=1` is set.

If Docker socket errors appear, start Colima: `colima start`.
If port conflicts persist after `docker compose down`: `colima restart`.
