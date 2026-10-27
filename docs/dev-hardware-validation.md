# Dev Hardware Validation

**Key points:** This procedure is for the operator or a lead with explicit
approval for the named host, devices, and tests. Loop workers must not run
`tests/integration` or touch live hardware, a dev host, or a production install.

**Detail:** Commands and reference tables below do not grant access. Follow
[Agent rules](../AGENTS.md#agent-rules). Use existing private credentials
without putting secret values on a command line.

This repo now has two live-dev validation lanes:

- `dev_blocking`: merge-gating validation against the shared dev host and dedicated lab devices
- `dev_sso`: separate non-blocking SSO/OIDC validation lane

The authorized dev host is
[`https://sixtyops-dev.infra.treehouse.mn/`](https://sixtyops-dev.infra.treehouse.mn/).
Set `SIXTYOPS_TEST_URL` to this URL for the authorized dev validation lane.
Use this lane only with its authorized test devices.

## Dev host access

The lead host reaches the authorized dev host through a recorded SSH alias and
its approved network path. The alias, host details, and access record are on
the lead host's local disk and in the private system record. Check those
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

## Manual Commands

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
pytest -m "integration and dev_blocking" -v -rA --tb=no --timeout=900
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
pytest -m "integration and dev_sso" -v -rA --tb=no
```

If a test fails, investigate it by rerunning only that test in a private
terminal. When sharing a failed lane result, include test names and short
failure reasons only. Do not include credentials, request or response bodies,
or raw logs.

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

- `.github/workflows/dev-hardware.yml` runs the merge-gating `dev_blocking` lane on `pull_request`, `workflow_dispatch`, and `schedule`.
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
before merge. Only the owner names bench devices.

**Detail:** Record the tested models, firmware, recovery evidence, and exact
commit in the pull request. Re-run bench proof after any change to the pull
request head. This is a target merge requirement. This documentation change
does not claim hardware proof.

## Read-only bench wrapper

**Key points:** The lead runs `scripts/bench/run-readonly-bench.sh` to get
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
  [--mode integration|local-poll|session-proof|local-switch] [--duration-min 35]
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

Access file keys: `Manager URL`, `Manager username`, `Manager password`,
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
- `local-poll`: logs in to the AP with the commit's driver and reads device
  information and the CPE list.
- `session-proof`: for the CPE session reuse proof
  ([PR 266](https://github.com/sixtyops/manager/pull/266)). It copies the
  commit to a temporary directory with a fresh database, adds only the AP,
  and runs the commit's poller for the set duration. The summary gives, per
  CPE, the count of `login()` calls, the count of reused sessions, and the
  time of each new login. It runs the poller only, not the web app, so it
  does not download firmware, check for self-updates, or open the RADIUS
  port.

For `local-switch`, the wrapper also requires an owner-only output directory
with mode `700`. It creates a new log and summary with mode `600` before
login. Unsafe output permissions or a failed file creation stop the run before
device contact. It uses the existing `TNS-100 IP`, `Shared device username`,
and `Shared device password` fields. Missing fields fail the run. Inherited
device inputs cannot replace missing fields in the access file. A log write
failure makes the lane fail even when both device reads succeed.

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
wrapper with `SIXTYOPS_BENCH_ACCESS_FILE`. Do not ask the owner for access
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
