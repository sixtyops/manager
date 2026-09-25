#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED="${AGENT_CODEX_REVIEW_SCRIPT:-/Users/serveradmin/repos/personal-processes/scripts/agent-codex-review.sh}"
CONFIG_FILE="${AGENT_LOOP_CONFIG:-$SCRIPT_DIR/../config/sixtyops-manager-loop.env}"
IDENTITY_HELPER="${AGENT_LOOP_IDENTITY_HELPER:-/Users/serveradmin/repos/personal-processes/dev-host/scripts/_load-bot-identity.sh}"

# Load the reviewer bot identity (treehousecodexbot) — GH_TOKEN for comments.
# shellcheck source=/dev/null  # resolved at runtime from the hub clone
source "$IDENTITY_HELPER" reviewer

if [[ ! -x "$SHARED" ]]; then
    echo "Shared agent-codex-review script not found or not executable: $SHARED" >&2
    exit 1
fi

exec "$SHARED" --config "$CONFIG_FILE" "$@"
