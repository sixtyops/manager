# Migration Recovery

If the Manager fails to start because a database migration stopped mid-run, restart the container first. `init_db()` resumes safely. If it still fails, restore `./data` from a local backup. Keep `./data/.encryption_key` with the database. Repair the schema by hand only as a last resort.

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

`integrity_check` returning `ok` means the file is structurally fine. The problem is migration state, and Path 1 below should resolve it.

## Recovery

### Path 1 — restart the container (try first)

```bash
docker compose restart sixtyops-mgmt
docker compose logs sixtyops-mgmt --tail=100
```

`init_db()` runs at every startup and adds whatever columns or triggers are missing. If the restart succeeds and `/healthz` returns 200, the recovery is complete.

### Path 2 — restore a local copy

Back up `./data` and `./firmware` outside the install directory. To restore a copy, stop the app and replace `./data` with the saved directory. Keep `.encryption_key` with the database.

1. Stop the app: `docker compose stop sixtyops-mgmt`.
2. Move the damaged directory aside and copy the saved `data` directory into place.
3. Start the app: `docker compose up -d sixtyops-mgmt`.

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

- Back up `./data` and `./firmware` before maintenance. The data directory includes the encryption key.
- The `tests/test_migration_failures.py` suite locks in the idempotency, schema-downgrade, and concurrent-writer contracts. Do not relax those tests without a strong reason.
