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
from pathlib import Path

logger = logging.getLogger(__name__)
_busy = False
_thread_start_failed = False


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
    large = {}
    for obj in objects:
        kind = type(obj)
        size = sys.getsizeof(obj, 0)
        totals[f"{kind.__module__}.{kind.__qualname__}"][0] += 1
        totals[f"{kind.__module__}.{kind.__qualname__}"][1] += size
    pending = list(objects)
    seen_objects = set()
    while pending:
        obj = pending.pop()
        object_id = id(obj)
        if object_id in seen_objects:
            continue
        seen_objects.add(object_id)
        try:
            referents = gc.get_referents(obj)
        except Exception:
            continue
        referrer_type = type(obj).__name__
        for value in referents:
            if isinstance(value, (bytes, bytearray, str)):
                value_id = id(value)
                size = sys.getsizeof(value, 0)
                if size > 1024 * 1024:
                    if value_id not in large:
                        large[value_id] = [size, type(value).__name__, set()]
                    large[value_id][2].add(referrer_type)
            else:
                pending.append(value)

    lines = ["Section 1: GC census (top 30 by summed size)"]
    lines.extend(f"{name}: count={count}, size={size}" for name, (count, size) in
                 sorted(totals.items(), key=lambda item: item[1][1], reverse=True)[:30])
    lines.append("Section 2: largest strings and byte objects (top 20)")
    for size, kind, referrers in sorted(large.values(), key=lambda item: item[0], reverse=True)[:20]:
        lines.append(f"size={size}, type={kind}, referrers={', '.join(sorted(referrers))}")

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
        global _busy, _thread_start_failed
        if _busy:
            return
        _busy = True

        def write_one():
            global _busy, _thread_start_failed
            try:
                if _thread_start_failed:
                    _thread_start_failed = False
                    logger.error("Could not start previous memory report thread")
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
            _thread_start_failed = True

    signal.signal(signal.SIGUSR2, handler)
