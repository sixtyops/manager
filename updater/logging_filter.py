"""Redact HTTP URL userinfo, canonical Slack URLs and registered log literals.

Call register_secret(value) before logging a secret. Then call
install_sanitizer(handler) on each output handler. Registration lasts for
the process lifetime. Credential sources are not registered here.
"""

import copy
import heapq
import json
import logging
import re
import sys
import threading
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import MethodType
from urllib.parse import unquote, urlsplit

import httpx

_secrets: set[str] = set()
_pattern: re.Pattern[str] | None = None
_registry_lock = threading.Lock()
# Match canonical capability URLs up to ordinary log delimiters. The fixed
# prefix and single character class also cover encoded paths, queries and fragments.
_slack_webhook = re.compile(
    r"(?<![\w+./-])https://hooks\.slack\.com/services/[^\s\"'<>()[\]{}]+",
    re.IGNORECASE,
)
# Match one ordinary URL token. Known raw webhook credentials use a local
# source context instead of an ambiguous scan through diagnostic text.
_http_userinfo = re.compile(
    r'(?<![\w+./-])(https?://)[^\s/?#@"<>\[\]{}]+@',
    re.IGNORECASE,
)


_url_redactor: ContextVar[Callable[[str], str] | None] = ContextVar("webhook_url_redactor", default=None)


def _replace_url_literals(output: str, pairs: dict[str, str]) -> str:
    """Replace the earliest known literal, with the longest match first."""
    pairs = {"[REDACTED]": "[REDACTED]", **pairs}
    matches = []
    for value in pairs:
        position = output.find(value)
        if position >= 0:
            heapq.heappush(matches, (position, -len(value), value))
    pieces = []
    cursor = 0
    while matches:
        position, _, value = heapq.heappop(matches)
        if position >= cursor:
            pieces.extend((output[cursor:position], pairs[value]))
            cursor = position + len(value)
        position = output.find(value, cursor)
        if position >= 0:
            heapq.heappush(matches, (position, -len(value), value))
    pieces.append(output[cursor:])
    return "".join(pieces)


@contextmanager
def redact_url_credentials(url: str, error_type: str = "Error") -> Iterator[Callable[[str], str] | None]:
    """Limit known credential redaction to one synchronous log call.

    Yield the output sanitizer, or None when extraction fails. The failure
    context emits only a fixed diagnostic, including formatter error output.
    Do not await or create tasks inside this context.
    """
    try:
        parsed = urlsplit(url)
        canonical = str(httpx.URL(url))
        pairs = {}
        if "@" in parsed.netloc:
            raw = url[:url.index("://") + 3] + parsed.netloc.rsplit("@", 1)[0] + "@"
            encoded = urlsplit(canonical)
            prefix = canonical[:canonical.index("://") + 3]
            canonical_prefix = prefix + encoded.netloc.rsplit("@", 1)[0] + "@"
            for value in (raw, canonical_prefix, repr(raw)[1:-1], json.dumps(raw)[1:-1]):
                pairs[value] = value[:value.index("://") + 3] + "[REDACTED]@"
            for field in (parsed.username, parsed.password):
                if field:
                    for value in (field, unquote(field)):
                        for form in (value, repr(value)[1:-1], json.dumps(value)[1:-1]):
                            pairs[form] = "[REDACTED]"
        # At most four prefixes and twelve field forms. Use literal searches,
        # not a dynamic regex. Preserve the fixed marker on repeated passes.
        redactor = (lambda output: _replace_url_literals(output, pairs)) if pairs else None
        available = True
    except Exception:
        diagnostic = f"Failed to send webhook: {error_type} (error details unavailable)"
        redactor = lambda output: diagnostic
        available = False
    token = _url_redactor.set(redactor)
    try:
        yield _sanitize if available else None
    finally:
        _url_redactor.reset(token)


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
    redactor = _url_redactor.get()
    if redactor is not None:
        output = redactor(output)
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
