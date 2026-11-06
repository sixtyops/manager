"""Check that the development Manager runs the release tag version."""

from __future__ import annotations

import os
import sys

import httpx


COMMIT_FIELDS = ("commit", "commit_sha", "git_commit", "git_sha", "revision")


def check_release_instance() -> bool:
    names = ("SIXTYOPS_TEST_URL", "SIXTYOPS_TEST_USER", "SIXTYOPS_TEST_PASS")
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    if missing:
        print("Missing required release test secrets: " + ", ".join(missing), file=sys.stderr)
        return False

    tag = os.environ.get("RELEASE_TAG", "")
    tagged_commit = os.environ.get("RELEASE_COMMIT", "")
    if not tag.startswith("v") or not tagged_commit:
        print("Release tag or tagged commit is unavailable.", file=sys.stderr)
        return False

    host = os.environ["SIXTYOPS_TEST_URL"].rstrip("/")
    session = httpx.Client(
        verify=False,
        timeout=30,
        follow_redirects=True,
        headers={"Origin": host},
    )
    try:
        login = session.post(
            host + "/login",
            data={"username": os.environ["SIXTYOPS_TEST_USER"], "password": os.environ["SIXTYOPS_TEST_PASS"]},
        )
        login.raise_for_status()
        response = session.get(host + "/api/system/info")
        response.raise_for_status()
        info = response.json()
    except (httpx.HTTPError, ValueError):
        print("Could not read release version from the development Manager.", file=sys.stderr)
        return False
    finally:
        session.close()

    expected_version = tag[1:]
    if not isinstance(info, dict) or info.get("version") != expected_version:
        print("Development Manager version does not match the release tag.", file=sys.stderr)
        return False

    reported_commit = next((info[field] for field in COMMIT_FIELDS if info.get(field)), None)
    if reported_commit is None:
        print("Development Manager reports the expected version; it exposes no commit, so commit matching was skipped.")
        return True
    if str(reported_commit).lower() != tagged_commit.lower():
        print("Development Manager commit does not match the release tag.", file=sys.stderr)
        return False

    print("Development Manager version and commit match the release tag.")
    return True


if __name__ == "__main__":
    raise SystemExit(0 if check_release_instance() else 1)
