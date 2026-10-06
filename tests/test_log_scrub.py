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
USERINFO_URL = "https://synthetic-user:synthetic-password@receiver.example.test:8443/hook?mode=test#result"
USERINFO_REDACTED = "https://[REDACTED]@receiver.example.test:8443/hook?mode=test#result"


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


@pytest.mark.asyncio
async def test_poller_fetch_warning_uses_sanitized_application_output(application_output):
    from unittest.mock import AsyncMock, patch

    from updater.poller import NetworkPoller

    _, stream, _ = application_output
    synthetic_secret = "synthetic-poller-warning-value"
    scrub.register_secret(synthetic_secret)
    fake_client = MagicMock()
    fake_client.connect = AsyncMock(
        side_effect=RuntimeError(f"fetch failed: {synthetic_secret}")
    )
    poller = NetworkPoller()

    with patch("updater.poller.get_driver", return_value=lambda *args, **kwargs: fake_client), \
            patch("updater.poller.db.update_device_config_poll_status") as update_status:
        await poller._fetch_and_store_config("10.0.0.1", "test-user", "test-password")

    output = stream.getvalue()
    assert " - updater.poller - WARNING - Config poll: error fetching config from 10.0.0.1" in output
    assert "fetch failed: [REDACTED]" in output
    assert synthetic_secret not in output
    update_status.assert_called_once_with(
        "10.0.0.1", "unknown", f"fetch failed: {synthetic_secret}"
    )


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
        sf.send_event("device", "Webhook failed: " + USERINFO_URL, "error")
        sender = transports[1].sendto if protocol == "udp" else transports[1].sendall
        packets = [call.args[0].decode("utf-8") for call in sender.call_args_list]
        assert len(packets) == 4
        assert packets[0] == "<131>sixtyops: [device] Failure: [REDACTED]\x00"
        assert "sixtyops: Request failed with [REDACTED]" in packets[1]
        assert "ValueError: lookup failed: [REDACTED]" in packets[1]
        assert "Traceback (most recent call last)" in packets[1]
        assert all("synthetic-syslog-value" not in packet for packet in packets)
        assert packets[2] == "<131>sixtyops: [device] Webhook failed: " + SLACK_REDACTED + "\x00"
        assert "synthetic-capability" not in packets[2]
        assert packets[3] == "<131>sixtyops: [device] Webhook failed: " + USERINFO_REDACTED + "\x00"
        assert "synthetic-password" not in packets[3]
    finally:
        sf._setup_handler({**config, "enabled": False})


@pytest.mark.parametrize("protocol", ["udp", "tcp"])
@pytest.mark.parametrize("diagnostics_enabled", [False, True])
@pytest.mark.parametrize("secret,redacted", [("synthetic-transport-value", "[REDACTED]"),
                                              (USERINFO_URL, USERINFO_REDACTED)])
def test_syslog_transport_failure_sanitizes_stderr(monkeypatch, protocol, diagnostics_enabled, secret, redacted):
    from updater import syslog_forwarder as sf

    transport = MagicMock()
    transport.sendto.side_effect = OSError("send failed: " + secret)
    transport.sendall.side_effect = OSError("send failed: " + secret)

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
    if secret != USERINFO_URL:
        scrub.register_secret(secret)
    try:
        sf._setup_handler(config)
        sf.send_event("device", "Failure: " + secret, "error")
        syslog_logger.error("Request failed with %s", secret)
        diagnostic = stream.getvalue()
        assert secret not in diagnostic
        if diagnostics_enabled:
            assert "OSError: send failed: " + redacted in diagnostic
            assert "Message: [device] Failure: " + redacted in diagnostic
            assert "Message: Request failed with " + redacted in diagnostic
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




