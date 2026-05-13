# Migration Recovery

**Bottom line:** if the Manager fails to start because a database migration crashed mid-run, the recovery is (1) restart the container — `init_db()` is idempotent and resumes from a partial state; (2) if it still crashes, restore from the most recent SFTP backup; (3) only as a last resort, repair the schema by hand. Genuine data loss is rare and only happens if the DB file itself is corrupt.

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

### Path 2 — restore from SFTP backup

If Path 1 still crashes, restore from the most recent backup configured under **Settings → Backups → SFTP**:

1. Stop the app: `docker compose stop sixtyops-mgmt`.
2. Move the broken DB aside: `mv ./data/sixtyops.db ./data/sixtyops.db.broken-$(date +%s)`.
3. Download the latest backup tarball from the configured SFTP server.
4. Extract `sixtyops.db` from the tarball into `./data/`.
5. Start the app: `docker compose up -d sixtyops-mgmt`.

The first start runs `init_db()` against the restored DB and applies any migrations that have shipped since the backup was taken.

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
