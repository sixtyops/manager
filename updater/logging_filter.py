"""Redact HTTP URL userinfo, canonical Slack URLs and registered log literals.

Call register_secret(value) before logging a secret. Then call
install_sanitizer(handler) on each output handler. Registration lasts for
the process lifetime for ordinary secrets. Generated API token literals use a
bounded registry and a fixed-format fallback. Credential sources are not
registered here. Device token registrations follow the owning client lifetime.
"""

import copy
import heapq
import json
import logging
import re
import sys
import threading
import traceback
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import MethodType
from urllib.parse import unquote, urlsplit

import httpx

_device_tokens: dict[weakref.ReferenceType, str] = {}

_secrets: set[str] = set()
_generated_api_tokens: OrderedDict[str, None] = OrderedDict()
_GENERATED_API_TOKEN_LIMIT = 100
_pattern: re.Pattern[str] | None = None
# Client collection can retire tokens while a literal pattern is being built.
_registry_lock = threading.RLock()
# Generated tokens use token_urlsafe(32): a fixed prefix and 43 URL-safe chars.
# This keeps evicted tokens redactable without retaining their literal values.
_generated_api_token_pattern = re.compile(r"tach_[A-Za-z0-9_-]{43}")
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


def _retire_device_token(owner_ref: weakref.ReferenceType) -> None:
    with _registry_lock:
        if owner_ref in _device_tokens:
            del _device_tokens[owner_ref]
            _rebuild_secret_pattern()


def register_device_token(owner: object, value: str | None) -> None:
    """Keep one token per live client, without retaining the client or history."""
    if not value:
        return
    owner_ref = weakref.ref(owner, _retire_device_token)
    with _registry_lock:
        _device_tokens[owner_ref] = value
        _rebuild_secret_pattern()


def register_secret(value: str | None) -> None:
    """Register a literal secret. Ignore None, empty values, and duplicates."""
    if not value:
        return
    with _registry_lock:
        if value in _secrets:
            return
        _secrets.add(value)
        _rebuild_secret_pattern()


def register_generated_api_token(value: str | None) -> None:
    """Register a generated API token in a bounded registry of recent values."""
    if not value:
        return
    with _registry_lock:
        _generated_api_tokens.pop(value, None)
        _generated_api_tokens[value] = None
        while len(_generated_api_tokens) > _GENERATED_API_TOKEN_LIMIT:
            _generated_api_tokens.popitem(last=False)
        _rebuild_secret_pattern()


def _rebuild_secret_pattern() -> None:
    """Compile one longest-first pattern for all registered literal forms."""
    global _pattern
    secrets = _secrets | set(_generated_api_tokens) | set(_device_tokens.values())
    forms = {form for secret in secrets for form in (
        secret, repr(secret)[1:-1], json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
    )}
    _pattern = re.compile("|".join(re.escape(form) for form in
                                 sorted(forms, key=len, reverse=True))) if forms else None


class _SanitizingFormatter(logging.Formatter):
    """Sanitize the full output of the existing formatter."""

    def __init__(self, formatter: logging.Formatter | None) -> None:
        super().__init__()
        self.formatter = formatter if formatter is not None else logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        # Format a copy so cached exception text and messages stay handler-local.
        output = self.formatter.format(copy.copy(record))
        return _sanitize(output)


def _redact_token_spans(output: str, pattern: re.Pattern[str] | None) -> str:
    """Collect format and literal matches before replacing overlapping spans."""
    spans = []
    for matcher in (_generated_api_token_pattern, pattern):
        if matcher is None:
            continue
        position = 0
        while match := matcher.search(output, position):
            spans.append(match.span())
            # An interior match can extend beyond the preceding literal.
            position = match.start() + 1
    spans.sort(key=lambda span: (span[0], -span[1]))
    merged = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    pieces = []
    position = 0
    for start, end in merged:
        pieces.extend((output[position:start], "[REDACTED]"))
        position = end
    pieces.append(output[position:])
    return "".join(pieces)


def _sanitize(output: str) -> str:
    redactor = _url_redactor.get()
    if redactor is not None:
        output = redactor(output)
    # Redact URLs first so a registered substring cannot break their recognition.
    output = _http_userinfo.sub(r"\1[REDACTED]@", output)
    output = _slack_webhook.sub("https://hooks.slack.com/services/[REDACTED]", output)
    with _registry_lock:
        pattern = _pattern
    return _redact_token_spans(output, pattern)


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
