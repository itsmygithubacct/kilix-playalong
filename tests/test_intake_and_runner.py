from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

import pytest

from kilix_playalong.errors import InvalidInputError, PlayalongError, ProviderFailedError
from kilix_playalong.providers import youtube
from kilix_playalong.runner import run_command


@pytest.mark.parametrize(
    "url",
    [
        "http://youtube.com/watch?v=abcdef",
        "https://example.com/watch?v=abcdef",
        "https://user:secret@youtube.com/watch?v=abcdef",
        "https://youtube.com:444/watch?v=abcdef",
        "https://youtube.com:99999/watch?v=abcdef",
        "https://youtube.com:abc/watch?v=abcdef",
        "https://[youtube.com/watch?v=abcdef",
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


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=PL0123456789",
        "https://music.youtube.com/playlist?list=PL0123456789",
        "https://www.youtube.com/watch?list=PL0123456789",
    ],
)
def test_youtube_url_gate_rejects_playlists(url: str) -> None:
    with pytest.raises(InvalidInputError, match="playlist"):
        youtube.validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abcdef&list=PL0123456789",
        "https://youtu.be/abcdef?list=PL0123456789",
    ],
)
def test_youtube_url_gate_accepts_a_single_video_inside_a_playlist(url: str) -> None:
    assert youtube.validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com//playlist?list=PL0123456789",
        "https://www.youtube.com/%70laylist?list=PL0123456789",
        "https://www.youtube.com//watch?list=PL0123456789",
    ],
)
def test_youtube_url_gate_rejects_obfuscated_playlist_paths(url: str) -> None:
    """P6: the pre-network gate must not be bypassable by a doubled slash or an escape."""
    with pytest.raises(InvalidInputError, match="playlist"):
        youtube.validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/embed/videoseries?list=PL0123456789",
        "https://www.youtube.com/?list=PL0123456789",
        "https://www.youtube.com/playlist/extra?list=PL0123456789",
        "https://www.youtube.com/watch/x?list=PL0123456789",
        "https://www.youtube.com/foo?list=PL0123456789",
        "https://music.youtube.com/x?list=PL0123456789",
        "https://m.youtube.com/?list=PL0123456789",
    ],
)
def test_youtube_url_gate_rejects_playlists_on_any_path(url: str) -> None:
    """N1: yt-dlp's playlist rule ignores the path; round 2's gate did not, and let these
    seven spellings through to the network. The locked yt-dlp claims every one of them with
    youtube:playlist or youtube:tab -- the embed form is the common real-world one."""
    with pytest.raises(InvalidInputError, match="playlist"):
        youtube.validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://youtu.be/abc\x00def",
        "https://youtu.be/abc\ndef",
        "https://youtu.be/ab cdef",
        "https://youtu.be/abc\x7fdef",
    ],
)
def test_youtube_url_gate_rejects_control_characters(url: str) -> None:
    """A NUL reaches ``run_command`` as a bare ValueError, which is not a PlayalongError."""
    with pytest.raises(InvalidInputError):
        youtube.validate_url(url)


@pytest.mark.parametrize(
    "value",
    ["not-a-size", "512MB", "0", "0.4", " 512M", "1e3", "-1", "", "9" * 400 + "g", "9" * 400],
)
def test_parse_filesize_rejects_unusable_values_as_playalong_errors(value: str) -> None:
    """P7: this function may reject only with a PlayalongError -- never the regex's bare
    ValueError and never OverflowError out of ``round(inf)``. One value per rejecting
    branch; the property over arbitrary input is an argument, not this test."""
    with pytest.raises(InvalidInputError, match="max_filesize"):
        youtube._parse_filesize(value)


