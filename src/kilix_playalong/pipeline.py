"""Resumable source-to-stems-to-lyrics-to-tab project pipeline.

This module is the wiring. Every other module in the package is a leaf that knows
its own job and nothing about a run; this is where their contracts meet, and it is
the one file whose reasoning is otherwise spread across a dozen method docstrings.
Four facts orient the rest.

*The source union.* A project is created from a link or from a local file, never
both -- `PipelineOptions.source_spec` refuses the contradiction. The two arms key
their acquisition stage differently on purpose, in shapes that cannot collide; see
`_acquisition_stage` for both keys and why each field is in or out of them.
Everything after acquisition reads the project's own copy, so neither arm is
visible again.

*The lyric routes.* `LYRIC_SOURCE_*` is the whole vocabulary and it is public
because `options_registry` offers exactly these as its `lyrics_source` choices.
`auto` walks `_AUTO_LYRIC_ROUTES` -- the user's own file, a sidecar, the media's
own tag, the publisher's captions -- and falls through to transcription; an
explicit choice takes its route or fails out of `_MISSING_LYRIC_SOURCE` rather
than silently degrading into another one. `_resolve_lyrics_plan` decides that
once, and the rest of the run reads the resolved `_LyricsPlan` instead of the raw
option. What the route means for the *timing* it produces -- the source's own
stamps, a forced alignment's measurement, or a spread across the duration -- is
written into the document as `lyrics.LyricTiming`, so no consumer has to infer it
from the spelling of a source id.

*Stage keys.* `_run_stage` runs a stage unless a recorded fingerprint already
covers it. ``inputs`` is the configuration this run would use and the only value a
finished stage ever writes; ``alternates`` are recorded configurations whose
artifact is good enough to keep instead. A match on an alternate is accepted and
never written back, which is what lets the adaptive lyrics key (`_whisper_keys`)
move in one direction only and still settle.

*Import direction.* `options_registry` imports this module for those constants and
this module never imports it back; the providers know nothing about either. That
edge is one-way so a rename here cannot leave a form offering a value the backend
no longer takes.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import uuid
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from .alignment import AlignmentResult, align_lines, hypothesis_from_cues
from .errors import (
    InvalidInputError,
    PlayalongError,
    ProviderFailedError,
    ProviderUnavailableError,
    RightsConfirmationRequired,
)
from .export import render_printable
from .lyrics import (
    EmbeddedLyrics,
    LyricAlignment,
    LyricsDocument,
    choose_subtitle_track,
    find_lyrics_sidecar,
    load_lyrics_document,
    parse_embedded_lyrics,
    read_bounded_text,
    select_embedded_lyrics,
    write_lyrics,
)
from .midi import load_note_events, validate_midi
from .paths import (
    ensure_private_directory,
    project_artifact,
    project_directory,
    projects_home,
)
from .providers import basic_pitch, media, separation, transcription, youtube
from .source import (
    MAX_EMBEDDED_LYRICS_BYTES,
    FileSource,
    SourceSpec,
    YouTubeSource,
    acquire,
    file_source,
    inspect_file,
    source_sha256,
)
from .state import (
    STAGE_NAMES,
    begin_stage,
    fail_stage,
    finish_stage,
    load_manifest,
    new_manifest,
    save_manifest,
    stage_is_current,
)
from .tablature import STANDARD_TUNING, infer_fingerings, render_ascii, tuning_labels, write_tab
from .text import MAX_DISPLAY_TEXT, printable_line
from .types import AudioTrack, ProjectManifest, Stage
from .util import (
    canonical_json,
    private_write,
    public_error,
    sha256_bytes,
    sha256_file,
    sha256_text,
)

RIGHTS_STATEMENT = "I confirmed that I have permission to process this media and its lyrics."
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")

#: Where the words come from. This is the one vocabulary: `options_registry` imports these
#: constants and offers them as its `lyrics_source` choices rather than spelling matching
#: string literals, so a rename here cannot leave a form offering a value this module no
#: longer takes. The edge runs one way only -- `options_registry` may import `pipeline`,
#: never the reverse -- and what a shared constant still cannot cover is a *new* member
#: that no surface offers, which is what that module's
#: `test_the_lyric_sources_offered_are_the_backend_vocabulary` asserts against this tuple.
LYRIC_SOURCE_AUTO = "auto"
LYRIC_SOURCE_CAPTIONS = "captions"
LYRIC_SOURCE_FILE = "file"
LYRIC_SOURCE_EMBEDDED = "embedded"
LYRIC_SOURCE_SIDECAR = "sidecar"
LYRIC_SOURCE_TRANSCRIBE = "transcribe"
LYRIC_SOURCE_CHOICES = (
    LYRIC_SOURCE_AUTO,
    LYRIC_SOURCE_CAPTIONS,
    LYRIC_SOURCE_FILE,
    LYRIC_SOURCE_EMBEDDED,
    LYRIC_SOURCE_SIDECAR,
    LYRIC_SOURCE_TRANSCRIBE,
)

#: What `auto` tries, in order, before falling through to transcription. Read it
#: as "the most authoritative words available": the user's own file beats a timed
#: sidecar they downloaded, which beats a tag inside the media, which beats the
#: publisher's captions, which beats a machine listening to the singing. Two of
#: these can only ever exist for the file arm and simply answer None for a
#: download, so one order serves both arms.
_AUTO_LYRIC_ROUTES = (
    LYRIC_SOURCE_FILE,
    LYRIC_SOURCE_SIDECAR,
    LYRIC_SOURCE_EMBEDDED,
    LYRIC_SOURCE_CAPTIONS,
)

#: Why an explicitly chosen lyric source could not be used. An explicit choice
#: never silently degrades to another one: the option exists for the user who
#: already knows the captions are wrong, and quietly handing them those captions
#: back is the failure the option was added to prevent.
_MISSING_LYRIC_SOURCE = {
    LYRIC_SOURCE_FILE: "no lyrics file was supplied; pass one or choose another lyric source",
    LYRIC_SOURCE_SIDECAR: "no .lrc sidecar was found beside the source file",
    LYRIC_SOURCE_EMBEDDED: "the source carries no embedded lyrics tag",
    LYRIC_SOURCE_CAPTIONS: "the source published no caption track this can read",
}

#: The two halves of the rename an accepted alignment applies to a lyric source id.
#: The id says which source won; a `-estimated` tail on it says this app spread those
#: words out itself, which stops being true the moment alignment measures them, so the
#: tail goes and `-aligned` takes its place. `_aligned_source` is the only place it
#: happens, and a rejected alignment keeps the estimate and its tail.
#:
#: This was an interlock as well until the document gained `timing`:
#: `lyrics.load_lyrics_document` decided whether a lyrics.json it was *re-reading*
#: carried observed timing by asking whether its source id ended in `-estimated`, so an
#: aligned document that kept the tail was re-aligned on every resume. That reader takes
#: the field now, which leaves these two as provenance and nothing more -- except in a
#: document written before the field existed, where the tail is still the only evidence
#: there is and `lyrics._stored_timing` still reads it.
#: `test_an_aligned_document_reads_back_as_measured` is what fails if the two modules
#: ever disagree about the spelling.
_ESTIMATED_SUFFIX = "-estimated"
_ALIGNED_SUFFIX = "-aligned"

#: What a run reports while it works: a stage name from `state.STAGE_NAMES`, one of
#: these four statuses, and one line of detail.
#:
#: This is not `types.StageStatus`, and the difference is not an oversight. That one
#: is the four states a stage is *persisted* in; these are the four things that
#: happen to a stage during a run. ``cached`` is an event and never a stored state,
#: and ``pending`` is a stored state no run ever announces.
#:
#: The detail is why this carries three values and not two. `_run_stage` already
#: computes both halves of it -- the note `finish_stage` records, and the redacted
#: reason `fail_stage` records -- and a surface told only "done" cannot report the
#: most important thing the lyrics stage produces, which is whether forced alignment
#: was accepted (`_apply_alignment`). It is empty on ``running``, where nothing is
#: known yet.
#:
#: What may be in it is STATED, not enforced here: no URL, no filesystem path, no
#: lyric text. The ``done``/``cached`` arm is this module's own literals and counts
#: plus `lyrics.LyricsDocument.note` and `alignment.AlignmentReport.summary`, both of
#: which their own modules document as holding a fixed vocabulary and numbers; the
#: ``error`` arm is what `public_error` left of a provider message with `_secrets`
#: applied. `public_error` erases URLs, ``/home/<name>`` heads and the paths this run
#: learned, and finishes with `text.printable_line`, so a terminal control character a
#: provider wrote to its stderr reaches a surface as visible text rather than as an
#: instruction -- the same guarantee, from the same function, as the message `cli.main`
#: prints out of a raised error, which is the same string.
ProgressStatus = Literal["cached", "running", "done", "error"]
ProgressCallback = Callable[[str, ProgressStatus, str], None]

# Best-first snapshots of the transcription provider's adaptive policy: its public
# `QUALITY_ORDER` and the device rule at the head of its `_auto_candidates`. They are only
# ever used to rank a *recorded* configuration against the one this machine would pick now;
# the provider itself still owns every real selection.
#
# A snapshot on purpose, and not for want of a name to import -- `QUALITY_ORDER` is
# public and this module already imports the provider. This order decides which recorded
# configurations `_whisper_keys` still accepts, so importing the provider's tuple would make
# a reorder there re-transcribe every project on the machine with no one having decided to:
# put `medium` above `large-v3` and `_no_worse_than` stops accepting a recorded `large-v3`
# at all. Copying it makes that a human decision instead --
# test_whisper_policy_mirrors_the_transcription_provider fails when the provider reorders
# its candidates or changes how `auto` picks a device, and someone then chooses whether the
# stage keys should move with it.
_WHISPER_QUALITY_ORDER = ("large-v3", "large-v3-turbo", "medium", "small")
_WHISPER_DEVICE_ORDER = ("cuda", "cpu")


def _recorded_whisper_receipt(manifest: ProjectManifest) -> transcription.Receipt | None:
    """Return the receipt that describes a project's lyrics, if a transcript made them.

    The receipt is the provider's format and the provider parses it. It used to be
    the bare string ``faster-whisper:<model>`` and is now that model followed by the
    audio source, the language and how the language was decided; `parse_receipt` reads
    both, which is the whole reason this asks the provider instead of slicing the
    prefix off itself. Slicing returned the entire extended receipt as if it were a
    model name -- see `test_lyrics_stage_survives_a_cleared_model_cache`, which is the
    behaviour that broke.
    """
    lyrics = manifest["lyrics"]
    source = lyrics.get("source") if isinstance(lyrics, dict) else None
    if not isinstance(source, str):
        return None
    return transcription.parse_receipt(source)


def _no_worse_than(order: tuple[str, ...], value: str) -> tuple[str, ...]:
    """Return the members of a best-first order that are at least as good as ``value``.

    ``value`` must be a member of ``order``. Both callers only ever ask about a value the
    provider's own policy produced -- a model it resolved from `auto`, or the `cuda`/`cpu`
    that `auto` landed on -- so an unranked value never reaches here. The separate rule that
    keeps an explicitly requested model comparable only with itself lives at the call site in
    `_whisper_keys`, which does not consult this order at all in that case.
    """
    return order[: order.index(value) + 1]


def _aligned_source(source: str) -> str:
    """Rename a lyric source id to say its timing was measured rather than invented."""
    base = source[: -len(_ESTIMATED_SUFFIX)] if source.endswith(_ESTIMATED_SUFFIX) else source
    return base + _ALIGNED_SUFFIX


def valid_tuning(pitches: Sequence[int]) -> bool:
    """True for the six ascending, distinct MIDI pitches this pipeline accepts.

    Public because `options_registry.tuning_pitches` has to apply exactly this rule
    before it hands a surface's free-entry tuning to `PipelineOptions`: a tuning that
    module accepts and this one refuses is the "a form ships a value the backend
    rejects" bug it exists to prevent. Today that module writes the rule out a second
    time and a corpus test reconciles the two; this is the body it should call instead.
    A predicate rather than a validator, so each
    caller keeps its own message -- one is about a form value, the other about a
    backend argument -- and so `tuning_pitches` keeps the int-only check that is
    genuinely its own, being what lets it promise a `tuple[int, ...]`.

    `bool` is excluded because `True` is an `int` and a tuning is not six flags.
    """
    return (
        len(pitches) == 6
        and not any(isinstance(note, bool) or not 0 <= note <= 127 for note in pitches)
        and len(set(pitches)) == 6
        and tuple(sorted(pitches)) == tuple(pitches)
    )


def _display_text(value: str) -> str:
    """What a title or an artist is allowed to be, for the arm `source` never sees.

    A local file's tags are cleaned in `source.read_metadata`; a download's reported
    title and artist are cleaned here. Both write the same two manifest fields, so
    both apply the same rule and the same ceiling -- `text.printable_line` and
    `text.MAX_DISPLAY_TEXT`, which is the whole of this function and the reason it
    is still a named function rather than the call inlined at its five sites.

    That it is the *strong* rule matters most here: this is where the third-party
    strings arrive. yt-dlp reports the title and artist of a video it did not write,
    and they land in a manifest that `cli.command_show` prints straight to a
    terminal, `tablature.render_ascii` puts in the header of a ``.txt`` a user will
    ``cat``, and `server` serves.
    """
    return printable_line(value, limit=MAX_DISPLAY_TEXT)


@dataclass(frozen=True)
class PipelineOptions:
    """Every answer a run needs, in the vocabulary `options_registry` publishes.

    Field names are the option ids of `optionspec`, so a surface can post its form
    straight at this dataclass; `options_registry.BACKEND_FIELDS` is the table that
    fails a test when the two namespaces drift.

    ``url`` and ``source_path`` are the two arms of the source union and at most one
    of them is ever set. Both default to "nothing", because a resume of a project
    that already recorded its source needs neither.
    """

    url: str = ""
    source_path: Path | None = None
    title: str = ""
    artist: str = ""
    lyrics_source: str = LYRIC_SOURCE_AUTO
    lyrics_path: Path | None = None
    language: str = "auto"
    model: str = "htdemucs_6s"
    whisper_model: str = transcription.DEFAULT_MODEL
    audio_source: str = transcription.DEFAULT_AUDIO_SOURCE
    align_supplied_text: bool = True
    device: str = "auto"
    max_duration: float = 30 * 60
    max_fret: int = 20
    tuning: tuple[int, ...] = STANDARD_TUNING
    allow_model_downloads: bool = False
    rights_confirmed: bool = False

    def source_spec(self) -> SourceSpec | None:
        """Which arm of the source union these options name, or None for neither.

        Syntax only: nothing is resolved and no file is opened, so an intake screen can
        call this while the user is still typing. The file arm's real gates -- existence,
        symlink escape, type, size, "is this media at all", duration -- live in
        `source.inspect_file` and run in `create_project`.
        """
        if self.url and self.source_path is not None:
            raise InvalidInputError("give one source: a link or a file, not both")
        if self.source_path is not None:
            return file_source(self.source_path)
        if self.url:
            return YouTubeSource(url=youtube.validate_url(self.url))
        return None


def _validate_options(options: PipelineOptions, *, require_rights: bool) -> None:
    """Reject any option whose *value* is not one this pipeline accepts.

    Deliberately not a cross-field check, with one exception. "You chose `--lyrics-source
    file` and named no file" and "you asked for the media's own lyrics tag and gave a
    YouTube link" are real errors, but they are errors about a *combination*, and they are
    raised where the combination can actually be judged: `create_project`, which can see
    both of them -- whether a file was supplied, and which arm of the source union was
    named -- and refuses them before it makes a directory or opens a provider. Raising them
    here would also make it impossible to describe those choices as legal values --
    `options_registry` submits every choice it offers through this function, so a value that
    only makes sense beside another option would have to be described as unavailable
    everywhere.

    Those two, and no others. `--lyrics-source sidecar` on a link, and `captions` on a local
    file, are equally certain to fail and are *not* refused early: the lyrics stage raises
    them out of `_MISSING_LYRIC_SOURCE`, after acquisition, normalisation and separation
    have already run. That is late, and it is stated here rather than described as a design.

    The exception is the source union itself: naming both arms is not a value that becomes
    valid in some other context, it is a contradiction, and `source_spec` refuses it.
    """
    if require_rights and not options.rights_confirmed:
        raise RightsConfirmationRequired("explicit permission confirmation is required")
    options.source_spec()
    if options.lyrics_source not in LYRIC_SOURCE_CHOICES:
        raise InvalidInputError("unsupported lyric source")
    if options.language != "auto" and not _LANGUAGE.fullmatch(options.language):
        raise InvalidInputError("language must be 'auto' or a short BCP 47 language tag")
    if options.model not in separation.SUPPORTED_MODELS:
        raise InvalidInputError("unsupported Demucs model")
    if options.whisper_model not in transcription.MODEL_CHOICES:
        raise InvalidInputError("unsupported faster-whisper model")
    if options.audio_source not in transcription.AUDIO_SOURCE_CHOICES:
        raise InvalidInputError("audio source must be vocals, mix, or auto")
    if options.device not in {"auto", "cpu", "cuda"}:
        raise InvalidInputError("device must be auto, cpu, or cuda")
    if not math.isfinite(options.max_duration) or not 1 <= options.max_duration <= 2 * 60 * 60:
        raise InvalidInputError("maximum duration must be between one second and two hours")
    if not 12 <= options.max_fret <= 30:
        raise InvalidInputError("maximum fret must be between 12 and 30")
    if not valid_tuning(options.tuning):
        raise InvalidInputError("tuning must contain six ascending MIDI pitches")


def _source_identity(spec: SourceSpec, *, max_duration: float) -> str:
    """One digest that names a source, whichever arm it came from.

    The YouTube arm is `sha256_text(url)`, byte for byte the value this module has always
    written as ``url_sha256`` -- `youtube.validate_url` is a gate that returns the string it
    was handed, not a normaliser, so passing the URL through it cannot move the digest. No
    existing project's fingerprints move because a second arm appeared, which
    `test_the_youtube_acquisition_key_survived_the_source_union` checks against the whole
    recorded fingerprint rather than against this line. The file arm is the digest of the
    file's *content*, which is what makes a path change harmless and a content change
    meaningful.

    The file is gated by `inspect_file` before it is read: size, type, symlink escape, "is
    this media", duration. That is a check and not a lock -- a file that grows between the
    gate and the digest is not stopped here -- and the backstop for that race is
    `media.copy_into`'s running byte count when the acquisition stage copies it.
    """
    if isinstance(spec, YouTubeSource):
        return sha256_text(spec.url)
    inspect_file(spec, max_duration=max_duration)
    return source_sha256(spec)


def create_project(options: PipelineOptions) -> tuple[Path, ProjectManifest]:
    _validate_options(options, require_rights=True)
    spec = options.source_spec()
    if spec is None:
        raise InvalidInputError("a source is required: a YouTube URL or a local file path")
    if options.lyrics_path is not None and not options.lyrics_path.is_file():
        raise InvalidInputError("the supplied lyrics file does not exist")
    # The two combinations `_validate_options` documents as belonging here. Both are
    # certain, not merely likely: no later stage can invent a lyrics file the user did not
    # pass, and only the file arm ever records an embedded tag (`_store_embedded_lyrics`
    # runs in `_acquire_file` and nowhere else). Refused before the directory exists, for
    # the same reason the duration gate is -- the alternative is the lyrics stage raising
    # the identical message once a download, a normalise and a Demucs separation have been
    # paid for. The file-arm message is `_MISSING_LYRIC_SOURCE`'s own and both places raise
    # `InvalidInputError`, so the user reads the same sentence and a caller sees the same
    # class whichever of the two catches it.
    if options.lyrics_source == LYRIC_SOURCE_FILE and options.lyrics_path is None:
        raise InvalidInputError(_MISSING_LYRIC_SOURCE[LYRIC_SOURCE_FILE])
    if options.lyrics_source == LYRIC_SOURCE_EMBEDDED and isinstance(spec, YouTubeSource):
        raise InvalidInputError(
            "a link carries no embedded lyrics tag; pass the file itself with --file, "
            "or choose another lyric source"
        )
    # Before the project directory exists, so a file this machine cannot use costs the
    # user an error and not a half-made project they then have to delete.
    identity = _source_identity(spec, max_duration=options.max_duration)
    project_id = "song-" + uuid.uuid4().hex[:16]
    project_dir = project_directory(project_id)
    project_dir.mkdir(mode=0o700)
    manifest = new_manifest(
        project_id,
        url_sha256=identity,
        rights_statement=RIGHTS_STATEMENT,
        title=_display_text(options.title),
        artist=_display_text(options.artist),
        model=options.model,
        language=options.language,
        whisper_model=options.whisper_model,
        device=options.device,
        max_duration=options.max_duration,
        tuning=options.tuning,
        max_fret=options.max_fret,
    )
    manifest["source"]["kind"] = spec.kind
    if isinstance(spec, YouTubeSource):
        manifest["source"]["url"] = spec.url
    else:
        # Recorded for the same reason the URL is: a project whose acquisition failed can
        # be resumed without the user retyping where the music was. Stripped from
        # `kilix-playalong show --json` exactly like the URL, and never needed once the
        # copy exists -- see `Pipeline._file_spec`.
        manifest["source"]["path"] = str(spec.path)
        manifest["source"]["name"] = spec.display_name
    manifest["settings"].update(
        lyrics_source=options.lyrics_source,
        audio_source=options.audio_source,
        align_supplied_text=options.align_supplied_text,
    )
    _store_supplied_lyrics(project_dir, manifest, options)
    save_manifest(project_dir, manifest)
    return project_dir, manifest


def _copy_private(source: Path, destination: Path, *, description: str) -> None:
    """Copy one small text file into a project at 0600, naming no path if it fails.

    `shutil.copyfile` raises an ``OSError`` carrying both filenames, and both of them are
    somewhere in the user's filesystem. That is the rule `providers/media.py` states for its
    own copy and the rule this module has to keep for the two copies it makes itself -- the
    more so because one of them happens in `create_project`, outside any stage, where there
    is no `_run_stage` handler to redact anything and a bare ``OSError`` would reach the CLI
    as a traceback rather than as a message.
    """
    try:
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
    except OSError as error:
        raise ProviderFailedError(f"{description} could not be copied into the project") from error


def _store_supplied_lyrics(
    project_dir: Path,
    manifest: ProjectManifest,
    options: PipelineOptions,
) -> None:
    """Copy a supplied lyrics file into the project so later resumes keep using it."""
    if options.lyrics_path is None:
        return
    lyrics_source = ensure_private_directory(project_dir / "source") / (
        "lyrics-input" + options.lyrics_path.suffix.lower()
    )
    _copy_private(options.lyrics_path, lyrics_source, description="the supplied lyrics file")
    manifest["source"]["lyrics_input_path"] = lyrics_source.relative_to(project_dir).as_posix()
    manifest["source"]["lyrics_input_sha256"] = sha256_file(lyrics_source)


@dataclass(frozen=True)
class _LyricsPlan:
    """Which lyric source this run will use, resolved once against the project.

    Resolved, never the raw option, for the same reason `_whisper_keys` keys on a
    resolved model: `auto` and the explicit choice `auto` happens to land on produce
    the same artifact, so they must key identically and neither may re-run the other.
    """

    route: str
    #: None only for the transcription route, whose document does not exist yet.
    document: LyricsDocument | None
    #: Digest of the file this route reads, and the only input digest in the stage
    #: key. The three routes this run did *not* take cannot change its output, so
    #: their digests are deliberately absent: keying on them would re-transcribe a
    #: song because an unrelated `.lrc` appeared beside the source.
    digest: str | None
    #: True when the words arrived without timing and this run will measure it.
    aligns: bool


class Pipeline:
    def __init__(
        self,
        project_dir: Path,
        manifest: ProjectManifest,
        options: PipelineOptions,
        progress: ProgressCallback | None = None,
    ):
        self.project_dir = project_dir
        self.manifest = manifest
        self.options = options
        self.progress: ProgressCallback = progress or (lambda _name, _status, _detail: None)
        self._plan: _LyricsPlan | None = None
        # Paths a provider can name in an exception that only become known while the
        # run is happening. `_secrets` reads them; the acquisition stage writes the
        # user's library path here, which nothing else in this process knows.
        self._learned_secrets: list[str] = []

    def _save(self) -> None:
        save_manifest(self.project_dir, self.manifest)

    def _secrets(self) -> tuple[str, ...]:
        """Every string a stage error must not carry out of this process.

        `public_error` already erases URLs and the ``/home/<name>`` head of a home-tree
        path, which is all the URL arm ever needed and which covers a library kept where
        most libraries are kept. It covers neither ``/mnt`` nor ``/media``, and the file arm
        has a library path to lose.

        This is a backstop, not the first line. `source.inspect_file` states that its
        rejections carry no path, `media.copy_into` states the same ENFORCED, and this
        module's own two copies go through `_copy_private`, which converts an ``OSError``
        naming both files into a literal. What is listed here is what an opener would name
        if one of those stopped keeping its promise: the path as typed, its directory --
        which is where the sidecar search reads -- and the *resolved* path, because a
        library reached through a symlinked ``~/Music`` is opened under its real name and
        not the one the user gave.
        """
        paths = [
            self.options.url,
            str(self.project_dir),
            str(self.options.lyrics_path or ""),
            *self._learned_secrets,
        ]
        if self.options.source_path is not None:
            paths.extend(
                (
                    str(self.options.source_path),
                    str(self.options.source_path.parent),
                    os.path.realpath(self.options.source_path),
                )
            )
        return tuple(path for path in paths if path)

    def _remember_secret(self, value: str) -> None:
        if value and value not in self._learned_secrets:
            self._learned_secrets.append(value)

    def _invalidate_from(self, name: str) -> tuple[dict[str, Stage], ...]:
        """Wipe every stage below `name`, returning what was wiped.

        The return value is the price of the wipe being reversible. Invalidation
        is persisted *before* the stage that caused it runs, so a stage that then
        fails used to leave the project holding neither its old records nor any
        new ones -- stems, lyrics, MIDI, tab and printable all still on disk,
        with a manifest that no longer admitted to any of them. Widening
        `--max-duration-minutes` on a finished project whose video had since been
        taken down did exactly that: the acquisition stage re-keyed, wiped
        everything below it, and only then discovered it could not re-fetch.

        The records handed back are deep copies, so the caller can put them back
        after the manifest has been mutated and saved.
        """

        offset = STAGE_NAMES.index(name)
        wiped: list[dict[str, Stage]] = []
        for later in STAGE_NAMES[offset + 1 :]:
            stage = self.manifest["stages"][later]
            wiped.append({later: deepcopy(stage)})
            stage["status"] = "pending"
            stage["started_at"] = None
            stage["finished_at"] = None
            stage["artifacts"] = []
            stage["error"] = None
            stage.pop("note", None)
            stage.pop("fingerprint", None)
        return tuple(wiped)

    def _restore(self, wiped: tuple[dict[str, Stage], ...]) -> None:
        """Put back what a failed re-run had no right to take.

        Only the stages *below* the one that failed. That stage keeps its error,
        because the user asked for it to run and needs to see why it did not.
        The ones below it were never attempted: their artifacts are untouched on
        disk and still describe the same inputs they always did, since the stage
        that would have replaced those inputs is precisely the one that failed.

        A restored ``done`` is not taken on trust either -- `stage_is_current`
        re-digests every artifact before it accepts one, so a record put back
        over missing or altered bytes simply is not current on the next resume.
        """

        for entry in wiped:
            for name, stage in entry.items():
                self.manifest["stages"][name] = deepcopy(stage)

    def _run_stage(
        self,
        name: str,
        provider: str,
        action: Callable[[], tuple[list[Path], str]],
        *,
        inputs: object,
        alternates: Sequence[object] = (),
    ) -> None:
        """Run a stage unless a recorded fingerprint already covers it.

        ``inputs`` describes the configuration this run would use, and is the only value a
        finished stage ever records -- so a stage always converges on the key it actually ran
        with, and no stage needs a second pass to settle. ``alternates`` are configurations
        whose recorded artifact is good enough to keep instead of re-running. A match on one
        of them keeps the recorded fingerprint exactly as it is: rewriting it to ``inputs``
        would erase the better configuration the artifact was really made with, and the next
        run on a recovered machine would re-do work it already has. That asymmetry --
        accepted, never written -- is what makes invalidation one-directional for the
        adaptive lyrics stage (see ``_whisper_keys``).

        A `KeyboardInterrupt` is deliberately not caught. It is a ``BaseException``, so it
        passes the handler below untouched and leaves the stage recorded as ``running``,
        which the next resume re-runs -- `state.stage_is_current` calls a stage current only
        when it is ``done``. Catching it to record a cancellation would put a manifest write
        on the interrupt path, which `runner._terminate_process_group` masks ``SIGINT`` to
        keep short, and would buy a distinction no surface in this release renders.
        """

        def fingerprint_of(value: object) -> str:
            return sha256_bytes(canonical_json({"provider": provider, "inputs": value}))

        fingerprint = fingerprint_of(inputs)
        # stage_is_current only re-digests artifacts once a fingerprint matches, and these
        # candidates are distinct, so at most one of them costs a digest pass.
        for candidate in (fingerprint, *(fingerprint_of(value) for value in alternates)):
            if stage_is_current(
                self.project_dir,
                self.manifest,
                name,
                fingerprint=candidate,
            ):
                # The recorded note, not silence: a cached lyrics stage still says which
                # route made the words and whether alignment was accepted, which is the
                # answer a resume is usually being run to see.
                self.progress(name, "cached", self.manifest["stages"][name].get("note", ""))
                return
        wiped = self._invalidate_from(name)
        begin_stage(self.manifest, name, provider, fingerprint=fingerprint)
        self._save()
        self.progress(name, "running", "")
        try:
            paths, note = action()
            finish_stage(self.project_dir, self.manifest, name, paths, note=note)
            self._save()
            self.progress(name, "done", note)
        except Exception as error:
            message = public_error(str(error), secrets=self._secrets())
            recorded = message or error.__class__.__name__
            # Before the failure is recorded, not after: `fail_stage` saves, and
            # the whole point is that the save which lands must already hold the
            # stages this run had no business disturbing.
            self._restore(wiped)
            fail_stage(self.manifest, name, recorded)
            self._save()
            # The same string `fail_stage` just recorded, so the line a user watches and
            # the manifest a surface reads back cannot say two different things.
            self.progress(name, "error", recorded)
            if isinstance(error, PlayalongError):
                raise
            raise ProviderFailedError(message or f"{provider} failed") from error

    # ----------------------------------------------------------------- the run

    def _source_kind(self) -> str:
        """Which arm this project was created from. Legacy projects predate the union."""
        kind = self.manifest["source"].get("kind")
        return kind if isinstance(kind, str) else "youtube"

    def run(self) -> ProjectManifest:
        acquisition = self._acquisition_stage()
        self._run_stage(
            "download",
            acquisition[0],
            acquisition[1],
            inputs=acquisition[2],
        )
        self._run_stage("normalize", "ffmpeg:pcm-s16le-44100-stereo", self._normalize, inputs={})
        # Known gap, deliberately not closed here: this key stores the raw `device` option, so
        # `auto` silently resolving differently after the hardware changes leaves the stems
        # reported as cached -- the same class of staleness the lyrics key below now catches.
        # Closing it needs a cheap "is a CUDA device present" probe that only `separation.py`
        # can supply; deciding it here would mean importing torch into the orchestrator on
        # every run, which is exactly the isolation f5ceb99 established. Flagged for that
        # module's owner rather than papered over.
        self._run_stage(
            "separate",
            f"demucs:{self.options.model}@eeac1d1",
            self._separate,
            inputs={"model": self.options.model, "device": self.options.device},
        )
        plan, whisper, whisper_alternates = self._lyrics_keys()
        self._run_stage(
            "lyrics",
            "captions-or-faster-whisper",
            self._lyrics,
            inputs=self._lyrics_inputs(plan, whisper),
            alternates=self._lyrics_alternates(plan, whisper_alternates),
        )
        self._run_stage(
            "transcribe-guitar",
            "basic-pitch-onnx:0.4.0",
            self._transcribe_guitar,
            inputs={},
        )
        # `title` and `artist` are in these two keys because they are rendered *into*
        # these two artifacts -- the ASCII tab's header and the printable's heading --
        # and nowhere else. They are read from the manifest rather than from the
        # options because the manifest is what the renderers are handed: a title the
        # source reported and the user never typed still has to invalidate.
        self._run_stage(
            "tablature",
            "kilix-playalong-fingering:v1",
            self._tablature,
            inputs={
                "tuning": self.options.tuning,
                "max_fret": self.options.max_fret,
                "title": self.manifest["title"],
                "artist": self.manifest["artist"],
            },
        )
        self._run_stage(
            "export",
            "kilix-playalong-print:v1",
            self._export,
            inputs={"title": self.manifest["title"], "artist": self.manifest["artist"]},
        )
        return self.manifest

    # --------------------------------------------------------------- acquiring

    def _acquisition_stage(
        self,
    ) -> tuple[str, Callable[[], tuple[list[Path], str]], dict[str, object]]:
        """Pick the arm that fetches this project's media, and how it is keyed.

        The stage is called ``download`` for both arms because `state.STAGE_NAMES` is a
        persisted contract this module does not own; the provider string is what records
        which arm actually ran.

        The two arms key differently on purpose, and the shapes are distinct so they can
        never collide:

        * the YouTube key is unchanged, field for field, from before the union existed, so
          no project already on this machine re-downloads because a second arm was added.
          That is why ``max_duration`` stays in it even though the argument below applies
          to a finished download too: removing it would re-key every project on this
          machine, which is the harm the field is being kept to avoid;
        * the file key holds neither ``language`` nor ``max_duration``. Nothing about
          copying a file depends on the language. The duration bound is an *admission*
          gate that has already run and cannot be re-decided usefully: `create_project`
          refused the file outright if it was over the limit, and what the project holds is
          that admitted file, byte for byte, at a duration that is now recorded. Keying on
          the bound would make raising `--max-duration-minutes` re-acquire -- and a resume
          is not entitled to assume the library is still reachable, so on a project whose
          music has since been reorganised that re-acquisition raises *after*
          `_invalidate_from` has wiped every finished stage below it, leaving stems, lyrics
          and printable on disk with no manifest that admits to them. That is the whole
          project lost to a widened limit;
          `test_raising_the_duration_limit_does_not_destroy_a_finished_file_project` is it.

        Dropping the field bounds nothing less: every place a local file is actually opened
        still applies the current bound -- `create_project` through `_source_identity`,
        `_verify_resume_source` when a resume names a file, and `source.acquire` inside the
        stage whenever it does run.

        Removing a field moves the digest, so a file project made by an *earlier build of
        this same release* re-acquires once, and needs its file back to do it. Only a
        development checkout can hold such a project: this app has never been in a
        published release -- the repository carries no tag, and its first commit postdates
        the last published Plebian-OS release. The YouTube key is left byte-identical all
        the same, because moving it buys nothing and costs every project on this machine a
        re-download; `test_the_youtube_acquisition_key_survived_the_source_union` is what
        holds it still.
        """
        if self._source_kind() == "file":
            return (
                "kilix-playalong-file-intake:v1",
                self._acquire_file,
                {
                    "kind": "file",
                    "source_sha256": self.manifest["source"].get("url_sha256"),
                },
            )
        return (
            "yt-dlp:2026.8.19",
            self._download,
            {
                "url_sha256": self.manifest["source"].get("url_sha256"),
                "language": self.options.language,
                "max_duration": self.options.max_duration,
            },
        )

    def _download(self) -> tuple[list[Path], str]:
        source_dir = ensure_private_directory(self.project_dir / "source")
        media_path, subtitles, metadata = youtube.download(
            self.options.url,
            source_dir,
            language=self.options.language,
            max_duration=self.options.max_duration,
        )
        media_path.chmod(0o600)
        for subtitle in subtitles:
            subtitle.chmod(0o600)
        title = metadata.get("title")
        if not self.manifest["title"] and isinstance(title, str):
            self.manifest["title"] = _display_text(title)
        artist = metadata.get("artist") or metadata.get("uploader")
        if not self.manifest["artist"] and isinstance(artist, str):
            self.manifest["artist"] = _display_text(artist)
        duration = metadata.get("duration")
        if not isinstance(duration, int | float):
            raise ProviderFailedError("yt-dlp returned an invalid source duration")
        language = metadata.get("language")
        self.manifest["source"].update(
            {
                "video_id": metadata["id"],
                "duration": float(duration),
                "media_path": media_path.relative_to(self.project_dir).as_posix(),
                "media_sha256": sha256_file(media_path),
                "subtitle_paths": [
                    path.relative_to(self.project_dir).as_posix() for path in subtitles
                ],
                # The video's own language, when yt-dlp reports one. It is the only way
                # `lyrics.rank_subtitles` can tell a machine translation from an original
                # track whose filename does not admit to being one.
                "language": language if isinstance(language, str) else None,
            }
        )
        return [media_path, *subtitles], f"downloaded {float(duration):.1f}s source"

    def _file_spec(self) -> FileSource:
        """Where the user's file is, for the one stage that still needs to read it.

        `options.source_path` first, then the path recorded at creation. Both can be gone
        by the time a resume runs, and that is fine everywhere except here: once the
        acquisition stage is done the project owns a copy, its key is the digest recorded
        at creation, and nothing reopens the library again. That is what makes a project
        survive the user reorganising their music, which is the whole reason
        `source.acquire` copies instead of linking.
        """
        if self.options.source_path is not None:
            return file_source(self.options.source_path)
        recorded = self.manifest["source"].get("path")
        if isinstance(recorded, str) and recorded:
            return file_source(recorded)
        raise ProviderFailedError("this project needs its source file to finish acquisition")

    def _acquire_file(self) -> tuple[list[Path], str]:
        spec = self._file_spec()
        self._remember_secret(str(spec.path))
        self._remember_secret(str(spec.path.parent))
        self._remember_secret(os.path.realpath(spec.path))
        source_dir = ensure_private_directory(self.project_dir / "source")
        # No `on_bytes`, deliberately. `acquire` will report copied and total bytes if it is
        # asked, and this is the only stage in the run that could answer: yt-dlp is invoked
        # `--quiet`, `runner._bounded_communicate` returns only once the child has exited,
        # and Demucs writes a carriage-return bar no line-oriented reader would split. So
        # asking would put a measured fraction on the shortest stage and none on the two
        # that run for minutes, which teaches a user to read "no bar" as "nothing is
        # happening" exactly when something is.
        acquired = acquire(spec, source_dir, max_duration=self.options.max_duration)
        acquired.path.chmod(0o600)
        metadata = acquired.metadata
        if not self.manifest["title"]:
            self.manifest["title"] = _display_text(metadata.title)
        if not self.manifest["artist"] and metadata.artist:
            self.manifest["artist"] = _display_text(metadata.artist)
        artifacts = [acquired.path]
        self.manifest["source"].update(
            {
                "duration": metadata.duration,
                "container": metadata.container,
                "media_path": acquired.path.relative_to(self.project_dir).as_posix(),
                "media_sha256": sha256_file(acquired.path),
                # A local file publishes no caption tracks. The key is written anyway so
                # every later reader sees the same shape whichever arm produced the project.
                "subtitle_paths": [],
                "language": None,
            }
        )
        self._store_embedded_lyrics(acquired.lyrics_path, metadata.lyrics, artifacts)
        self._store_sidecar(spec, artifacts)
        return artifacts, f"copied {metadata.duration:.1f}s source from a local file"

    def _store_embedded_lyrics(
        self,
        path: Path | None,
        embedded: object,
        artifacts: list[Path],
    ) -> None:
        """Record the lyrics tag `source.acquire` lifted out of the media, if there was one."""
        tag = getattr(embedded, "tag", None)
        if path is None or not isinstance(tag, str):
            self.manifest["source"].pop("embedded_lyrics_path", None)
            self.manifest["source"].pop("embedded_lyrics_tag", None)
            return
        path.chmod(0o600)
        artifacts.append(path)
        self.manifest["source"]["embedded_lyrics_path"] = path.relative_to(
            self.project_dir
        ).as_posix()
        self.manifest["source"]["embedded_lyrics_tag"] = tag

    def _store_sidecar(self, spec: FileSource, artifacts: list[Path]) -> None:
        """Copy the `.lrc` sitting beside the user's file into the project, if one is there.

        Discovered here, in the one stage that has the user's directory open, and copied so
        that every later stage reads the project's own bytes. `find_lyrics_sidecar` prefers a
        track in the requested language, so the sidecar chosen is the one that matched the
        `--language` of the run that acquired the media. A later resume that changes
        `--language` does *not* re-scan the user's directory: the acquisition key holds no
        language for the file arm, on purpose, because a resume is not entitled to assume
        the library is still reachable.

        Nor does naming the file again. The file arm's key is the content digest and a
        re-supplied file has the same digest, so the stage stays cached -- measured, not
        assumed: `test_a_local_file_flows_through_the_pipeline_like_a_download` resumes with
        the file named and asserts every stage cached. Acquisition re-runs only when its own
        recorded artifacts are gone or the stage never finished, so an `.lrc` that appears
        beside the music *after* the project was made is not picked up by any resume. The
        way in for that file is `--lyrics`, which `resume` copies into the project through
        `_store_supplied_lyrics` and which the lyrics stage then prefers.
        """
        found = find_lyrics_sidecar(spec.path, language=self.options.language)
        if found is None:
            self.manifest["source"].pop("sidecar_path", None)
            return
        target = self.project_dir / "source" / "lyrics-sidecar.lrc"
        self._remember_secret(str(found))
        _copy_private(found, target, description="the .lrc beside the source file")
        artifacts.append(target)
        self.manifest["source"]["sidecar_path"] = target.relative_to(self.project_dir).as_posix()

    # ------------------------------------------------------- audio and stems

    def _source_path(self) -> Path:
        value = self.manifest["source"].get("media_path")
        if not isinstance(value, str):
            raise ProviderFailedError("project has no acquired source path")
        return project_artifact(self.project_dir, value)

    def _duration(self) -> float:
        value = self.manifest["source"].get("duration")
        if not isinstance(value, int | float):
            raise ProviderFailedError("project has no source duration")
        return float(value)

    def _normalize(self) -> tuple[list[Path], str]:
        output = self.project_dir / "media" / "normalized.wav"
        media.probe(self._source_path())
        media.normalize(self._source_path(), output)
        output.chmod(0o600)
        return [output], "44.1 kHz stereo PCM"

    def _normalized_path(self) -> Path:
        return self.project_dir / "media" / "normalized.wav"

    def _separate(self) -> tuple[list[Path], str]:
        outputs = separation.separate(
            self._normalized_path(),
            self.project_dir / "stems",
            model=self.options.model,
            device=self.options.device,
            allow_model_downloads=self.options.allow_model_downloads,
        )
        # Every stem is digested twice in the run that makes it: once here for the
        # manifest's track list and once by `finish_stage` for the stage's artifact list.
        # Measured on this machine, `sha256_file` reads 52.4 MB in 0.112s -- 470 MB/s -- so
        # six htdemucs_6s stems cost about 0.67s of a Demucs stage that runs for minutes.
        # Declined rather than plumbed: removing it means handing `finish_stage` a digest
        # its caller computed, and `state.stage_is_current` -- the freshness check every
        # resume is built on -- reads the artifact list those digests land in. Paying for a
        # second independent pass is worth more than the second. `_acquire_file` and
        # `_download` make the same trade for the source file, for the same reason.
        tracks: list[AudioTrack] = []
        labels = {
            "vocals": ("Vocals", "vocals"),
            "drums": ("Drums", "rhythm"),
            "bass": ("Bass", "bass"),
            "guitar": ("Guitar", "guitar"),
            "piano": ("Piano", "keys"),
            "other": ("Other", "other"),
        }
        for stem, path in outputs.items():
            label, kind = labels.get(stem, (stem.title(), "other"))
            tracks.append(
                {
                    "id": stem,
                    "label": label,
                    "kind": kind,
                    "path": path.relative_to(self.project_dir).as_posix(),
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                    "default_muted": False,
                }
            )
        self.manifest["tracks"] = tracks
        return list(outputs.values()), f"{len(outputs)} independently controllable stems"

    # ----------------------------------------------------- choosing the lyrics

    def _project_file(self, key: str) -> Path | None:
        value = self.manifest["source"].get(key)
        if not isinstance(value, str) or not value:
            return None
        path = project_artifact(self.project_dir, value)
        return path if path.is_file() else None

    def _supplied_lyrics(self, duration: float) -> tuple[LyricsDocument, str] | None:
        """The lyrics file the user handed us, read from the project's own copy.

        The copy, not `options.lyrics_path`, because both `create_project` and `resume`
        copy before this ever runs and the copy is what survives a resume with no
        `--lyrics`. Its digest is the digest of what they supplied.
        """
        path = self._project_file("lyrics_input_path")
        if path is None:
            return None
        return load_lyrics_document(path, duration=duration), sha256_file(path)

    def _sidecar_lyrics(self, duration: float) -> tuple[LyricsDocument, str] | None:
        path = self._project_file("sidecar_path")
        if path is None:
            return None
        document = load_lyrics_document(path, duration=duration, source_hint="sidecar-lrc")
        return document, sha256_file(path)

    def _embedded_lyrics(self, duration: float) -> tuple[LyricsDocument, str] | None:
        """The media's own lyrics tag, as `source.acquire` wrote it beside the copy."""
        path = self._project_file("embedded_lyrics_path")
        tag = self.manifest["source"].get("embedded_lyrics_tag")
        if path is None or not isinstance(tag, str):
            return None
        # `lyrics.read_bounded_text` rather than a second read-N+1-and-decode here: it
        # is the same seven steps and it holds the reasoning for one of them (read the
        # bound, never stat it -- a file that grows in between, or a path that is a
        # pipe, defeats a size check made before the bytes are taken). Reading it
        # through there also gains the NUL rejection and the BOM tolerance this arm
        # wrote without, neither of which changes a shipped input: `source.acquire`
        # wrote this file from text `_clean_lyrics` had already stripped, as plain
        # UTF-8 with no BOM. That last fact is also what keeps the digest still: with
        # no BOM to drop, `sha256_text` of the decoded text is `sha256_bytes` of the
        # file, so no existing project's lyrics stage key moves.
        text = read_bounded_text(
            path, limit=MAX_EMBEDDED_LYRICS_BYTES, what="the embedded lyrics tag"
        )
        # One-entry mapping so that `lyrics` -- not this module -- reads the language out
        # of the tag key. `lyrics.embedded_tag_key` is now the package's only vocabulary
        # for that and `source` defers to it, so a key that got this far cannot be
        # rejected here and no language is lost on the way through. What is left that can
        # still come back None is a tag whose text is blank once stripped; it is handed on
        # unchanged so that `parse_embedded_lyrics` refuses it in this module's own words
        # rather than in a second sentence invented here.
        selected = select_embedded_lyrics({tag: text}, language=self.options.language)
        embedded = selected or EmbeddedLyrics(tag=tag, text=text)
        return parse_embedded_lyrics(embedded, duration=duration), sha256_text(text)

    def _caption_lyrics(self, duration: float) -> tuple[LyricsDocument, str] | None:
        raw_paths = self.manifest["source"].get("subtitle_paths", [])
        if not isinstance(raw_paths, list):
            raise ProviderFailedError("project subtitle paths are invalid")
        paths = [
            project_artifact(self.project_dir, value)
            for value in raw_paths
            if isinstance(value, str)
        ]
        original = self.manifest["source"].get("language")
        choice = choose_subtitle_track(
            paths,
            self.options.language,
            original_language=original if isinstance(original, str) else None,
        )
        if choice is None:
            return None
        # `choice.source` distinguishes a human track from an auto-generated one and from a
        # machine translation. Recording all three as "youtube-captions" is exactly what
        # `SubtitleChoice` exists to stop, so the classification is the source id we write.
        document = load_lyrics_document(
            choice.path,
            duration=duration,
            source_hint=choice.source,
        )
        return document, sha256_file(choice.path)

    def _lyrics_plan(self) -> _LyricsPlan:
        if self._plan is None:
            self._plan = self._resolve_lyrics_plan()
        return self._plan

    def _resolve_lyrics_plan(self) -> _LyricsPlan:
        """Decide where the words come from, and whether their timing has to be measured.

        `auto` walks `_AUTO_LYRIC_ROUTES` and takes the first that answers, then falls
        through to transcription. An explicit choice takes that route or fails: see
        `_MISSING_LYRIC_SOURCE`.
        """
        duration = self._duration()
        requested = self.options.lyrics_source
        if requested == LYRIC_SOURCE_TRANSCRIBE:
            return _LyricsPlan(
                route=LYRIC_SOURCE_TRANSCRIBE, document=None, digest=None, aligns=False
            )
        readers: dict[str, Callable[[float], tuple[LyricsDocument, str] | None]] = {
            LYRIC_SOURCE_FILE: self._supplied_lyrics,
            LYRIC_SOURCE_SIDECAR: self._sidecar_lyrics,
            LYRIC_SOURCE_EMBEDDED: self._embedded_lyrics,
            LYRIC_SOURCE_CAPTIONS: self._caption_lyrics,
        }
        if requested != LYRIC_SOURCE_AUTO:
            found = readers[requested](duration)
            if found is None:
                raise InvalidInputError(_MISSING_LYRIC_SOURCE[requested])
            return self._plan_for(requested, found)
        for route in _AUTO_LYRIC_ROUTES:
            found = readers[route](duration)
            if found is not None:
                return self._plan_for(route, found)
        return _LyricsPlan(route=LYRIC_SOURCE_TRANSCRIBE, document=None, digest=None, aligns=False)

    def _plan_for(self, route: str, found: tuple[LyricsDocument, str]) -> _LyricsPlan:
        document, digest = found
        return _LyricsPlan(
            route=route,
            document=document,
            digest=digest,
            aligns=self.options.align_supplied_text and not document.has_timing,
        )

    # -------------------------------------------------------- keying the lyrics

    def _resolved_whisper_device(self) -> str:
        """Report the device `auto` will actually land on, mirroring `_auto_candidates`.

        The raw option is not enough for the stage key: `auto` on a machine that has since
        gained a GPU keeps the same option string while the worker switches backend and
        compute type, which is the "adaptive resolution changed" staleness the lyrics key
        exists to catch.
        """
        if self.options.device != "auto":
            return self.options.device
        return "cuda" if transcription.cuda_available() else "cpu"

    def _whisper_configuration(self) -> tuple[str, str]:
        """Resolve (model, device) exactly as the provider will when the stage runs.

        Deciding whether `auto` now resolves better than the recorded run means asking this
        machine, so a resume that turns out to be a no-op still costs the provider's memory
        and CUDA probes in this process. That is the price of catching the upgrade at all; the
        probes are skipped entirely when nothing this run does needs a transcript, and the
        heavy work -- weights, inference, torch -- still happens only in the worker
        subprocess.
        """
        model = transcription.resolve_model(
            self.options.whisper_model,
            device=self.options.device,
            model_cache=transcription.model_cache_path(),
            allow_model_downloads=self.options.allow_model_downloads,
        )
        return model, self._resolved_whisper_device()

    def _lyrics_keys(
        self,
    ) -> tuple[_LyricsPlan, dict[str, str] | None, tuple[dict[str, str], ...]]:
        """The lyrics plan, its Whisper key, and the recorded keys still good enough."""
        try:
            plan = self._lyrics_plan()
        except PlayalongError:
            # The plan cannot be made -- an unreadable lyrics file, a caption track that
            # will not parse. Keying degrades and the stage itself raises the real error
            # when it runs, which is where it can be recorded against the stage.
            undecidable = self._undecidable_whisper_keys()
            return _LyricsPlan(LYRIC_SOURCE_AUTO, None, None, False), *undecidable
        return plan, *self._whisper_keys(plan)

    def _whisper_keys(
        self,
        plan: _LyricsPlan,
    ) -> tuple[dict[str, str] | None, tuple[dict[str, str], ...]]:
        """Return the lyrics stage's Whisper key and the recorded keys still good enough.

        The key names the configuration this run would hand the worker -- resolved, never the
        raw option -- so a finished stage records what actually produced its lyrics and a
        repeat run keys identically. A recorded configuration is additionally accepted when it
        is *at least as good*, and "as good" ranks the whole ``(model, device)`` pair rather
        than each dimension on its own: the model decides first, the device only between
        equal models. The model is what a transcript is made of and the device only how fast
        it was made, so a strictly better recorded model is kept even from the worse device.

        What that guarantees, for the dimensions left on `auto`: a machine that shrank (RAM
        gone, GPU gone, weights evicted) keeps its transcript, and so does one that shrank on
        the model while growing a GPU -- `auto` resolving to (medium, cuda) never discards a
        recorded (large-v3, cpu). A re-run happens only when the pair this machine resolves
        strictly outranks the recorded one, and it records the pair it ran; from then on, with
        these options fixed, the recorded pair only ever ascends a finite ordering, so the
        stage settles and cannot alternate between two configurations. A dimension the caller
        pinned accepts only itself and does not relax the other one, so `--whisper-model` and
        `--device` mean exactly what they say in both directions -- including the single
        re-run that pinning something other than the recording costs.

        ``audio`` rides along in the same dict and is matched exactly, never ranked. The
        ranking above exists because a *machine* shrinks and grows underneath a user who
        never asked for either; which audio the transcriber listens to is a preference the
        user states, and a preference that quietly declines to take effect is worse than a
        re-run.

        This is a keying decision only. Which model the worker is handed stays the provider's
        call in `_lyrics`: the accepted set here is deliberately allowed to outrank what this
        machine can run, and nothing that is may be allowed to choose the run.
        """
        if not self._transcript_is_needed(plan):
            return None, ()
        try:
            model, device = self._whisper_configuration()
        except PlayalongError:
            return self._undecidable_whisper_keys()
        audio = self.options.audio_source
        # The `else` branch is the whole of the explicit-model rule: a requested model is
        # comparable only with itself, whether or not the adaptive order happens to rank it.
        models = (
            _no_worse_than(_WHISPER_QUALITY_ORDER, model)
            if self.options.whisper_model == transcription.AUTO_MODEL
            else (model,)
        )
        devices = (
            _no_worse_than(_WHISPER_DEVICE_ORDER, device)
            if self.options.device == "auto"
            else (device,)
        )
        # Pairs, ranked model-major -- not each dimension separately. Componentwise, a
        # machine that gains a GPU while its large-v3 weights are pruned resolves to
        # (medium, cuda) and matches the recorded (large-v3, cpu) on neither dimension, so a
        # finished better transcript would be replaced by a worse one and every later stage
        # re-run. A strictly better model is therefore accepted from either device wherever
        # the device is still `auto`; an equal model falls to the device rule unchanged,
        # which is what keeps a GPU appearing an upgrade.
        return {"model": model, "device": device, "audio": audio}, tuple(
            {"model": candidate, "device": backend, "audio": audio}
            for candidate in models
            for backend in (
                _WHISPER_DEVICE_ORDER
                if candidate != model and self.options.device == "auto"
                else devices
            )
            if (candidate, backend) != (model, device)
        )

    def _transcript_is_needed(self, plan: _LyricsPlan) -> bool:
        """True when this run would ask the worker for a transcript.

        Two reasons, and only two: transcription is the lyric source, or the words
        arrived with no timing and alignment is going to measure it from the singing.
        """
        return plan.route == LYRIC_SOURCE_TRANSCRIBE or plan.aligns

    def _undecidable_whisper_keys(self) -> tuple[dict[str, str], tuple[dict[str, str], ...]]:
        """Return keys for a machine whose Whisper configuration will not resolve at all.

        Nothing can be transcribed here, so this run cannot improve on what is recorded:
        accept the configuration that produced the existing lyrics under either device --
        the device it ran on is not recorded, and no run that could distinguish them is
        possible now -- and let the stage itself surface the provider's diagnostic if it has
        to run anyway. The audio source is taken from the receipt too, so a machine that has
        lost its provider does not re-key merely because the user changed a preference it
        cannot act on; the moment the provider resolves again, the ordinary key applies and
        the preference takes effect.
        """
        receipt = _recorded_whisper_receipt(self.manifest)
        if receipt is None:
            return {
                "model": self.options.whisper_model,
                "device": self.options.device,
                "audio": self.options.audio_source,
            }, ()
        return (
            {
                "model": receipt.model,
                "device": _WHISPER_DEVICE_ORDER[0],
                "audio": receipt.audio_source,
            },
            tuple(
                {"model": receipt.model, "device": value, "audio": receipt.audio_source}
                for value in _WHISPER_DEVICE_ORDER[1:]
            ),
        )

    def _lyrics_inputs(
        self,
        plan: _LyricsPlan,
        whisper: dict[str, str] | None,
    ) -> dict[str, object]:
        """Describe the lyrics stage under one candidate Whisper configuration.

        Every option that can change this stage's artifact is here, and nothing that
        cannot:

        * ``route`` -- resolved, so `auto` and the explicit choice it lands on share a key;
        * ``language`` -- picks the caption track, the tag language, and the decode language;
        * ``source_sha256`` -- the bytes of the file this route reads, and only that
          route's. The acquisition key spells a field the same way and means something
          else by it: there it is the digest of the *media*, here of the lyric document.
          Nothing reads them together, but a reader comparing two fingerprints by eye
          should know the shared spelling is a coincidence and not a shared value;
        * ``whisper`` -- the resolved model, device and audio source, or None when this run
          asks the worker for nothing at all.

        ``align_supplied_text`` is in the key, through ``whisper`` and not as a field of its
        own. It is one of exactly two things `_transcript_is_needed` consults, so for every
        route but transcription "this run aligns" and "``whisper`` is not None" are the same
        statement -- and where they are not, the option changes nothing: words that arrived
        with their own timing are never aligned whichever way the flag is set. A separate
        ``align`` field would therefore be a key dimension that cannot move an outcome, and
        a comment claiming otherwise. The two halves of
        `test_turning_alignment_off_re_runs_the_lyrics_stage` pin both directions: flipping
        the flag on untimed words re-runs the stage, and flipping it on timed ones does not.

        Not here, and why: the Demucs ``model`` changes the vocal stem, so it invalidates
        an *earlier* stage and cascades into this one; ``max_duration`` does the same on
        the URL arm, where it is part of the acquisition key, and on the file arm it moves
        no stage at all -- see `_acquisition_stage`, which explains why the file arm's copy
        is not re-keyed by a bound that was applied when it was admitted;
        ``allow_model_downloads`` is already folded into the model `auto` resolves to;
        ``title``, ``artist`` and ``max_fret`` never touch a lyric.
        """
        return {
            "route": plan.route,
            "language": self.options.language,
            "source_sha256": plan.digest,
            "whisper": whisper,
        }

    def _lyrics_alternates(
        self,
        plan: _LyricsPlan,
        whisper_alternates: tuple[dict[str, str], ...],
    ) -> list[object]:
        """The recorded lyrics keys this run would accept instead of re-running.

        One family, and deliberately only one: the same route and language under a
        Whisper configuration `_whisper_keys` ranks as no worse than this machine's.
        An earlier shape of this key was also offered here, for projects made before
        the source union added ``route`` and ``source_sha256``. It is gone: this app
        has never been in a published release, so the only manifests that could carry
        that shape are in development checkouts, which is the same judgement
        `_acquisition_stage` already makes about the file arm's key.
        """
        return [self._lyrics_inputs(plan, value) for value in whisper_alternates]

    # ------------------------------------------------------- running the lyrics

    def _transcribe_audio(self, output: Path) -> None:
        """Ask the provider for a transcript, unresolved.

        The request goes to the provider unresolved on purpose. `_whisper_keys` resolves
        the same call to key the stage, but keying may accept a recorded model this
        machine can no longer run and the run may not: only the provider applies its
        memory policy and its cached-weights check, and only it reports a missing
        `transcribe` extra before resolving any model at all. Re-resolving in the parent
        would also open a window between the parent's answer and the worker's.
        """
        transcription.transcribe(
            self._track_path("vocals"),
            output,
            language=self.options.language,
            model=self.options.whisper_model,
            device=self.options.device,
            allow_model_downloads=self.options.allow_model_downloads,
            audio_source=self.options.audio_source,
            mix=self._normalized_path(),
        )

    def _align(
        self,
        document: LyricsDocument,
        duration: float,
    ) -> tuple[AlignmentResult, LyricsDocument, Path]:
        """Time the user's own words from a transcript of the same audio."""
        transcript_path = self.project_dir / "lyrics" / "transcript.json"
        self._transcribe_audio(transcript_path)
        hypothesis = load_lyrics_document(transcript_path, duration=duration)
        result = align_lines(
            document.lines,
            hypothesis_from_cues(hypothesis.cues),
            audio_duration=duration,
        )
        return result, hypothesis, transcript_path

    def _lyrics(self) -> tuple[list[Path], str]:
        output = self.project_dir / "lyrics" / "lyrics.json"
        duration = self._duration()
        plan = self._lyrics_plan()
        artifacts = [output]
        record: dict[str, object] = {"route": plan.route}
        if plan.document is None:
            self._transcribe_audio(output)
            document = load_lyrics_document(output, duration=duration)
            note = f"transcribed the {self.options.audio_source} audio"
        else:
            document = plan.document
            note = f"lyrics from {plan.route}"
        if plan.aligns:
            document, aligned_note = self._apply_alignment(document, duration, artifacts, record)
            note = f"{note}; {aligned_note}"
        elif document.note:
            note = f"{note}; {document.note}"
        # The document is written with the provenance it carries, whichever route made
        # it: `authored` for a caption track, an .lrc, a tag that held one, and for a
        # transcript, whose stamps are the transcriber's own and which no forced
        # alignment placed; `measured` only where `_apply_alignment` just measured them;
        # `estimated` for a plain sheet whose spans this app spread out.
        write_lyrics(
            output,
            document.cues,
            source=document.source,
            language=document.language,
            timing=document.timing,
            alignment=document.alignment,
        )
        record.update(
            path=output.relative_to(self.project_dir).as_posix(),
            source=document.source,
            language=document.language,
            visible=True,
        )
        self.manifest["lyrics"] = record
        return artifacts, note

    def _apply_alignment(
        self,
        document: LyricsDocument,
        duration: float,
        artifacts: list[Path],
        record: dict[str, object],
    ) -> tuple[LyricsDocument, str]:
        """Replace invented cue spans with measured ones, or say why it did not.

        Returns the document the stage should write -- the one it was handed, unchanged,
        in every outcome but the accepted one -- and the line of detail the run reports.

        The accept/reject threshold is `AlignmentReport.usable`, and it is the aligner's own
        published predicate rather than a second one invented here: it is False exactly when
        the alignment fell below `alignment.USABLE_MATCHED_FRACTION`,
        `USABLE_MEAN_DISPLACEMENT` or `USABLE_UNALIGNED_RUN`, and the module documents that
        as "the caller should keep whatever timing it already had". Restating those numbers
        in this file would be a second policy that drifts from the first; quoting the
        predicate cannot.

        Three outcomes, and all three are recorded rather than silent:

        * accepted -- the words keep their own line breaks and gain measured times; the
          document's `timing` becomes ``measured`` and carries four of the report's numbers,
          and the source id loses its ``-estimated`` suffix because the timing is no longer
          invented;
        * rejected -- the evenly spread estimate is kept, suffix and `timing` of ``estimated``
          and all, and the report says how badly it scored;
        * impossible -- the words land anyway, estimate and suffix intact. Failing the stage
          would cost the user the words they already had over an improvement they merely
          asked for, and they supplied those words themselves.

        Three ways into that third case: the `transcribe` extra is not installed, so
        `is_available()` is False; it is installed but will not resolve a model to run,
        which raises `ProviderUnavailableError`; or an input sits past a bound, which raises
        `InvalidInputError`. The middle one is not hypothetical -- an installed extra with
        an empty weights cache and no `--allow-model-downloads` is the state of the machine
        this was found on, and `is_available()` says True there, which is why guarding on it
        alone lost the whole lyrics stage for a plain-text sheet.
        `test_alignment_survives_a_provider_that_will_not_run` is that case, and its second
        half is why skipping is not a one-way door on the default options: the skipped run
        keys the stage on the unresolved `auto`/`auto` pair (`_undecidable_whisper_keys`),
        so the first resume after weights arrive keys differently, runs, and aligns.

        Two things this deliberately does not do with the cues it returns.

        They go straight to `write_lyrics` and never through `lyrics._space_cues`, so the
        readability floor that module gives an *estimated* line -- raise a fast line's end
        so it can be read, never past the next start -- is not applied to a measured one.
        That is the one place the two lyric paths space cues differently, and it is right:
        stretching a span the transcript actually measured would make the file say the
        singer held a word longer than they did. The aligner's own sub-second floor still
        applies, and that one is a degeneracy floor, not a readability one.

        And `result.cues()` is the lossy view, chosen knowingly over `annotated_cues`.
        The annotated one keeps each word's ``origin`` -- whether its time was measured or
        interpolated between measured neighbours -- and nothing downstream can hold it:
        `types.LyricWord` has no field for it, `LYRICS_SCHEMA` and the C reader do not
        expect one, and no surface draws it. What survives is the aggregate: the whole
        report in ``record["alignment"]``, which is the manifest, and the four numbers of
        `lyrics.LyricAlignment` in the document itself, which is what a surface rendering
        a highlighted line can reach. Adding the per-word key is a schema change on both
        sides of the language boundary and belongs with the surface that would render it.

        Deliberately *not* in the list: a worker that resolves, starts and then fails or
        times out. That raises `ProviderFailedError` and still fails the stage, because it
        is the kind of failure that may not repeat, and only a stage left in `error` is
        re-run by the next resume --
        `state.stage_is_current` reports a stage current only when it is `done`. Swallowing
        it would record a finished stage under this run's key, and the alignment would never
        be attempted again for those options.
        """
        if not transcription.is_available():
            record["alignment"] = {"applied": False, "reason": "no transcriber"}
            return (
                document,
                "alignment skipped: timed lyrics are unavailable, so the spacing is estimated",
            )
        try:
            result, hypothesis, transcript_path = self._align(document, duration)
        except (InvalidInputError, ProviderUnavailableError) as error:
            # Both messages are safe to record. Every `InvalidInputError` on this path --
            # `align_lines` past MAX_TOKENS, MAX_REFERENCE_CHARS, MAX_ALIGNMENT_CELLS or
            # MAX_COMPARISON_CELLS, a reference with no words in it, a non-finite duration,
            # and `load_lyrics_document` on a transcript that will not read or parse -- is a
            # literal carrying counts and limits, with no path and no lyric text; so is every
            # `ProviderUnavailableError` the transcription provider raises. `public_error`
            # runs over them regardless.
            record["alignment"] = {"applied": False, "reason": public_error(str(error))}
            return document, f"alignment skipped: {public_error(str(error))}"
        artifacts.append(transcript_path)
        report = result.report
        record["alignment"] = {"applied": report.usable, **report.as_json()}
        if not report.usable:
            return (
                document,
                f"alignment rejected, estimated spacing kept: {report.summary()}",
            )
        # The transcript knows what language was sung; an imported sheet almost never does.
        language = document.language if document.language != "unknown" else hypothesis.language
        # The report's own numbers, four of them, copied and not derived again here.
        measured: LyricAlignment = {
            "matched_fraction": report.matched_fraction,
            "interpolated_words": report.interpolated_words,
            "mean_displacement": report.mean_displacement,
            "usable": report.usable,
        }
        # `lines` and `note` described the estimate that has just been replaced: the
        # words are timed now, so there is nothing left to hand an aligner and nothing
        # left to warn about.
        return (
            replace(
                document,
                cues=result.cues(),
                source=_aligned_source(document.source),
                language=language,
                timing="measured",
                alignment=measured,
                lines=(),
                note="",
            ),
            report.summary(),
        )

    # ------------------------------------------------------- tab and printable

    def _track_path(self, track_id: str) -> Path:
        for track in self.manifest["tracks"]:
            if track["id"] == track_id:
                return project_artifact(self.project_dir, track["path"])
        raise ProviderFailedError(f"project has no {track_id} stem")

    def _guitar_source(self) -> Path:
        try:
            return self._track_path("guitar")
        except ProviderFailedError:
            return self._track_path("other")

    def _transcribe_guitar(self) -> tuple[list[Path], str]:
        midi_path = self.project_dir / "midi" / "guitar.mid"
        notes_path = self.project_dir / "midi" / "guitar-notes.json"
        basic_pitch.transcribe(self._guitar_source(), midi_path, notes_path)
        count = validate_midi(midi_path)
        return [midi_path, notes_path], f"{count} MIDI note-on events"

    def _tablature(self) -> tuple[list[Path], str]:
        notes = load_note_events(self.project_dir / "midi" / "guitar-notes.json")
        events, omitted = infer_fingerings(
            notes,
            tuning=self.options.tuning,
            max_fret=self.options.max_fret,
        )
        if not events:
            raise ProviderFailedError("no playable guitar fingerings could be inferred")
        tab_path = self.project_dir / "tab" / "guitar-tab.json"
        ascii_path = self.project_dir / "exports" / "guitar-tab.txt"
        write_tab(
            tab_path,
            events,
            source_midi="midi/guitar.mid",
            tuning=self.options.tuning,
            max_fret=self.options.max_fret,
            omitted_notes=omitted,
        )
        private_write(
            ascii_path,
            render_ascii(
                events,
                title=self.manifest["title"],
                artist=self.manifest["artist"],
                labels=tuning_labels(self.options.tuning),
            ).encode("utf-8"),
        )
        self.manifest["tablature"] = {
            "path": tab_path.relative_to(self.project_dir).as_posix(),
            "ascii_path": ascii_path.relative_to(self.project_dir).as_posix(),
            "midi_path": "midi/guitar.mid",
            "visible": True,
            "tuning": list(self.options.tuning),
            "max_fret": self.options.max_fret,
        }
        return [tab_path, ascii_path], f"{len(events)} timed events; {omitted} notes omitted"

    def _export(self) -> tuple[list[Path], str]:
        lyrics = self.manifest["lyrics"]
        tablature = self.manifest["tablature"]
        if lyrics is None or tablature is None:
            raise ProviderFailedError("lyrics or tablature are unavailable")
        output = self.project_dir / "exports" / "playalong.html"
        render_printable(
            output,
            title=self.manifest["title"],
            artist=self.manifest["artist"],
            lyrics_path=project_artifact(self.project_dir, str(lyrics["path"])),
            tab_path=project_artifact(self.project_dir, str(tablature["path"])),
        )
        return [output], "self-contained printable HTML"


def run_new(
    options: PipelineOptions,
    *,
    progress: ProgressCallback | None = None,
) -> tuple[Path, ProjectManifest]:
    project_dir, manifest = create_project(options)
    return project_dir, Pipeline(project_dir, manifest, options, progress).run()


def _verify_resume_source(manifest: ProjectManifest, options: PipelineOptions) -> None:
    """Refuse a resume that names a different song from the one the project holds.

    Naming no source at all is fine and is the ordinary case for the file arm: the project
    recorded its source's identity when it was created and does not need the library back.
    Naming one is checked against that recording, for both arms and by the same rule -- a
    file that was renamed or moved has the same content and passes, a file whose content
    changed is a different source and does not.
    """
    spec = options.source_spec()
    if spec is None:
        return
    recorded_kind = manifest["source"].get("kind")
    if isinstance(recorded_kind, str) and recorded_kind != spec.kind:
        raise InvalidInputError("resume source is not the kind this project was created from")
    max_duration = options.max_duration
    recorded = manifest["source"].get("url_sha256")
    if recorded == _source_identity(spec, max_duration=max_duration):
        return
    # The digests differ. Before refusing, ask the question this check is
    # actually for -- "is this a different song?" -- rather than the byte
    # question. A project created before `validate_url` refused unprintable
    # characters can hold a URL with a stray carriage return from a pasted
    # link; its recorded digest covers that raw string, so the stripped URL a
    # current build produces can never match it, and the project becomes
    # unresumable through no fault of the user. A re-pasted URL carrying a
    # trailing space is the same situation arriving fresh.
    #
    # Comparing the recorded URL to the offered one with surrounding
    # whitespace removed answers the real question and moves no digest: the
    # recording is untouched, and nothing is fetched with the loosened value --
    # `youtube.validate_url` has already gated the string the spec carries, and
    # that is the string any provider is handed.
    recorded_url = manifest["source"].get("url")
    if (
        isinstance(spec, YouTubeSource)
        and isinstance(recorded_url, str)
        and recorded_url.strip() == spec.url.strip()
    ):
        return
    raise InvalidInputError("resume source does not match the project's source fingerprint")


def resume(
    project_dir: Path,
    options: PipelineOptions,
    *,
    progress: ProgressCallback | None = None,
) -> ProjectManifest:
    _validate_options(options, require_rights=False)
    manifest = load_manifest(project_dir)
    authorization = manifest["source"].get("authorization")
    if not isinstance(authorization, dict) or authorization.get("confirmed") is not True:
        raise RightsConfirmationRequired("project has no recorded permission confirmation")
    _verify_resume_source(manifest, options)
    if options.lyrics_path is not None:
        if not options.lyrics_path.is_file():
            raise InvalidInputError("the supplied lyrics file does not exist")
        _store_supplied_lyrics(project_dir, manifest, options)
    # A blank title means "leave whatever is recorded", which is what an unspecified
    # `--title` sends; renaming is why the tablature and export keys read the manifest.
    if options.title:
        manifest["title"] = _display_text(options.title)
    if options.artist:
        manifest["artist"] = _display_text(options.artist)
    manifest["settings"].update(
        separation_model=options.model,
        language=options.language,
        whisper_model=options.whisper_model,
        device=options.device,
        max_duration=options.max_duration,
        tuning=list(options.tuning),
        max_fret=options.max_fret,
        lyrics_source=options.lyrics_source,
        audio_source=options.audio_source,
        align_supplied_text=options.align_supplied_text,
    )
    save_manifest(project_dir, manifest)
    return Pipeline(project_dir, manifest, options, progress).run()


def list_projects() -> list[tuple[Path, ProjectManifest]]:
    result: list[tuple[Path, ProjectManifest]] = []
    paths = projects_home().iterdir()
    for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True):
        if not path.is_dir():
            continue
        try:
            result.append((path, load_manifest(path)))
        except PlayalongError:
            continue
    return result
