"""Check that shipped Manager JavaScript parses. Skips when node is missing."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = sorted((ROOT / "updater/templates").glob("*.html"))
JS_FILES = sorted((ROOT / "static/js").glob("*.js"))
INLINE_SCRIPT = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S | re.I)

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    NODE is None, reason="node is not installed; JavaScript syntax checks need `node --check`"
)


def _check(path: Path):
    result = subprocess.run(
        [NODE, "--check", str(path)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"{path.name}: {result.stderr.strip()}"


def _scripts(html: str):
    """Yield the body of each inline JavaScript block in `html`."""
    for match in INLINE_SCRIPT.finditer(html):
        attrs = match.group("attrs")
        body = match.group("body").strip()
        if "src=" in attrs or not body:
            continue
        if "type=" in attrs and "javascript" not in attrs and "module" not in attrs:
            continue
        yield body


def _write_and_check(tmp_path, name, bodies):
    for index, body in enumerate(bodies):
        script = tmp_path / f"{name}.{index}.js"
        script.write_text(body, encoding="utf-8")
        _check(script)


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_static_js_parses(path):
    _check(path)


def test_monitor_inline_scripts_parse(authed_client, tmp_path):
    """Check the rendered page, because the template mixes Jinja into its script."""
    bodies = list(_scripts(authed_client.get("/").text))
    assert bodies, "monitor page has no inline script"
    _write_and_check(tmp_path, "monitor", bodies)


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_plain_template_scripts_parse(template, tmp_path):
    """Check templates with no Jinja syntax inside their scripts."""
    bodies = [b for b in _scripts(template.read_text(encoding="utf-8")) if "{%" not in b and "{{" not in b]
    _write_and_check(tmp_path, template.name, bodies)
