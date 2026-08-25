"""The source union: what is accepted, what is refused, and in whose words.

Every media file these tests touch is generated here, by ffmpeg, at test time: a
few seconds of a 440 Hz sine in each container the app is likely to be handed.
No real music enters the repository, and nothing here depends on a fixture file
whose provenance a reader would have to take on trust.
"""

from __future__ import annotations

import errno
import inspect as inspect_module
import math
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from kilix_playalong import source
from kilix_playalong.errors import InvalidInputError, PlayalongError
from kilix_playalong.lyrics import embedded_tag_key, looks_like_lrc, select_embedded_lyrics
from kilix_playalong.providers import media, youtube
from kilix_playalong.text import MAX_DISPLAY_TEXT
from kilix_playalong.util import canonical_json, public_error, sha256_file, sha256_text

VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg tools are not installed",
)


def _ffmpeg(*arguments: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *arguments],
        check=True,
        capture_output=True,
        timeout=180,
    )


def make_tone(path: Path, *, seconds: float = 3.0, codec: str | None = None, **tags: str) -> Path:
    arguments = ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    for key, value in tags.items():
        arguments += ["-metadata", f"{key}={value}"]
    if codec is not None:
        arguments += ["-c:a", codec]
    _ffmpeg(*arguments, str(path))
    return path


def make_video(path: Path, *, seconds: float = 3.0, audio: bool = True, **tags: str) -> Path:
    arguments = ["-f", "lavfi", "-i", f"testsrc=size=64x64:rate=10:duration={seconds}"]
    if audio:
        arguments += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    for key, value in tags.items():
        arguments += ["-metadata", f"{key}={value}"]
    arguments += ["-shortest", "-c:v", "libx264"]
    if audio:
        arguments += ["-c:a", "aac"]
    _ffmpeg(*arguments, str(path))
    return path


def _syncsafe(value: int) -> bytes:
    return bytes([(value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F])


def _id3_frame(identifier: bytes, payload: bytes) -> bytes:
    return identifier + _syncsafe(len(payload)) + b"\x00\x00" + payload


def make_uslt_mp3(
    path: Path, text: str, *, language: str = "eng", title: str = "USLT Title"
) -> Path:
    """An MP3 carrying a real ID3v2.4 USLT frame.

    Hand-built on purpose. ffmpeg's own muxer writes ``-metadata lyrics=`` as a
    ``TXXX`` frame described "USLT", which is not the same thing on disk, so a
    test that only used ffmpeg would never exercise the frame every real tagger
    writes -- the one libavformat reports back as ``lyrics-<language>``.
    """
    bare = path.with_name(path.stem + "-bare.mp3")
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2",
        "-map_metadata",
        "-1",
        "-write_id3v1",
        "0",
        "-id3v2_version",
        "0",
        str(bare),
    )
    body = _id3_frame(
        b"USLT", b"\x03" + language.encode("ascii") + b"\x00" + text.encode("utf-8")
    ) + _id3_frame(b"TIT2", b"\x03" + title.encode("utf-8") + b"\x00")
    path.write_bytes(b"ID3\x04\x00\x00" + _syncsafe(len(body)) + body + bare.read_bytes())
    bare.unlink()
    return path


@pytest.fixture(scope="session")
def library(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg tools are not installed")
    root = tmp_path_factory.mktemp("library")
    files = {
        "wav": make_tone(root / "tone.wav"),
        "mp3": make_tone(
            root / "tone.mp3", title="Tag Title", artist="Tag Artist", album="Tag Album"
        ),
        "m4a": make_tone(root / "tone.m4a", codec="aac", title="Tag Title", artist="Tag Artist"),
        "flac": make_tone(
            root / "tone.flac", TITLE="Tag Title", ARTIST="Tag Artist", UNSYNCEDLYRICS="flac line"
        ),
        "opus": make_tone(
            root / "tone.opus",
            codec="libopus",
            TITLE="Tag Title",
            ARTIST="Tag Artist",
            LYRICS="opus line",
        ),
        "mp4": make_video(root / "clip.mp4", title="Video Title"),
        "video_only": make_video(root / "silent.mp4", audio=False),
        "uslt": make_uslt_mp3(root / "uslt.mp3", "real line one\nreal line two"),
    }
    text = root / "text.mp3"
    text.write_text("this file is prose with a media extension\n" * 20, encoding="utf-8")
    files["text_as_mp3"] = text
    return files


def _copy(library_file: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(library_file, destination)
    return destination


# --------------------------------------------------------------------------
# Parsing: which arm, and what is refused rather than guessed
# --------------------------------------------------------------------------


def test_each_spelling_of_a_source_lands_in_the_right_arm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # HOME is a real temporary directory rather than an invented literal under
    # a home root: the publication checklist greps for a home path belonging to
    # anyone but this account, and a fictional name trips it exactly as loudly
    # as a real one -- so the check would be trained to be ignored by its own
    # test fixtures. (Writing the example out in this comment tripped it too,
    # which is the point made twice.)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir("/tmp")
    assert source.parse_source(VIDEO_URL) == source.YouTubeSource(url=VIDEO_URL)
    assert source.parse_source("  " + VIDEO_URL + "  ").kind == "youtube"
    assert source.parse_source("/music/song.mp3") == source.FileSource(path=Path("/music/song.mp3"))
    assert source.parse_source("~/song.mp3").path == tmp_path / "song.mp3"
    assert source.parse_source("./song.mp3").path == Path("/tmp/song.mp3")
    assert source.parse_source("../song.mp3").path == Path("/song.mp3")
    assert source.parse_source("song.mp3").path == Path("/tmp/song.mp3")
    assert source.parse_source("album/song.mp3").path == Path("/tmp/album/song.mp3")
    assert source.parse_source("file:///music/a%20b.mp3").path == Path("/music/a b.mp3")
    assert source.parse_source("file://localhost/music/song.mp3").path == Path("/music/song.mp3")
    # A file:// path is literal: ~ in a URL is a directory called "~".
    assert source.parse_source("file:///~/song.mp3").path == Path("/~/song.mp3")


@pytest.mark.parametrize(
    "value",
    [
        "foo.com/bar",
        "youtu.be/dQw4w9WgXcQ",
        "www.youtube.com/watch?v=dQw4w9WgXcQ",
        "example.co.uk/music/song.mp3",
    ],
)
def test_a_string_that_reads_as_both_a_host_and_a_path_is_refused(value: str) -> None:
    with pytest.raises(InvalidInputError) as caught:
        source.parse_source(value)
    message = str(caught.value)
    assert "./" in message and "https://" in message


def test_prefixing_settles_the_ambiguity_in_either_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir("/tmp")
    assert source.parse_source("./foo.com/bar").path == Path("/tmp/foo.com/bar")
    assert source.parse_source("https://youtu.be/dQw4w9WgXcQ").kind == "youtube"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ftp://host/song.mp3", "https://"),
        ("s3://bucket/key.mp3", "https://"),
        ("data:audio/mpeg;base64,AAAA", "https://"),
        ("weird:name.mp3", "prefix a relative path"),
        ("http://www.youtube.com/watch?v=dQw4w9WgXcQ", "HTTPS"),
        ("https://example.com/song.mp3", "youtube.com"),
        ("https://www.youtube.com/playlist?list=PL1", "playlist"),
        ("file://elsewhere/share/song.mp3", "remote host"),
        ("file:///song.mp3?x=1", "plain absolute path"),
        ("file:///song.mp3#top", "plain absolute path"),
        ("file:song.mp3", "absolute path"),
        ("file:///song%00.mp3", "NUL"),
        ("", "a source is required"),
        ("    ", "a source is required"),
        ("a\nb.mp3", "unprintable"),
        ("a\tb.mp3", "unprintable"),
        ("\x00.mp3", "unprintable"),
        ("\ud800.mp3", "unprintable"),
        ("/" + "x" * 5000, "too long"),
    ],
)
def test_every_refused_spelling_says_what_it_needed(value: str, expected: str) -> None:
    with pytest.raises(InvalidInputError) as caught:
        source.parse_source(value)
    assert expected in str(caught.value)


