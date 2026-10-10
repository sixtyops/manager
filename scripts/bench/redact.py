"""Redact bench output before a worker or a log file sees it.

Read stdin line by line and write the redacted line to stdout. Replace:

- the value of each SIXTYOPS_TEST_* and SIXTYOPS_BENCH_* variable.
  run-readonly-bench.sh exports each value from the access file as
  SIXTYOPS_BENCH_REDACT_<n>, so values with no mapped name are also replaced.
- each IPv4 address.

Values shorter than 3 characters are not replaced, because they would
remove normal text. The wrapper never sets such values on purpose.
"""

from __future__ import annotations

import os
import re
import sys

IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
PREFIXES = ("SIXTYOPS_TEST_", "SIXTYOPS_BENCH_")
# The summary directory is not secret. The wrapper prints it so the worker
# can find the summary.
SKIP_NAMES = {"SIXTYOPS_BENCH_SUMMARY_DIR", "SIXTYOPS_BENCH_PYTHON"}
MIN_LEN = 3


def secret_values(environ: dict[str, str]) -> list[str]:
    names = {n for n in environ if n.startswith(PREFIXES)} - SKIP_NAMES
    values = {environ[n] for n in names if len(environ.get(n, "")) >= MIN_LEN}
    # Replace long values first so a short value inside a long one does not
    # leave part of the long value.
    return sorted(values, key=len, reverse=True)


def redact(line: str, values: list[str]) -> str:
    for value in values:
        line = line.replace(value, "[REDACTED]")
    return IPV4.sub("[IP]", line)


def main() -> int:
    values = secret_values(dict(os.environ))
    for line in sys.stdin:
        sys.stdout.write(redact(line, values))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
