"""Tests for the read-only epic eligibility helper."""

from scripts.epic_ready import find_eligible_issues, parse_dependencies, render_graph


def _issue(number, state="open", phase="A", dependency_text=None, labels=None):
    body = f"Phase: Phase {phase}." if phase else "No phase declared."
    if dependency_text is not None:
        body += f"\nDepends on: {dependency_text}"
    if labels is None:
        labels = ["rearchitecture"]
    return {
        "number": number,
        "title": f"issue {number}",
        "state": state,
        "body": body,
        "labels": [{"name": label} for label in labels],
    }


def test_dependency_parser_accepts_issue_numbers_and_full_issue_links():
    body = (
        "Goal: keep dependencies explicit.\n"
        "Depends on: #41, "
        "https://github.com/sixtyops/manager/issues/42"
    )

    dependencies, error = parse_dependencies(body)

    assert dependencies == (41, 42)
    assert error is None


def test_only_open_issues_with_closed_known_dependencies_are_listed():
    issues = [
        _issue(13, dependency_text="#90 or #91"),
        _issue(12, dependency_text="#999"),
        _issue(11, dependency_text="#91"),
        _issue(10, phase="B", dependency_text="#90"),
        _issue(4, phase="A", dependency_text="#90"),
        _issue(3),
        _issue(2, state="closed"),
        _issue(90, state="closed", labels=[]),
        _issue(91, state="open", labels=[]),
        _issue(1, labels=["bug"]),
    ]

    eligible, blocked = find_eligible_issues(issues)

    assert [issue["number"] for issue in eligible] == [3, 4, 10]
    assert {number for number, _ in blocked} == {11, 12, 13}


def test_empty_or_repeated_dependency_fields_fail_closed():
    empty = _issue(20)
    empty["body"] += "\nDepends on:"
    repeated = _issue(21, dependency_text="#90")
    repeated["body"] += "\nDepends on: #91"

    assert parse_dependencies(empty["body"])[1]
    assert parse_dependencies(repeated["body"])[1]

    eligible, blocked = find_eligible_issues([empty, repeated, _issue(90, state="closed")])
    assert eligible == []
    assert {number for number, _ in blocked} == {20, 21}


def test_malformed_dependency_fails_closed_and_missing_phase_sorts_last():
    malformed = _issue(20, dependency_text="#90 then #91")
    unphased = _issue(12, phase=None)

    eligible, blocked = find_eligible_issues(
        [unphased, _issue(10, phase="B"), malformed, _issue(4, phase="A")]
    )

    assert [issue["number"] for issue in eligible] == [4, 10, 12]
    assert blocked == [(20, "dependency references are malformed or ambiguous")]


def test_phase_and_issue_number_order_does_not_need_graph_traversal():
    issues = [
        _issue(8, phase="B", dependency_text="#90"),
        _issue(7, phase="A", dependency_text="#90"),
        _issue(5, phase="A"),
        _issue(90, state="closed", labels=[]),
    ]

    eligible, _ = find_eligible_issues(issues)
    graph = render_graph(issues)

    assert [issue["number"] for issue in eligible] == [5, 7, 8]
    assert "issue_7 -> issue_90;" in graph
    assert "issue_8 -> issue_90;" in graph
    assert "issue_90 -> issue_7;" not in graph


def test_graph_renders_unknown_dependencies_without_marking_them_eligible():
    issue = _issue(30, dependency_text="#999")

    graph = render_graph([issue])
    eligible, blocked = find_eligible_issues([issue])

    assert "issue_30 -> issue_999;" in graph
    assert eligible == []
    assert blocked == [(30, "dependencies are open or unknown")]