def test_parse_raises_nothing_but_a_playalong_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The entry-point contract ``providers/youtube.py`` states, restated here.

    cli.py catches ``PlayalongError`` and prints a traceback for anything else, so
    a ValueError out of urlsplit or a RuntimeError out of ``expanduser`` is a
    crash report rather than a message.
    """
    monkeypatch.delenv("HOME", raising=False)
    hostile = [
        "https://[",
        "https://youtube.com:999999/watch?v=x",
        "https://user:pw@youtube.com/watch?v=x",
        "file://",
        "file:///%ff%fe.mp3",
        "://x",
        ":",
        "~" * 4000,
        "~nosuchuser/song.mp3",
        "." * 3000,
        "/" * 3000,
        "\N{ZERO WIDTH SPACE}.mp3",
    ]
    for value in hostile:
        try:
            source.parse_source(value)
        except PlayalongError:
            continue
        except Exception as error:  # pragma: no cover - the failure this pins
            raise AssertionError(f"{value!r} raised {error.__class__.__name__}") from error


# --------------------------------------------------------------------------
# The file arm: every rejection
# --------------------------------------------------------------------------


@requires_ffmpeg
@pytest.mark.parametrize("kind", ["wav", "mp3", "m4a", "flac", "opus", "mp4"])
def test_every_container_is_accepted_and_measured(library: dict[str, Path], kind: str) -> None:
    inspection = source.inspect_file(source.file_source(library[kind]))
    assert inspection.path == library[kind]
    assert inspection.size == library[kind].stat().st_size
    assert 2.5 <= inspection.metadata.duration <= 3.5
    assert inspection.metadata.container


@requires_ffmpeg
def test_a_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError, match="directory"):
        source.inspect_file(source.file_source(tmp_path))


@requires_ffmpeg
def test_a_fifo_is_refused_without_opening_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both halves, because the second one is the whole point of the ``lstat``.

    ``_measure``'s ``O_NONBLOCK`` open catches a FIFO too, so "refused" passes
    with the type gate deleted; the property that gate exists for is that nothing
    opens the thing at all -- a device can have side effects on open, and a FIFO
    on a filesystem that ignores ``O_NONBLOCK`` blocks forever.
    """
    fifo = tmp_path / "pipe.mp3"
    os.mkfifo(fifo)
    opened: list[str] = []
    real_open = os.open

    def watched(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        opened.append(str(path))
        return real_open(path, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", watched)
    with pytest.raises(InvalidInputError, match="regular file"):
        source.inspect_file(source.file_source(fifo))
    monkeypatch.undo()
    assert str(fifo) not in opened


@requires_ffmpeg
@pytest.mark.skipif(not Path("/dev/null").exists(), reason="no /dev/null on this platform")
def test_a_device_is_refused() -> None:
    with pytest.raises(InvalidInputError, match="regular file"):
        source.inspect_file(source.file_source("/dev/null"))


@requires_ffmpeg
def test_a_missing_file_says_what_to_check(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError) as caught:
        source.inspect_file(source.file_source(tmp_path / "absent.mp3"))
    assert "no such file" in str(caught.value) and "https://" in str(caught.value)


@requires_ffmpeg
def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty.mp3"
    empty.touch()
    with pytest.raises(InvalidInputError, match="empty"):
        source.inspect_file(source.file_source(empty))


@requires_ffmpeg
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_an_unreadable_file_is_refused(tmp_path: Path, library: dict[str, Path]) -> None:
    locked = _copy(library["mp3"], tmp_path / "locked.mp3")
    locked.chmod(0o000)
    try:
        with pytest.raises(InvalidInputError, match="not readable"):
            source.inspect_file(source.file_source(locked))
    finally:
        locked.chmod(0o600)


@requires_ffmpeg
def test_a_file_above_the_size_bound_is_refused(library: dict[str, Path]) -> None:
    with pytest.raises(InvalidInputError) as caught:
        source.inspect_file(source.file_source(library["mp3"]), max_bytes=1024)
    assert str(caught.value) == "source media exceeds the 1K size limit"


@requires_ffmpeg
def test_the_extension_is_not_evidence(library: dict[str, Path]) -> None:
    with pytest.raises(InvalidInputError, match="not media"):
        source.inspect_file(source.file_source(library["text_as_mp3"]))


@requires_ffmpeg
def test_media_with_no_audio_track_says_so(library: dict[str, Path]) -> None:
    with pytest.raises(InvalidInputError, match="no audio track"):
        source.inspect_file(source.file_source(library["video_only"]))


@requires_ffmpeg
def test_a_link_out_of_the_home_tree_is_refused_and_the_real_path_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library: dict[str, Path]
) -> None:
    root = tmp_path.resolve()
    home = root / "home"
    home.mkdir()
    outside = root / "elsewhere"
    outside.mkdir()
    target = _copy(library["mp3"], outside / "tone.mp3")
    monkeypatch.setenv("HOME", str(home))
    escaping = home / "music.mp3"
    escaping.symlink_to(target)

    with pytest.raises(InvalidInputError, match="symbolic link"):
        source.inspect_file(source.file_source(escaping))
    # The message is true: naming the real file is all it takes.
    assert source.inspect_file(source.file_source(target)).path == target


@requires_ffmpeg
def test_a_link_inside_the_home_tree_is_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library: dict[str, Path]
) -> None:
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    real = _copy(library["mp3"], home / "music" / "tone.mp3")
    link = home / "shortcut.mp3"
    link.symlink_to(real)
    assert source.inspect_file(source.file_source(link)).path == real


