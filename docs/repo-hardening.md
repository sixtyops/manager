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

## CI signature gate (apply once to release.yml)

Defense-in-depth that mirrors the client check: the `release` job verifies the
tag is signed by a trusted key and **fails the release** otherwise, so an
unsigned release at/after the cutover is never published. Add this step to the
`release` job in `.github/workflows/release.yml`, right after the
`actions/checkout@v4` step (which already uses `fetch-depth: 0`):

```yaml
      - name: Verify release tag is signed by a trusted key
        run: |
          set -euo pipefail
          TAG="${{ github.event_name == 'workflow_dispatch' && inputs.tag || github.ref_name }}"
          VER="${TAG#v}"
          CUTOVER="1.4.0"  # keep in sync with release_checker.py MIN_SIGNED_VERSION
          pip install --quiet packaging
          REQUIRED=$(python3 -c "from packaging import version as v; print('1' if v.parse('$VER') >= v.parse('$CUTOVER') else '0')")
          if [ "$REQUIRED" != "1" ]; then
            echo "Tag $TAG predates the signing cutover $CUTOVER — skipping (legacy)."; exit 0
          fi
          export GNUPGHOME="$(mktemp -d)"; chmod 700 "$GNUPGHOME"
          gpg --batch --quiet --import updater/trusted_keys/*.asc
          if git -c gpg.format=openpgp verify-tag "$TAG"; then
            echo "Tag $TAG carries a trusted release signature."
          else
            echo "::error::Release tag $TAG is not signed by a trusted release key. Refusing to publish (see docs/self-update-signing.md)."; exit 1
          fi
```

## Visibility

The repo is intentionally **public** — `scripts/install.sh` clones anonymously
and the GHCR image is public. The real exposure was the *unprotected* trust
root, not public visibility, so visibility is unchanged. Keep it that way unless
the install/image-pull flow is reworked to require credentials.
