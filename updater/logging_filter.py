"""Redact HTTP URL userinfo, canonical Slack URLs and registered log literals.

Call register_secret(value) before logging a secret. Then call
install_sanitizer(handler) on each output handler. Registration lasts for
the process lifetime. Credential sources are not registered here.
"""

import copy
import logging
import re
import sys
import threading
import traceback
from types import MethodType

_secrets: set[str] = set()
_pattern: re.Pattern[str] | None = None
_registry_lock = threading.Lock()
# Match canonical capability URLs up to ordinary log delimiters. The fixed
# prefix and single character class also cover encoded paths, queries and fragments.
_slack_webhook = re.compile(
    r"(?<![\w+./-])https://hooks\.slack\.com/services/[^\s\"'<>()[\]{}]+",
    re.IGNORECASE,
)
# Scan only the authority, before a slash, query, fragment or log delimiter.
# One character class avoids nested repetition; retain the rest of the URL.
_http_userinfo = re.compile(
    r"(?<![\w+./-])(https?://)[^/?#\s\"<>[\]{}]+@",
    re.IGNORECASE,
)


def register_secret(value: str | None) -> None:
    """Register a literal secret. Ignore None, empty values, and duplicates."""
    global _pattern
    if not value:
        return
    with _registry_lock:
        if value in _secrets:
            return
        _secrets.add(value)
        # Match longer values first and do not scan replacement text again.
        _pattern = re.compile("|".join(re.escape(secret) for secret in
                                     sorted(_secrets, key=len, reverse=True)))


class _SanitizingFormatter(logging.Formatter):
    """Sanitize the full output of the existing formatter."""

    def __init__(self, formatter: logging.Formatter | None) -> None:
        super().__init__()
        self.formatter = formatter if formatter is not None else logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        # Format a copy so cached exception text and messages stay handler-local.
        output = self.formatter.format(copy.copy(record))
        return _sanitize(output)


def _sanitize(output: str) -> str:
    # Redact URLs first so a registered substring cannot break their recognition.
    output = _http_userinfo.sub(r"\1[REDACTED]@", output)
    output = _slack_webhook.sub("https://hooks.slack.com/services/[REDACTED]", output)
    with _registry_lock:
        pattern = _pattern
    return pattern.sub("[REDACTED]", output) if pattern is not None else output


def _handle_error(handler: logging.Handler, record: logging.LogRecord) -> None:
    """Keep stdlib error diagnostics enabled without printing raw arguments."""
    if not logging.raiseExceptions or not sys.stderr:
        return
    try:
        try:
            message = record.getMessage()
        except Exception:
            message = "Unable to format the log message."
        diagnostic = (
            "--- Logging error ---\n" + traceback.format_exc()
            + "Call stack:\n" + "".join(traceback.format_stack()[:-1])
            + f"Logged from file {record.filename}, line {record.lineno}\n"
            + f"Message: {message}\n"
        )
        sys.stderr.write(_sanitize(diagnostic))
    except OSError:
        pass


def install_sanitizer(handler: logging.Handler) -> None:
    """Wrap this handler's formatter once. Leave other handlers unchanged."""
    handler.acquire()
    try:
        if not isinstance(handler.formatter, _SanitizingFormatter):
            handler.setFormatter(_SanitizingFormatter(handler.formatter))
        # Stdlib handleError bypasses the formatter and prints raw arguments.
        # Leave custom error handlers unchanged; this covers our current outputs.
        if getattr(handler.handleError, "__func__", None) is logging.Handler.handleError:
            handler.handleError = MethodType(_handle_error, handler)
    finally:
        handler.release()
