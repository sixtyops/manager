#!/usr/bin/env bash
set -euo pipefail

# Keep all git output non-interactive, including commands that may use a pager.
export GIT_PAGER=cat

readonly RELEASE_KEY="E2C19E86B24A87283068AEFAF2D6F70883960DCE"
readonly REPOSITORY="sixtyops/manager"

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

usage() {
  printf 'Usage: %s <full-commit-sha> [--yes]\n' "$0" >&2
  exit 2
}

yes=false
sha=""
for argument in "$@"; do
  case "$argument" in
    --yes) yes=true ;;
    *)
      if [[ -n "$sha" ]]; then
        usage
      fi
      sha="$argument"
      ;;
  esac
done
[[ -n "$sha" ]] || usage
[[ "$sha" =~ ^[0-9a-fA-F]{40}$ ]] || fail "Pass a full 40-character commit SHA."

git fetch origin
main_sha="$(git rev-parse --verify 'origin/main^{commit}')"
[[ "$sha" == "$main_sha" ]] || fail "Commit is not the current origin/main head."

ci_result=""
ci_sha="$sha"
ignore_patterns="$(awk 'BEGIN { found=0 } /^[[:space:]]+paths-ignore:/ && !found { found=1; next } found && /^[[:space:]]+- / { sub(/^[[:space:]]+- [\047\"]?/, ""); sub(/[\047\"].*$/, ""); print; next } found { exit }' .github/workflows/ci.yml)"
[[ -n "$ignore_patterns" ]] || fail "Could not read paths-ignore from .github/workflows/ci.yml."
while [[ -n "$ci_sha" ]]; do
  ci_result="$(gh run list --repo "$REPOSITORY" --workflow CI --commit "$ci_sha" \
    --json headSha,status,conclusion,createdAt \
    --jq '[.[] | select(.headSha == "'"$ci_sha"'" and .status == "completed")] | sort_by(.createdAt) | last | if . then [.status,.conclusion] | @tsv else empty end')"
  if [[ -n "$ci_result" ]]; then
    [[ "$ci_result" == $'completed\tsuccess' ]] || \
      fail "CI did not succeed on commit $ci_sha."
    printf 'Trusted CI from commit: %s\n' "$ci_sha"
    break
  fi
  parent="$(git rev-parse --verify "$ci_sha^1" 2>/dev/null || true)"
  [[ -n "$parent" ]] || fail "CI has not passed on this commit or any first-parent ancestor."

  # Read paths-ignore from CI so policy changes do not silently diverge here.
  changed_paths_file="$(mktemp)" || fail "Could not create a temporary file for changed paths."
  if ! git --no-pager diff --name-only --no-renames -z \
    "$ci_sha^1" "$ci_sha" > "$changed_paths_file"; then
    rm -f "$changed_paths_file"
    fail "Could not check changed paths for commit $ci_sha."
  fi
  non_ignored_path=""
  while IFS= read -r -d '' changed_path; do
    ignored=false
    while IFS= read -r ignore_pattern; do
      case "$ignore_pattern" in
        */\*\*) [[ "$changed_path" == "${ignore_pattern%/**}"/* ]] && ignored=true ;;
        \*.md) [[ "$changed_path" != */* && "$changed_path" == *.md ]] && ignored=true ;;
        *) [[ "$changed_path" == "$ignore_pattern" ]] && ignored=true ;;
      esac
    done <<< "$ignore_patterns"
    if [[ "$ignored" != true ]]; then
      non_ignored_path="$changed_path"
      break
    fi
  done < "$changed_paths_file"
  rm -f "$changed_paths_file"
  [[ -z "$non_ignored_path" ]] || \
    fail "CI is missing for $sha and commit $ci_sha..$parent changes non-ignored path: $non_ignored_path."
  ci_sha="$parent"
done

version=""
while IFS= read -r line; do
  if [[ "$line" =~ ^__version__[[:space:]]*=[[:space:]]*\"([^\"]+)\" ]]; then
    version="${BASH_REMATCH[1]}"
    break
  fi
done < <(git show "$sha:updater/__init__.py")
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+-dev[0-9]+$ ]] || \
  fail "updater/__init__.py at this commit does not have a -devN version."

tag="v${version}"
if git show-ref --verify --quiet "refs/tags/$tag"; then
  fail "Tag $tag already exists."
fi
if remote_tag="$(git ls-remote --exit-code --tags origin "refs/tags/$tag")"; then
  [[ -z "$remote_tag" ]] || fail "Tag $tag already exists."
else
  lookup_status=$?
  [[ "$lookup_status" -eq 2 ]] || \
    fail "Could not check whether tag $tag exists (git ls-remote exited $lookup_status)."
fi

last_tag="$(git describe --tags --abbrev=0 "$sha" 2>/dev/null || true)"
printf 'Version: %s\nCommit: %s\n' "$version" "$sha"
printf 'Commits since %s:\n' "${last_tag:-the start of history}"
if [[ -n "$last_tag" ]]; then
  git --no-pager log --oneline "${last_tag}..${sha}"
else
  git --no-pager log --oneline "$sha"
fi

if [[ "$yes" != true ]]; then
  printf 'Only the owner may sign and push this release tag. Type yes to continue: '
  IFS= read -r answer
  [[ "$answer" == yes ]] || fail "Release canceled."
fi

git -c gpg.format=openpgp \
  -c "user.signingkey=$RELEASE_KEY" \
  tag -s "$tag" -m "$tag" origin/main
if ! git -c gpg.format=openpgp verify-tag "$tag"; then
  fail "Signature verification failed for $tag."
fi
git push origin "refs/tags/$tag"

release_run=""
push_time="$(date -u +%s)"
deadline=$((push_time + 120))
while [[ -z "$release_run" && "$(date -u +%s)" -lt "$deadline" ]]; do
  release_run="$(gh run list --repo "$REPOSITORY" --workflow Release --event push \
    --commit "$sha" --json databaseId,headSha,event,headBranch,createdAt \
    --jq "[.[] | select(.headSha == \"$sha\" and .event == \"push\" and .headBranch == \"$tag\" and (.createdAt | fromdateiso8601) >= $push_time)] | sort_by(.createdAt) | last | .databaseId // empty")"
  [[ -n "$release_run" ]] || sleep 5
done
[[ -n "$release_run" ]] || \
  fail "No Release workflow run for $tag appeared within 120 seconds after the tag push."

if ! gh run watch --exit-status "$release_run"; then
  fail "The Release workflow failed for $tag."
fi

printf '\nGitHub pre-release:\n'
gh release view "$tag" --repo "$REPOSITORY"
printf '\nGHCR image tags:\n'
gh api --repo "$REPOSITORY" \
  '/orgs/sixtyops/packages/container/manager/versions?per_page=3' \
  --jq '.[].metadata.container.tags'

website="$(git show "$sha:website/index.html" 2>/dev/null || true)"
if [[ "$website" != *"ghcr.io/sixtyops/manager:$tag"* ]]; then
  printf 'Warning: website/index.html does not pin ghcr.io/sixtyops/manager:%s.\n' "$tag" >&2
fi