def test_youtube_entry_points_raise_only_playalong_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B1: every caller-supplied *value*, through every public keyword of all three entry
    points, is rejected as a PlayalongError -- which is what cli.py's ``except
    PlayalongError`` catches and a bare ValueError is not.

    Round 2 claimed this property while driving hostile urls and max_filesize values only,
    so ``inspect(url, timeout=0)`` and ``download(url, dest, language="e\x00n")`` still came
    back as run_command's own ValueError. Every keyword is driven here instead, and
    ``destination`` is the documented exception -- see the error contract in
    providers/youtube.py for why an OSError from the filesystem stays an OSError.

    run_command is replaced by a tripwire rather than stubbed: a rejection that happened
    after the provider was launched would fail this test, so it also pins that none of
    these arguments costs a network call.
    """

    def tripwire(*_arguments: object, **_kwargs: object) -> object:
        raise AssertionError("a rejected argument reached run_command")

    monkeypatch.setattr(youtube, "run_command", tripwire)
    valid = "https://youtu.be/abcdef12345"
    destination = tmp_path / "source"
    hostile_urls = [
        "https://youtu.be/abc\x00def",
        "https://youtu.be/abc\ndef",
        "https://youtube.com:99999/watch?v=abcdef",
        "https://[youtube.com/watch?v=abcdef",
        "https://www.youtube.com//playlist?list=PL0123456789",
        "https://www.youtube.com/embed/videoseries?list=PL0123456789",
        "x" * (youtube.MAX_URL_LENGTH + 1),
    ]
    for url in hostile_urls:
        with pytest.raises(PlayalongError):
            youtube.validate_url(url)
        with pytest.raises(PlayalongError):
            youtube.inspect(url)
        with pytest.raises(PlayalongError):
            youtube.download(url, destination)
    for timeout in [0, -1, float("nan"), float("inf"), 10**400]:
        with pytest.raises(PlayalongError):
            youtube.inspect(valid, timeout=timeout)
        with pytest.raises(PlayalongError):
            youtube.download(valid, destination, timeout=timeout)
    for language in ["e\x00n", "e n", "", "e\nn", "x" * (youtube.MAX_LANGUAGE_LENGTH + 1)]:
        with pytest.raises(PlayalongError):
            youtube.download(valid, destination, language=language)
    for max_duration in [0, -1, float("nan"), float("inf"), 10**400]:
        with pytest.raises(PlayalongError):
            youtube.download(valid, destination, max_duration=max_duration)
    for size in ["not-a-size", "0", "9" * 400 + "g", "512\u212a"]:
        with pytest.raises(PlayalongError):
            youtube.download(valid, destination, max_filesize=size)


@pytest.mark.parametrize("value", ["512M", "1K", "8k", "1.5m", "512", "2G", "1t"])
def test_parse_filesize_agrees_with_the_locked_yt_dlp_parser(value: str) -> None:
    """Whatever we accept must mean to us exactly what it means to yt-dlp's own --max-filesize."""
    from yt_dlp.utils import parse_bytes

    assert youtube._parse_filesize(value) == parse_bytes(value)


def test_parse_filesize_agrees_across_the_whole_accepted_grammar() -> None:
    """The docstring claims agreement on everything we accept, not on seven values.

    Every number/unit combination the regex admits is driven through both parsers; a value
    we reject is a narrowing and allowed, a value we accept and read differently is not.
    """
    from yt_dlp.utils import parse_bytes

    numbers = ["0.5", "1", "1.5", "2.5", "512", "1023", "1023.5", "1.05", "3.14159", "999999"]
    accepted = 0
    for number in numbers:
        for unit in ["", "k", "K", "m", "M", "g", "G", "t", "T"]:
            value = number + unit
            try:
                parsed = youtube._parse_filesize(value)
            except InvalidInputError:
                continue
            accepted += 1
            assert parsed == parse_bytes(value), value
    assert accepted >= 80


