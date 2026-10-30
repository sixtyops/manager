"""Tests for changelog fragment validation and collection."""

from scripts import collect_changelog


def test_collect_groups_fragments_and_preserves_unreleased(tmp_path, monkeypatch) -> None:
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (fragments / "12.fixed.md").write_text("Fix update retry handling.\n", encoding="utf-8")
    (fragments / "8.added.md").write_text("Add device status export.\n", encoding="utf-8")
    changelog = tmp_path / "CHANGELOG.md"
    original = "# Changelog\n\n## Unreleased\n\n### Added\n- Keep this entry.\n"
    changelog.write_text(original, encoding="utf-8")
    monkeypatch.setattr(collect_changelog, "FRAGMENTS", fragments)
    monkeypatch.setattr(collect_changelog, "CHANGELOG", changelog)

    collect_changelog.collect("1.2.0", "2026-10-29")

    result = changelog.read_text(encoding="utf-8")
    assert result == (
        "# Changelog\n\n## 1.2.0 - 2026-10-29\n\n### Added\n"
        "- Add device status export.\n\n### Fixed\n- Fix update retry handling.\n\n"
        "## Unreleased\n\n### Added\n- Keep this entry.\n"
    )
    assert list(fragments.glob("*.md")) == []


def test_check_rejects_invalid_fragment_name(tmp_path, monkeypatch, capsys) -> None:
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (fragments / "12.other.md").write_text("Entry.\n", encoding="utf-8")
    monkeypatch.setattr(collect_changelog, "FRAGMENTS", fragments)
    monkeypatch.setattr(collect_changelog, "CHANGELOG", tmp_path / "CHANGELOG.md")
    monkeypatch.setattr("sys.argv", ["collect_changelog.py", "--check"])

    assert collect_changelog.main() == 1
    assert "12.other.md" in capsys.readouterr().err
