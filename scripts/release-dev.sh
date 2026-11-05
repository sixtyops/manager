#!/usr/bin/env bash
set -euo pipefail

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

ci_run="$(gh run list --repo "$REPOSITORY" --workflow CI --commit "$sha" \
  --json databaseId,headSha,status,conclusion,createdAt \
  --jq '[.[] | select(.headSha == "'"$sha"'")] | sort_by(.createdAt) | last | select(.status == "completed" and .conclusion == "success") | .databaseId')"
[[ -n "$ci_run" ]] || fail "CI has not passed on this commit."

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
  git log --oneline "${last_tag}..${sha}"
else
  git log --oneline "$sha"
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
