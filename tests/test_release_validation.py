"""Offline checks for release host version validation."""

from scripts import check_release_instance


def _configure(monkeypatch):
    monkeypatch.setenv("SIXTYOPS_TEST_URL", "https://manager.test")
    monkeypatch.setenv("SIXTYOPS_TEST_USER", "release-user")
    monkeypatch.setenv("SIXTYOPS_TEST_PASS", "release-password")
    monkeypatch.setenv("RELEASE_TAG", "v1.2.3")
    monkeypatch.setenv("RELEASE_COMMIT", "a" * 40)


def test_release_validation_fails_when_required_secrets_are_missing(monkeypatch, capsys):
    monkeypatch.delenv("SIXTYOPS_TEST_URL", raising=False)
    monkeypatch.setenv("SIXTYOPS_TEST_USER", "release-user")
    monkeypatch.setenv("SIXTYOPS_TEST_PASS", "release-password")
    monkeypatch.setenv("RELEASE_TAG", "v1.2.3")
    monkeypatch.setenv("RELEASE_COMMIT", "a" * 40)

    class NoHttp:
        def __init__(self):
            raise AssertionError("HTTP must not run without required secrets")

    monkeypatch.setattr(check_release_instance.httpx, "Client", NoHttp)

    assert check_release_instance.check_release_instance() is False
    assert "SIXTYOPS_TEST_URL" in capsys.readouterr().err


def test_release_validation_fails_when_version_does_not_match(monkeypatch, capsys):
    _configure(monkeypatch)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"version": "1.2.2"}

    class Session:
        def __init__(self, **kwargs):
            pass

        def post(self, *args, **kwargs):
            return Response()

        def get(self, *args, **kwargs):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(check_release_instance.httpx, "Client", Session)

    assert check_release_instance.check_release_instance() is False
    assert "version does not match" in capsys.readouterr().err


def test_release_validation_checks_commit_when_manager_exposes_it(monkeypatch, capsys):
    _configure(monkeypatch)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"version": "1.2.3", "commit": "b" * 40}

    class Session:
        def __init__(self, **kwargs):
            pass

        def post(self, *args, **kwargs):
            return Response()

        def get(self, *args, **kwargs):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(check_release_instance.httpx, "Client", Session)

    assert check_release_instance.check_release_instance() is False
    assert "commit does not match" in capsys.readouterr().err


def test_release_validation_accepts_version_only_when_manager_has_no_commit(monkeypatch, capsys):
    _configure(monkeypatch)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"version": "1.2.3"}

    class Session:
        def __init__(self, **kwargs):
            pass

        def post(self, *args, **kwargs):
            return Response()

        def get(self, *args, **kwargs):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(check_release_instance.httpx, "Client", Session)

    assert check_release_instance.check_release_instance() is True
    assert "exposes no commit" in capsys.readouterr().out