@pytest.mark.parametrize("url,expected", [
    (USERINFO_URL, USERINFO_REDACTED),
    ("HtTpS://synthetic-user:synthetic-password@host.test/path", "HtTpS://[REDACTED]@host.test/path"),
    ("http://synthetic-user@host.test", "http://[REDACTED]@host.test"),
    ("https://user%40example:p%3Ass%2Fword@host.test/?q=a@b#c@d", "https://[REDACTED]@host.test/?q=a@b#c@d"),
    ("https://user:password@[2001:db8::1]:8443/path", "https://[REDACTED]@[2001:db8::1]:8443/path"),
    ("https://user:password@host.test?next=a@b", "https://[REDACTED]@host.test?next=a@b"),
    ("https://synthetic'user:p(ass)@host.test/path", "https://[REDACTED]@host.test/path"),
])
@pytest.mark.parametrize("wrapper", ["%s", "'%s'", '"%s"', "(%s)", "[%s]", "<%s>", "{%s}", "url=%s"])
def test_userinfo_authority_and_log_boundaries(application_output, url, expected, wrapper):
    app, stream, _ = application_output
    app.logger.warning("URL: %s next", wrapper % url)
    assert stream.getvalue().endswith("URL: " + wrapper % expected + " next\n")
    assert scrub._secrets == set()


@pytest.mark.parametrize("url", [
    "https://host.test/path@user:password",
    "https://host.test?email=user:password@other.test",
    "https://host.test#user:password@other.test",
    "https://[2001:db8::1]:8443/path",
    "https://host.test/no-credentials",
    "user:password@host.test",
    "ftp://user:password@host.test/path",
    "xhttps://user:password@host.test/path",
    "https%3A%2F%2Fuser%3Apassword%40host.test",
    "https://host.test/path?text=a@b#c@d",
])
def test_userinfo_non_authority_controls(application_output, url):
    app, stream, _ = application_output
    app.logger.warning("%s", url)
    assert stream.getvalue().endswith(url + "\n")


def test_userinfo_multiple_urls_and_literal_order(application_output):
    app, stream, _ = application_output
    scrub.register_secret("synthetic-user")
    pattern = scrub._pattern
    for _ in range(100):
        app.logger.warning("%s %s %s", USERINFO_URL, USERINFO_URL, SLACK_URL)
    assert stream.getvalue().count(USERINFO_REDACTED) == 200
    assert stream.getvalue().count(SLACK_REDACTED) == 100
    assert "synthetic-user" not in stream.getvalue()
    assert "synthetic-password" not in stream.getvalue()
    assert scrub._secrets == {"synthetic-user"}
    assert scrub._pattern is pattern


@pytest.mark.parametrize("userinfo", [
    "synthetic-user:synthetic-password", 'synthetic-user:synthetic"password',
    "synthetic-user:synthetic password", "synthetic-user:synthetic{password",
    "synthetic-user:synthetic}password", "synthetic-user:synthetic<password",
    "synthetic-user:synthetic>password",
])
def test_userinfo_chained_exceptions_and_formatter_diagnostics(application_output, monkeypatch, userinfo):
    app, stream, handler = application_output
    url = "https://" + userinfo + "@receiver.example.test:8443/hook?mode=test#result"
    redacted = USERINFO_REDACTED
    with scrub.redact_url_credentials(url):
        try:
            try:
                raise ValueError("Request: " + url)
            except ValueError as cause:
                raise RuntimeError("Retry: " + url) from cause
        except RuntimeError:
            app.logger.exception("Failure: %s", url)
        assert "ValueError: Request: " + redacted in stream.getvalue()
        assert "RuntimeError: Retry: " + redacted in stream.getvalue()
        assert "Traceback (most recent call last)" in stream.getvalue()

        class BrokenFormatter(logging.Formatter):
            def format(self, record):
                raise ValueError("Formatter: " + url)

        handler.setFormatter(BrokenFormatter())
        scrub.install_sanitizer(handler)
        monkeypatch.setattr(sys, "stderr", stream)
        monkeypatch.setattr(logging, "raiseExceptions", True)
        record = logging.LogRecord("synthetic", logging.ERROR, __file__, 1,
                                   "Failure: %s", (url,), None)
        handler.handle(record)
        assert "ValueError: Formatter: " + redacted in stream.getvalue()
        assert "Message: Failure: " + redacted in stream.getvalue()
        assert userinfo.split(":", 1)[1] not in stream.getvalue()
    assert scrub._secrets == set()


