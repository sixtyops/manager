"""Check log output with synthetic secrets and local transports only."""

import io
import logging
import logging.handlers
import sys
from unittest.mock import MagicMock

import httpx
import pytest

from updater import logging_filter as scrub


SLACK_URL = "https://hooks.slack.com/services/SYNTHETIC_TEAM/SYNTHETIC_CHANNEL/synthetic-capability"
SLACK_REDACTED = "https://hooks.slack.com/services/[REDACTED]"


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(scrub, "_secrets", set())
    monkeypatch.setattr(scrub, "_pattern", None)


@pytest.fixture
def application_output(monkeypatch):
    from updater import app

    root = logging.getLogger()
    old_level = root.level
    stream = io.StringIO()
    # Exercise basicConfig's real handler creation without changing test capture.
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(sys, "stderr", stream)
    app.configure_logging()
    try:
        yield app, stream, root.handlers[0]
    finally:
        root.handlers[0].close()
        root.setLevel(old_level)


def test_application_output_and_repeated_initialization(application_output):
    app, stream, handler = application_output
    formatter = handler.formatter
    scrub.register_secret("synthetic-app-value")
    handlers = list(logging.getLogger().handlers)
    for _ in range(3):
        app.configure_logging()
    assert logging.getLogger().handlers == handlers
    assert handler.formatter is formatter
    assert handler.level == logging.NOTSET
    assert logging.getLogger().level == logging.INFO

    app.logger.warning("Request failed with %s; retry %d", "synthetic-app-value", 2)
    output = stream.getvalue()
    assert " - updater.app - WARNING - Request failed with [REDACTED]; retry 2" in output
    assert "synthetic-app-value" not in output


def test_child_logger_and_mapping_arguments(application_output):
    _, stream, _ = application_output
    scrub.register_secret("synthetic-child-value")
    child = logging.getLogger("updater.log_scrub.child")
    child.warning("Device %(device)s failed with %(secret)s", {
        "device": "lab-device", "secret": "synthetic-child-value",
    })
    output = stream.getvalue()
    assert "updater.log_scrub.child - WARNING - Device lab-device failed with [REDACTED]" in output
    assert "synthetic-child-value" not in output


@pytest.mark.parametrize("explicit_cause", [False, True])
def test_application_chained_exception_output(application_output, explicit_cause):
    app, stream, _ = application_output
    scrub.register_secret("synthetic-inner-value")
    scrub.register_secret("synthetic-outer-value")
    try:
        try:
            raise ValueError("lookup failed: " + "synthetic-inner-value")
        except ValueError as error:
            if explicit_cause:
                raise RuntimeError("request failed: " + "synthetic-outer-value") from error
            raise RuntimeError("request failed: " + "synthetic-outer-value")
    except RuntimeError:
        app.logger.exception("Device operation failed")
    output = stream.getvalue()
    assert "synthetic-inner-value" not in output
    assert "synthetic-outer-value" not in output
    assert "Traceback (most recent call last)" in output
    assert "ValueError: lookup failed: [REDACTED]" in output
    assert "RuntimeError: request failed: [REDACTED]" in output
    assert "Device operation failed" in output
    assert ("direct cause" if explicit_cause else "During handling") in output


def test_empty_and_duplicate_values(application_output):
    app, stream, _ = application_output
    scrub.register_secret(None)
    scrub.register_secret("")
    app.logger.warning("Unchanged context")
    assert stream.getvalue().endswith(" - WARNING - Unchanged context\n")
    assert scrub._pattern is None

    scrub.register_secret("synthetic-duplicate-value")
    pattern = scrub._pattern
    scrub.register_secret("synthetic-duplicate-value")
    assert scrub._pattern is pattern
    app.logger.warning("%s", "synthetic-duplicate-value")
    assert stream.getvalue().endswith(" - WARNING - [REDACTED]\n")


def test_overlapping_and_literal_values(application_output):
    app, stream, _ = application_output
    for value in ("synthetic-prefix", "synthetic-prefix-long", "synthetic.[x]+\\value", "REDACTED"):
        scrub.register_secret(value)
    app.logger.warning("%s | %s | %s", "synthetic-prefix-long", "synthetic-prefix", "synthetic.[x]+\\value")
    assert stream.getvalue().endswith(" - WARNING - [REDACTED] | [REDACTED] | [REDACTED]\n")


