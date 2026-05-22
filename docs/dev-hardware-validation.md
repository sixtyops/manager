# Dev Hardware Validation

This repo now has two live-dev validation lanes:

- `dev_blocking`: merge-gating validation against the shared dev host and dedicated lab devices
- `dev_sso`: separate non-blocking SSO/OIDC validation lane

The dev host URL is whatever the operating team has put behind
`SIXTYOPS_TEST_URL` — no shared URL is committed in this repo.

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
- `.github/workflows/dev-host-deploy.yml` manually deploys a branch to the shared dev host, or restores the host to the latest `-dev` tag.
- `.github/workflows/dev-host-drift.yml` checks daily that the shared dev host is running the latest `-dev` tag.
- The Actions workflows accept repository variables or secrets for the non-sensitive host and device inputs, and secrets for passwords.
- `Dev Hardware Validation` and `Dev Host Deploy` share one queue because both mutate the same dev host and lab devices; during busy periods, later runs will wait instead of overlapping.
- Missing `dev_blocking` inputs are hard failures in every trigger mode.

Manual deploy requires `SIXTYOPS_DEV_HOST`, `SIXTYOPS_DEV_USER`, `SIXTYOPS_DEV_SSH_KEY`, and optionally `SIXTYOPS_DEV_DEPLOY_DIR` if the install is not in `/opt/sixtyops`.

## Branch Protection

GitHub branch protection is not repo-tracked. After merging the workflow, configure the `Dev Hardware Validation` check as a required status check in repository settings.
