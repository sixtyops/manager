from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "updater/templates/monitor.html"


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
    return sum(c * w for c, w in zip(linear, (0.2126, 0.7152, 0.0722)))


def _contrast(foreground: str, background: str) -> float:
    a, b = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def test_cvd_palette_token_pairs_meet_text_contrast():
    html = TEMPLATE.read_text()
    tokens = dict(re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})", html.split("</style>", 1)[0]))
    pairs = {
        "neutral": ("state-neutral-text", "state-neutral-bg"),
        "info": ("info-text", "info-bg"),
        "caution": ("warning-text", "warning-bg"),
        "critical": ("danger-fill-text", "danger-fill"),
        "selected": ("primary-fill-text", "primary-fill"),
    }
    for name, (foreground, background) in pairs.items():
        assert _contrast(tokens[foreground], tokens[background]) >= 4.5, name


def test_template_has_no_inline_low_contrast_status_colors():
    html = TEMPLATE.read_text()
    assert "style.color = '#f59e0b'" not in html.lower()
    assert "style=\"color:#16a34a;\"" not in html.lower()
    assert "color:#16a34a;" not in html.lower()
