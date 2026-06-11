# Self-Update Signing

**Bottom line:** Every manager release from **v1.4.0** onward must be a
GPG-signed git tag from the trusted release key. The manager refuses to install
an unsigned or untrusted update at or after that version, and CI refuses to
publish one. Older releases stay installable so rollback always works.

The self-update path checks out a release tag and rebuilds from it, so the tag
*is* the code that runs on the host (which holds the Docker socket). Signing the
tag makes that code authentic: a compromised GitHub, a forged tag, or a
man-in-the-middle can no longer push code to the fleet, because only releases
signed by the release key are accepted. Trust is verified in two independent
places — the manager before it checks out, and CI before it publishes.

## How it works

- The **release key** is an OpenPGP key held only by the release maintainer. Its
  **public** key is committed at `updater/trusted_keys/release-signing.pub.asc`;
  its fingerprint is in `TRUSTED_SIGNING_FINGERPRINTS` (`release_checker.py`).
- The maintainer signs each release tag: `git tag -s vX.Y.Z -m "vX.Y.Z"`.
- **Client gate** (`updater/release_checker.py:_verify_tag_signature`): before
  `git checkout`, the manager imports the trusted public key into a throwaway
  keyring and runs `git verify-tag`. It proceeds only on a good signature from a
  fingerprint on the allowlist — otherwise it blocks and tells the operator.
- **CI gate** (`.github/workflows/release.yml`): the `release` job verifies the
  tag the same way and fails the release if it isn't trusted, so an unsigned
  release is never published (no GitHub Release, no GHCR image).

## The cutover (why nothing in the field breaks)

Enforcement is gated on `MIN_SIGNED_VERSION` (currently `1.4.0`). Releases below
it predate signing and install without a signature check; releases at or after
it require one. Because updates only move forward, every real update past the
cutover is verified, while no deployed instance is ever stranded:

- An instance on old (pre-signing) code updates to the first signed release with
  no check — it's running old code. It lands on signed code, and every update
  after that is verified.
- Existing unsigned releases remain installable for rollback/reinstall.

Verification **fails closed** at/after the cutover: if the signing tool, the
trusted key, or a valid signature is missing, the update is blocked rather than
applied unverified. `gpg` and `git` ship in the image, so a signed instance can
always verify the next release.

Note: PEP 440 orders `1.4.0-devN` *below* `1.4.0`, so 1.4.0 pre-releases fall
under the cutover floor. Sign them anyway — sign everything from 1.4.0 onward.

## Key custody and rotation

- Keep the **private** key offline (password manager / hardware token), never in
  the repo or CI. CI and clients only ever need the public key.
- **Rotate** by generating a new key, committing its public key to
  `updater/trusted_keys/`, and adding its fingerprint to the allowlist. Keep the
  old fingerprint until every fielded release has moved past it, then remove it.

## If a bad release ships

Cut and sign a higher patch release with the fix; the fleet updates forward to
it. To pull a release, delete its GitHub Release/tag so the checker stops
offering it. Operators can roll a single host back via the in-app rollback
(the prior ref is saved before every update).

Related: [release-system.md](release-system.md), [deployment.md](deployment.md).
