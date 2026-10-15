# Repository Hardening

**Bottom line:** `main` and the `v*` release tags are protected by GitHub
repository rulesets. Nobody can force-push or delete `main`, changes land via
pull request, and **only admins can create or move release tags** — which is
what the fleet's self-update trusts. This closes the "unprotected trust root"
critical from the architecture review.

The manager self-updates by checking out a release tag, so whoever can push a
`v*` tag can ship code to every customer host. Previously `main` had no
protection and anyone with write access could create a release tag. These
rulesets, paired with signed-tag verification (`docs/self-update-signing.md`),
make the release path tamper-resistant: a tag must be both admin-created and
GPG-signed before any instance will install it.

## What is enforced

| Ruleset | Target | Rules |
|---------|--------|-------|
| Protect main | default branch (`main`) | block deletion, block force-push (non-fast-forward), require pull request (0 approvals) |
| Protect release tags | `refs/tags/v*` | restrict creation, update, deletion, and force-moves to admins |

- **Require pull request (0 approvals)** enforces "no direct pushes to `main`"
  without blocking a solo maintainer — you can still open and merge your own PR.
- **Admin bypass** is enabled on both rulesets so a maintainer is never locked
  out in an emergency. Uptime over rigidity; the rules still catch accidents and
  block non-admins by default.

## Applying or changing it

Rulesets are managed under **Settings → Rules → Rulesets**, or via the API:

```bash
gh api repos/sixtyops/manager/rulesets                 # list
gh api repos/sixtyops/manager/rulesets/<id>            # inspect
gh api -X POST repos/sixtyops/manager/rulesets --input ruleset.json   # create
```

The admin bypass actor is the built-in Admin repository role (`actor_id: 5`,
`actor_type: RepositoryRole`, `bypass_mode: always`).

## CI signature gate

Defense-in-depth that mirrors the client check: the `release` job in
`.github/workflows/release.yml` (the source of truth — no copy here) verifies
the tag is signed by a trusted key and **fails the release** otherwise, so an
unsigned release at/after the cutover is never published. Keep its `CUTOVER`
in sync with `MIN_SIGNED_VERSION` in `updater/release_checker.py`.

Two self-hosted-runner constraints shaped the step — don't "simplify" them away:

- **No `pip install`.** The runner's Python is externally managed (PEP 668),
  so the cutover comparison uses `sort -V` instead of the `packaging` module.
  `sort -V` deviates from PEP 440 only for pre-releases of the cutover version
  itself (e.g. `1.4.0-devN`), where it over-enforces — the safe direction.
- **Re-fetch the tag before `git verify-tag`.** `actions/checkout` rewrites
  `refs/tags/<tag>` to point at the bare commit, which makes `verify-tag` fail
  with "cannot verify a non-tag object". The step runs
  `git fetch origin --force "refs/tags/$TAG:refs/tags/$TAG"` first to restore
  the annotated tag object.

## Visibility

The repo is intentionally **public** — `scripts/install.sh` clones anonymously
and the GHCR image is public. The real exposure was the *unprotected* trust
root, not public visibility, so visibility is unchanged. Keep it that way unless
the install/image-pull flow is reworked to require credentials.

## Planned security scan acceptance

**Key points:** The security workflow is not installed. The checks below are
planned acceptance criteria, not passing scan results. The
[security baseline issue](https://github.com/sixtyops/manager/issues/409)
tracks installation and full evidence.

**Detail:** Accept the baseline only after these checks pass:

1. Run gitleaks on every PR commit range with full checkout history and
   `--redact`. Pin the binary version and verify its SHA-256 checksum. Fail on
   findings or scanner/report errors. Prove that a fake token fails a hosted PR
   job on a scratch branch, then remove that test branch.
2. Run one manual full-history scan with `--redact`. Public output must contain
   only JSON-escaped file, rule ID, and commit metadata. Suppress direct scanner
   output. Upload no raw report. Verify report cleanup on success, failure,
   parse errors, and catchable cancellation. Abrupt runner loss cannot run
   cleanup traps; use ephemeral hosted runners.
3. Run pinned actionlint and zizmor on all workflows. Fail on actionlint errors
   and high-severity zizmor findings. Fix findings or record a narrow, reviewed
   exception with a reason. Permit only confirmed gitleaks false positives with
   a reason; do not allow broad path or rule exclusions.
4. Run pip-audit on `requirements.txt`. Fail on known vulnerabilities. An
   exception needs an advisory ID and a reason when no fixed version exists.
   Verify fresh Linux dependency resolution and online zizmor rules.

The 2026-10-02 preparation record in the baseline issue reports synthetic
redaction/cleanup tests, offline workflow lint, and an audit of a recorded
resolved dependency set. The release-input and SNMP dependency prerequisites
merged through [PR 438](https://github.com/sixtyops/manager/pull/438) and
[PR 441](https://github.com/sixtyops/manager/pull/441). The scratch candidate is
not present for current verification. These historical results do not prove
an installed workflow, current scan success, or full-history acceptance.

Installation still needs approved write access to
`.github/workflows/security.yml` and independent review of the exact candidate.
Use `permissions: contents: read` and full commit SHA action pins. Hosted PR
jobs and one redacted full-history run remain required. The workflow publication
hold in the baseline issue remains in force. Report a real leak to the operator;
do not rotate a secret. Keep historical findings and secret values out of this
document.