@pytest.mark.parametrize(
    ("value", "yt_dlp_reading"),
    [("512\u212a", None), ("512\u212am", None), ("\u0665\u0661\u0662", 512)],
)
def test_parse_filesize_rejects_the_non_ascii_spellings(
    value: str, yt_dlp_reading: int | None
) -> None:
    """``str.lower`` folds U+212A KELVIN SIGN onto ``k``, so ``512`` + U+212A used to parse
    here as 524288 while yt-dlp rejects that spelling outright and exits 2. Unicode digits
    are the harmless half of the same hole: both parsers read them the same way, and they
    are rejected because the docstring's "strict subset" is easier to hold than to qualify.
    """
    from yt_dlp.utils import parse_bytes

    assert parse_bytes(value) == yt_dlp_reading
    with pytest.raises(InvalidInputError, match="max_filesize"):
        youtube._parse_filesize(value)


def _stub_result(stdout: str) -> object:
    return type("Result", (), {"stdout": stdout, "stderr": ""})()


def test_inspect_uses_locked_module_and_rejects_live(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def fake_run(arguments: list[str], **_kwargs: object) -> object:
        captured.extend(arguments)
        return _stub_result(json.dumps({"id": "abcdef", "duration": 60, "is_live": True}))

    monkeypatch.setattr(youtube, "run_command", fake_run)
    with pytest.raises(InvalidInputError, match="live"):
        youtube.inspect("https://youtu.be/abcdef")
    assert captured[:3] == [sys.executable, "-m", "yt_dlp"]
    assert "--ignore-config" in captured
    assert "--no-playlist" in captured


def test_inspect_rejects_playlist_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"_type": "playlist", "id": "PL0123456789", "entries": []})

    def fake_run(_arguments: list[str], **_kwargs: object) -> object:
        return _stub_result(payload)

    monkeypatch.setattr(youtube, "run_command", fake_run)
    with pytest.raises(InvalidInputError, match="playlist"):
        youtube.inspect("https://youtu.be/abcdef")


def test_download_enforces_the_configured_media_size_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "source"

    def fake_run(arguments: list[str], **_kwargs: object) -> object:
        if "--dump-single-json" in arguments:
            return _stub_result(json.dumps({"id": "abcdef", "duration": 60}))
        (destination / "source.webm").write_bytes(b"x" * 4096)
        return _stub_result("")

    monkeypatch.setattr(youtube, "run_command", fake_run)
    url = "https://youtu.be/abcdef"
    with pytest.raises(InvalidInputError, match="size limit"):
        youtube.download(url, destination, max_filesize="1K")
    media, subtitles, metadata = youtube.download(url, destination, max_filesize="8K")
    assert media.name == "source.webm"
    assert subtitles == []
    assert metadata["id"] == "abcdef"
    with pytest.raises(InvalidInputError, match="max_filesize"):
        youtube.download(url, destination, max_filesize="not-a-size")


def _metadata_or(action: object) -> object:
    """Build a fake run_command: metadata first, then whatever the download call should do."""

    def fake_run(arguments: list[str], **_kwargs: object) -> object:
        if "--dump-single-json" in arguments:
            return _stub_result(json.dumps({"id": "abcdef", "duration": 60}))
        assert callable(action)
        return action()

    return fake_run


