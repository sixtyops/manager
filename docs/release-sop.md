# Release SOP

**Bottom line:** every release is a **GPG-signed tag on `main`**: merge the
version-bump PR first, then sign the tag at `origin/main` and push the tag by
name. Dev tags publish automatically; stable tags need the manual workflow
approval. Unsigned tags are refused — by CI and by every fielded instance.

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
3. The tag push auto-runs **Release**: tests → signature gate → GitHub
   pre-release → `ghcr.io/sixtyops/manager:vX.Y.Z-devN` + `:dev`. Watch it
   under Actions (`gh run watch`).

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

## If the release run fails

Nothing publishes on failure — the gate is fail-closed, so a failed run is
safe, just dead. Fix forward:

1. Land the fix on `main` via PR, bump to the next `devN`.
2. Delete the dead tag, then cut the new one per the steps above:
   ```bash
   git push origin :refs/tags/vX.Y.Z-devN
   ```
   The remote prints `Bypassed rule violations` — that's the tag ruleset
   logging your admin bypass, expected.
3. Gotcha: tag pushes run the workflow **from the tagged commit**, not from
   `main` — a workflow fix only takes effect for tags created after it merged.
