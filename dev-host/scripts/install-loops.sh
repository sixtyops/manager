#!/bin/bash
# Install the sixtyops-manager self-adapter LaunchDaemons on llm01.
#
# Prerequisites:
#   - Run as serveradmin on llm01 (passwordless sudo configured).
#   - /Users/serveradmin/repos/sixtyops-manager exists (a clone of sixtyops/manager).
#   - /Users/serveradmin/repos/personal-processes contains the shared loop scripts at scripts/agent-review-loop.sh,
#     scripts/agent-issue-pickup.sh, and scripts/agent-codex-review.sh.
#
# Idempotent: re-running is safe; diff against installed plist, only update
# if changed.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
skip() { echo -e "${YELLOW}→${NC} $1 (already configured)"; }
run()  {
    if $DRY_RUN; then
        echo "  [dry-run] $*"
    else
        "$@"
    fi
}

echo "Installing sixtyops-manager self-adapter LaunchDaemons..."

SHARED_LOOP="/Users/serveradmin/repos/personal-processes/scripts/agent-review-loop.sh"
SHARED_PICKUP="/Users/serveradmin/repos/personal-processes/scripts/agent-issue-pickup.sh"
SHARED_CODEX_REVIEW="/Users/serveradmin/repos/personal-processes/scripts/agent-codex-review.sh"
REVIEW_PLIST_SRC="$SCRIPT_DIR/../config/com.treehouse.agent-review-loop-sixtyops-manager.plist"
PICKUP_PLIST_SRC="$SCRIPT_DIR/../config/com.treehouse.agent-issue-pickup-sixtyops-manager.plist"
CODEX_REVIEW_PLIST_SRC="$SCRIPT_DIR/../config/com.treehouse.agent-codex-review-sixtyops-manager.plist"

for shared in "$SHARED_LOOP" "$SHARED_PICKUP" "$SHARED_CODEX_REVIEW"; do
    if ! $DRY_RUN && [[ ! -x "$shared" ]]; then
        echo "  Shared script missing or not executable: $shared" >&2
        echo "  Make sure /Users/serveradmin/repos/personal-processes is on a branch that has all three shared scripts." >&2
        exit 1
    fi
done

run mkdir -p "$HOME/Library/Logs" "$HOME/.local/state/agent-review-loop/sixtyops-manager/pickup" "$HOME/.local/state/agent-review-loop/sixtyops-manager/codex-review"

install_daemon() {
    local plist_src="$1"
    local label="$2"
    local plist_dst
    plist_dst="/Library/LaunchDaemons/$(basename "$plist_src")"
    if [[ -f "$plist_dst" ]] && sudo diff -q "$plist_src" "$plist_dst" &>/dev/null; then
        skip "$label"
        return
    fi
    if sudo launchctl print "system/$label" &>/dev/null; then
        run sudo launchctl bootout system "$plist_dst" || true
    fi
    run sudo cp "$plist_src" "$plist_dst"
    run sudo chown root:wheel "$plist_dst"
    run sudo chmod 644 "$plist_dst"
    run sudo launchctl bootstrap system "$plist_dst"
    ok "$label installed"
}

# Make the wrappers executable BEFORE any bootstrap. Every plist has
# RunAtLoad=true, so 'launchctl bootstrap' can fire the wrapper immediately; on
# a fresh clone the wrappers were written with the operator's umask and may not
# be executable yet. chmod first so a first install can never start a daemon
# against a non-executable wrapper.
run chmod +x "$SCRIPT_DIR/agent-review-loop.sh" "$SCRIPT_DIR/agent-issue-pickup.sh" "$SCRIPT_DIR/agent-codex-review.sh"
ok "wrapper scripts executable"

install_daemon "$REVIEW_PLIST_SRC" "com.treehouse.agent-review-loop-sixtyops-manager"
install_daemon "$PICKUP_PLIST_SRC" "com.treehouse.agent-issue-pickup-sixtyops-manager"
install_daemon "$CODEX_REVIEW_PLIST_SRC" "com.treehouse.agent-codex-review-sixtyops-manager"

echo ""
echo "Verify with:"
echo "  sudo launchctl print system/com.treehouse.agent-review-loop-sixtyops-manager"
echo "  sudo launchctl print system/com.treehouse.agent-issue-pickup-sixtyops-manager"
echo "  sudo launchctl print system/com.treehouse.agent-codex-review-sixtyops-manager"
echo "  bash dev-host/scripts/agent-review-loop.sh --status"
echo "  bash dev-host/scripts/agent-issue-pickup.sh --status"
echo "  bash dev-host/scripts/agent-codex-review.sh --status"