@requires_ffmpeg
def test_a_long_file_is_refused_in_the_same_words_as_a_long_video(
    library: dict[str, Path],
) -> None:
    with pytest.raises(InvalidInputError) as caught:
        source.inspect_file(source.file_source(library["mp3"]), max_duration=1.0)
    assert str(caught.value) == "source exceeds the 0.0166667-minute limit"

    max_duration = 3 * 60 * 60
    assert (
        f"source exceeds the {max_duration / 60:g}-minute limit"
        == "source exceeds the 180-minute limit"
    )


def test_the_duration_and_size_wordings_are_the_youtube_ones() -> None:
    """A drift check, not a restatement.

    "The same message shape" is a property of two modules, so it is pinned
    against the other module's source rather than against a copy of the string
    that would go stale silently the moment ``youtube.py`` reworded its own.
    """
    downloader = inspect_module.getsource(youtube.download)
    assert 'f"source exceeds the {max_duration / 60:g}-minute limit"' in downloader
    accept = inspect_module.getsource(youtube._accept_media)
    assert 'f"source media exceeds the {max_filesize} size limit"' in accept
    # ...and the two render identically for the ceiling both arms default to.
    assert source.format_size(source.MAX_FILE_BYTES) == "512M"


@requires_ffmpeg
def test_unusable_bounds_are_input_errors_not_arithmetic(library: dict[str, Path]) -> None:
    spec = source.file_source(library["mp3"])
    for bound in (0.0, -1.0, math.nan, math.inf):
        with pytest.raises(InvalidInputError, match="maximum duration"):
            source.inspect_file(spec, max_duration=bound)
    with pytest.raises(InvalidInputError, match="provider timeout"):
        source.inspect_file(spec, timeout=0)
    with pytest.raises(InvalidInputError, match="maximum source size"):
        source.inspect_file(spec, max_bytes=0)


# --------------------------------------------------------------------------
# What the file says about itself
# --------------------------------------------------------------------------


@requires_ffmpeg
def test_tags_are_read_and_the_filename_is_only_the_fallback(
    tmp_path: Path, library: dict[str, Path]
) -> None:
    tagged = source.inspect_file(source.file_source(library["mp3"])).metadata
    assert (tagged.title, tagged.artist) == ("Tag Title", "Tag Artist")
    assert (tagged.title_tag, tagged.artist_tag) == ("title", "artist")
    assert tagged.title_from_tag
    assert "album" in tagged.ignored_tags

    untagged_path = _copy(library["wav"], tmp_path / "Some Song Name.wav")
    untagged = source.inspect_file(source.file_source(untagged_path)).metadata
    assert untagged.title == "Some Song Name"
    assert untagged.title_tag == ""
    assert not untagged.title_from_tag
    # No artist is invented from the filename: "01 - Title.mp3" would put a track
    # number in the artist field and the user would never know where it came from.
    assert (untagged.artist, untagged.artist_tag) == ("", "")


@requires_ffmpeg
def test_vorbis_comments_on_the_stream_are_found_too(library: dict[str, Path]) -> None:
    """Ogg/Opus carries its tags on the stream, not on the container."""
    metadata = source.inspect_file(source.file_source(library["opus"])).metadata
    assert (metadata.title, metadata.artist) == ("Tag Title", "Tag Artist")
    assert metadata.title_tag == "TITLE"


@requires_ffmpeg
@pytest.mark.parametrize(
    ("kind", "tag", "origin", "text"),
    [
        ("uslt", "lyrics-eng", "id3-uslt", "real line one\nreal line two"),
        ("flac", "UNSYNCEDLYRICS", "vorbis-unsyncedlyrics", "flac line"),
        ("opus", "LYRICS", "vorbis-lyrics", "opus line"),
    ],
)
def test_embedded_lyrics_are_extracted_with_the_tag_they_came_from(
    library: dict[str, Path], kind: str, tag: str, origin: str, text: str
) -> None:
    lyrics = source.inspect_file(source.file_source(library[kind])).metadata.lyrics
    assert lyrics is not None
    assert (lyrics.tag, lyrics.origin, lyrics.text) == (tag, origin, text)
    # The text is never in the serialisable form: it is a payload, like a URL.
    assert "text" not in lyrics.as_json()