def test_custom_formatter_and_record_are_preserved():
    class CustomFormatter(logging.Formatter):
        def format(self, record):
            return "custom context: " + super().format(record) + " synthetic-custom-value"

    scrub.register_secret("synthetic-custom-value")
    handler = logging.StreamHandler(io.StringIO())
    formatter = CustomFormatter("%(levelname)s %(message)s %(detail)s")
    handler.setFormatter(formatter)
    scrub.install_sanitizer(handler)
    try:
        raise ValueError("failure: " + "synthetic-custom-value")
    except ValueError:
        record = logging.LogRecord("synthetic", logging.ERROR, __file__, 1,
                                   "value=%s", ("synthetic-custom-value",), sys.exc_info())
    record.detail = "synthetic-custom-value"
    before = record.__dict__.copy()
    output = handler.format(record)
    assert "synthetic-custom-value" not in output
    assert "custom context: ERROR value=[REDACTED] [REDACTED]" in output
    assert "ValueError: failure: [REDACTED]" in output
    assert record.__dict__ == before
    assert handler.formatter.formatter is formatter

    # An unrelated handler still sees the original record and formatter.
    unrelated = logging.StreamHandler(io.StringIO())
    unrelated.setFormatter(formatter)
    assert "synthetic-custom-value" in unrelated.format(record)
    assert unrelated.formatter is formatter


def test_default_formatter_and_cached_exception_text():
    handler = logging.StreamHandler(io.StringIO())
    scrub.install_sanitizer(handler)
    scrub.register_secret("synthetic-cached-value")
    record = logging.LogRecord("synthetic", logging.ERROR, __file__, 1,
                               "value=%s", ("synthetic-cached-value",), None)
    record.exc_text = "cached exception: synthetic-cached-value"
    record.stack_info = "synthetic stack: synthetic-cached-value"
    before = record.__dict__.copy()
    assert handler.format(record) == (
        "value=[REDACTED]\ncached exception: [REDACTED]\nsynthetic stack: [REDACTED]"
    )
    assert record.__dict__ == before


def test_existing_root_formatter_and_unrelated_handler(monkeypatch):
    from updater import app

    output = io.StringIO()
    handler = logging.StreamHandler(output)
    formatter = logging.Formatter("existing: %(message)s")
    handler.setFormatter(formatter)
    handler.setLevel(logging.WARNING)
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [handler])
    unrelated = logging.StreamHandler(io.StringIO())
    unrelated_logger = logging.getLogger("unrelated.log_scrub")
    monkeypatch.setattr(unrelated_logger, "handlers", [unrelated])
    scrub.register_secret("synthetic-existing-value")
    app.configure_logging()
    app.logger.warning("%s", "synthetic-existing-value")
    assert output.getvalue() == "existing: [REDACTED]\n"
    assert handler.formatter.formatter is formatter
    assert handler.level == logging.WARNING
    assert unrelated.formatter is None


def test_application_leaves_custom_root_handler_unchanged(monkeypatch):
    from updater import app

    class CaptureHandler(logging.StreamHandler):
        pass

    handler = CaptureHandler(io.StringIO())
    formatter = logging.Formatter("capture: %(message)s")
    handler.setFormatter(formatter)
    monkeypatch.setattr(logging.getLogger(), "handlers", [handler])
    app.configure_logging()
    assert handler.formatter is formatter
    assert handler.handleError.__func__ is logging.Handler.handleError


@pytest.mark.parametrize("protocol", ["udp", "tcp"])
def test_syslog_output_and_reinitialization(monkeypatch, protocol):
    from updater import syslog_forwarder as sf

    transports = []

    def local_socket(handler):
        handler.unixsocket = False
        handler.socket = MagicMock()
        transports.append(handler.socket)

    # Use the real SysLogHandler formatter and emit method with a fake transport.
    monkeypatch.setattr(logging.handlers.SysLogHandler, "createSocket", local_socket)
    monkeypatch.setattr(sf, "_syslog_handler", None)
    syslog_logger = logging.getLogger("sixtyops.syslog")
    monkeypatch.setattr(syslog_logger, "handlers", [])
    monkeypatch.setattr(syslog_logger, "propagate", syslog_logger.propagate)
    monkeypatch.setattr(syslog_logger, "level", syslog_logger.level)
    monkeypatch.setattr(sf, "_syslog_logger", None)
    monkeypatch.setattr(sf, "_current_config", {})
    config = {"enabled": True, "host": "192.0.2.1", "port": 514,
              "protocol": protocol, "facility": "local0"}
    scrub.register_secret("synthetic-syslog-value")
    try:
        old_handler = sf._setup_handler(config)
        handler = sf._setup_handler(config)
        assert syslog_logger.handlers == [handler]
        assert not syslog_logger.propagate
        transports[0].close.assert_called_once()
        assert old_handler is not handler
        formatter = handler.formatter
        scrub.install_sanitizer(handler)
        assert handler.formatter is formatter

        sf.send_event("device", "Failure: synthetic-syslog-value", "error")
        try:
            raise ValueError("lookup failed: " + "synthetic-syslog-value")
        except ValueError:
            sf._syslog_logger.exception("Request failed with %s", "synthetic-syslog-value")
        sf.send_event("device", "Webhook failed: " + SLACK_URL, "error")
        sender = transports[1].sendto if protocol == "udp" else transports[1].sendall
        packets = [call.args[0].decode("utf-8") for call in sender.call_args_list]
        assert len(packets) == 3
        assert packets[0] == "<131>sixtyops: [device] Failure: [REDACTED]\x00"
        assert "sixtyops: Request failed with [REDACTED]" in packets[1]
        assert "ValueError: lookup failed: [REDACTED]" in packets[1]
        assert "Traceback (most recent call last)" in packets[1]
        assert all("synthetic-syslog-value" not in packet for packet in packets)
        assert packets[2] == "<131>sixtyops: [device] Webhook failed: " + SLACK_REDACTED + "\x00"
        assert "synthetic-capability" not in packets[2]
    finally:
        sf._setup_handler({**config, "enabled": False})


