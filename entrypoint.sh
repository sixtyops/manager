#!/bin/sh
# Match Docker socket GID so appuser can run docker commands for self-update
if [ -S /var/run/docker.sock ]; then
    SOCK_GID=$(stat -c '%g' /var/run/docker.sock)
    if ! getent group "$SOCK_GID" > /dev/null 2>&1; then
        groupadd -g "$SOCK_GID" dockersock
    fi
    SOCK_GROUP=$(getent group "$SOCK_GID" | cut -d: -f1)
    usermod -aG "$SOCK_GROUP" appuser 2>/dev/null || true
fi

# Fix bind-mounted repo permissions for self-update
# Host-side git operations (run as root) leave root-owned files that
# appuser can't overwrite. Chown the entire repo so git checkout works.
if [ -d /app/repo/.git ]; then
    chown -R 1500:1500 /app/repo
    git config --global --add safe.directory /app/repo
fi

# Seed dev data after app creates the DB (local development only)
if [ "${SEED_DATA:-}" = "1" ] && [ -f /app/repo/scripts/seed_dev_data.py ]; then
    (
        # Wait for the app to create the DB and become healthy
        echo "entrypoint: waiting for app to initialise before seeding..."
        for i in $(seq 1 30); do
            if [ -f /app/data/tachyon.db ]; then
                sleep 1
                python3 /app/repo/scripts/seed_dev_data.py && break
                break
            fi
            sleep 1
        done
    ) &
fi

exec gosu appuser "$@"