@requires_ffmpeg
def test_a_file_without_lyrics_offers_none(library: dict[str, Path]) -> None:
    assert source.inspect_file(source.file_source(library["wav"])).metadata.lyrics is None
    assert source.inspect_file(source.file_source(library["m4a"])).metadata.lyrics is None


@requires_ffmpeg
def test_an_oversized_lyric_tag_is_ignored_rather_than_carried(tmp_path: Path) -> None:
    """A quarter of a megabyte in a lyric frame is a payload, not a lyric sheet.

    Written straight into the ID3 frame rather than through ``-metadata``, which
    cannot carry a value this size in an argv at all -- which is also why the
    bound has to be here and not left to the tagger.
    """
    payload = "la la la\n" * 40_000
    assert len(payload.encode("utf-8")) > source.MAX_EMBEDDED_LYRICS_BYTES
    path = make_uslt_mp3(tmp_path / "huge.mp3", payload)
    metadata = source.inspect_file(source.file_source(path)).metadata
    assert metadata.lyrics is None
    assert "lyrics-eng" in metadata.ignored_tags


@requires_ffmpeg
def test_a_synchronised_lyric_tag_is_found_under_the_name_the_file_gave_it(
    tmp_path: Path,
) -> None:
    """``SYNCEDLYRICS`` reaches ffprobe verbatim, and was invisible here.

    A Vorbis comment and a Matroska tag arrive under whatever name the file gave
    them -- generated here rather than argued, because that is the premise the key
    vocabulary rests on. This is the *timed* spelling, so while this module kept
    its own shorter key list a file whose only lyrics were in this tag produced no
    ``embedded-lyrics.txt``, no ``LYRIC_SOURCE_EMBEDDED`` route and no timings.
    """
    path = make_tone(
        tmp_path / "synced.flac",
        TITLE="Synced",
        SYNCEDLYRICS="[00:01.00]first line\n[00:02.00]second line",
    )
    lyrics = source.inspect_file(source.file_source(path)).metadata.lyrics
    assert lyrics is not None
    assert (lyrics.tag, lyrics.origin) == ("SYNCEDLYRICS", "vorbis-syncedlyrics")
    assert looks_like_lrc(lyrics.text)


@pytest.mark.parametrize(
    "key",
    [
        "LYRICS",
        "SYNCEDLYRICS",
        "UNSYNCEDLYRICS",
        "UNSYNCED_LYRICS",
        "unsynced lyrics",
        "lyrics-eng",
        "lyrics_eng",
        "USLT",
        "uslt-eng",
        "com.apple.iTunes:LYRICS",
        "ALBUM",
        "COMMENT",
    ],
)
def test_a_key_names_lyrics_here_exactly_when_lyrics_py_says_it_does(key: str) -> None:
    """One vocabulary for one question, checked from this side of it.

    This module lifts the tag out and ``pipeline._embedded_lyrics`` hands the same
    key straight back to ``lyrics`` to be parsed, so a key only one side accepts is
    either a sheet no lyric route ever sees or a declared language dropped on the
    way through. Two lists here disagreed on ten of sixteen real spellings; there
    is now one, and this is what stops a second one from growing back.
    """
    document: dict[str, object] = {
        "format": {"format_name": "flac", "tags": {key: "verse one\nverse two"}},
        "streams": [{"codec_type": "audio"}],
    }
    metadata = source.read_metadata(Path("/library/song.flac"), document, 12.0)
    if embedded_tag_key(key) is None:
        assert metadata.lyrics is None
        assert key in metadata.ignored_tags
        return
    assert metadata.lyrics is not None
    # The tag is published as the file spelled it, not as either side folded it.
    assert metadata.lyrics.tag == key


def test_a_stamped_tag_is_the_longer_one_when_the_lines_are_the_same() -> None:
    """Why "longest wins" here does not fight "stamps win" in ``lyrics``.

    ``_extract_lyrics`` keeps one tag and discards the rest, so its ranking is the
    last word for a file carrying several, and it ranks on length where
    ``lyrics.select_embedded_lyrics`` ranks a synchronised tag first. The two
    cannot disagree while both tags carry the same lines, because a stamp only
    *adds* characters to a line -- asserted rather than argued, since the argument
    is the whole reason the two rankings are allowed to differ.
    """
    lines = ["first line", "second line", "third line"]
    plain = "\n".join(lines)
    stamped = "\n".join(f"[00:{index:02d}.00]{line}" for index, line in enumerate(lines))
    assert len(stamped) > len(plain)

    tags = {"UNSYNCEDLYRICS": plain, "LYRICS": stamped}
    document: dict[str, object] = {
        "format": {"format_name": "flac", "tags": tags},
        "streams": [{"codec_type": "audio"}],
    }
    metadata = source.read_metadata(Path("/library/song.flac"), document, 12.0)
    assert metadata.lyrics is not None and metadata.lyrics.tag == "LYRICS"
    assert looks_like_lrc(metadata.lyrics.text)
    # ...and the module that ranks stamps first arrives at the same text. Compared
    # on the text, because ``select_embedded_lyrics`` reports the folded prefix as
    # its ``tag`` where this module reports the key the file actually wrote.
    selected = select_embedded_lyrics(tags)
    assert selected is not None and selected.text == stamped


@requires_ffmpeg
def test_the_richest_lyric_tag_wins_and_the_others_are_named(tmp_path: Path) -> None:
    path = make_tone(
        tmp_path / "both.flac",
        TITLE="Both",
        LYRICS="stub",
        UNSYNCEDLYRICS="verse one\nverse two\nverse three",
    )
    metadata = source.inspect_file(source.file_source(path)).metadata
    assert metadata.lyrics is not None
    assert metadata.lyrics.tag == "UNSYNCEDLYRICS"
    assert "LYRICS" in metadata.ignored_tags


