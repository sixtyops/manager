"""Collect changelog fragments into a versioned CHANGELOG.md section."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
FRAGMENTS = ROOT / "changelog.d"
CHANGELOG = ROOT / "CHANGELOG.md"
KINDS = ("added", "changed", "fixed", "removed", "security")
FRAGMENT_NAME = re.compile(r"^[1-9][0-9]*\.(added|changed|fixed|removed|security)\.md$")


def fragment_paths() -> list[Path]:
    return sorted(path for path in FRAGMENTS.glob("*.md") if path.name != "README.md")


def invalid_names(paths: list[Path]) -> list[Path]:
    return [path for path in paths if not FRAGMENT_NAME.fullmatch(path.name)]


def collect(version: str, release_date: str) -> None:
    paths = fragment_paths()
    invalid = invalid_names(paths)
    if invalid:
        names = ", ".join(path.name for path in invalid)
        raise ValueError(f"Invalid changelog fragment name(s): {names}")
    if not paths:
        raise ValueError("No changelog fragments found")

    grouped: dict[str, list[str]] = {kind: [] for kind in KINDS}
    for path in paths:
        kind = path.name.split(".")[1]
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            raise ValueError(f"Empty changelog fragment: {path.name}")
        grouped[kind].extend(line if line.startswith("-") else f"- {line}" for line in lines)

    changelog = CHANGELOG.read_text(encoding="utf-8")
    sections = [f"## {version} - {release_date}"]
    for kind in KINDS:
        if grouped[kind]:
            sections.extend(("", f"### {kind.title()}", *grouped[kind]))
    release_section = "\n".join(sections) + "\n\n"

    unreleased = re.search(r"^## Unreleased\s*$", changelog, re.MULTILINE)
    if unreleased is None:
        raise ValueError("CHANGELOG.md has no ## Unreleased section")
    changelog = changelog[: unreleased.start()] + release_section + changelog[unreleased.start() :]
    CHANGELOG.write_text(changelog, encoding="utf-8")
    for path in paths:
        path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?")
    parser.add_argument("date", nargs="?")
    parser.add_argument("--check", action="store_true", help="validate fragment names without changing files")
    args = parser.parse_args()

    paths = fragment_paths()
    invalid = invalid_names(paths)
    if invalid:
        for path in invalid:
            print(f"Invalid changelog fragment name: {path.name}", file=sys.stderr)
        return 1
    if args.check:
        if args.version or args.date:
            parser.error("--check does not take a version or date")
        return 0
    if not args.version or not args.date:
        parser.error("version and date are required unless --check is used")
    try:
        date.fromisoformat(args.date)
        collect(args.version, args.date)
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
