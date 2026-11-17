import os
import stat
import pytest
from updater import memdiag


def test_report_has_sections_and_does_not_write_object_contents(tmp_path, monkeypatch):
    secret = b"SECRET-LIKE-CONTENT-" * (2 * 1024 * 1024 // 20)
    holder = [secret]
    monkeypatch.setattr(memdiag.gc, "get_referents", lambda _obj: pytest.fail("referent walk"))
    path = memdiag._write_report(tmp_path)
    report = path.read_text()

    assert path.name.startswith("memdiag-") and path.suffix == ".txt"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for section in (1, 3, 4, 5):
        assert f"Section {section}:" in report
    assert "Section 2:" not in report
    assert "SECRET-LIKE-CONTENT" not in report
    assert "VmRSS:" in report or "VmData:" in report or "Section 5: /proc/self/status\nunavailable" in report
    assert holder[0] is secret


def test_malloc_info_skips_non_glibc(monkeypatch):
    monkeypatch.setattr(memdiag.platform, "libc_ver", lambda: ("musl", "1.2"))
    assert memdiag._malloc_info() is None


def test_gc_census_does_not_walk_referents(monkeypatch):
    monkeypatch.setattr(memdiag.gc, "get_referents", lambda _obj: pytest.fail("referent walk"))
    report = memdiag._make_report()

    assert "Section 1: GC census" in report
    assert "Section 2:" not in report
