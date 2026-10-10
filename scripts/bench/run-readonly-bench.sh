#!/usr/bin/env bash
# Run read-only bench checks against the lab devices without printing
# credentials.
#
# Usage, from the root of the checkout under test:
#   /path/to/run-readonly-bench.sh <full-commit-sha> \
#     [--mode integration|local-poll|session-proof] [--duration-min N]
#
# Run this script from a trusted checkout of main. It tests the checkout in
# the current directory, so the commit under test does not need to contain it.
#
# The owner sets SIXTYOPS_BENCH_ACCESS_FILE to a private access file with
# "Key: value" lines. This script parses that file in a subshell, runs one
# fixed mode, redacts all output, and writes a summary outside the
# repository. See docs/dev-hardware-validation.md, "Read-only bench wrapper".
#
# Do not add tracing (set -x) or an environment dump to this script.

set -euo pipefail
set +x

# Read-only integration test files. Each test in these files reads data or
# starts a poll that the Manager scheduler also runs. No test here changes
# device firmware, device configuration, or Manager settings or inventory.
# tests/test_bench_wrapper.py checks this list.
READONLY_TESTS=(
  tests/integration/test_smoke.py
  tests/integration/test_device_polling.py
  tests/integration/test_cpe_lifecycle.py
  tests/integration/test_config_backup.py
  tests/integration/test_system_surface.py
)

# This test reads device logins from the device portal. Do not run it here.
DESELECT=(
  tests/integration/test_system_surface.py::test_device_portal_surfaces_ap_switch_and_cpe
)

PYTEST_MARKER="integration and dev_blocking and not slow"

# The integration mode runs only against the authorized dev host.
ALLOWED_TEST_HOST="sixtyops-dev.infra.treehouse.mn"

# Write-lane inputs. The script removes them from the child environment, so
# a write test fixture skips even if it is collected.
WRITE_LANE_VARS="SIXTYOPS_TEST_FIRMWARE_AP_IP SIXTYOPS_TEST_RADIUS_AP_IP SIXTYOPS_TEST_OIDC_PROVIDER_URL SIXTYOPS_TEST_OIDC_CLIENT_ID SIXTYOPS_TEST_OIDC_CLIENT_SECRET SIXTYOPS_TEST_OIDC_REDIRECT_URI SIXTYOPS_TEST_OIDC_ALLOWED_GROUP SIXTYOPS_TEST_OIDC_ADMIN_GROUP"

# Changes to these paths in the commit under test stop the run. The owner
# must review such a change and run the bench manually.
PROTECTED_PATHS="tests/integration tests/conftest.py conftest.py pyproject.toml"

refuse() {
  echo "run-readonly-bench: REFUSED: $*" >&2
  exit 2
}

usage() {
  echo "usage: run-readonly-bench.sh <full-commit-sha> [--mode integration|local-poll|session-proof] [--duration-min N]" >&2
  exit 2
}

# Print the physical path of a directory.
physical_dir() {
  (cd "$1" 2>/dev/null && pwd -P)
}

# Remove leading and trailing white space.
trim() {
  local s=$1
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

[ $# -ge 1 ] || usage
expected_sha=$1
shift
mode=integration
duration_min=35
while [ $# -gt 0 ]; do
  case $1 in
    --mode)
      [ $# -ge 2 ] || usage
      mode=$2
      shift
      ;;
    --duration-min)
      [ $# -ge 2 ] || usage
      duration_min=$2
      shift
      ;;
    *) refuse "unknown option" ;;
  esac
  shift
done
case $mode in
  integration | local-poll | session-proof) ;;
  *) refuse "unknown mode" ;;
esac
[[ $duration_min =~ ^[1-9][0-9]{0,2}$ ]] || refuse "--duration-min must be a whole number from 1 to 999"

bench_dir=$(physical_dir "$(dirname "$0")")

# 1. Exact-head proof. This runs before the script reads the access file.
[[ $expected_sha =~ ^[0-9a-f]{40}$ ]] || refuse "the SHA must be a full 40-character lowercase commit ID"
repo=$(git rev-parse --show-toplevel 2>/dev/null) || refuse "run this script inside the checkout under test"
repo=$(physical_dir "$repo")
cd "$repo"
head_sha=$(git rev-parse HEAD)
[ "$head_sha" = "$expected_sha" ] || refuse "HEAD is $head_sha, not $expected_sha"
[ -z "$(git status --porcelain)" ] || refuse "the working tree has changes or untracked files"

