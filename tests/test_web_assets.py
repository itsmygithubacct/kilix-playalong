from __future__ import annotations

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

WEB_ROOT = Path(__file__).parents[1] / "src" / "kilix_playalong" / "web"


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.external_assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        candidate = values.get("src") or values.get("href")
        if (
            tag in {"script", "link", "img", "audio"}
            and candidate
            and candidate.startswith(("http://", "https://", "//"))
        ):
            self.external_assets.append(candidate)


def test_web_surface_has_no_external_assets_or_unsafe_html_sinks() -> None:
    html = (WEB_ROOT / "index.html").read_text()
    script = (WEB_ROOT / "app.js").read_text()
    parser = _DocumentParser()
    parser.feed(html)
    assert parser.external_assets == []
    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function("):
        assert unsafe not in script
    assert "api/project" in script
    assert "localStorage" in script
    assert "requestAnimationFrame" in script


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_browser_script_parses_in_node() -> None:
    result = subprocess.run(
        ["node", "--check", str(WEB_ROOT / "app.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
