# Release SOP

**Key points:** This procedure is for the operator or a lead with explicit
release approval. Loop workers must not bump the version, create, move, or
push a release tag, or run the Release workflow. A merge grant does not grant
release authority. Follow [Agent rules](../AGENTS.md#agent-rules).

**Detail:** The steps below describe the approved release process.

**Bottom line:** every release is a **GPG-signed tag on `main`**: merge the
version-bump PR first, then sign the tag at `origin/main` and push the tag by
name. Dev tags start validation and publish only after it passes; stable tags
need the manual workflow approval. Unsigned tags are refused by CI and by
every fielded instance.

The tag *is* the code customers run: self-update checks it out and rebuilds on
hosts that hold the Docker socket. The signature is what makes that safe, so
the order and the signing flags below are load-bearing, not style. Dev-channel
installs pick up pre-releases automatically; stable-channel installs only see
full releases. How the machinery works: [release-system.md](release-system.md);
trust model: [self-update-signing.md](self-update-signing.md).

## One-time setup (per release machine)

- Import the release signing key (fingerprint
  `E2C19E86B24A87283068AEFAF2D6F70883960DCE`) into your gpg keyring.
- Give gpg a passphrase prompt that works where you cut releases:
  - **Interactive terminal:** add `export GPG_TTY=$(tty)` to your shell rc —
    without it the prompt fails with "Inappropriate ioctl for device".
  - **macOS, including non-interactive shells** (agent sessions and scripts
    have no tty, so terminal pinentry can't prompt and signing just fails):
    ```bash
    brew install pinentry-mac
    echo "pinentry-program $(brew --prefix)/bin/pinentry-mac" >> ~/.gnupg/gpg-agent.conf
    gpgconf --kill gpg-agent
    ```
    The passphrase dialog pops on the desktop — someone must be at the
    machine to enter it.

## Dev release (`vX.Y.Z-devN`)

1. **Bump on a branch, merge the PR first.** Update `updater/__init__.py`,
   `pyproject.toml`, and the pinned image tag in `website/index.html`. Tagging
   before the bump is on `main` creates a tag whose code claims the wrong
   version — clients refuse it.
2. **Sign the tag at `origin/main` and verify before pushing:**
   ```bash
   git fetch origin
   git -c gpg.format=openpgp -c user.signingkey=E2C19E86B24A87283068AEFAF2D6F70883960DCE \
     tag -s vX.Y.Z-devN -m "vX.Y.Z-devN" origin/main
   git verify-tag vX.Y.Z-devN   # expect: Good signature ... SixtyOps Manager Release Signing
   git push origin vX.Y.Z-devN  # push the tag by name — never `--tags`
   ```
   The `-c` overrides force the GPG release key; without them git uses the
   machine's default (e.g. SSH/1Password) and produces a tag the gate rejects.
3. The tag push auto-runs **Release**: tests and development-instance
   validation → signature gate → GitHub pre-release →
   `ghcr.io/sixtyops/manager:vX.Y.Z-devN` + `:dev`. The signed tag must exist
   to start validation. The workflow creates no GitHub Release or GHCR image,
   and does not move `:dev`, until validation passes. Watch it under Actions
   (`gh run watch`).

## Stable release (`vX.Y.Z`)

1. Bump to the stable version and move CHANGELOG `Unreleased` items under a
   version header. PR → merge.
2. Sign + verify + push the tag exactly as above.
3. **Actions → Release → Run workflow**, enter the tag, type `RELEASE`.
4. Publishes the GitHub Release and `:vX.Y.Z` + `:latest`; the non-prerelease
   publish also triggers the appliance build.

## Verify

- `gh release view vX.Y.Z-devN` — release exists, correct prerelease flag.
- Image tags: `gh api "/orgs/sixtyops/packages/container/manager/versions?per_page=3" --jq '.[].metadata.container.tags'`
- In-app: Settings → Updates on the dev host offers/applies the new version.

## If release validation fails

A failed validation leaves the owner-signed tag in the repository without a
GitHub Release or GHCR image. The signed tag is the trust root; the workflow
cannot validate before the tag exists. Fix forward:

1. Read the failed validation output and address the cause.
2. Land any code or workflow fix on `main` via PR, then bump to the next `devN`.
3. Sign and push the next tag per the steps above. Do not delete or move the
   failed signed tag. A tag push runs the workflow from that tagged commit, so
   workflow fixes apply only to tags created after they merge.

If a later release stage fails after validation passes, check Actions and the
GitHub Release and GHCR state before retrying. Fix forward with the next
version; do not assume that all publication steps completed together.

## Version conventions

- **App version (Dev)**: `X.Y.Z-devN` in code, `vX.Y.Z-devN` in tags
- **App version (Stable)**: `X.Y.Z` in code, `vX.Y.Z` in tags
- `updater/__init__.py` is the source of truth for app version
- Default release-check repo is `sixtyops/manager` (`GITHUB_REPO` can override)
- Self-update checks out git tags from the mounted source repo (`/app/repo`),
  so release tags must exist in the code repo.

## Release notes

- **Dev releases**: auto-generated from commit messages between tags.
  Each release also links to the GHCR Docker image.
- **Stable releases**: auto-generated by GitHub from PR titles, categorized
  by label (`feature`, `bug`, `chore`, `docs`, `ci`) via `.github/release.yml`.
- **PR labels are auto-applied** by `.github/workflows/auto-label.yml` from
  conventional commit prefixes in the PR title (`feat:` → `feature`,
  `fix:` → `bug`, `chore:` → `chore`, etc.). No manual labeling needed.
- **Write clear PR titles**: they become the stable release note line items.
  Use conventional format: `feat: add X`, `fix: resolve Y`, `chore: update Z`.
- **CHANGELOG.md**: Update the `## Unreleased` section when making notable
  changes. When cutting a release, move the Unreleased items under a new
  version header (e.g., `## 1.2.0 - 2025-03-15`).
- Release notes are displayed inline in the app's Settings > Updates panel.
  The backend fetches up to 2000 characters of the GitHub release body.
