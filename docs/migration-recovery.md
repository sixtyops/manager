# Migration Recovery

**Key points:** First try a restart after a migration failure. If startup still
fails, restore a verified matching database, encryption key, and firmware set.
A restore loses writes after its backup cutoff. Keep deployment on hold until
an isolated restore drill proves the recovery procedure.

**Detail:** Reserve manual schema repair for operator-led diagnosis. A successful
SFTP status or database integrity check does not prove complete recovery.

The migration system in `updater/database.py::init_db` uses `CREATE TABLE IF NOT EXISTS`, `CREATE TRIGGER IF NOT EXISTS`, and a `PRAGMA table_info()` check before every `ALTER TABLE`. Each step is safe to re-run, so a crash partway through leaves a partially-migrated DB that the next app start completes. The contract is locked in by `tests/test_migration_failures.py`.

## Detection

Symptoms that point to a migration problem:

- The `sixtyops-mgmt` container restarts on a loop and the logs end in `sqlite3.OperationalError`.
- Logs include `Database integrity check failed` or `Database integrity check error`.
- An API call after a self-update returns a 500 with `no such column: ...` or `no such table: ...`.

Verify the schema by hand from the host:

```bash
docker compose exec sixtyops-mgmt sqlite3 /app/data/sixtyops.db "PRAGMA integrity_check;"
docker compose exec sixtyops-mgmt sqlite3 /app/data/sixtyops.db ".schema devices"
```

`integrity_check` returning `ok` means the file is structurally fine — the problem is migration state, not corruption, and Path 1 below will resolve it.

## Recovery

### Path 1 — restart the container (try first)

```bash
docker compose restart sixtyops-mgmt
docker compose logs sixtyops-mgmt --tail=100
```

`init_db()` runs at every startup and adds whatever columns or triggers are missing. If the restart succeeds and `/healthz` returns 200, the recovery is complete.

### Path 2: restore a matching recovery bundle

**Key points:** Restore one matching SQLite database, `.encryption_key`, and
complete firmware directory. This procedure requires operator approval and an
isolated restore drill. No host restore drill has verified this procedure.
Keep deployment on hold until the drill and required live validation pass.

**Detail:** SFTP `last_status=success` does not prove complete recovery. The
archive can omit a missing database or key. It excludes `./firmware`. Use an
SFTP archive only with a separately captured matching firmware directory and
private evidence that ties all members to the same backup cutoff. Stop if that
evidence is missing. Never replace only the database.

#### Capture before migration

**Key points:** Stop all writers before capture. Preserve the original files.

**Detail:** Run these steps from the deployment directory. Use the host's
actual Compose files and service controller. The examples use the base
Compose file. These commands require host access; they are not offline checks.

1. Record the current code commit, image digest, candidate version, rollback
   version, UTC cutoff, deployment paths, and restore decision in a private
   record. A restore loses every write after the cutoff. A code rollback can
   keep the additive artifact table, but does not undo data migrations. Test
   the chosen rollback code against the bundle before approval.
2. Stop external writers and timers that can restart the app. Stop the app
   with `docker compose stop sixtyops-mgmt`. Check
   `docker inspect --format '{{.State.Running}}' sixtyops-management`; it must
   return `false`. Confirm that no other container, process, or open writable
   file handle uses `./data` or `./firmware`. If shutdown is incomplete, stop.
3. Create a new private capture directory outside the deployment directory.
   Set `umask 077`. Copy both original directories, including any
   `sixtyops.db-wal` and `sixtyops.db-shm`, into `original/`. Keep this copy
   unchanged. Create a separate `payload/` copy for restoration.

   ```bash
   umask 077
   capture_dir=$(mktemp -d /var/tmp/sixtyops-recovery.XXXXXX)
   mkdir "$capture_dir/original" "$capture_dir/payload"
   cp -a ./data ./firmware "$capture_dir/original/"
   cp -a ./data ./firmware "$capture_dir/payload/"
   ```

4. Require a nonempty database and key. Reject links or special files in the
   payload. Create a consistent SQLite snapshot with Python's SQLite backup
   API. It includes committed WAL contents. Replace only the payload database
   with this snapshot. Remove only the payload WAL/SHM copies. Preserve the
   original files from step 3.

   ```bash
   python3 - "$capture_dir" <<'PY'
   import sqlite3
   import sys
   from contextlib import closing
   from pathlib import Path

   root = Path(sys.argv[1])
   for path in (root / "payload").rglob("*"):
       if path.is_symlink() or not (path.is_file() or path.is_dir()):
           raise SystemExit("Unsafe payload member")
   for name in ("sixtyops.db", ".encryption_key"):
       if (root / "original/data" / name).stat().st_size == 0:
           raise SystemExit("Required recovery member is empty")
   with closing(sqlite3.connect(str(root / "payload/data/sixtyops.db"))) as source:
       with closing(sqlite3.connect(str(root / "snapshot.db"))) as target:
           source.backup(target)
   (root / "snapshot.db").replace(root / "payload/data/sixtyops.db")
   for suffix in ("-wal", "-shm"):
       (root / ("payload/data/sixtyops.db" + suffix)).unlink(missing_ok=True)
   PY
   ```

