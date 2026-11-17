import os
import stat

from updater import memdiag


def test_report_has_all_sections_and_does_not_write_object_contents(tmp_path):
    secret = b"SECRET-LIKE-CONTENT-" * (2 * 1024 * 1024 // 20)
    holder = [secret]
    path = memdiag._write_report(tmp_path)
    report = path.read_text()

    assert path.name.startswith("memdiag-") and path.suffix == ".txt"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for section in range(1, 6):
        assert f"Section {section}:" in report
    assert "SECRET-LIKE-CONTENT" not in report
    assert "type=bytes" in report
    assert "VmRSS:" in report or "VmData:" in report or "Section 5: /proc/self/status\nunavailable" in report
    assert holder[0] is secret


def test_malloc_info_skips_non_glibc(monkeypatch):
    monkeypatch.setattr(memdiag.platform, "libc_ver", lambda: ("musl", "1.2"))
    assert memdiag._malloc_info() is None