@requires_ffmpeg
def test_lyric_line_structure_survives_extraction(tmp_path: Path) -> None:
    """An LRC transcript in a lyric tag is one line per stamp, and must stay so."""
    path = make_uslt_mp3(tmp_path / "lrc.mp3", "[00:12.30]first line\r\n[00:15.00]second line")
    lyrics = source.inspect_file(source.file_source(path)).metadata.lyrics
    assert lyrics is not None
    assert lyrics.text == "[00:12.30]first line\n[00:15.00]second line"


# --------------------------------------------------------------------------
# Acquisition
# --------------------------------------------------------------------------


@requires_ffmpeg
def test_acquire_copies_the_file_and_writes_its_lyrics_beside_it(
    tmp_path: Path, library: dict[str, Path]
) -> None:
    destination = tmp_path / "project" / "source"
    acquired = source.acquire(source.file_source(library["uslt"]), destination)
    assert acquired.path == destination / "source.mp3"
    assert acquired.path.read_bytes() == library["uslt"].read_bytes()
    assert stat.S_IMODE(acquired.path.stat().st_mode) == 0o600
    assert acquired.lyrics_path == destination / source.EMBEDDED_LYRICS_NAME
    assert acquired.lyrics_path is not None
    assert acquired.lyrics_path.read_text(encoding="utf-8") == "real line one\nreal line two"
    assert stat.S_IMODE(acquired.lyrics_path.stat().st_mode) == 0o600
    assert acquired.metadata.lyrics is not None
    assert acquired.metadata.lyrics.origin == "id3-uslt"
    # The name cannot collide with what the YouTube arm sweeps or with the
    # pipeline's own copy of a user-supplied lyrics file.
    assert not source.EMBEDDED_LYRICS_NAME.startswith("source")
    assert not source.EMBEDDED_LYRICS_NAME.startswith("lyrics-input")


@requires_ffmpeg
def test_acquiring_leaves_the_users_library_exactly_as_it_was(
    tmp_path: Path, library: dict[str, Path]
) -> None:
    """The copy property, from the library's side: nothing about it changed."""
    original = _copy(library["mp3"], tmp_path / "library" / "tone.mp3")
    before = original.read_bytes()
    before_stat = original.stat()

    acquired = source.acquire(source.file_source(original), tmp_path / "project" / "source")

    assert original.exists()
    assert original.read_bytes() == before
    assert original.stat().st_mtime == before_stat.st_mtime
    assert original.stat().st_nlink == 1
    assert not original.is_symlink()
    assert acquired.path.stat().st_ino != before_stat.st_ino
    assert not acquired.path.is_symlink()
    assert acquired.path.stat().st_nlink == 1

    # ...and from the project's side: it survives the library being reorganised.
    original.unlink()
    assert media.probe(acquired.path)["streams"]


@requires_ffmpeg
def test_acquire_leaves_nothing_behind_when_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library: dict[str, Path]
) -> None:
    destination = tmp_path / "project" / "source"

    def explode(path: Path, data: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(source, "private_write", explode)
    with pytest.raises(OSError, match="disk full"):
        source.acquire(source.file_source(library["uslt"]), destination)
    assert sorted(path.name for path in destination.iterdir()) == []


@requires_ffmpeg
def test_a_source_is_identified_by_its_bytes_not_by_where_it_sits(
    tmp_path: Path, library: dict[str, Path]
) -> None:
    original = _copy(library["mp3"], tmp_path / "library" / "tone.mp3")
    spec = source.file_source(original)
    acquired = source.acquire(spec, tmp_path / "project" / "source")

    # Byte-for-byte, so the caller can hash the cheap copy instead of the library.
    assert source.source_sha256(spec) == sha256_file(acquired.path)
    # Same bytes under a different name is the same source...
    moved = _copy(original, tmp_path / "library" / "renamed.mp3")
    assert source.source_sha256(source.file_source(moved)) == source.source_sha256(spec)
    # ...and a rewrite under the same name is not.
    original.write_bytes(original.read_bytes() + b"\x00")
    assert source.source_sha256(spec) != sha256_file(acquired.path)


def test_the_youtube_arm_keeps_the_fingerprint_the_pipeline_already_stores() -> None:
    assert source.source_sha256(source.YouTubeSource(url=VIDEO_URL)) == sha256_text(VIDEO_URL)


def test_a_source_document_never_publishes_where_the_file_lives() -> None:
    # Deliberately outside any home directory, and deliberately not spelled
    # /home/<name>: the point is a path whose disclosure would be a leak, and
    # the checklist that hunts for leaks must not be desensitised by the test
    # that proves this one is prevented.
    document = source.FileSource(path=Path("/srv/library/Music/Private Demo.mp3")).as_json()
    assert document == {"kind": "file", "name": "Private Demo.mp3"}
    assert "/srv/library" not in repr(document)


def test_a_source_document_cannot_carry_a_hostile_filename_either() -> None:
    """``display_name`` is published, and a filename is user-supplied text.

    ``MAX_SOURCE_LENGTH`` bounds what ``parse_source`` accepts, not what a
    ``FileSource`` can hold -- it is a plain dataclass -- and no filesystem
    forbids an ESC or a BEL in a name. The document goes to a browser page and a
    terminal, so the name is bounded and cleaned exactly like a tag name.
    """
    name = "\x1b[2Jwiped\x07" + "n" * 500 + ".mp3"
    document = source.FileSource(path=Path("/library") / name).as_json()
    published = document["name"]
    assert isinstance(published, str)
    assert published.isprintable()
    assert "\x1b" not in published and "\x07" not in published
    assert len(published) <= MAX_DISPLAY_TEXT


@requires_ffmpeg
def test_a_link_out_of_the_home_tree_cannot_be_hashed_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library: dict[str, Path]
) -> None:
    """The fingerprint is a second door into a user-supplied path; same gates.

    Closing the check in ``inspect_file`` and leaving ``source_sha256`` open would
    mean a caller that fingerprints before it validates reads through exactly the
    link the other function refuses.
    """
    root = tmp_path.resolve()
    home = root / "home"
    home.mkdir()
    outside = root / "elsewhere"
    outside.mkdir()
    target = _copy(library["mp3"], outside / "tone.mp3")
    monkeypatch.setenv("HOME", str(home))
    escaping = home / "music.mp3"
    escaping.symlink_to(target)

    with pytest.raises(InvalidInputError, match="symbolic link"):
        source.source_sha256(source.file_source(escaping))
    with pytest.raises(InvalidInputError, match="no such file"):
        source.source_sha256(source.file_source(root / "absent.mp3"))
    with pytest.raises(InvalidInputError, match="directory"):
        source.source_sha256(source.file_source(root))


