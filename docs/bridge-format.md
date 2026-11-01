# Bridge archive format (`bridge-v1`)

## Purpose

**Key points**

- The new schema cannot open a database from the current schema.
- The bridge archive is the only supported way to move data across that change.
- One encrypted file holds credentials, settings, history, and the encryption key.

**Detail**

`build_bridge_export` in `updater/backup.py` writes the archive.
`POST /api/backup/bridge` returns it as a download. The bridge importer (#325)
reads it into an empty new-schema database.

## Export route

**Key points**

- Route: `POST /api/backup/bridge`. Only an admin can call it.
- Send the passphrase in the JSON body: `{"passphrase": "..."}`.
- The passphrase must have 8 or more characters.

**Detail**

Do not put the passphrase in the query string. nginx logs query strings. The
route reads all tables in one read transaction, so the row counts agree with
each other. If a table or the key file is missing, the route returns HTTP 500
and writes no file. The download name is `sixtyops-bridge-<timestamp>.bridge`.

## Encrypted file layout

**Key points**

- Line 1: `sixtyops-bridge-v1`.
- Line 2: the 16-byte salt, standard base64.
- The rest: one Fernet token that holds the tar file.

**Detail**

The key comes from the passphrase and the salt through PBKDF2-HMAC-SHA256 with
480,000 iterations and a 32-byte output. Fernet uses that output as its key,
in URL-safe base64. This is the same scheme as the CSV export.
`decrypt_bridge_export` reverses this step and returns the tar bytes. A wrong
passphrase or a damaged token raises `ValueError`.

## Tar contents

**Key points**

- `manifest.json` comes first, then `key.bin`, then one JSON file per table.
- Each table file is `<table>.json`: a JSON list of row objects.
- Each row object maps column name to stored value.

**Detail**

The export copies values as stored. It does not decrypt or re-encrypt them.
Rows come in `rowid` order. A column value is a string, an integer, a float, or
`null`. If a column holds any other type, such as a BLOB, the export fails.

`key.bin` is the content of the credential encryption key file
(`data/.encryption_key`). It is a URL-safe base64 Fernet key with a trailing
newline. It decrypts every stored device password, every encrypted setting,
and every config snapshot. Protect the archive and the passphrase together.

## Tables

**Key points**

- The archive holds the 12 tables below, in this order.
- Every column of each table is in the archive.

| Table | Notes |
|-------|-------|
| `devices` | Credentials as stored Fernet ciphertext. |
| `tower_sites` | |
| `users` | Password hashes as stored. |
| `settings` | All keys. Secret keys hold stored ciphertext. |
| `device_configs` | Includes recycle-bin rows (`deleted_at` set). `config_json` is stored ciphertext. |
| `firmware_registry` | |
| `firmware_confirmations` | |
| `job_history` | |
| `device_update_history` | |
| `device_durations` | |
| `audit_log` | |
| `freeze_windows` | |

**Detail**

The archive does not hold these tables: RADIUS (`radius_users`,
`radius_auth_log`, `radius_client_overrides`, `radius_rollouts`,
`radius_rollout_devices`), sessions and API tokens, telemetry (`link_metrics`,
`device_uptime_events`, `cpe_cache`, and similar), and the config enforce and
rollout tables. The legacy `access_points` and `switches` tables mirror
`devices`, so the archive does not hold them.

## Manifest

**Key points**

- `manifest.json` is a JSON object with these keys:

| Key | Value |
|-----|-------|
| `format` | `"bridge-v1"` |
| `schema_version` | `"legacy"`: the schema before the new schema builder (#327) |
| `app_version` | The Manager version that wrote the archive |
| `created_at` | UTC time of the export, ISO 8601 |
| `tables` | `{"<table>": {"rows": <count>}}` for each table above |
| `sha256` | `{"<file name>": "<hex digest>"}` for `key.bin` and each table file |

**Detail**

The manifest has no digest of itself. Fernet authenticates the whole tar, so
a change to any byte fails decryption. An importer must check that the
`format` value is `bridge-v1`, that each row count matches its table file, and
that each digest matches its file.
