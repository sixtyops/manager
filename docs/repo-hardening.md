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