@requires_ffmpeg
def test_acquisition_reports_a_fraction_it_measured(
    tmp_path: Path, library: dict[str, Path]
) -> None:
    """A fraction inferred from a clock is not a fraction; this one is measured.

    Bytes written over bytes to write is not inferred, so the copy hands a caller
    the real numbers and a surface that draws a bar has something honest behind
    it. The rule itself lives in ``media.copy_into``'s ``on_bytes`` paragraph,
    which is the function that could cheat and does not.
    """
    seen: list[tuple[int, int]] = []
    acquired = source.acquire(
        source.file_source(library["mp3"]),
        tmp_path / "project" / "source",
        on_bytes=lambda copied, total: seen.append((copied, total)),
    )
    assert seen, "no progress was reported at all"
    assert [copied for copied, _ in seen] == sorted(copied for copied, _ in seen)
    assert seen[-1][0] == seen[-1][1] == acquired.path.stat().st_size
    assert all(0 < copied <= total for copied, total in seen)


@requires_ffmpeg
def test_a_hand_built_relative_spec_is_not_mistaken_for_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library: dict[str, Path]
) -> None:
    """``FileSource`` is a dataclass, so not every spec came from ``file_source``.

    A relative path differs from its resolved form on every comparison, which is
    exactly the shape the link check keys on -- so without the normalisation this
    would refuse an ordinary file for being a symbolic link that it is not.
    """
    original = _copy(library["mp3"], tmp_path / "library" / "tone.mp3")
    monkeypatch.chdir(original.parent)
    inspection = source.inspect_file(source.FileSource(path=Path("tone.mp3")))
    assert inspection.path == original.resolve()


# --------------------------------------------------------------------------
# Untrusted text: a tag *name* is file content too
# --------------------------------------------------------------------------


def test_a_tag_name_is_bounded_and_sanitised_exactly_like_a_tag_value() -> None:
    """A tag name is as untrusted as a tag value and reaches the same surfaces.

    ``MediaMetadata.as_json`` is rendered by a browser page, printed to a terminal
    and written into the manifest. Every *value* went through ``text.printable_line``;
    every *key* went through nothing, so one crafted Vorbis field name put 60,001
    characters -- or a raw ESC, BEL and newline -- straight into that document.
    Built as a document rather than as a file because no tagger has to be talked
    into writing this: the container format does not bound a field name either.
    """
    hostile = "a" * 60_001
    escapes = "ok\x1b[31mRED\x07\nsecond line"
    # A tag name is not ASCII either, and the cap is on characters: an ASCII-only
    # document cannot tell a 200-character bound from a 200-*byte* one, which is
    # how "the whole document is under 8 KiB" survived in the docstring.
    astral = "\U0001d11e"
    document: dict[str, object] = {
        "format": {
            "format_name": "flac",
            "tags": {
                hostile: "value",
                escapes: "value",
                astral * 60_001: "value",
                "TITLE": "Real Title",
                "ARTIST": "Real Artist",
                "UNSYNCEDLYRICS": "line one\nline two",
                "ALBUM": "Real Album",
            },
        },
        "streams": [{"codec_type": "audio"}],
    }
    metadata = source.read_metadata(Path("/library/song.flac"), document, 12.0)

    # Cleaning the name must not stop a name from being recognised or from
    # being excluded once it has been used.
    assert (metadata.title, metadata.artist) == ("Real Title", "Real Artist")
    assert (metadata.title_tag, metadata.artist_tag) == ("TITLE", "ARTIST")
    assert metadata.lyrics is not None and metadata.lyrics.tag == "UNSYNCEDLYRICS"
    assert "ALBUM" in metadata.ignored_tags
    assert "TITLE" not in metadata.ignored_tags

    for name in metadata.ignored_tags:
        assert len(name) <= MAX_DISPLAY_TEXT
        assert name.isprintable()
        assert name == name.strip()
    assert hostile not in metadata.ignored_tags
    assert escapes not in metadata.ignored_tags
    assert "\x1b" not in "".join(metadata.ignored_tags)
    assert len(metadata.ignored_tags) <= source._MAX_IGNORED_TAGS

    kept = astral * MAX_DISPLAY_TEXT
    assert kept in metadata.ignored_tags
    # 200 characters, 800 bytes: the same cap that costs 200 bytes in ASCII. What
    # that is worth for the whole document is
    # ``test_the_document_is_capped_in_characters_and_what_that_costs_in_bytes``.
    assert len(kept.encode("utf-8")) == 4 * MAX_DISPLAY_TEXT
    assert len(canonical_json(metadata.as_json())) < 32 * 1024


def test_a_hostile_filename_cannot_become_the_title() -> None:
    """The filename is user-supplied too, and it is the title of last resort.

    A stem that cleans away to nothing fell through to a raw slice of the name,
    which is the same unsanitised-text hole one level down.
    """
    document: dict[str, object] = {
        "format": {"format_name": "wav"},
        "streams": [{"codec_type": "audio"}],
    }
    metadata = source.read_metadata(Path("/library/\x07\x07.wav"), document, 3.0)
    assert metadata.title.isprintable()
    assert "\x07" not in metadata.title
    assert not metadata.title_from_tag


