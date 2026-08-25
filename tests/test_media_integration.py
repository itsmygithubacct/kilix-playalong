"""ffmpeg-facing behaviour: probing, normalizing, and copying media in.

Everything here runs against media generated at test time by ffmpeg -- the
container matrix comes from ``test_source``, which owns the generators -- so no
real music enters the repository.
"""

from __future__ import annotations

import errno
import inspect as inspect_module
import os
import shutil
import stat
import struct
import wave
from pathlib import Path

import pytest
from test_source import make_tone, make_video

from kilix_playalong import source
from kilix_playalong.errors import InvalidInputError, PlayalongError, ProviderFailedError
from kilix_playalong.providers import media
from kilix_playalong.providers.media import copy_into, normalize, probe

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg tools are not installed",
)


@pytest.fixture(scope="session")
def library(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """One container of each kind, generated once. Generators live in test_source."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg tools are not installed")
    root = tmp_path_factory.mktemp("containers")
    return {
        "wav": make_tone(root / "tone.wav"),
        "mp3": make_tone(root / "tone.mp3", title="Tag Title", artist="Tag Artist"),
        "m4a": make_tone(root / "tone.m4a", codec="aac"),
        "flac": make_tone(root / "tone.flac", TITLE="Tag Title"),
        "opus": make_tone(root / "tone.opus", codec="libopus", TITLE="Tag Title"),
        "mp4": make_video(root / "clip.mp4"),
        "video_only": make_video(root / "silent.mp4", audio=False),
    }


@requires_ffmpeg
def test_ffmpeg_normalizes_a_tiny_synthetic_waveform(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"".join(struct.pack("<h", index % 200) for index in range(800)))

    source_metadata = probe(source)
    assert source_metadata["streams"]
    target = normalize(source, tmp_path / "normalized.wav")
    metadata = probe(target)
    audio = next(
        stream
        for stream in metadata["streams"]
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    )
    assert audio["sample_rate"] == "44100"
    assert audio["channels"] == 2


@requires_ffmpeg
@pytest.mark.parametrize("kind", ["wav", "mp3", "m4a", "flac", "opus", "mp4"])
def test_normalize_needs_no_help_with_a_local_container(
    tmp_path: Path, library: dict[str, Path], kind: str
) -> None:
    """The local arm reuses ``normalize`` unchanged, and this is why it may.

    A file from the user's disk arrives in containers the YouTube arm never
    produces -- FLAC, Opus, a video with a real video stream, an MP3 with cover
    art -- and the existing invocation already covers all of them: ``-map 0:a:0``
    takes the first audio stream and nothing else, ``-vn`` drops the picture, and
    the output is the same 44.1 kHz stereo PCM every later stage expects. If that
    ever stops being true this fails, which is the check that keeps the "no second
    ffmpeg invocation" claim honest.
    """
    target = normalize(library[kind], tmp_path / f"{kind}.wav")
    audio = next(
        stream
        for stream in probe(target)["streams"]
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    )
    assert (audio["sample_rate"], audio["channels"], audio["codec_name"]) == (
        "44100",
        2,
        "pcm_s16le",
    )


@requires_ffmpeg
def test_probe_insists_on_audio_by_default_and_can_be_asked_not_to(
    library: dict[str, Path],
) -> None:
    """The default is what the pipeline relies on; the opt-out has one caller.

    ``source.inspect_file`` needs the document of an audio-less file so it can say
    "this has no audio track" instead of "this is not media"; every other caller
    wants the failure. Both halves are pinned so widening the default would fail
    here rather than in a stage that silently accepted a silent film.
    """
    with pytest.raises(ProviderFailedError, match="no audio stream"):
        probe(library["video_only"])
    document = probe(library["video_only"], require_audio=False)
    kinds = {stream.get("codec_type") for stream in document["streams"] if isinstance(stream, dict)}
    assert kinds == {"video"}


@requires_ffmpeg
def test_copying_media_in_never_moves_or_links_the_original(
    tmp_path: Path, library: dict[str, Path]
) -> None:
    """The user's library must be untouched, and the project must be standalone.

    A move empties the library, a symlink makes the project break the moment the
    user reorganises, and a hardlink makes writing to one write to the other. All
    three are cheaper than a copy and all three are wrong, so each is checked
    rather than assumed from "it copied the bytes".
    """
    original = tmp_path / "library" / "tone.mp3"
    original.parent.mkdir()
    shutil.copyfile(library["mp3"], original)
    before = original.read_bytes()
    before_stat = original.stat()

    destination = copy_into(original, tmp_path / "project" / "source" / "source.mp3")

    assert original.exists(), "the source was moved"
    assert original.read_bytes() == before
    assert original.stat().st_mtime == before_stat.st_mtime
    assert original.stat().st_nlink == 1, "the source gained a hardlink"
    assert not destination.is_symlink(), "the project points at the library"
    assert destination.stat().st_ino != before_stat.st_ino
    assert destination.stat().st_nlink == 1
    assert destination.read_bytes() == before
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not (destination.parent / "source.mp3.partial").exists()

    # Writing to the project's copy cannot reach back into the library...
    destination.write_bytes(b"scribbled over")
    assert original.read_bytes() == before
    # ...and losing the library cannot break the project.
    original.unlink()
    assert destination.read_bytes() == b"scribbled over"


@requires_ffmpeg
def test_a_copy_that_runs_past_the_size_bound_leaves_nothing_behind(
    tmp_path: Path, library: dict[str, Path]
) -> None:
    destination = tmp_path / "project" / "source.mp3"
    with pytest.raises(InvalidInputError, match="size limit"):
        copy_into(library["mp3"], destination, max_bytes=1024)
    assert sorted(path.name for path in destination.parent.iterdir()) == []
    assert library["mp3"].exists()


def test_copy_refuses_a_bound_that_is_not_a_size(tmp_path: Path) -> None:
    payload = tmp_path / "tone.bin"
    payload.write_bytes(b"0123456789")
    with pytest.raises(InvalidInputError, match="positive number of bytes"):
        copy_into(payload, tmp_path / "copy.bin", max_bytes=0)


def test_copy_refuses_anything_that_is_not_a_regular_file(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe.mp3"
    os.mkfifo(fifo)
    with pytest.raises(InvalidInputError, match="regular file"):
        copy_into(fifo, tmp_path / "copy.mp3")
    assert not (tmp_path / "copy.mp3").exists()


def test_copy_refuses_to_copy_a_file_onto_itself(tmp_path: Path) -> None:
    payload = tmp_path / "tone.bin"
    payload.write_bytes(b"0123456789")
    with pytest.raises(InvalidInputError, match="onto itself"):
        copy_into(payload, payload)
    assert payload.read_bytes() == b"0123456789"


def test_copy_refuses_an_empty_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.mp3"
    empty.touch()
    with pytest.raises(InvalidInputError, match="empty"):
        copy_into(empty, tmp_path / "copy.mp3")
    assert not (tmp_path / "copy.mp3").exists()


@requires_ffmpeg
def test_copy_progress_is_measured_in_bytes_not_guessed(
    tmp_path: Path, library: dict[str, Path]
) -> None:
    seen: list[tuple[int, int]] = []
    destination = copy_into(
        library["mp3"],
        tmp_path / "copy.mp3",
        on_bytes=lambda copied, total: seen.append((copied, total)),
    )
    size = destination.stat().st_size
    assert seen[-1] == (size, size)
    assert all(total == size for _, total in seen)


def test_a_callback_that_fails_takes_the_partial_file_with_it(tmp_path: Path) -> None:
    """And keeps its own exception -- with one documented exception to that.

    An ``OSError`` out of the callback is indistinguishable from one of the copy's
    own, so it goes through the same path-stripping restatement. Saying so in the
    docstring and not checking it is how that stops being true.
    """
    payload = tmp_path / "tone.bin"
    payload.write_bytes(b"0123456789")

    def explode(copied: int, total: int) -> None:
        raise RuntimeError("the surface went away")

    with pytest.raises(RuntimeError, match="surface went away"):
        copy_into(payload, tmp_path / "copy.bin", on_bytes=explode)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["tone.bin"]

    def explode_as_oserror(copied: int, total: int) -> None:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), str(payload))

    with pytest.raises(PlayalongError) as caught:
        copy_into(payload, tmp_path / "copy.bin", on_bytes=explode_as_oserror)
    assert str(payload) not in str(caught.value)
    assert os.strerror(errno.ENOSPC) in str(caught.value)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["tone.bin"]


def test_a_failed_copy_never_names_the_file_it_was_reading(tmp_path: Path) -> None:
    """``os.open`` puts the path it failed on into the message; this must not.

    Not because nothing downstream would catch it -- ``pipeline._run_stage`` passes
    the file arm's library path, its parent and its resolved form to
    ``public_error``, so a library on ``/mnt`` is redacted there. Because
    ``copy_into`` is public and its callers are not all that stage: the property is
    this function's, so it is measured against this function. ``O_NOFOLLOW`` on a
    symlink is the cheapest real ``OSError`` this can be provoked with; the errno
    is incidental, the path is the point.
    """
    library_path = tmp_path / "library"
    library_path.mkdir()
    dangling = library_path / "tone.mp3"
    dangling.symlink_to(library_path / "somewhere-else.mp3")

    with pytest.raises(PlayalongError) as caught:
        copy_into(dangling, tmp_path / "project" / "source.mp3")
    message = str(caught.value)
    assert str(dangling) not in message
    assert "tone.mp3" not in message and "library" not in message
    # The kernel's own explanation survives; only the path is gone.
    assert os.strerror(errno.ELOOP) in message
    assert not (tmp_path / "project" / "source.mp3.partial").exists()


def test_a_failed_write_never_names_the_file_it_was_writing(tmp_path: Path) -> None:
    payload = tmp_path / "tone.bin"
    payload.write_bytes(b"0123456789")
    project = tmp_path / "project"
    project.mkdir()
    # A directory where the partial file has to go: EISDIR from the O_EXCL open.
    (project / "source.bin.partial").mkdir()

    with pytest.raises(PlayalongError) as caught:
        copy_into(payload, project / "source.bin")
    message = str(caught.value)
    assert "source.bin" not in message and str(project) not in message
    assert not (project / "source.bin").exists()


@pytest.mark.parametrize("destination", [Path("/"), Path(".")])
def test_a_destination_that_names_no_file_is_refused_without_quoting_it(
    tmp_path: Path, destination: Path
) -> None:
    """``Path.with_name`` reports a nameless path by printing it.

    The same rule as every other error here: the refusal is a literal. Checked
    before the ``.partial`` name is built, which is the only place it can be.
    """
    payload = tmp_path / "tone.bin"
    payload.write_bytes(b"0123456789")
    with pytest.raises(InvalidInputError, match="must name a file") as caught:
        copy_into(payload, destination)
    assert str(destination) not in str(caught.value)


def test_the_probe_and_the_normalizer_name_the_protocols_they_allow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A media file's *bytes* can be a playlist that names a URL.

    ffmpeg refuses that by default -- ``Protocol 'http' not on whitelist
    'file,crypto,data'`` -- but a default is the tool's policy, not this tree's,
    and ``source.py``'s own threat model is that the extension is not evidence of
    what is inside. Passing the whitelist explicitly makes the bound ours, so a
    future ffmpeg that widens its default cannot widen this.
    """
    seen: list[list[str]] = []

    def record(arguments: list[str], **keywords: object) -> object:
        seen.append(list(arguments))
        raise ProviderFailedError("stopped after the argv was built")

    monkeypatch.setattr("kilix_playalong.providers.media.run_command", record)
    for call in (
        lambda: probe(tmp_path / "song.mp3"),
        lambda: normalize(tmp_path / "song.mp3", tmp_path / "out.wav"),
    ):
        with pytest.raises(ProviderFailedError):
            call()
    assert seen, "no argv was built"
    for arguments in seen:
        assert "-protocol_whitelist" in arguments
        index = arguments.index("-protocol_whitelist")
        assert arguments[index + 1] == "file,crypto,data"
        # An input option: it only binds what comes after it.
        input_at = arguments.index("-i" if arguments[0] == "ffmpeg" else str(tmp_path / "song.mp3"))
        assert index < input_at


@pytest.mark.parametrize("side", ["source", "destination"])
def test_a_path_that_cannot_be_encoded_is_refused_rather_than_thrown(
    tmp_path: Path, side: str
) -> None:
    """A lone surrogate in either path is a message here, not a traceback.

    ``json.loads('"\\ud800.mp3"')`` yields one and no filesystem call can encode
    it, so every ``os.open`` below answers with a bare ``UnicodeEncodeError`` --
    which ``cli.py``, catching ``PlayalongError`` and nothing else, prints as a
    traceback. ``os.fsencode`` asks exactly the question those calls will ask.
    """
    payload = tmp_path / "tone.bin"
    payload.write_bytes(b"0123456789")
    lone = "\ud800.bin"
    given = Path(lone) if side == "source" else payload
    destination = tmp_path / (lone if side == "destination" else "source.bin")

    with pytest.raises(InvalidInputError, match="cannot represent") as caught:
        copy_into(given, destination)
    assert lone not in str(caught.value)
    assert sorted(item.name for item in tmp_path.iterdir()) == ["tone.bin"]


def test_both_reads_of_a_users_file_use_the_same_open_flags() -> None:
    """The flag set is a security property, and it is applied in two modules.

    ``source._measure`` opens to size the file and closes again; ``copy_into``
    reopens by path to read it, so the window the first one narrows is closed by
    the second. A flag added to one of the two opens and not the other -- or
    ``O_NOFOLLOW`` quietly dropped from either during a later edit -- produces no
    failing test and no visible symptom, which is why there is one name and this
    checks that both sites still spell it.
    """
    assert media.SAFE_OPEN_FLAGS & os.O_NOFOLLOW
    assert media.SAFE_OPEN_FLAGS & os.O_NONBLOCK
    assert media.SAFE_OPEN_FLAGS & os.O_NOCTTY
    assert not media.SAFE_OPEN_FLAGS & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
    for function in (source._measure, copy_into):
        body = inspect_module.getsource(function)
        assert "SAFE_OPEN_FLAGS" in body
        # ...and neither one has quietly gone back to spelling its own set.
        assert "os.O_NOFOLLOW" not in body