def test_download_reports_the_size_ceiling_when_yt_dlp_writes_no_media(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P4: --max-filesize aborts the transfer silently and exits 0; say so in the users' words."""
    destination = tmp_path / "source"

    def aborted_download() -> object:
        (destination / "source.en.vtt").write_bytes(b"WEBVTT\n")
        return _stub_result("")

    monkeypatch.setattr(youtube, "run_command", _metadata_or(aborted_download))
    with pytest.raises(InvalidInputError, match="size limit"):
        youtube.download("https://youtu.be/abcdef", destination, max_filesize="1K")
    assert sorted(destination.glob("source*")) == []


def test_download_removes_its_files_when_it_rejects_or_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F25 adjacent: a rejected or failed acquisition leaves none of what it wrote behind.

    The neighbour in that directory is the user's own lyrics file, which pipeline.py copies
    to source/lyrics-input.*; the cleanup glob is anchored to yt-dlp's --output template
    and must never reach it.
    """
    destination = tmp_path / "source"
    destination.mkdir(mode=0o700, parents=True)
    neighbour = destination / "lyrics-input.txt"
    neighbour.write_bytes(b"the user's own lyrics\n")

    def oversized_download() -> object:
        (destination / "source.webm").write_bytes(b"x" * 4096)
        (destination / "source.en.vtt").write_bytes(b"WEBVTT\n")
        return _stub_result("")

    monkeypatch.setattr(youtube, "run_command", _metadata_or(oversized_download))
    with pytest.raises(InvalidInputError, match="size limit"):
        youtube.download("https://youtu.be/abcdef", destination, max_filesize="1K")
    assert sorted(destination.glob("source*")) == []

    def failed_download() -> object:
        (destination / "source.webm.part").write_bytes(b"x" * 4096)
        raise ProviderFailedError("provider failed")

    monkeypatch.setattr(youtube, "run_command", _metadata_or(failed_download))
    with pytest.raises(ProviderFailedError):
        youtube.download("https://youtu.be/abcdef", destination)
    assert sorted(destination.glob("source*")) == []
    assert neighbour.read_bytes() == b"the user's own lyrics\n"


def test_download_keeps_calling_a_real_provider_failure_a_provider_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The size message must not swallow a genuine yt-dlp fault: two media files is not a size."""
    destination = tmp_path / "source"

    def ambiguous_download() -> object:
        (destination / "source.webm").write_bytes(b"x")
        (destination / "source.m4a").write_bytes(b"x")
        return _stub_result("")

    monkeypatch.setattr(youtube, "run_command", _metadata_or(ambiguous_download))
    with pytest.raises(ProviderFailedError, match="media file"):
        youtube.download("https://youtu.be/abcdef", destination)


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
            max_output_per_stream=32,
        )


def test_runner_bounds_each_stream_separately_and_captures_both() -> None:
    script = (
        "import sys; "
        "sys.stdout.write('o' * 900); sys.stdout.flush(); "
        "sys.stderr.write('e' * 900); sys.stderr.flush()"
    )
    result = run_command([sys.executable, "-c", script], timeout=5, max_output_per_stream=1000)
    assert result.stdout == "o" * 900
    assert result.stderr == "e" * 900
    with pytest.raises(ProviderFailedError, match="more diagnostic output"):
        run_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('e' * 4096)"],
            timeout=5,
            max_output_per_stream=32,
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


@pytest.mark.parametrize(
    "timeout",
    [0, -1, float("nan"), float("inf"), pytest.param(10**400, id="int-past-float-range")],
)
def test_runner_rejects_a_timeout_it_cannot_honour(timeout: float) -> None:
    """The bound is a precondition, so it has to reject what it cannot enforce.

    ``timeout <= 0`` is False for a NaN, so NaN and infinity used to pass this check and
    surface out of ``selectors`` as ValueError("cannot convert float NaN to integer") and
    OverflowError -- past a check whose message says the timeout must be positive. An int
    is a legal float argument, and one past the float ceiling made the check itself raise
    OverflowError out of ``math.isfinite``.
    """
    with pytest.raises(ValueError, match="provider timeout"):
        run_command([sys.executable, "-c", "pass"], timeout=timeout)


def test_runner_honours_a_timeout_longer_than_one_select_wait() -> None:
    """The other half: a finite timeout must be honoured however large it is.

    epoll's timeout is an int of milliseconds, so a wait longer than 2147483 seconds
    (INT_MAX ms) raised OverflowError out of the read loop instead of running the provider.
    Each wait is capped now, and the deadline -- not the cap -- ends the call.
    """
    result = run_command([sys.executable, "-c", "print('out')"], timeout=1e15)
    assert result.stdout.strip() == "out"


