# Dev Hardware Validation

This repo now has two live-dev validation lanes:

- `dev_blocking`: merge-gating validation against the shared dev host and dedicated lab devices
- `dev_sso`: separate non-blocking SSO/OIDC validation lane

The authorized dev host is
[`https://sixtyops-dev.infra.treehouse.mn/`](https://sixtyops-dev.infra.treehouse.mn/).
Set `SIXTYOPS_TEST_URL` to this URL for the authorized dev validation lane.
This does not authorize tests against production devices.

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
pytest -m "integration and dev_blocking" -v --timeout=900
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
pytest -m "integration and dev_sso" -v
```

## Required Blocking Inputs

| Variable | Purpose |
|----------|---------|
| `SIXTYOPS_TEST_URL` | Shared dev base URL |
| `SIXTYOPS_TEST_USER` | Dedicated local admin for automation |
| `SIXTYOPS_TEST_PASS` | Dedicated local admin password |
| `SIXTYOPS_TEST_AP_IP` | Dedicated AP with attached CPEs |
| `SIXTYOPS_TEST_SWITCH_IP` | Dedicated switch for polling/portal coverage |
| `SIXTYOPS_TEST_FIRMWARE_AP_IP` | Dedicated AP safe for upgrade and rollback |
| `SIXTYOPS_TEST_CONFIG_AP_IP` | Dedicated AP safe for config poll/push/rollback |
| `SIXTYOPS_TEST_RADIUS_AP_IP` | Dedicated AP safe for targeted RADIUS rollout and restore |

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
before merge. Only Isaac names bench devices.

**Detail:** Record the tested models, firmware, recovery evidence, and exact
commit in the pull request. Re-run bench proof after any change to the pull
request head. This is a target merge requirement. This documentation change
does not claim hardware proof.

## Private access records

**Key points:** Keep the access manifest on the authorized private host. It
contains role names and references to existing credential fields. It does not
contain credential values or device inventory.

**Detail:** Store the manifest beside the existing private credential file.
Use an owner-only directory with mode `700` and a manifest with mode `600`.
Do not commit the manifest or its path. Do not copy usernames, passwords,
tokens, device addresses, or inventory into shared issues, pull requests, or
logs.

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