@pytest.mark.parametrize("protocol", ["udp", "tcp"])
@pytest.mark.parametrize("diagnostics_enabled", [False, True])
def test_syslog_transport_failure_sanitizes_stderr(monkeypatch, protocol, diagnostics_enabled):
    from updater import syslog_forwarder as sf

    transport = MagicMock()
    transport.sendto.side_effect = OSError("send failed: synthetic-transport-value")
    transport.sendall.side_effect = OSError("send failed: synthetic-transport-value")

    def local_socket(handler):
        handler.unixsocket = False
        handler.socket = transport

    monkeypatch.setattr(logging.handlers.SysLogHandler, "createSocket", local_socket)
    monkeypatch.setattr(sf, "_syslog_handler", None)
    monkeypatch.setattr(sf, "_syslog_logger", None)
    monkeypatch.setattr(sf, "_current_config", {})
    syslog_logger = logging.getLogger("sixtyops.syslog")
    monkeypatch.setattr(syslog_logger, "handlers", [])
    monkeypatch.setattr(syslog_logger, "propagate", syslog_logger.propagate)
    monkeypatch.setattr(syslog_logger, "level", syslog_logger.level)
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(logging, "raiseExceptions", diagnostics_enabled)
    config = {"enabled": True, "host": "192.0.2.1", "port": 514,
              "protocol": protocol, "facility": "local0"}
    scrub.register_secret("synthetic-transport-value")
    try:
        sf._setup_handler(config)
        sf.send_event("device", "Failure: synthetic-transport-value", "error")
        syslog_logger.error("Request failed with %s", "synthetic-transport-value")
        diagnostic = stream.getvalue()
        assert "synthetic-transport-value" not in diagnostic
        if diagnostics_enabled:
            assert "OSError: send failed: [REDACTED]" in diagnostic
            assert "Message: [device] Failure: [REDACTED]" in diagnostic
            assert "Message: Request failed with [REDACTED]" in diagnostic
        else:
            assert diagnostic == ""
    finally:
        sf._setup_handler({**config, "enabled": False})


def test_stream_formatter_failure_sanitizes_stderr(monkeypatch):
    class BrokenFormatter(logging.Formatter):
        def format(self, record):
            raise ValueError("formatter failed: " + "synthetic-formatter-value")

    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(logging, "raiseExceptions", True)
    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(BrokenFormatter())
    scrub.install_sanitizer(handler)
    scrub.register_secret("synthetic-formatter-value")
    record = logging.LogRecord("synthetic", logging.ERROR, __file__, 1,
                               "value=%s", ("synthetic-formatter-value",), None)
    before = record.__dict__.copy()
    handler.handle(record)
    assert "synthetic-formatter-value" not in stream.getvalue()
    assert "ValueError: formatter failed: [REDACTED]" in stream.getvalue()
    assert "Message: value=[REDACTED]" in stream.getvalue()
    assert record.__dict__ == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403, None])
async def test_actual_slack_send_sanitizes_httpx_output(application_output, monkeypatch, status):
    from updater import slack

    _, stream, _ = application_output
    url = SLACK_URL + "%2Fencoded?synthetic-query=value#synthetic-fragment"
    payload = {"text": "Synthetic notification"}
    requests = []

    def respond(request):
        requests.append(request)
        if status is None:
            raise httpx.ConnectError("Synthetic connection failure: " + url, request=request)
        return httpx.Response(status, text="Synthetic response")

    real_client = httpx.AsyncClient

    def local_client(**kwargs):
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(slack.db, "get_setting", lambda key, default: url)
    monkeypatch.setattr(slack.httpx, "AsyncClient", local_client)
    assert await slack.send_slack_notification(payload) is (status == 200)
    assert len(requests) == 1
    assert requests[0].url == httpx.URL(url)
    assert requests[0].content == b'{"text":"Synthetic notification"}'
    output = stream.getvalue()
    assert SLACK_REDACTED in output
    assert "synthetic-capability" not in output
    assert "synthetic-query" not in output
    assert "synthetic-fragment" not in output
    if status is None:
        assert "Failed to send Slack notification: Synthetic connection failure:" in output
    else:
        assert f'HTTP Request: POST {SLACK_REDACTED} "HTTP/1.1 {status}' in output
        assert ("Slack notification sent successfully" if status == 200
                else "Slack webhook returned status 403") in output
    assert scrub._secrets == set()
    assert scrub._pattern is None


