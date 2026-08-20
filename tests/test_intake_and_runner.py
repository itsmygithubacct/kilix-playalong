from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kilix_playalong.errors import InvalidInputError, ProviderFailedError
from kilix_playalong.providers import youtube
from kilix_playalong.runner import run_command


@pytest.mark.parametrize(
    "url",
    [
        "http://youtube.com/watch?v=abcdef",
        "https://example.com/watch?v=abcdef",
        "https://user:secret@youtube.com/watch?v=abcdef",
        "https://youtube.com:444/watch?v=abcdef",
        "javascript:alert(1)",
    ],
)
def test_youtube_url_gate_rejects_unsafe_sources(url: str) -> None:
    with pytest.raises(InvalidInputError):
        youtube.validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abcdef",
        "https://youtu.be/abcdef",
        "https://music.youtube.com/watch?v=abcdef",
    ],
)
def test_youtube_url_gate_accepts_supported_https_hosts(url: str) -> None:
    assert youtube.validate_url(url) == url


def test_inspect_uses_locked_module_and_rejects_live(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def fake_run(arguments: list[str], **_kwargs: object) -> object:
        captured.extend(arguments)
        return type(
            "Result",
            (),
            {
                "stdout": json.dumps({"id": "abcdef", "duration": 60, "is_live": True}),
                "stderr": "",
            },
        )()

    monkeypatch.setattr(youtube, "run_command", fake_run)
    with pytest.raises(InvalidInputError, match="live"):
        youtube.inspect("https://youtu.be/abcdef")
    assert captured[:3] == [sys.executable, "-m", "yt_dlp"]
    assert "--ignore-config" in captured
    assert "--no-playlist" in captured


def test_runner_captures_output_and_redacts_failures(tmp_path: Path) -> None:
    result = run_command(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        timeout=5,
    )
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"

    secret = "https://youtube.com/watch?v=private123"
    with pytest.raises(ProviderFailedError) as raised:
        run_command(
            [sys.executable, "-c", f"import sys; print({secret!r}, file=sys.stderr); sys.exit(3)"],
            timeout=5,
            redact=(secret,),
            log_path=tmp_path / "provider.log",
        )
    assert "private123" not in str(raised.value)
    assert "private123" not in (tmp_path / "provider.log").read_text()


def test_runner_enforces_diagnostic_bound() -> None:
    with pytest.raises(ProviderFailedError, match="more diagnostic output"):
        run_command(
            [sys.executable, "-c", "print('x' * 128)"],
            timeout=5,
            max_output=32,
        )