@pytest.mark.timeout(5)
def test_userinfo_long_input_is_bounded(application_output):
    app, stream, _ = application_output
    # Exercise long nonmatches, a long authority and many repeated matches.
    context = "https://" + "a" * 1000000 + "/path@ordinary "
    repeated = ("https://user:password@host.test/path " * 10000)
    app.logger.warning("%s%s%s end", context, repeated,
                       "https://user:" + "p" * 1000000 + "@host.test/path")
    expected = context + "https://[REDACTED]@host.test/path " * 10000
    assert stream.getvalue().endswith(expected + "https://[REDACTED]@host.test/path end\n")
    assert scrub._secrets == set()


@pytest.mark.parametrize("message,expected", [
    ("https://host.test failed; contact support@example.test", "https://host.test failed; contact support@example.test"),
    ("https://host.test:8443 failed; contact support@example.test", "https://host.test:8443 failed; contact support@example.test"),
    ("https://host.test\nERROR timeout\ncontact support@example.test", "https://host.test\nERROR timeout\ncontact support@example.test"),
    ("https://user:password@host.test failed; contact support@example.test", "https://[REDACTED]@host.test failed; contact support@example.test"),
    ("https://user:password@host.test\nERROR timeout\ncontact support@example.test", "https://[REDACTED]@host.test\nERROR timeout\ncontact support@example.test"),
    ("https://user:password@[2001:db8::1]:8443 failed; contact support@example.test", "https://[REDACTED]@[2001:db8::1]:8443 failed; contact support@example.test"),
    ("https://u:p@one.test https://v:q@two.test", "https://[REDACTED]@one.test https://[REDACTED]@two.test"),
])
def test_userinfo_diagnostics_do_not_cross_authority(application_output, message, expected):
    app, stream, _ = application_output
    app.logger.error("Endpoint %s", message)
    assert stream.getvalue().endswith("Endpoint " + expected + "\n")


@pytest.mark.parametrize("url", [
    'HtTpS://synthetic-user:synthetic" password@host.test:8443/hook',
    "https://synthetic%40user:synthetic%3Apassword@host.test/hook",
    "https://synthetic-user@[2001:db8::1]:8443",
    "https://:synthetic-password@host.test/hook",
])
def test_known_url_raw_canonical_and_escaped_forms(application_output, url):
    import json
    app, stream, _ = application_output
    control = "Endpoint https://host.test:8443 failed; contact support@example.test"
    with scrub.redact_url_credentials(url) as sanitize:
        assert sanitize is not None
        for form in (url, str(httpx.URL(url)), repr(url)[1:-1], json.dumps(url)[1:-1]):
            app.logger.error("URL %s; %s", sanitize(form), control)
    output = stream.getvalue()
    assert output.count(control) == 4
    assert "synthetic" not in output
    assert output.count("[REDACTED]@") == 4
    assert scrub._secrets == set() and scrub._pattern is None


@pytest.mark.parametrize("userinfo,message,expected", [
    ("u:R", "R", "[REDACTED]"),
    ("x:failed", "failed at host.test; contact support@example.test", "[REDACTED] at host.test; contact support@e[REDACTED]ample.test"),
    ("receiver.example.test:synthetic-password", "receiver.example.test failed; other.test", "[REDACTED] failed; other.test"),
    (":", "Ordinary error", "Ordinary error"),
])
def test_known_short_empty_and_colliding_credentials(userinfo, message, expected):
    with scrub.redact_url_credentials("https://" + userinfo + "@host.test") as sanitize:
        assert sanitize(message) == expected
        assert sanitize(sanitize(message)) == expected
    assert scrub._sanitize(message) == message
    assert scrub._secrets == set()


@pytest.mark.asyncio
async def test_known_url_scopes_are_isolated_nested_and_cancellation_safe():
    import asyncio

    async def observe(password):
        with scrub.redact_url_credentials("https://synthetic-user:" + password + "@host.test") as sanitize:
            await asyncio.sleep(0)
            other = "FIRST_PASSWORD" if password == "SECOND_PASSWORD" else "SECOND_PASSWORD"
            assert sanitize(password + " " + other) == "[REDACTED] " + other

    await asyncio.gather(observe("FIRST_PASSWORD"), observe("SECOND_PASSWORD"))
    with scrub.redact_url_credentials("https://synthetic-user:FIRST_PASSWORD@host.test") as sanitize:
        with scrub.redact_url_credentials("https://synthetic-user:SECOND_PASSWORD@host.test") as nested:
            assert nested("FIRST_PASSWORD SECOND_PASSWORD") == "FIRST_PASSWORD [REDACTED]"
        assert sanitize("FIRST_PASSWORD SECOND_PASSWORD") == "[REDACTED] SECOND_PASSWORD"
    with pytest.raises(asyncio.CancelledError):
        with scrub.redact_url_credentials("https://synthetic-user:FIRST_PASSWORD@host.test"):
            raise asyncio.CancelledError()
    assert scrub._sanitize("FIRST_PASSWORD SECOND_PASSWORD") == "FIRST_PASSWORD SECOND_PASSWORD"


