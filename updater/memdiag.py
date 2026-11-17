"""Write a read-only process memory report after SIGUSR2."""

import ctypes
import datetime
import gc
import logging
import os
import platform
import signal
import sys
import threading
import tracemalloc
from collections import defaultdict
from itertools import chain
from pathlib import Path

logger = logging.getLogger(__name__)
_busy = False


def _malloc_info() -> str | None:
    if platform.libc_ver()[0] != "glibc":
        return None
    libc = ctypes.CDLL(None)
    if not all(hasattr(libc, name) for name in ("open_memstream", "malloc_info", "fclose")):
        return None
    buffer = ctypes.c_void_p()
    length = ctypes.c_size_t()
    libc.open_memstream.restype = ctypes.c_void_p
    libc.malloc_info.argtypes = [ctypes.c_int, ctypes.c_void_p]
    libc.fflush.argtypes = [ctypes.c_void_p]
    libc.fclose.argtypes = [ctypes.c_void_p]
    libc.free.argtypes = [ctypes.c_void_p]
    stream = libc.open_memstream(ctypes.byref(buffer), ctypes.byref(length))
    if not stream:
        return None
    try:
        if libc.malloc_info(0, stream) != 0:
            return None
        libc.fflush(stream)
        return ctypes.string_at(buffer, length.value).decode("utf-8", "replace")
    finally:
        libc.fclose(stream)
        libc.free(buffer)


def _make_report() -> str:
    objects = gc.get_objects()
    totals = defaultdict(lambda: [0, 0])
    large = []
    seen_large = set()
    seen_containers = set()
    for obj in objects:
        kind = type(obj)
        size = sys.getsizeof(obj, 0)
        totals[f"{kind.__module__}.{kind.__qualname__}"][0] += 1
        totals[f"{kind.__module__}.{kind.__qualname__}"][1] += size
    pending = [obj for obj in objects if isinstance(obj, (list, tuple, dict, set, frozenset))]
    while pending:
        container = pending.pop()
        if id(container) in seen_containers:
            continue
        seen_containers.add(id(container))
        values = chain(container.keys(), container.values()) if isinstance(container, dict) else container
        for value in values:
            if isinstance(value, (bytes, bytearray, str)) and sys.getsizeof(value, 0) > 1024 * 1024:
                if id(value) not in seen_large:
                    seen_large.add(id(value))
                    large.append((sys.getsizeof(value, 0), value))
            elif isinstance(value, (list, tuple, dict, set, frozenset)):
                pending.append(value)

    lines = ["Section 1: GC census (top 30 by summed size)"]
    lines.extend(f"{name}: count={count}, size={size}" for name, (count, size) in
                 sorted(totals.items(), key=lambda item: item[1][1], reverse=True)[:30])
    lines.append("Section 2: largest strings and byte objects (top 20)")
    for size, value in sorted(large, key=lambda item: item[0], reverse=True)[:20]:
        referrers = ", ".join(sorted({type(ref).__name__ for ref in gc.get_referrers(value)}))
        lines.append(f"size={size}, type={type(value).__name__}, referrers={referrers}")

    lines.append("Section 3: tracemalloc")
    if tracemalloc.is_tracing():
        snapshot = tracemalloc.take_snapshot()
        lines.append(f"total traced size={sum(stat.size for stat in snapshot.statistics('lineno'))}")
        lines.extend(str(stat) for stat in snapshot.statistics("lineno")[:30])
    else:
        lines.append("not tracing")

    lines.append("Section 4: glibc malloc_info")
    try:
        xml = _malloc_info()
    except Exception:
        xml = None
    if xml:
        lines.append(xml)
    lines.append("Section 5: /proc/self/status")
    try:
        lines.extend(line.strip() for line in Path("/proc/self/status").read_text().splitlines()
                     if line.startswith(("VmRSS:", "VmData:")))
    except OSError:
        lines.append("unavailable")
    return "\n".join(lines) + "\n"


def _write_report(data_dir: Path) -> Path:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = data_dir / f"memdiag-{stamp}.txt"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as report:
        report.write(_make_report())
    return path


def register(data_dir: Path) -> None:
    """Register the SIGUSR2 handler for this process."""
    def handler(_signum, _frame):
        global _busy
        if _busy:
            return
        _busy = True

        def write_one():
            global _busy
            try:
                path = _write_report(Path(data_dir))
                logger.info("Memory report written: %s", path)
            except Exception:
                logger.exception("Memory report failed")
            finally:
                _busy = False

        try:
            threading.Thread(target=write_one, name="memdiag", daemon=True).start()
        except Exception:
            _busy = False
            logger.exception("Could not start memory report thread")

    signal.signal(signal.SIGUSR2, handler)
