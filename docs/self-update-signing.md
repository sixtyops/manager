# Self-Update Signing

**Bottom line:** Every manager release from **v1.4.0** onward must be a
GPG-signed git tag from the trusted release key. The manager refuses to install
an unsigned or untrusted update at or after that version, and CI refuses to
publish one. Older releases remain eligible for reinstall; this does not prove
that a host can recover or that its data is compatible with older code.

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

**Key points:** Manager has no app rollback button or app rollback API.
Verify host recovery before deployment.

**Detail:** Cut and sign a higher patch release with the fix; the fleet updates forward to
it. To pull a release, delete its GitHub Release/tag so the checker stops
offering it.

The guarded recovery changes in
[PR 451](https://github.com/sixtyops/manager/pull/451) fixed the defects recorded
in [issue 450](https://github.com/sixtyops/manager/issues/450). The updater now
pins the healthy running container's immutable image ID before source
checkout/build or appliance pull. A missing recovery image, host path, or
watchdog prevents an automatic update. There is no direct update fallback.
The watchdog checks the retained image ID before mutation. It handles build,
initial swap, and health-check failures explicitly. A failed source build
restores the saved source ref and image alias without swapping the old
container. Swap or health failure attempts source/image recovery and restart.
Failed recovery retains the prior image and reports failure.

An acknowledged launch saves the exact watchdog container ID. Status and
apply calls reconcile only a proven stopped daemon with a recognized terminal
outcome and verified healthy runtime. Failure recovery also requires the
prior immutable image and restored source where applicable. Cleanup removes
only that stopped ID, without force, and is read back before pending state
clears. Lost acknowledgements, legacy attempts without a validated ID,
missing proof, or failed cleanup/readback/persistence remain blocked and can
require operator reconciliation on the host. Neither elapsed time nor daemon
absence nor a version match alone permits retry. A launch response means
initiation, not verified final health.

The shared process lock covers the shipped single-Uvicorn-worker app and its
request threads. It does not coordinate multiple app processes or external
host operations. Completion messages are best effort. A failure after safe
pending cleanup can leave stale availability or omit the message; it does
not release an unknown or active watchdog for retry. See the
[final safety review](https://github.com/sixtyops/manager/pull/451#pullrequestreview-5398501930)
and [exact-head CI](https://github.com/sixtyops/manager/actions/runs/37087732268).
Their synthetic command/daemon evidence is not a host or data recovery drill.
Appliance image-integrity verification remains a separate follow-up; the
git-tag signing gate does not establish authenticity of a pulled image.

Operator recovery must match the verified install shape. A source/Compose
host needs the prior source ref, a retained working image, and the actual
Compose files and host path. An image-only host can restore a retained
container or re-pin its previous image as described in
[deployment.md](deployment.md#updating-an-image-based-install).
Before deployment, verify a restorable complete data backup, including the
database and encryption key. Code or image rollback does not restore data
or prove compatibility. Verify runtime version and health after recovery.

These are recovery paths in the code and documentation, not evidence of a
successful host recovery drill. See the
[dev5 readiness limits](release-system.md#dev5-draft-notes-and-readiness).

Related: [release-sop.md](release-sop.md) (how to cut a release),
[release-system.md](release-system.md), [deployment.md](deployment.md).
