#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED="${AGENT_PICKUP_SCRIPT:-/Users/serveradmin/repos/personal-processes/scripts/agent-issue-pickup.sh}"
CONFIG_FILE="${AGENT_LOOP_CONFIG:-$SCRIPT_DIR/../config/sixtyops-manager-loop.env}"
IDENTITY_HELPER="${AGENT_LOOP_IDENTITY_HELPER:-/Users/serveradmin/repos/personal-processes/dev-host/scripts/_load-bot-identity.sh}"

# Load the author bot identity (treehouseclaudebot) — GH_TOKEN + git author.
# shellcheck source=/dev/null  # resolved at runtime from the hub clone
source "$IDENTITY_HELPER" author

if [[ ! -x "$SHARED" ]]; then
    echo "Shared agent-issue-pickup script not found or not executable: $SHARED" >&2
    exit 1
fi

exec "$SHARED" --config "$CONFIG_FILE" "$@"