base=$(git merge-base origin/main HEAD 2>/dev/null) || refuse "cannot find the merge base with origin/main"
# shellcheck disable=SC2086
git diff --quiet "$base" HEAD -- $PROTECTED_PATHS || refuse "the commit changes test infrastructure; the owner must run it manually"

# 2. Access file checks. Do not print the path or the contents.
access_file=${SIXTYOPS_BENCH_ACCESS_FILE:-}
[ -n "$access_file" ] || refuse "SIXTYOPS_BENCH_ACCESS_FILE is not set"
[ -f "$access_file" ] && [ -r "$access_file" ] || refuse "the access file is missing or not readable"
[ -O "$access_file" ] || refuse "the current user does not own the access file"
access_mode=$(stat -f %Lp "$access_file" 2>/dev/null || stat -c %a "$access_file")
case $access_mode in
  600 | 400) ;;
  *) refuse "the access file mode must be 600 or 400 (chmod 600)" ;;
esac
access_dir=$(physical_dir "$(dirname "$access_file")")
case "$access_dir/" in
  "$repo"/* | "$bench_dir"/*) refuse "the access file must be outside the repository" ;;
esac

# 3. Summary location, outside the repository.
umask 077
summary_dir=${SIXTYOPS_BENCH_SUMMARY_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/sixtyops-bench}
mkdir -p "$summary_dir"
summary_dir=$(physical_dir "$summary_dir")
case "$summary_dir/" in
  "$repo"/*) refuse "the summary directory must be outside the repository" ;;
esac
run_id="$(date -u +%Y%m%dT%H%M%SZ)-${head_sha:0:12}-$mode"
log_file="$summary_dir/$run_id.log"
summary_file="$summary_dir/$run_id.summary.txt"

if [ -n "${SIXTYOPS_BENCH_PYTHON:-}" ]; then
  python=$SIXTYOPS_BENCH_PYTHON
elif [ -x "$repo/.venv/bin/python" ]; then
  python="$repo/.venv/bin/python"
else
  python=$(command -v python3) || refuse "python3 is not available"
fi
if [ "$mode" = integration ]; then
  "$python" -c "import pytest, pytest_timeout" 2>/dev/null || refuse "pytest and pytest-timeout are required (pip install -r requirements.txt)"
fi
# The local modes run an exact copy of the commit in a temporary directory.
# updater.database creates data/sixtyops.db on import, so the copy gets a
# fresh database and key, and the checkout stays unchanged. The script
# deletes the copy at exit.
code_dir=$repo
work_dir=""
# shellcheck disable=SC2329  # Called by the EXIT trap.
cleanup() {
  if [ -n "$work_dir" ]; then
    rm -rf "$work_dir"
  fi
}
trap cleanup EXIT
if [ "$mode" != integration ]; then
  work_dir=$(mktemp -d "${TMPDIR:-/tmp}/sixtyops-bench-XXXXXX")
  code_dir="$work_dir/src"
  mkdir "$code_dir"
  git archive HEAD | tar -x -C "$code_dir"
  (cd "$code_dir" && "$python" -c "import updater.poller" >/dev/null 2>&1) || refuse "the commit dependencies are not installed for this Python (set SIXTYOPS_BENCH_PYTHON)"
fi

# 4. Parse the access file and run one mode in a subshell. The credential
# values exist only in this subshell and its children.
set +e
(
  set +x
  redact_n=0
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    case $line in
      "#"* | "") continue ;;
      *:*) ;;
      *) continue ;;
    esac
    key=$(trim "${line%%:*}")
    value=$(trim "${line#*:}")
    # redact.py does not replace values shorter than 3 characters. Stop
    # before a run can print such a value. Do not print the key or value.
    if [ -n "$value" ] && [ "${#value}" -lt 3 ]; then
      echo "run-readonly-bench: REFUSED: an access file value is shorter than 3 characters" >&2
      exit 2
    fi
    case $key in
      "Manager URL") export SIXTYOPS_TEST_URL="$value" ;;
      "Manager username") export SIXTYOPS_TEST_USER="$value" ;;
      "Manager password") export SIXTYOPS_TEST_PASS="$value" ;;
      "AP IP")
        export SIXTYOPS_TEST_AP_IP="$value"
        export SIXTYOPS_TEST_CONFIG_AP_IP="$value"
        export SIXTYOPS_BENCH_AP_IP="$value"
        ;;
      "TNS-100 IP") export SIXTYOPS_TEST_SWITCH_IP="$value" ;;
      "303L SM IP") export SIXTYOPS_BENCH_SM_303L_IP="$value" ;;
      "303X SM IP") export SIXTYOPS_BENCH_SM_303X_IP="$value" ;;
      "Shared device username") export SIXTYOPS_BENCH_AP_USER="$value" ;;
      "Shared device password") export SIXTYOPS_BENCH_AP_PASS="$value" ;;
    esac
    # Redact every value in the file, also values with no mapped name.
    if [ -n "$value" ]; then
      redact_n=$((redact_n + 1))
      export "SIXTYOPS_BENCH_REDACT_$redact_n=$value"
    fi
  done < "$access_file"

  # Also redact the Manager host name alone.
  url_host=${SIXTYOPS_TEST_URL:-}
  url_host=${url_host#*://}
  url_host=${url_host%%/*}
  if [ -n "$url_host" ] && [ "${#url_host}" -lt 3 ]; then
    echo "run-readonly-bench: REFUSED: the Manager host name is shorter than 3 characters" >&2
    exit 2
  fi
  export SIXTYOPS_BENCH_REDACT_HOST="$url_host"

  case $mode in
    integration) required="SIXTYOPS_TEST_URL SIXTYOPS_TEST_USER SIXTYOPS_TEST_PASS SIXTYOPS_TEST_AP_IP SIXTYOPS_TEST_SWITCH_IP" ;;
    *) required="SIXTYOPS_BENCH_AP_IP SIXTYOPS_BENCH_AP_USER SIXTYOPS_BENCH_AP_PASS" ;;
  esac
  missing=""
  for name in $required; do
    [ -n "${!name:-}" ] || missing="$missing $name"
  done
  if [ -n "$missing" ]; then
    echo "run-readonly-bench: REFUSED: the access file does not give:$missing" >&2
    exit 2
  fi

  unset_args=()
  for name in $WRITE_LANE_VARS; do
    unset_args+=(-u "$name")
  done

  case $mode in
    integration)
      if [ "${SIXTYOPS_TEST_URL%%://*}" != https ] || [ "$url_host" != "$ALLOWED_TEST_HOST" ]; then
        echo "run-readonly-bench: REFUSED: the Manager URL is not the authorized dev host" >&2
        exit 2
      fi
      deselect_args=()
      for node in "${DESELECT[@]}"; do
        deselect_args+=(--deselect "$node")
      done
      echo "== lane: integration (read-only allowlist)"
      env "${unset_args[@]}" "$python" -m pytest \
        -p no:cacheprovider -o addopts= \
        -m "$PYTEST_MARKER" --timeout=900 \
        -rfEs --tb=short --show-capture=no \
        "${deselect_args[@]}" "${READONLY_TESTS[@]}" 2>&1 \
        | "$python" "$bench_dir/redact.py"
      status=${PIPESTATUS[0]}
      ;;
    local-poll)
      echo "== lane: local-poll (checked-out commit, read-only)"
      env "${unset_args[@]}" "$python" "$bench_dir/readonly_poll.py" --repo "$code_dir" 2>&1 \
        | "$python" "$bench_dir/redact.py"
      status=${PIPESTATUS[0]}
      ;;
    session-proof)
      echo "== lane: session-proof (checked-out commit, $duration_min min)"
      env "${unset_args[@]}" "$python" "$bench_dir/session_proof.py" \
        --repo "$code_dir" --duration-min "$duration_min" 2>&1 \
        | "$python" "$bench_dir/redact.py"
      status=${PIPESTATUS[0]}
      ;;
  esac
  echo "== lane result: $mode exit=$status"
  exit "$status"
) 2>&1 | tee "$log_file"
run_status=${PIPESTATUS[0]}
set -e

# 5. Summary. The log is already redacted.
pytest_line=$(grep -E '^=+ .*(passed|failed|error|skipped|no tests ran|deselected).* =+$' "$log_file" | tail -n 1 || true)
{
  echo "run_id: $run_id"
  echo "utc_finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "commit: $head_sha"
  echo "working_tree: clean"
  echo "mode: $mode"
  if [ "$mode" = integration ]; then
    echo "integration_files: ${READONLY_TESTS[*]}"
    echo "deselected: ${DESELECT[*]}"
    echo "integration_result: ${pytest_line:-none}"
  fi
  grep -E '^== lane result: ' "$log_file" || true
  grep -E '^(local-poll|session-proof): ' "$log_file" || true
  if [ "$run_status" = 0 ]; then
    echo "overall: PASS"
  else
    echo "overall: FAIL (exit $run_status)"
  fi
  echo "log: $log_file"
} > "$summary_file"

echo
cat "$summary_file"
echo "summary: $summary_file"
exit "$run_status"