@pytest.mark.parametrize("wrapper", ["%s", "'%s'", '"%s"', "(%s)", "[%s]", "<%s>", "{%s}", "failed:%s", "url=%s"])
@pytest.mark.parametrize("suffix", ["", "%2Fencoded", "?synthetic-query=a%26b,c;d#synthetic-fragment"])
def test_slack_url_boundaries_and_encoding(application_output, wrapper, suffix):
    app, stream, _ = application_output
    app.logger.warning("URL: %s next", wrapper % (SLACK_URL + suffix))
    assert stream.getvalue().endswith("URL: " + wrapper % SLACK_REDACTED + " next\n")


@pytest.mark.parametrize("url", [
    "https://example.com/services/synthetic-capability",
    "https://hooks.slack.com.example.com/services/synthetic-capability",
    "https://other.slack.com/services/synthetic-capability",
    "https://hooks.slack.com/ordinary/synthetic-context",
    "http://hooks.slack.com/services/synthetic-capability",
    "xhttps://hooks.slack.com/services/synthetic-capability",
    "https%3A%2F%2Fhooks.slack.com%2Fservices%2Fsynthetic-capability",
])
def test_noncanonical_urls_are_unchanged(application_output, url):
    app, stream, _ = application_output
    app.logger.warning("URL: %s", url)
    assert stream.getvalue().endswith("URL: " + url + "\n")


def test_slack_scheme_and_host_case(application_output):
    app, stream, _ = application_output
    app.logger.warning("%s", SLACK_URL.replace("https://hooks.slack.com", "HTTPS://HOOKS.SLACK.COM"))
    assert stream.getvalue().endswith(SLACK_REDACTED + "\n")


def test_multiple_slack_urls_do_not_grow_registry(application_output):
    app, stream, _ = application_output
    scrub.register_secret("synthetic-registered-value")
    pattern = scrub._pattern
    for index in range(100):
        app.logger.warning("%s | %s | %s", SLACK_URL + str(index),
                           SLACK_URL + "%2Fsecond", "synthetic-registered-value")
    assert stream.getvalue().count(SLACK_REDACTED) == 200
    assert "synthetic-capability" not in stream.getvalue()
    assert "synthetic-registered-value" not in stream.getvalue()
    assert scrub._secrets == {"synthetic-registered-value"}
    assert scrub._pattern is pattern


def test_slack_redaction_precedes_registered_literal_matching(application_output):
    app, stream, _ = application_output
    scrub.register_secret("hooks.slack.com")
    app.logger.warning("%s", SLACK_URL)
    assert stream.getvalue().endswith("https://[REDACTED]/services/[REDACTED]\n")


def test_slack_traceback_and_formatter_failure(application_output, monkeypatch):
    app, stream, handler = application_output
    try:
        raise ValueError("Synthetic failure: " + SLACK_URL)
    except ValueError:
        app.logger.exception("Request failed: %s", SLACK_URL)
    assert "Traceback (most recent call last)" in stream.getvalue()
    assert "ValueError: Synthetic failure: " + SLACK_REDACTED in stream.getvalue()
    assert "synthetic-capability" not in stream.getvalue()

    class BrokenFormatter(logging.Formatter):
        def format(self, record):
            raise ValueError("Synthetic formatter failure: " + SLACK_URL)

    handler.setFormatter(BrokenFormatter())
    scrub.install_sanitizer(handler)
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(logging, "raiseExceptions", True)
    record = logging.LogRecord("synthetic", logging.ERROR, __file__, 1,
                               "Request failed: %s", (SLACK_URL,), None)
    before = record.__dict__.copy()
    handler.handle(record)
    output = stream.getvalue()
    assert "ValueError: Synthetic formatter failure: " + SLACK_REDACTED in output
    assert "Message: Request failed: " + SLACK_REDACTED in output
    assert "synthetic-capability" not in output
    assert record.__dict__ == before


def test_long_slack_input(application_output):
    app, stream, _ = application_output
    # Exercise long matches and many near matches without a timing-dependent gate.
    context = "https://hooks.slack.com/service/ordinary " * 10000
    app.logger.warning("%s%s end", context, SLACK_URL + "a" * 1000000)
    assert stream.getvalue().endswith(context + SLACK_REDACTED + " end\n")
    assert scrub._secrets == set()