5. Add the private version/cutoff record to the payload. Generate a private
   SHA256 manifest of every payload file, including the database and key.
   Validate the payload as described below. Copy the bundle, manifest, and
   unchanged originals to protected storage off the host. Compare checksums
   after transfer. Restart the original app only after capture succeeds.
   Keep file paths, the key, encrypted records, and the manifest private.

   ```bash
   python3 - "$capture_dir/payload" <<'PY'
   import hashlib
   import json
   import sys
   from pathlib import Path

   root = Path(sys.argv[1])
   hashes = {}
   for path in sorted(root.rglob("*")):
       if path.is_file():
           digest = hashlib.sha256()
           with path.open("rb") as stream:
               for block in iter(lambda: stream.read(1024 * 1024), b""):
                   digest.update(block)
           hashes[str(path.relative_to(root))] = digest.hexdigest()
   (root.parent / "manifest.json").write_text(json.dumps(hashes, indent=2))
   (root.parent / "manifest.json").chmod(0o600)
   PY
   ```

#### Validate staging before replacement

**Key points:** Validate a fresh private staging directory. Reject an incomplete
or mismatched bundle before replacing deployment files.

**Detail:** Verify the bundle identity against the private capture record.
Before archive extraction, inspect every member. Allow only regular files and
directories below the expected `data/`, `firmware/`, and metadata paths. Reject
absolute paths, `..`, duplicate names, links, special files, and unexpected
members. Use Python 3.12 or later with `tarfile`'s `filter="data"` as a second
check. Do not extract directly into deployment directories.

Compare the complete staged file set and every SHA256 with the manifest.
Reject missing or extra files. Require `data/sixtyops.db`, its matching
`data/.encryption_key`, the complete firmware directory, and the private
version/cutoff record. Reject staged WAL/SHM files.

Open the staged database read-only. Require exactly `ok` from
`PRAGMA integrity_check`. Load the staged key with `cryptography.fernet.Fernet`.
For every encrypted value in existing database records, call `Fernet.decrypt`
directly and discard the plaintext. Include device passwords, settings,
device configs, and config-template fields. Report only the count and pass or
fail. Reject an invalid key or any failed decryption. Application read helpers
can tolerate legacy values; they do not prove that every key matches.
If no encrypted record exists, record that key compatibility is unproven and
obtain a private known-ciphertext check from the matching capture.

Hash each currently registered firmware file and compare it with the registry
SHA256. Require a valid hash and a present file for every configured firmware
target. Where an artifact row exists, confirm that its SHA256 matches the
current registry row. Historical artifact rows can describe replaced bytes;
their old paths do not prove the current file identity. The private manifest
also covers files outside the registry. Reject current file hash mismatches.

#### Replace and verify

**Key points:** Keep reversal possible. Restore the complete set while the app
and all other writers remain stopped.

**Detail:** After staging validation and the drill pass:

1. Repeat the shutdown and writer checks from capture step 2. Record the
   current cutoff. Rename the entire existing `./data` and `./firmware`
   directories into a new protected reversal directory on the same filesystem.
   Preserve their WAL/SHM files. Never mix those files with the staged database.
2. Copy the validated staged `data/` and `firmware/` into the deployment
   directory. Set ownership to the runtime UID/GID, `1500:1500` for the standard
   install. Set directories to `0700` and files to `0600`. Recheck the manifest,
   ownership, key permissions, integrity, decryption, and firmware hashes.
3. Start the recorded candidate image. Check startup logs and migrations,
   `/healthz`, local login, credential decryption, artifact attribution, and
   configured firmware targets. Repeat a full stop/start and check again.
   A health response alone does not prove recovery. Keep job execution blocked
   until the restored state passes the checks.
4. If a check fails, stop all writers and verify shutdown again. Preserve the
   failed restored directories under new names. Put both original directories
   back from the reversal directory. Start the recorded original image and
   repeat the same checks. Record the failed check and reversal result.
5. Record the restored cutoff and loss of later writes. Retain the recovery
   bundle and original directories. Resume only the operator-approved services
   and timers after all checks pass. Publish counts and results only.

#### Isolated restore drill

**Key points:** An actual authorized host bundle must pass this drill before
the recovery hold clears. Synthetic fixtures do not prove host recovery.

**Detail:** Use a disposable private VM or container with its own data copies.
Preload the candidate and rollback images. Block outbound access at the network
boundary before startup. Verify the block independently. Provide no device
network route, Docker socket, deployment repository mount, or production
volume. Allow operator login through a private local connection only.