def test_runner_terminates_timed_out_provider() -> None:
    started = time.monotonic()
    with pytest.raises(ProviderFailedError, match="timed out"):
        run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.05,
        )
    assert time.monotonic() - started < 10


_INTERRUPTED_PARENT = """
import resource
import sys

from kilix_playalong.runner import run_command

# SIGQUIT is one of the signals driven at this process and dumps core by default.
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
pid_file, child = sys.argv[1], sys.argv[2]
run_command([sys.executable, "-c", child, pid_file], timeout=300)
"""

_REPORTING_CHILD = (
    "import os, pathlib, sys, time; "
    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()) + chr(10)); "
    "time.sleep(300)"
)

_SIGTERM_IGNORING_CHILD = (
    "import os, pathlib, signal, sys, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()) + chr(10)); "
    "time.sleep(300)"
)


def _process_is_running(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    return status.rpartition(b")")[2].split()[0] != b"Z"


def _wait_for_provider_pid(pid_file: Path, *, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reported = pid_file.read_text() if pid_file.is_file() else ""
        if reported.endswith("\n"):
            return int(reported)
        time.sleep(0.02)
    raise AssertionError("the provider never reported its pid")


def _assert_provider_dies_with_its_parent(
    tmp_path: Path, *, child: str, signals: Sequence[signal.Signals], message: str
) -> None:
    """Run ``run_command`` in a real process, signal that process, watch the provider's pid.

    The first signal is delivered once the provider has reported its pid; every later one
    half a second after the one before, which lands it inside the teardown's three-second
    SIGTERM wait whenever the child ignores SIGTERM. Liveness is read from /proc, so a
    zombie the parent has not reaped does not count as running.
    """
    pid_file = tmp_path / "provider.pid"
    parent = subprocess.Popen(
        [sys.executable, "-c", _INTERRUPTED_PARENT, str(pid_file), child],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    provider_pid = 0
    try:
        provider_pid = _wait_for_provider_pid(pid_file, timeout=30)
        for index, number in enumerate(signals):
            if index:
                time.sleep(0.5)
            parent.send_signal(number)
        assert parent.wait(timeout=30) != 0
        deadline = time.monotonic() + 10
        while _process_is_running(provider_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _process_is_running(provider_pid), message
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        if provider_pid and _process_is_running(provider_pid):
            with suppress(ProcessLookupError):
                os.kill(provider_pid, signal.SIGKILL)


def test_runner_terminates_provider_when_the_parent_is_interrupted(tmp_path: Path) -> None:
    _assert_provider_dies_with_its_parent(
        tmp_path,
        child=_REPORTING_CHILD,
        signals=[signal.SIGINT],
        message="the provider outlived its interrupted parent",
    )


def test_runner_terminates_the_provider_on_a_second_interrupt(tmp_path: Path) -> None:
    """P2: a second Ctrl-C landing inside the teardown's SIGTERM wait must not skip SIGKILL."""
    _assert_provider_dies_with_its_parent(
        tmp_path,
        child=_SIGTERM_IGNORING_CHILD,
        signals=[signal.SIGINT, signal.SIGINT],
        message="a second interrupt let the provider outlive its parent",
    )


@pytest.mark.parametrize("interrupting", [signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT])
def test_runner_finishes_its_teardown_when_another_signal_lands_inside_it(
    interrupting: signal.Signals, tmp_path: Path
) -> None:
    """N3: ``_TEARDOWN_SIGNALS`` has four members and only SIGINT was pinned by a test.

    The parent is interrupted, which starts a teardown; half a second later -- while that
    teardown is inside its three-second SIGTERM wait, because this child ignores SIGTERM --
    the signal under test arrives. Blocked, it is deferred until the mask comes down, so the
    SIGKILL escalation still runs and the provider still dies. Remove that signal from
    ``_TEARDOWN_SIGNALS`` and the parent dies on delivery instead, leaving the provider up:
    one case per member, so deleting any of the three fails a test rather than nothing.
    """
    _assert_provider_dies_with_its_parent(
        tmp_path,
        child=_SIGTERM_IGNORING_CHILD,
        signals=[signal.SIGINT, interrupting],
        message=f"{interrupting.name} inside the teardown let the provider outlive its parent",
    )


def test_runner_signals_the_provider_group_exactly_once_per_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: teardown runs from both _bounded_communicate and run_command; it must not repeat.

    A repeat pass signals a pid that ``process.wait()`` has already reaped, which the kernel
    is free to hand to an unrelated process-group leader. Both abnormal exits that tear down
    from inside the read loop are driven, not just the timeout one round 2 pinned, and so is
    the ordinary non-zero exit: that one must not signal the group at all.
    """
    real_killpg = os.killpg
    calls: list[tuple[int, int]] = []

    def spy(pgid: int, signal_number: int) -> None:
        calls.append((pgid, signal_number))
        real_killpg(pgid, signal_number)

    def escalation(source: str, *, timeout: float, max_output: int = 4096) -> list[int]:
        calls.clear()
        with pytest.raises(ProviderFailedError):
            run_command(
                [sys.executable, "-c", source], timeout=timeout, max_output_per_stream=max_output
            )
        assert len({pgid for pgid, _signal_number in calls}) <= 1, f"two groups: {calls}"
        return [signal_number for _pgid, signal_number in calls]

    monkeypatch.setattr(os, "killpg", spy)
    escalated = [signal.SIGTERM, signal.SIGKILL]
    noisy = "import sys, time; sys.stdout.write('x' * 4096); sys.stdout.flush(); time.sleep(30)"
    assert escalation("import time; time.sleep(30)", timeout=0.05) == escalated, "timeout path"
    assert escalation(noisy, timeout=30, max_output=32) == escalated, "output-bound path"
    assert escalation("raise SystemExit(3)", timeout=30) == [], (
        "a provider that exited on its own must not be signalled at all"
    )


def test_runner_teardown_hands_the_caller_signal_mask_back_untouched() -> None:
    """Guard for the P2 mechanism, not a before/after test: the teardown blocks SIGINT,
    SIGTERM, SIGHUP and SIGQUIT while it runs, so it must restore exactly what the caller
    had -- including a signal the caller had deliberately blocked itself.
    """
    caller_blocked = {signal.SIGUSR1, signal.SIGTERM}
    outer = signal.pthread_sigmask(signal.SIG_BLOCK, caller_blocked)
    try:
        with pytest.raises(ProviderFailedError, match="timed out"):
            run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.05)
        assert signal.pthread_sigmask(signal.SIG_BLOCK, frozenset()) == outer | caller_blocked
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, outer)


def test_runner_terminates_the_child_when_popen_construction_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P3: CPython reads the exec errpipe after the fork, and PEP 475 runs handlers there.

    Raising out of ``_execute_child`` after the real fork reproduces that window without
    depending on signal timing: the child exists, but ``Popen(...)`` never returns.
    """
    original = subprocess.Popen._execute_child
    captured: dict[str, int] = {}

    def failing_execute_child(
        self: subprocess.Popen[bytes], *args: object, **kwargs: object
    ) -> None:
        original(self, *args, **kwargs)
        captured["pid"] = self.pid
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, "_execute_child", failing_execute_child)
    with pytest.raises(KeyboardInterrupt):
        run_command([sys.executable, "-c", "import time; time.sleep(300)"], timeout=30)
    monkeypatch.undo()
    provider_pid = captured["pid"]
    try:
        deadline = time.monotonic() + 10
        while _process_is_running(provider_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _process_is_running(provider_pid), (
            "an interrupt inside Popen.__init__ orphaned the provider"
        )
    finally:
        if _process_is_running(provider_pid):
            with suppress(ProcessLookupError):
                os.kill(provider_pid, signal.SIGKILL)