def test_known_url_rotation_does_not_grow_registry():
    for index in range(200):
        password = "synthetic-password-" + str(index)
        with scrub.redact_url_credentials("https://synthetic-user:" + password + "@host.test") as sanitize:
            assert sanitize(password) == "[REDACTED]"
        assert scrub._sanitize(password) == password
    assert scrub._secrets == set() and scrub._pattern is None


@pytest.mark.timeout(5)
def test_known_url_long_input_is_bounded():
    password = "synthetic-password-" + "a" * 60000
    control = "https://host.test:8443 failed; contact support@example.test\n"
    with scrub.redact_url_credentials("https://synthetic-user:" + password + "@host.test") as sanitize:
        message = control * 10000 + password
        assert sanitize(message) == control * 10000 + "[REDACTED]"


def test_extraction_failure_also_hides_formatter_error(application_output, monkeypatch):
    app, stream, handler = application_output
    url = 'https://synthetic-user:synthetic" password@host.test'

    def fail(value):
        raise ValueError("Synthetic parser failure: " + url)

    class BrokenFormatter(logging.Formatter):
        def format(self, record):
            raise ValueError("Synthetic formatter failure: " + url)

    monkeypatch.setattr(scrub, "urlsplit", fail)
    handler.setFormatter(BrokenFormatter())
    scrub.install_sanitizer(handler)
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(logging, "raiseExceptions", True)
    with scrub.redact_url_credentials(url, "ConnectError") as sanitize:
        assert sanitize is None
        app.logger.error("Failed to send webhook: ConnectError")
    assert stream.getvalue() == "Failed to send webhook: ConnectError (error details unavailable)"
    assert scrub._sanitize("synthetic password") == "synthetic password"


@pytest.mark.timeout(5)
def test_known_repetitive_credential_nonmatch_is_bounded():
    password = "a" * 60000 + "b"
    with scrub.redact_url_credentials("https://synthetic-user:" + password + "@host.test") as sanitize:
        message = "a" * 1000000 + " end"
        assert sanitize(message) == message


def test_known_short_credential_marker_survives_handler_pass(application_output):
    app, stream, _ = application_output
    with scrub.redact_url_credentials("https://synthetic-user:R@host.test") as sanitize:
        app.logger.error("Known password: %s", sanitize("R"))
    assert stream.getvalue().endswith("Known password: [REDACTED]\n")
    assert "[[REDACTED]" not in stream.getvalue()


ESCAPED_SECRETS = [
    "synthetic\\credential", "synthetic\n\tcredential",
    "synthetic-é-credential", "synthetic'\"credential",
]


@pytest.mark.parametrize("secret", ESCAPED_SECRETS)
@pytest.mark.parametrize("form", ["raw", "repr", "nested", "json_ascii", "json_unicode"])
def test_registered_secret_escaped_output(application_output, monkeypatch, secret, form):
    import json
    from cryptography.fernet import Fernet
    from updater import crypto

    app, stream, _ = application_output
    monkeypatch.setattr(crypto, "_fernet", Fernet(Fernet.generate_key()))
    crypto.encrypt_password(secret)
    pattern = scrub._pattern
    if form == "raw":
        rendered = secret
        app.logger.warning("Failure: %s; retry later", secret)
    elif form == "repr":
        rendered = repr(secret)[1:-1]
        app.logger.warning("Failure: %r; retry later", secret)
    elif form == "nested":
        rendered = repr(secret)[1:-1]
        app.logger.warning("Failure: %s; retry later", {"values": [secret]})
    else:
        ascii_mode = form == "json_ascii"
        rendered = json.dumps(secret, ensure_ascii=ascii_mode)[1:-1]
        app.logger.warning("Failure: %s; retry later",
                           json.dumps({"values": [secret]}, ensure_ascii=ascii_mode))
    output = stream.getvalue()
    assert rendered not in output
    assert "Failure:" in output and "; retry later" in output
    assert "[REDACTED]" in output
    assert scrub._secrets == {secret}
    assert scrub._pattern is pattern
    scrub.register_secret(secret)
    assert scrub._pattern is pattern


