#!/usr/bin/env python3
"""List open Manager rearchitecture issues with closed declared dependencies."""

import argparse
import json
import re
import subprocess
import sys
from typing import Any


_REPO = "sixtyops/manager"
_MAX_ISSUES = 1000
_DEPENDENCY_MENTION = re.compile(r"\bdepends\s+on\b", re.IGNORECASE)
_DEPENDENCY_FIELD = re.compile(
    r"^[ \t]*(?:[-*][ \t]*)?(?:\*\*)?depends[ \t]+on[ \t]*:"
    r"[ \t]*(?P<value>[^\r\n]*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_DEPENDENCY_REFERENCE = re.compile(
    r"(?:\[[^\]]+\]\(https://github\.com/sixtyops/manager/issues/"
    r"(?P<markdown>\d+)(?:#[A-Za-z0-9_-]+)?\))|"
    r"(?:https://github\.com/sixtyops/manager/issues/(?P<url>\d+)"
    r"(?:#[A-Za-z0-9_-]+)?)|"
    r"(?<![A-Za-z0-9_/])#(?P<number>\d+)\b",
    re.IGNORECASE,
)
_PHASE_FIELD = re.compile(r"\bPhase:[ \t]*Phase[ \t]+([A-Z])\b", re.IGNORECASE)
_PHASE_LABEL = re.compile(r"^phase-(\d+)$", re.IGNORECASE)


def parse_dependencies(body: object) -> tuple[tuple[int, ...], str | None]:
    """Return declared issue IDs or a reason that the declaration is unclear."""
    if not isinstance(body, str) or not body.strip():
        return (), "issue body is missing"

    mentions = list(_DEPENDENCY_MENTION.finditer(body))
    if not mentions:
        return (), None
    fields = list(_DEPENDENCY_FIELD.finditer(body))
    if len(mentions) != 1 or len(fields) != 1:
        return (), "dependency declaration is missing or ambiguous"

    remaining = fields[0].group("value")
    dependency_ids: list[int] = []
    previous_end = 0
    malformed = False
    for match in _DEPENDENCY_REFERENCE.finditer(remaining):
        separator = remaining[previous_end : match.start()]
        if re.sub(r"\b(?:and|or)\b|[\s,;.*]+", "", separator, flags=re.IGNORECASE):
            malformed = True
        reference = next(value for value in match.groups() if value is not None)
        dependency_ids.append(int(reference))
        previous_end = match.end()
    trailing = remaining[previous_end:]
    if re.sub(r"[\s,;.*]+", "", trailing):
        malformed = True

    if not dependency_ids or malformed:
        return (), "dependency references are malformed or ambiguous"
    if len(dependency_ids) != len(set(dependency_ids)):
        return (), "dependency references are duplicated"
    return tuple(dependency_ids), None


def _labels(issue: dict[str, Any]) -> set[str]:
    return {
        label["name"]
        for label in issue.get("labels", [])
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    }


def _is_rearchitecture(issue: dict[str, Any]) -> bool:
    return "rearchitecture" in _labels(issue)


def _phase_sort_key(issue: dict[str, Any]) -> tuple[int, int, int]:
    body = issue.get("body")
    body_phases = _PHASE_FIELD.findall(body) if isinstance(body, str) else []
    number = issue["number"]
    if len(body_phases) == 1:
        return 0, ord(body_phases[0].upper()), number
    if len(body_phases) > 1:
        return 2, 0, number

    label_phases = [
        int(match.group(1))
        for label in _labels(issue)
        if (match := _PHASE_LABEL.fullmatch(label))
    ]
    if len(label_phases) == 1:
        return 1, label_phases[0], number
    return 2, 0, number