Set `drill_dir` to a disposable copy of the validated bundle. Set
`candidate_image` to the exact preloaded image digest. Before any application
import or first startup, run the following against that copy only. It disables
the scheduler and self-update, holds restored running jobs without deleting
them, and disables notification settings. The unchanged bundle preserves the
original settings and job states. Record these drill-only differences privately.

```bash
python3 - "$drill_dir/data/sixtyops.db" <<'PY'
import sqlite3
import sys
from contextlib import closing

disabled = (
    "schedule_enabled", "autoupdate_enabled", "backup_enabled", "email_enabled",
    "webhook_enabled", "snmp_traps_enabled", "syslog_forward_enabled",
    "radius_server_enabled",
)
with closing(sqlite3.connect(sys.argv[1])) as conn:
    with conn:
        for key in disabled:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, 'false')", (key,))
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('slack_webhook_url', '')")
        conn.execute("UPDATE active_jobs SET status = 'drill_held' WHERE status = 'running'")
    assert all(conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()[0] == "false" for key in disabled)
    assert conn.execute("SELECT COUNT(*) FROM active_jobs WHERE status = 'running'").fetchone()[0] == 0
PY
docker run --rm --pull never --network none --entrypoint python "$candidate_image" -c '
import socket
with socket.socket() as probe:
    probe.settimeout(2)
    try:
        probe.connect(("192.0.2.1", 9))
    except OSError:
        print("egress blocked")
    else:
        raise SystemExit("Isolation failed")
'
docker run -d --pull never --name sixtyops-recovery-drill --network none --user 1500:1500 \
  --mount "type=bind,src=$drill_dir/data,dst=/app/data" \
  --mount "type=bind,src=$drill_dir/firmware,dst=/app/firmware" \
  --entrypoint python "$candidate_image" -m updater.app
docker inspect --format '{{.HostConfig.NetworkMode}}' sixtyops-recovery-drill
```

Require `none` from the network check. Inspect mounts privately and confirm
that only the disposable directories are mounted. Confirm no published ports,
Docker socket, deployment repository, or production volume. Probe a reserved
external address with the separate container first, as shown above. Require
`egress blocked` before application startup. Prepare the disposable directories
with ownership `1500:1500` and permissions `0700`/`0600` before starting the app.
Check health with a localhost request through
`docker exec`. Use that private connection for login checks. Do not submit a
job or an update request. Do not supply credentials as command arguments.

Polling and firmware fetching have no general disable setting. The network
boundary must block their access. Development mode still starts the scheduler
and seeds data; it does not prove an unchanged host restore. Stop the drill if
any disabled setting changes or any restored job becomes running. Repeat the
pre-start hold steps on the disposable rollback copy before its first startup.

Startup moves unproven earlier upload marks to `historical_uploaded_by` and
`historical_verified_at`. It clears their upload-only verification authority.
Current roles do not prove historical admin status. Validate that these fields
and audit records survive. Vendor checksum verification stays valid. An actual
admin re-upload records the current admin and time while keeping artifact
identity and original source. Include these expected migration effects in the
private state comparison.

Run staging validation on the untouched bundle. Restore another copy and run
the startup, migration, login, decryption, target, health, and repeated-start
checks. Exercise the exact reversal steps with the rollback image and original
set. Verify that no job, device write, self-update, or notification executed.
Record the exact candidate and rollback commits/digests, bundle identity,
cutoff, manifest result, encrypted-record count, firmware hash counts, restored
state, startup results, and reversal results in private evidence.

Publish a safe pass/fail summary and the evidence reference. Keep the hold if
any result is missing. This PR supplies a plan only. It does not supply a host
bundle, a restore drill, live validation, or device recovery proof.

### Path 3 — manual schema repair (last resort)

Reserved for cases where the DB is structurally fine (`integrity_check` = `ok`) but `init_db()` cannot resume on its own — for example, a customer-edited DB or a half-completed `_migrate_to_devices_table` that left orphan rows. Stop the app, open the DB with `sqlite3`, and add the missing columns by hand using the same statements the migration uses:

```bash
docker compose stop sixtyops-mgmt
sqlite3 ./data/sixtyops.db
sqlite> ALTER TABLE devices ADD COLUMN <missing_column> <type>;
sqlite> .quit
docker compose up -d sixtyops-mgmt
```

The authoritative column list and types live in the `CREATE TABLE` block at the top of `init_db()` (around line 386 of `updater/database.py`). The `_migrate()` function below it shows the exact `ALTER TABLE` statements for every post-launch column.

## Prevention

- Verify the most recent SFTP backup completed successfully before triggering an in-app self-update — **Settings → Backups** shows `last_status` and `last_run_at`.
- The `tests/test_migration_failures.py` suite locks in the idempotency, schema-downgrade, and concurrent-writer contracts. Do not relax those tests without a strong reason.