def test_the_number_of_tags_read_is_capped() -> None:
    """Two caps, and they are different caps -- and neither one bounds the walk.

    ``_MAX_TAGS`` bounds how many *distinct* tags come out of a probe document;
    ``_MAX_IGNORED_TAGS`` bounds how many of those are published. Asserting only
    the second would leave the first deletable with every test still green.

    The second document is the one the comment on ``_MAX_TAGS`` used to claim it
    stopped: 4,000 entries whose names all clean to ``field``, so the guard --
    which fires on the count of *distinct* names -- never fires, and the loop runs
    to the end. What bounds that is ``runner.run_command``'s 4 MiB output ceiling,
    not this cap. The result is bounded either way, which is what the cap is for.
    """
    document: dict[str, object] = {
        "format": {
            "format_name": "flac",
            "tags": {f"field{index}": f"value {index}" for index in range(4_000)},
        },
        "streams": [{"codec_type": "audio"}],
    }
    assert len(source._collect_tags(document)) == source._MAX_TAGS
    metadata = source.read_metadata(Path("/library/song.flac"), document, 3.0)
    assert len(metadata.ignored_tags) == source._MAX_IGNORED_TAGS
    # ASCII, which is the only text for which 8 KiB is the right number.
    assert len(canonical_json(metadata.as_json())) < 8 * 1024

    repeated: dict[str, object] = {
        "format": {
            "format_name": "flac",
            # ``\x01`` is unprintable, so ``text.printable_line`` turns each of these into
            # the one name ``field``.
            "tags": {"field" + "\x01" * index: f"value {index}" for index in range(4_000)},
        },
        "streams": [{"codec_type": "audio"}],
    }
    assert list(source._collect_tags(repeated)) == ["field"]


def test_the_document_is_capped_in_characters_and_what_that_costs_in_bytes() -> None:
    """The size ``MediaMetadata.as_json`` states, measured at its true maximum.

    Nothing in this module caps bytes: ``text.printable_line`` caps characters and
    ``_MAX_IGNORED_TAGS`` caps entries. Together they cap the document at 7,081
    characters -- 200 each for the title, the artist and the lyric tag, 64 for the
    container, 5 and 12 for the two tag names that have to fold to ``title`` and
    ``album_artist``, and 32 x 200 for ``ignored_tags``. A character is up to four
    UTF-8 bytes, which is why the docstring said "under 8 KiB" for years while an
    ASCII-only test agreed with it.

    Everything here is at its cap and every character is astral-plane, so this is
    the largest document the module can produce, and the number asserted is the
    number the docstring states. Exact rather than an inequality: a bound stated
    to the byte is only worth having if a change to it fails a test.
    """
    astral = "\U0001d11e"  # U+1D11E MUSICAL SYMBOL G CLEF: four UTF-8 bytes.
    tags = {
        "TITLE": astral * 400,
        # ``album_artist`` is the longest key in ``_ARTIST_KEYS``, so it is the
        # longest ``artist_tag`` the module can publish.
        "ALBUM_ARTIST": astral * 400,
        "lyrics-" + astral * 193: "line one\nline two",
    }
    for index in range(200):
        tags[astral * 199 + chr(0x4E00 + index)] = "value"
    document: dict[str, object] = {
        "format": {"format_name": astral * 200, "tags": tags},
        "streams": [{"codec_type": "audio"}],
    }
    metadata = source.read_metadata(Path("/library/" + astral * 300 + ".mkv"), document, 1234.5678)

    published = (
        metadata.title,
        metadata.artist,
        metadata.container,
        metadata.title_tag,
        metadata.artist_tag,
        "" if metadata.lyrics is None else metadata.lyrics.tag,
        *metadata.ignored_tags,
    )
    assert (metadata.title_tag, metadata.artist_tag) == ("TITLE", "ALBUM_ARTIST")
    assert len(metadata.ignored_tags) == source._MAX_IGNORED_TAGS
    assert max(len(text) for text in published) == MAX_DISPLAY_TEXT
    assert sum(len(text) for text in published) == 7_081

    blob = canonical_json(metadata.as_json())
    assert len(blob) == 28_481
    # The ceiling the docstring offers a caller who needs one in bytes, and the
    # number it used to offer.
    assert 8 * 1024 < len(blob) < 32 * 1024


def test_a_container_with_no_duration_falls_back_to_its_audio_stream() -> None:
    """The fallback, pinned directly.

    ffmpeg will not readily write a container that omits ``format.duration`` while
    its audio stream still carries one, so this is a document rather than a file --
    and without it the fallback is a branch that could be deleted with every test
    still passing, which is not a bound at all.
    """
    assert (
        source._document_duration(
            {
                "format": {"format_name": "matroska,webm"},
                "streams": [
                    {"codec_type": "video", "duration": "99.0"},
                    {"codec_type": "audio", "duration": "12.5"},
                ],
            }
        )
        == 12.5
    )
    # The container still wins when it has one...
    assert (
        source._document_duration(
            {"format": {"duration": "3.0"}, "streams": [{"codec_type": "audio", "duration": "9.0"}]}
        )
        == 3.0
    )
    # ...and nothing usable anywhere is None rather than a guess.
    unusable: dict[str, object] = {"streams": [{"codec_type": "audio", "duration": "N/A"}]}
    assert source._document_duration(unusable) is None
    assert source._document_duration({"format": {}, "streams": []}) is None


def test_a_duration_no_float_can_hold_is_refused_rather_than_thrown() -> None:
    """``float(10 ** 400)`` raises OverflowError instead of answering.

    ffprobe writes durations as strings, so this shape does not come off any real
    file -- but the document is JSON that another program produced, ``json.loads``
    hands over an integer of any length at all, and the rule this module is built
    on is that nothing the file says about itself is trusted. Bare, it was an
    ``OverflowError`` out of ``inspect_file``, whose stated contract is a
    ``PlayalongError`` and nothing else, and which ``cli.py`` would have printed as
    a traceback. Closed by taking the predicate from ``runner.usable_seconds``,
    which is where the trap is documented.
    """
    huge = 10**400
    assert source._positive_seconds(huge) is None
    assert source._document_duration({"format": {"duration": huge}, "streams": []}) is None
    # The rest of the predicate, so delegating cannot quietly widen it either.
    assert source._positive_seconds(True) is None  # not a one-second duration
    assert source._positive_seconds(-1) is None
    assert source._positive_seconds("1e400") is None
    assert source._positive_seconds("12.5") == 12.5


