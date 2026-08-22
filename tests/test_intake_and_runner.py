from __future__ import annotations

import json
import os
import sys
import time
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


def test_runner_uses_private_allowlisted_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KILIX_PLAYALONG_SENTINEL_TOKEN", "do-not-inherit")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@proxy.invalid")
    monkeypatch.setenv("PYTHONPATH", "/tmp/untrusted-python")
    parent_home = os.environ.get("HOME")
    script = (
        "import json, os; "
        "print(json.dumps({"
        "'token': os.environ.get('KILIX_PLAYALONG_SENTINEL_TOKEN'), "
        "'proxy': os.environ.get('HTTPS_PROXY'), "
        "'pythonpath': os.environ.get('PYTHONPATH'), "
        "'explicit': os.environ.get('KILIX_EXPLICIT_TEST'), "
        "'home': os.environ.get('HOME')}))"
    )
    result = run_command(
        [sys.executable, "-c", script],
        timeout=5,
        env={"KILIX_EXPLICIT_TEST": "kept"},
    )
    child = json.loads(result.stdout)
    assert child["token"] is None
    assert child["proxy"] is None
    assert child["pythonpath"] is None
    assert child["explicit"] == "kept"
    assert child["home"] != parent_home


@pytest.mark.parametrize(
    "environment",
    [
        {"BAD=NAME": "x"},
        {"VÄR": "x"},
        {"GOOD_NAME": "bad\x00value"},
        {"HOME": "/tmp/not-the-provider-home"},
    ],
)
def test_runner_rejects_invalid_explicit_environment(environment: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="provider environment"):
        run_command([sys.executable, "-c", "pass"], timeout=5, env=environment)


def test_runner_terminates_timed_out_provider() -> None:
    started = time.monotonic()
    with pytest.raises(ProviderFailedError, match="timed out"):
        run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.05,
        )
    assert time.monotonic() - started < 10
