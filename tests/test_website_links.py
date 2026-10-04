"""Check static website footer targets without network access."""

from html.parser import HTMLParser
from pathlib import Path
import subprocess
from urllib.parse import unquote, urlsplit


class _PageLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.footer_depth = 0
        self.footer_links: list[tuple[dict[str, str | None], str]] = []
        self.anchors: set[str] = set()
        self._label: list[str] | None = None
        self._attrs: dict[str, str | None] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.anchors.add(values["id"])
        if tag == "a" and values.get("name"):
            self.anchors.add(values["name"])
        if tag == "footer":
            self.footer_depth += 1
        if tag == "a" and self.footer_depth:
            self._attrs = values
            self._label = []

    def handle_data(self, data: str) -> None:
        if self._label is not None:
            self._label.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._label is not None:
            self.footer_links.append((self._attrs, "".join(self._label).strip()))
            self._label = None
        if tag == "footer" and self.footer_depth:
            self.footer_depth -= 1


def _local_footer_link_errors(
    document: Path,
    website_root: Path,
    tracked_files: set[Path] | None = None,
) -> list[str]:
    """Return missing local footer files or fragment targets."""
    page = _PageLinks()
    page.feed(document.read_text(encoding="utf-8"))
    root = website_root.resolve()
    errors = []

    for attrs, _label in page.footer_links:
        href = attrs.get("href", "")
        url = urlsplit(href)
        if url.scheme or url.netloc:
            continue

        target_path = unquote(url.path)
        if target_path.startswith("/"):
            target = root / target_path.lstrip("/")
        elif target_path:
            target = document.parent / target_path
        else:
            target = document
        target = target.resolve()
        try:
            relative_target = target.relative_to(root)
        except ValueError:
            errors.append(f"{href}: target is outside the website")
            continue

        if not target.is_file():
            errors.append(f"{href}: target file does not exist")
            continue
        if tracked_files is not None and relative_target not in tracked_files:
            errors.append(f"{href}: target file is not tracked")
            continue

        fragment = unquote(url.fragment)
        if fragment:
            target_page = _PageLinks()
            target_page.feed(target.read_text(encoding="utf-8"))
            if fragment not in target_page.anchors:
                errors.append(f"{href}: fragment does not exist")

    return errors


def test_policy_footers_use_existing_tracked_pages():
    repo_root = Path(__file__).resolve().parents[1]
    website_root = repo_root / "website"
    tracked_files = {
        Path(path.removeprefix("website/"))
        for path in subprocess.run(
            ["git", "ls-files", "--", "website"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    }
    expected = {
        "Home": "index.html",
        "Terms": "terms.html",
        "Privacy": "privacy.html",
    }

    for filename in ("privacy.html", "terms.html"):
        document = website_root / filename
        page = _PageLinks()
        page.feed(document.read_text(encoding="utf-8"))
        links = {label: attrs.get("href") for attrs, label in page.footer_links}

        assert _local_footer_link_errors(document, website_root, tracked_files) == []
        assert all(links.get(label) == href for label, href in expected.items())
        assert "Billing" not in links


def test_missing_relative_footer_target_is_rejected(tmp_path):
    document = tmp_path / "privacy.html"
    document.write_text(
        '<footer><a href="billing.html">Billing</a></footer>', encoding="utf-8"
    )

    assert _local_footer_link_errors(document, tmp_path, {Path("privacy.html")}) == [
        "billing.html: target file does not exist"
    ]


def test_missing_footer_fragment_is_rejected(tmp_path):
    document = tmp_path / "privacy.html"
    document.write_text(
        '<footer><a href="index.html#known">Home</a>'
        '<a href="index.html#missing">Missing section</a></footer>',
        encoding="utf-8",
    )
    (tmp_path / "index.html").write_text(
        '<h1 id="known">Home</h1>', encoding="utf-8"
    )

    assert _local_footer_link_errors(
        document, tmp_path, {Path("privacy.html"), Path("index.html")}
    ) == ["index.html#missing: fragment does not exist"]