# --------------------------------------------------------------------------
# Bounds and errors that are checks rather than advice
# --------------------------------------------------------------------------


@requires_ffmpeg
def test_the_fingerprint_is_bounded_by_the_same_gates_as_inspection(
    tmp_path: Path, library: dict[str, Path]
) -> None:
    """Hashing reads every byte, so the size gate has to be here too.

    ``inspect_file`` refuses an over-sized or empty file before touching it;
    ``source_sha256`` is a second door into the same user-supplied path, and a
    ceiling one door enforces and the other only recommends is caller advice.
    """
    spec = source.file_source(library["mp3"])
    with pytest.raises(InvalidInputError) as caught:
        source.source_sha256(spec, max_bytes=1024)
    assert str(caught.value) == "source media exceeds the 1K size limit"

    empty = tmp_path / "empty.mp3"
    empty.touch()
    with pytest.raises(InvalidInputError, match="empty"):
        source.source_sha256(source.file_source(empty))

    with pytest.raises(InvalidInputError, match="maximum source size"):
        source.source_sha256(spec, max_bytes=0)
    # The bound is the same one, spelled the same way, as the inspection gate.
    assert source.source_sha256(spec) == sha256_file(library["mp3"])


@requires_ffmpeg
def test_a_race_at_copy_time_keeps_the_users_path_out_of_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library: dict[str, Path]
) -> None:
    """The file is swapped for a link between validation and the copy.

    That is the TOCTOU the ``O_NOFOLLOW`` open exists to catch, and what it
    catches it with is an ``OSError`` whose text ends in the source path.
    ``pipeline._run_stage`` does redact that path today -- ``_secrets()`` carries
    the file arm's library path, its parent and its resolved form -- but
    ``source.acquire`` is public and reachable from a JSON or IPC surface that has
    no secrets list, so the path has to be gone before it is returned. That is what
    is measured here, which is why the redaction below is applied with the *narrow*
    tuple the URL arm would have used: passing the real one would redact the path
    whether or not ``copy_into`` had kept its promise, and prove nothing.
    """
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    original = _copy(library["mp3"], library_dir / "tone.mp3")
    project = tmp_path / "project" / "source"
    real_copy_into = media.copy_into  # the real one, kept before the module attribute moves

    def swap_then_copy(from_path: Path, to_path: Path, **keywords: object) -> Path:
        from_path.unlink()
        from_path.symlink_to(library_dir / "somewhere-else.mp3")
        return real_copy_into(from_path, to_path, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr("kilix_playalong.providers.media.copy_into", swap_then_copy)
    with pytest.raises(PlayalongError) as caught:
        source.acquire(source.file_source(original), project)

    raw = str(caught.value)
    assert str(original) not in raw
    assert str(library_dir) not in raw
    assert "tone.mp3" not in raw
    # The kernel's own explanation survives; only the path is gone.
    assert os.strerror(errno.ELOOP) in raw
    # ...and it is still true after the redaction the pipeline actually applies.
    assert str(original) not in public_error(raw, secrets=(VIDEO_URL, str(project)))
    assert sorted(path.name for path in project.iterdir()) == []


def test_inspect_and_fingerprint_raise_nothing_but_a_playalong_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same entry-point contract ``parse_source`` holds, for the file arm.

    ``FileSource`` is a plain dataclass, so a spec does not have to have come
    through ``file_source``: a JSON or IPC surface can hand these functions a path
    with a NUL in it or a lone surrogate in it (``json.loads('"\\ud800.mp3"')``
    yields one, and ``file_source`` returns it happily), and a removed working
    directory turns any relative path into a bare ``FileNotFoundError`` out of
    ``os.getcwd``. Bare, those are ``ValueError``, ``UnicodeEncodeError`` and
    ``OSError``; ``cli.py`` catches ``PlayalongError`` and prints a traceback for
    everything else.
    """
    hostile = [
        Path("a\x00b.mp3"),
        Path("\ud800.mp3"),
        Path("a\udfffb.mp3"),
        Path(""),
        Path("."),
        Path("/"),
        Path("~/song.mp3"),
        Path("x" * 5000 + ".mp3"),
        Path("relative/song.mp3"),
    ]
    for path in hostile:
        for call in (source.inspect_file, source.source_sha256):
            try:
                call(source.FileSource(path=path))
            except PlayalongError:
                continue
            except Exception as error:  # pragma: no cover - the failure this pins
                raise AssertionError(f"{path!r} raised {error.__class__.__name__}") from error

    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    for attempt in (
        lambda: source.file_source("song.mp3"),
        lambda: source.parse_source("song.mp3"),
        lambda: source.inspect_file(source.FileSource(path=Path("song.mp3"))),
        lambda: source.source_sha256(source.FileSource(path=Path("song.mp3"))),
    ):
        with pytest.raises(PlayalongError):
            attempt()


@requires_ffmpeg
def test_a_filename_the_shell_could_not_decode_is_still_a_file(
    tmp_path: Path, library: dict[str, Path]
) -> None:
    """The guard above must refuse only what no filename can be.

    A filename is bytes. One that is not valid UTF-8 reaches Python through
    ``surrogateescape`` as ``\\udc80``-``\\udcff``, which ``os.fsencode`` turns
    straight back into the original byte -- an ordinary file, read here as one.
    Only an *unpaired* surrogate outside that range, which no byte can produce and
    which ``json.loads`` can, is refused. Without this the guard would be a
    regression for every user whose library predates a UTF-8 locale.
    """
    path = _copy(library["mp3"], tmp_path / os.fsdecode(b"a\xffb.mp3"))
    assert os.fsencode(path.name) == b"a\xffb.mp3"

    inspection = source.inspect_file(source.FileSource(path=path))
    assert inspection.size == path.stat().st_size
    assert source.source_sha256(source.FileSource(path=path)) == sha256_file(path)