def test_registered_escaped_chained_exceptions(application_output):
    app, stream, _ = application_output
    secret = ESCAPED_SECRETS[1]
    scrub.register_secret(secret)
    try:
        try:
            raise ValueError({"lookup": secret})
        except ValueError as error:
            raise RuntimeError({"request": [secret]}) from error
    except RuntimeError:
        app.logger.exception("Operation failed; retry later")
    output = stream.getvalue()
    assert repr(secret)[1:-1] not in output
    assert "lookup" in output and "request" in output
    assert "[REDACTED]" in output and "direct cause" in output
    assert "Operation failed; retry later" in output


def test_registered_escaped_formatter_failure(application_output, monkeypatch):
    import json

    app, stream, handler = application_output
    secret = ESCAPED_SECRETS[2]
    scrub.register_secret(secret)

    class BrokenFormatter(logging.Formatter):
        def format(self, record):
            raise ValueError("Formatter failed: " + json.dumps({"value": secret}))

    monkeypatch.setattr(logging, "raiseExceptions", True)
    monkeypatch.setattr(sys, "stderr", stream)
    handler.setFormatter(BrokenFormatter())
    scrub.install_sanitizer(handler)
    record = logging.LogRecord("synthetic", logging.WARNING, __file__, 1,
                               "Failure: %r; retry later", (secret,), None)
    before = record.__dict__.copy()
    handler.handle(record)
    output = stream.getvalue()
    assert secret not in output
    assert json.dumps(secret)[1:-1] not in output
    assert "ValueError: Formatter failed:" in output
    assert "Message: Failure:" in output and "; retry later" in output
    assert "[REDACTED]" in output
    assert record.__dict__ == before


@pytest.mark.parametrize("protocol", ["udp", "tcp"])
def test_registered_escaped_syslog(monkeypatch, protocol):
    import socket

    def local_socket(handler):
        handler.unixsocket = False
        handler.socket = MagicMock()

    monkeypatch.setattr(logging.handlers.SysLogHandler, "createSocket", local_socket)
    secret = ESCAPED_SECRETS[0]
    scrub.register_secret(secret)
    handler = logging.handlers.SysLogHandler(
        address=("192.0.2.1", 514),
        socktype=socket.SOCK_DGRAM if protocol == "udp" else socket.SOCK_STREAM,
    )
    scrub.install_sanitizer(handler)
    try:
        record = logging.LogRecord("synthetic", logging.WARNING, __file__, 1,
                                   "Failure: %r; retry later", (secret,), None)
        before = record.__dict__.copy()
        handler.handle(record)
        sender = handler.socket.sendto if protocol == "udp" else handler.socket.sendall
        output = sender.call_args.args[0].decode()
        assert repr(secret)[1:-1] not in output
        assert "[REDACTED]" in output and "; retry later" in output
        assert record.__dict__ == before
    finally:
        handler.close()


def test_registered_escaped_long_output_has_no_registry_growth(application_output):
    import json

    app, stream, _ = application_output
    secret = ESCAPED_SECRETS[1]
    scrub.register_secret(secret)
    pattern = scrub._pattern
    # 1.1 million unmatched characters precede 100 known escaped values.
    context = "diagnostic-" * 100000
    values = " | ".join(json.dumps(secret) for _ in range(100))
    app.logger.warning("%s %s END", context, values)
    output = stream.getvalue()
    assert context in output and output.endswith(" END\n")
    assert json.dumps(secret)[1:-1] not in output
    assert output.count("[REDACTED]") == 100
    assert scrub._secrets == {secret}
    assert scrub._pattern is pattern