def find_eligible_issues(
    issues: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[int, str]]]:
    """Check each open rearchitecture issue against its direct dependencies."""
    states = {issue["number"]: issue["state"] for issue in issues}
    eligible: list[dict[str, Any]] = []
    blocked: list[tuple[int, str]] = []

    for issue in issues:
        if issue["state"] != "open" or not _is_rearchitecture(issue):
            continue
        dependencies, error = parse_dependencies(issue.get("body"))
        if error:
            blocked.append((issue["number"], error))
            continue

        unresolved = [
            dependency
            for dependency in dependencies
            if states.get(dependency) != "closed"
        ]
        if unresolved:
            blocked.append((issue["number"], "dependencies are open or unknown"))
            continue
        eligible.append(issue)

    eligible.sort(key=_phase_sort_key)
    return eligible, blocked


def render_graph(issues: list[dict[str, Any]]) -> str:
    """Render declared direct dependency links as DOT without ranking issues."""
    by_number = {issue["number"]: issue for issue in issues}
    roots = [
        issue
        for issue in issues
        if issue["state"] == "open" and _is_rearchitecture(issue)
    ]
    nodes: set[int] = {issue["number"] for issue in roots}
    edges: set[tuple[int, int]] = set()
    unclear: set[int] = set()

    for issue in roots:
        dependencies, error = parse_dependencies(issue.get("body"))
        if error:
            unclear.add(issue["number"])
            continue
        for dependency in dependencies:
            nodes.add(dependency)
            edges.add((issue["number"], dependency))

    lines = ["digraph issues {"]
    for number in sorted(nodes):
        issue = by_number.get(number)
        title = issue.get("title", "unknown issue") if issue else "unknown issue"
        state = issue.get("state", "unknown") if issue else "unknown"
        suffix = "; dependency declaration unclear" if number in unclear else ""
        label = f"#{number} {title} ({state}){suffix}"
        lines.append(f"  issue_{number} [label={json.dumps(label)}];")
    for source, target in sorted(edges):
        lines.append(f"  issue_{source} -> issue_{target};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _load_issue_data(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) >= _MAX_ISSUES:
        raise ValueError("issue list is invalid or may be incomplete")

    issues: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("issue record is invalid")
        number = item.get("number")
        title = item.get("title")
        state = item.get("state")
        body = item.get("body")
        labels = item.get("labels")
        if (
            not isinstance(number, int)
            or number < 1
            or number in seen
            or not isinstance(title, str)
            or state not in {"OPEN", "CLOSED", "open", "closed"}
            or (body is not None and not isinstance(body, str))
            or not isinstance(labels, list)
        ):
            raise ValueError("issue record is incomplete or ambiguous")
        normalized = dict(item)
        normalized["state"] = state.lower()
        normalized["labels"] = labels
        issues.append(normalized)
        seen.add(number)
    return issues


def _read_issues() -> list[dict[str, Any]]:
    command = [
        "gh",
        "issue",
        "list",
        "--repo",
        _REPO,
        "--state",
        "all",
        "--limit",
        str(_MAX_ISSUES),
        "--json",
        "number,title,state,body,labels",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise RuntimeError("cannot run the GitHub CLI") from error
    if result.returncode != 0:
        raise RuntimeError("the GitHub CLI could not read issue data")
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("the GitHub CLI returned invalid JSON") from error
    try:
        return _load_issue_data(raw)
    except ValueError as error:
        raise RuntimeError(str(error)) from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--graph",
        action="store_true",
        help="print declared dependency links as DOT",
    )
    args = parser.parse_args(argv)

    try:
        issues = _read_issues()
    except RuntimeError as error:
        print(f"epic_ready: {error}; no eligibility results", file=sys.stderr)
        return 1

    if args.graph:
        print(render_graph(issues), end="")
        return 0

    eligible, blocked = find_eligible_issues(issues)
    for number, reason in blocked:
        print(f"blocked issue {number}: {reason}", file=sys.stderr)
    if not eligible:
        print("No eligible open rearchitecture issues.")
        return 0
    for issue in eligible:
        print(f"{issue['number']}\t{issue['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
