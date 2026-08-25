"""Describe every backend option as it stands *on this machine*.

`optionspec` defines what a description is; this module is the single place that
decides what the description says here, so that the browser studio, the native
surface and `--help` cannot disagree about which options exist, what they default
to, or why one of them is greyed out.

Three rules shape everything below.

**Availability is measured, never asserted.** Every `available=False` in the
document came from a probe of this machine -- `transcription.is_available()`,
`separation.is_available()`, `basic_pitch.is_available()`,
`media.require_media_tools()`, an `importlib` spec lookup, or a real call into
`transcription.resolve_model`. "faster-whisper is not installed" and "no cached
weights and downloads are not permitted" are two different sentences here because
they have two different fixes, and a screen that renders one for the other sends
the user to the wrong command.

**Nothing is hidden, and nothing is greyed further than it has to be.** An
option that cannot run is still described, with the reason attached: a hidden
option reads as a missing feature. The same argument bounds greying itself. An
option is greyed only when *nothing* behind it can run here, never merely because
its most convenient choice cannot -- `auto` scans four of the sixteen
faster-whisper models, so greying the whole transcription-model control when
`auto` finds nothing cached would tell a user who has `distil-large-v3` weights on
disk that a feature they can use today is missing. And when an option *is* greyed,
every one of its choices is greyed with it (`_greyed_choices`, applied to every
option in `build_options_document`), because a greyed control cannot offer an
escape: a reason may only name a way out that is still on the screen.
`test_a_greyed_control_never_leaves_a_live_choice_behind`,
`test_an_available_option_always_leaves_something_to_pick` and
`test_a_blocked_auto_still_offers_the_models_its_reason_points_at` are what make
that a rule rather than a paragraph.

**Describing is side-effect free.** No directory is created, no subprocess is
started, nothing is downloaded. The model cache is asked for by
`transcription.model_cache_path`, which is the provider's non-creating accessor and
exists for exactly this caller: the path the weights actually land on, named without
`ensure_private_directory`, because merely opening an intake screen must not write to
the user's disk.

The document is also the contract in the other direction: every option id names a
field of `pipeline.PipelineOptions` (see `BACKEND_FIELDS`), so a surface can post
the form straight at the backend. Both directions are checked rather than
described: `test_every_option_id_names_a_pipeline_field` fails on an option id
with no field to receive it, and `test_the_document_and_the_backend_describe_the_same_fields`
on a backend field no option offers, which is a setting no surface can reach.

**Nothing here asks whether the rest of this package exists.** That is a decision,
not an omission. The source union, the two lyric readers and the force-aligner
ship in this wheel, and `pipeline` binds every one of their entry points at module
level -- `from .alignment import align_lines`, `from .lyrics import
find_lyrics_sidecar, parse_embedded_lyrics`, `from .source import file_source` --
so importing *this* module has already forced all four to exist. A build missing
one raises `ImportError` before `build_options_document` can be reached, and a
build whose option ids have outrun `PipelineOptions` fails the two tests named
above. Neither absence is a state a runnable build can be in, so an availability
branch describing one would describe a build the gate refuses, and a branch that
only runs against a red suite is not a safeguard. `_has_module` stays, because
`yt_dlp` and `ctranslate2` are genuinely optional extras a green build can be
without -- the distinction is "outside this wheel", not "not written yet".

What the two arms of the source union *are* is still the document's business, and
one fact about them cannot be carried by `available` at all: filling both is a
contradiction, not an invalid value. `OptionGroup.exclusive` is where that is
written down, so a surface can refuse it while the user types instead of learning
it from `InvalidInputError` after a submit.

`tuning_pitches` is the one value this module rewrites on the way to that
dataclass, so it enforces the backend's own rule -- six *distinct*, *ascending*
pitches in 0..127, the rule at `pipeline._validate_options` -- rather than a looser
one of its own. The two are separate code and are kept from drifting by
`test_tuning_pitches_accepts_exactly_what_the_pipeline_accepts`, which runs both
over one corpus of candidate tunings and fails on any disagreement.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from .errors import InvalidInputError, PlayalongError
from .lyrics import MAX_LYRICS_BYTES
from .optionspec import Choice, OptionGroup, OptionsDocument, OptionSpec
from .pipeline import (
    LYRIC_SOURCE_AUTO,
    LYRIC_SOURCE_CAPTIONS,
    LYRIC_SOURCE_EMBEDDED,
    LYRIC_SOURCE_FILE,
    LYRIC_SOURCE_SIDECAR,
    LYRIC_SOURCE_TRANSCRIBE,
    valid_tuning,
)
from .providers import basic_pitch, media, separation, transcription
from .source import MAX_FILE_BYTES, format_size
from .tablature import STANDARD_TUNING, tuning_labels
from .util import public_error

#: Named tunings a surface can offer. It has replaced `cli`'s copy: `cli` imports
#: this mapping and takes its defaults from this document, so there is one list of
#: named tunings rather than two that can disagree. The edge stays one-way -- `cli`
#: may import this module, this module may never import `cli`, or the cycle closes.
#: `test_named_tunings_match_the_cli` now checks the surface instead of the copy:
#: every name here is accepted by `--tuning` and reaches the backend as the pitches
#: here, and a name that is not here is refused by that flag's `choices`.
TUNINGS: Mapping[str, tuple[int, ...]] = {
    "standard": STANDARD_TUNING,
    "drop-d": (38, 45, 50, 55, 59, 64),
    "dadgad": (38, 45, 50, 55, 57, 62),
}

#: option id -> the `pipeline.PipelineOptions` field it fills. The mapping is
#: identity today, which is the point: a surface can build the dataclass straight
#: from `defaults()` (`tuning` excepted, see `tuning_pitches`). It exists as a
#: table because the two namespaces are separate contracts, and this is where
#: their drift is detected instead of being discovered by a user.
BACKEND_FIELDS: Mapping[str, str] = {
    "url": "url",
    "source_path": "source_path",
    "title": "title",
    "artist": "artist",
    "model": "model",
    "device": "device",
    "lyrics_source": "lyrics_source",
    "lyrics_path": "lyrics_path",
    "language": "language",
    "whisper_model": "whisper_model",
    "audio_source": "audio_source",
    "align_supplied_text": "align_supplied_text",
    "tuning": "tuning",
    "max_fret": "max_fret",
    "max_duration": "max_duration",
    "allow_model_downloads": "allow_model_downloads",
    "rights_confirmed": "rights_confirmed",
}

_SYNC = "run `uv sync --all-extras` from the repository"
_WHISPER_MISSING = f"faster-whisper is not installed; {_SYNC} to add the transcribe extra"
_DEMUCS_MISSING = f"Demucs is not installed; {_SYNC}"
_BASIC_PITCH_MISSING = f"Basic Pitch is not installed; {_SYNC}"
_YT_DLP_MISSING = f"yt-dlp is not installed; {_SYNC}"
#: Why `auto` -- and only `auto` -- cannot pick a model. It is attached to the
#: `auto` choice and never to the option, because the escape it names is one of
#: the option's other choices: greying the control would forbid the very thing the
#: sentence asks for. `test_a_blocked_auto_still_offers_the_models_its_reason_points_at`
#: pins both halves of that -- the clause, and the models being live behind it.
_NO_CACHED_WEIGHTS = (
    "'auto' found no cached weights among the models it scans, and model downloads are "
    "not permitted; turn on 'Allow model downloads', or select a model whose weights "
    "you already have"
)
#: The provider names no model under either download answer, so nothing here can
#: be preselected and nothing can be promised: this one does grey the control.
_NO_USABLE_MODEL = (
    "faster-whisper is installed but could not choose a model for this machine; "
    "`kilix-playalong doctor` reports what it found"
)

#: Mirrors the bounds `pipeline._validate_options` enforces. Drift here would ship
#: a preselected value the backend rejects, which is the one bug this file exists
#: to prevent, so `test_document_defaults_satisfy_the_pipeline_validator` and
#: `test_numeric_bounds_match_the_pipeline_validator` run the real validator
#: against these numbers rather than trusting them.
_MIN_DURATION = 1.0
_MAX_DURATION = 2.0 * 60 * 60
_MIN_FRET = 12
_MAX_FRET = 30


def tuning_pitches(value: object) -> tuple[int, ...]:
    """Resolve a `tuning` option value to the six MIDI pitches the backend takes.

    The `tuning` option's default is a *name*, not a pitch tuple, because a
    default has to be a member of `choices` and a surface has to be able to show
    "Drop D" rather than six integers. That makes this the one place a name turns
    into what `PipelineOptions.tuning` accepts; a custom six-pitch sequence is
    passed through so a surface can offer free entry without a second code path.

    Passing a sequence through is only safe if it is checked against the rule the
    backend will apply to it. Returning a tuple is this function's signal that the
    value is legal, so a sequence accepted here and refused by `create_project` is
    exactly the "a form ships a value the backend rejects" bug the module exists to
    prevent. So the rule is not restated here: `pipeline.valid_tuning` is the body
    `_validate_options` itself calls, and calling it is what makes the two answers
    one answer rather than two that a corpus test has to keep reconciled.

    The length is checked before the sequence is materialised, so a lazy
    `Sequence` -- `range(10**10)` is the cheap example -- is rejected rather than
    allocated. Non-integers are rejected outright, which is *stricter* than the
    validator rather than a restatement of it: measured, `_validate_options` accepts
    a six-float tuple, fractional or not, and meets a tuple of strings as a
    `TypeError` (`bool` it rejects, as this does). Both differences are in the safe
    direction -- everything this function returns is a tuple of `int` the backend
    accepts -- and that gap is this function's own, being what lets it promise a
    `tuple[int, ...]`. The message differs deliberately too: one is about a form
    value that may be a name, the other about a backend argument.
    """

    if isinstance(value, str):
        named = TUNINGS.get(value)
        if named is None:
            raise InvalidInputError("unknown tuning name")
        return named
    if isinstance(value, Sequence) and not isinstance(value, str | bytes) and len(value) == 6:
        candidate = tuple(value)
        if all(isinstance(pitch, int) and not isinstance(pitch, bool) for pitch in candidate):
            pitches = tuple(int(pitch) for pitch in candidate)
            if valid_tuning(pitches):
                return pitches
    raise InvalidInputError("tuning must be a known name or six ascending MIDI pitches")


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _media_tools_reason() -> str | None:
    """Reuse the provider's own ffmpeg/ffprobe check -- the one `doctor` reports."""

    try:
        media.require_media_tools()
    except PlayalongError as error:
        return public_error(str(error))
    return None


def _reason(*candidates: str | None) -> str | None:
    present = [candidate for candidate in candidates if candidate]
    return "; ".join(present) if present else None


def _auto_model(*, allow_model_downloads: bool) -> str | None:
    """Ask the provider what `auto` means here, or None when nothing is obtainable.

    The policy stays where it lives. This calls `resolve_model` twice with the two
    download answers so the note can say both what runs now and what enabling
    downloads would change; a failure is a described, greyed-out choice, never a
    crash in an intake screen.
    """

    if not transcription.is_available():
        return None
    try:
        return transcription.resolve_model(
            transcription.AUTO_MODEL,
            device="auto",
            model_cache=transcription.model_cache_path(),
            allow_model_downloads=allow_model_downloads,
        )
    except (PlayalongError, OSError):
        return None


@dataclass(frozen=True)
class _Machine:
    """Every probe this document depends on, taken once."""

    media_tools_reason: str | None
    yt_dlp: bool
    demucs: bool
    guitar: bool
    whisper: bool
    #: None when this machine cannot be asked. Enumerating devices is ctranslate2's
    #: job, so without it installed this module does not ask at all rather than
    #: record a `False` it cannot stand behind -- on the screen those are two
    #: different sentences, "no CUDA device is visible" and "not verified here".
    #: The provider may answer `False` without importing ctranslate2 when no driver
    #: is present, but that shortcut only ever produces the negative, so the guard
    #: on `_has_module` is what keeps "not installed" unknown rather than "no GPU".
    cuda: bool | None
    cached_model: str | None
    downloadable_model: str | None
    allow_model_downloads: bool

    @property
    def whisper_reason(self) -> str | None:
        """Why the *automatic* model choice cannot pick a model here.

        Three situations, three sentences, because they take three different
        commands: install the extra, allow the download, or look at a machine
        that answers neither. Which of the last two applies is decided by whether
        the provider can name a model when downloads *are* permitted, not by the
        answer this document was resolved under -- with downloads off and the
        provider naming nothing either way, "turn on 'Allow model downloads'" is
        an instruction that would fail too, and `_NO_USABLE_MODEL` is the honest
        sentence. `test_a_provider_that_answers_nothing_does_not_blame_the_toggle`
        is that distinction.

        This is not the reason the *control* is greyed: see `transcription_reason`.
        """

        if not self.whisper:
            return _WHISPER_MISSING
        if self.resolved_model is not None:
            return None
        if self.downloadable_model is None:
            return _NO_USABLE_MODEL
        return _NO_CACHED_WEIGHTS

    @property
    def resolved_model(self) -> str | None:
        return self.downloadable_model if self.allow_model_downloads else self.cached_model

    @property
    def fallback_model(self) -> str | None:
        """A named model to preselect when `auto` has nothing to load here.

        `resolve_model` with downloads permitted answers with the head of the
        provider's own candidate list, which is the model `auto` becomes the
        moment the user ticks the box beside this one. Preselecting it is not a
        claim that it runs: the offline pass scanned that same candidate list
        moments earlier and found nothing cached, so this is a model whose weights
        are missing, and `_whisper_default`'s note says exactly that. It is a
        claim about what to have highlighted while the user decides between
        allowing a download and naming a model of their own -- nothing more.
        """

        if not self.whisper or self.resolved_model is not None:
            return None
        if self.downloadable_model in transcription.SUPPORTED_MODELS:
            return self.downloadable_model
        return None

    @property
    def transcription_reason(self) -> str | None:
        """Why *nothing* here can transcribe -- not merely why `auto` cannot.

        This is the reason the control carries, and it is deliberately weaker
        than `whisper_reason`. They differ in exactly one situation: faster-whisper
        is installed, downloads are off, nothing `auto` scans is cached, and the
        provider can still name a model to preselect if downloads were allowed.
        `auto` scans `transcription.QUALITY_ORDER`, a strict subset of
        `transcription.SUPPORTED_MODELS`
        (`test_the_gap_between_auto_and_the_model_list_is_real`), so "nothing
        `auto` scans is cached" is not "nothing is cached": greying the control on
        it tells a user with `distil-large-v3` on disk that transcription is
        unavailable when it is one click away. Over-greying is not the safe
        direction here -- it reads as a missing feature and it is silent.
        """

        if not self.whisper:
            return _WHISPER_MISSING
        if self.resolved_model is None and self.fallback_model is None:
            return self.whisper_reason
        return None


def _probe(*, allow_model_downloads: bool) -> _Machine:
    whisper = transcription.is_available()
    cuda: bool | None = None
    if _has_module("ctranslate2"):
        cuda = transcription.cuda_available()
    return _Machine(
        media_tools_reason=_media_tools_reason(),
        yt_dlp=_has_module("yt_dlp"),
        demucs=separation.is_available(),
        guitar=basic_pitch.is_available(),
        whisper=whisper,
        cuda=cuda,
        cached_model=_auto_model(allow_model_downloads=False),
        downloadable_model=_auto_model(allow_model_downloads=True),
        allow_model_downloads=allow_model_downloads,
    )


def _url_source_reason(machine: _Machine) -> str | None:
    """Why the link arm cannot be used here."""

    return _reason(None if machine.yt_dlp else _YT_DLP_MISSING, machine.media_tools_reason)


def _file_source_reason(machine: _Machine) -> str | None:
    """Why the local-file arm cannot be used here.

    ffprobe and ffmpeg are the whole of it: reading a local file needs no extra,
    so the arm is as available as the media tools that measure the file. Kept as a
    function rather than inlined because it is shared with the lyric sources that
    can only be fed by this arm -- an embedded tag and a sidecar `.lrc` are both
    read from the file the source arm opened, so offering them while that arm is
    greyed offers a path with no way in, and one expression makes that automatic.
    """

    return machine.media_tools_reason


def _source_group(machine: _Machine) -> OptionGroup:
    """The two arms of the source union, described separately.

    `source.parse_source` reads *one* string as either arm, so a surface is free
    to render these as a single box with a file picker beside it. They are two
    options here because they are two backend fields -- `PipelineOptions.url` and
    `PipelineOptions.source_path`, both of which now exist -- and because they are
    greyed for different reasons: the link arm needs yt-dlp, the file arm needs
    ffprobe and ffmpeg. Filling both is a contradiction rather than an invalid
    value, so it is not an availability -- greying an arm would have to happen
    before the user has typed anything -- and it is declared as the group's
    `exclusive` set instead. `pipeline.PipelineOptions.source_spec` is what
    actually refuses it, and
    `test_the_exclusive_set_is_the_one_the_backend_refuses` runs the two together
    so the sentence on the screen cannot outlive the rule behind it.
    """

    url_reason = _url_source_reason(machine)
    file_reason = _file_source_reason(machine)
    return OptionGroup(
        id="source",
        label="Source",
        help=(
            "Where the music comes from. Fill exactly one of the two: a link is fetched "
            "with yt-dlp, a file is read from disk with ffmpeg and never leaves it."
        ),
        exclusive=(("url", "source_path"),),
        options=(
            OptionSpec(
                id="url",
                label="Link",
                type="text",
                default="",
                stage="source",
                help="A single YouTube video link. Playlists and live streams are refused.",
                available=url_reason is None,
                unavailable_reason=url_reason,
            ),
            # The size ceiling is quoted from the constant that enforces it, not
            # retyped: `source.inspect_file` refuses a larger file with
            # `format_size(MAX_FILE_BYTES)` in its own sentence, and a 700 MB video
            # is an ordinary thing to pick. A bound no screen names is a refusal
            # the user was never warned of, which is the failure the availability
            # machinery exists to prevent, one rule over.
            # `test_the_file_size_ceiling_on_the_screen_is_the_one_that_refuses`
            # keeps the sentence and the constant from drifting apart.
            OptionSpec(
                id="source_path",
                label="Audio or video file",
                type="path",
                default=None,
                stage="source",
                help=(
                    "A local file to take the audio from instead of downloading one. "
                    f"Up to {format_size(MAX_FILE_BYTES)}, refused before the project "
                    "directory is made."
                ),
                available=file_reason is None,
                unavailable_reason=file_reason,
            ),
            OptionSpec(
                id="title",
                label="Title",
                type="text",
                default="",
                stage="source",
                help="Leave blank to use the title the source reports.",
            ),
            OptionSpec(
                id="artist",
                label="Artist",
                type="text",
                default="",
                stage="source",
                help="Leave blank to use the artist or uploader the source reports.",
            ),
        ),
    )


def _separation_group(machine: _Machine) -> OptionGroup:
    demucs_reason = None if machine.demucs else _DEMUCS_MISSING
    device_reason = (
        None
        if machine.demucs or machine.whisper
        else "neither Demucs nor faster-whisper is installed, so there is nothing to place "
        "on a device"
    )
    device_note = ""
    if machine.cuda is True:
        device_note = "'auto' uses the CUDA device visible on this machine."
    elif machine.cuda is False:
        device_note = "'auto' uses the CPU: no CUDA device is visible on this machine."
    return OptionGroup(
        id="separation",
        label="Separation",
        help="Splitting the mix into stems, which every later stage reads.",
        options=(
            OptionSpec(
                id="model",
                label="Demucs model",
                type="enum",
                default="htdemucs_6s",
                stage="separate",
                help="The stem separator. Six-stem separation is what gives a guitar stem.",
                choices=tuple(
                    Choice(
                        value=name,
                        label=name,
                        help="Six stems: vocals, drums, bass, guitar, piano, other.",
                        available=demucs_reason is None,
                        unavailable_reason=demucs_reason,
                    )
                    for name in sorted(separation.SUPPORTED_MODELS)
                ),
                advanced=True,
                available=demucs_reason is None,
                unavailable_reason=demucs_reason,
            ),
            OptionSpec(
                id="device",
                label="Device",
                type="enum",
                default="auto",
                stage="separate",
                help=(
                    "Where separation and lyric transcription run. 'auto' takes the GPU "
                    "when one is visible and the CPU otherwise."
                ),
                choices=(
                    Choice(
                        value="auto",
                        label="Automatic",
                        help="Use a CUDA device if this machine has one, otherwise the CPU.",
                    ),
                    Choice(value="cpu", label="CPU", help="Always the CPU. Slow but universal."),
                    Choice(
                        value="cuda",
                        label="CUDA GPU",
                        help=(
                            "Always a CUDA device."
                            if machine.cuda is not None
                            else "Always a CUDA device. Not verified here: CUDA is probed "
                            "through ctranslate2, which is not installed."
                        ),
                        available=machine.cuda is not False,
                        unavailable_reason=(
                            None
                            if machine.cuda is not False
                            else "no CUDA device is visible to this machine's runtime"
                        ),
                    ),
                ),
                advanced=True,
                available=device_reason is None,
                unavailable_reason=device_reason,
                default_is_resolved=bool(device_note),
                resolved_note=device_note,
            ),
        ),
    )


def _lyrics_source_arms(machine: _Machine) -> tuple[Choice, ...]:
    """The ways of getting lyrics, each gated by everything it needs.

    "Needs" is the source arm that feeds it: an embedded tag and a sidecar `.lrc`
    are read from a local file, and captions are pulled by the same yt-dlp that
    fetches the link, so each folds in the reason its own source arm is greyed.
    Without that, this build offers lyric sources that no reachable source arm can
    feed -- `test_a_lyric_source_is_only_as_available_as_the_arm_that_feeds_it`.

    The values are `pipeline`'s own `LYRIC_SOURCE_*` constants rather than string
    literals that happen to match. That is not a style preference: the alternative
    is two vocabularies drifting silently, and this module already imports from
    `pipeline` (the edge runs one way, so there is no cycle to close).
    `test_the_lyric_sources_offered_are_the_backend_vocabulary` covers the half a
    shared constant cannot -- a source the backend grows and this form never
    offers, which is a feature nobody can reach.
    """

    captions_reason = _url_source_reason(machine)
    # Both readers ship in this wheel and `pipeline` binds them by name at import,
    # so the arm that feeds them is the only thing that can grey either -- which is
    # why one expression reaches both choices below.
    local_file_reason = _file_source_reason(machine)
    return (
        Choice(
            value=LYRIC_SOURCE_CAPTIONS,
            label="The source's captions",
            help="Subtitles published with the video, in the language chosen below.",
            available=captions_reason is None,
            unavailable_reason=captions_reason,
        ),
        Choice(
            value=LYRIC_SOURCE_FILE,
            label="A file I supply",
            help="The lyrics file named below: LRC, SRT, WebVTT, JSON, or plain text.",
        ),
        Choice(
            value=LYRIC_SOURCE_EMBEDDED,
            label="The file's own lyrics tag",
            help=(
                "Lyrics stored in the media file's own metadata. Local files only: a "
                "download has no tag to read."
            ),
            available=local_file_reason is None,
            unavailable_reason=local_file_reason,
        ),
        Choice(
            value=LYRIC_SOURCE_SIDECAR,
            label="A .lrc beside the file",
            help=(
                "A timed .lrc sitting beside the source file. Local files only, and "
                "found without you naming it."
            ),
            available=local_file_reason is None,
            unavailable_reason=local_file_reason,
        ),
        Choice(
            value=LYRIC_SOURCE_TRANSCRIBE,
            label="Transcribe the singing",
            help="Listen to the vocals and write the words down, with timings.",
            available=machine.transcription_reason is None,
            unavailable_reason=machine.transcription_reason,
        ),
    )


def _lyrics_source_choices(machine: _Machine) -> tuple[Choice, ...]:
    """The arms, led by the automatic choice that degrades through them.

    `auto` is exactly as available as the arms it falls through: it is not a way
    of getting lyrics of its own, so if every arm is greyed it has nothing to
    degrade to and saying otherwise would preselect a dead end. The supplied-file
    arm has no gate on this build, so today that branch never fires --
    `test_the_automatic_lyric_choice_dies_with_its_last_arm` drives it directly
    rather than waiting for a machine that can reach it.
    """

    arms = _lyrics_source_arms(machine)
    auto_reason = (
        None
        if any(arm.available for arm in arms)
        else "nothing on this machine can supply lyrics, so there is nothing to fall back to"
    )
    return (
        Choice(
            value=LYRIC_SOURCE_AUTO,
            label="Whatever is best available",
            help=(
                "A supplied file first, then the source's own captions, then transcription. "
                "Degrades to whatever this machine can actually do."
            ),
            available=auto_reason is None,
            unavailable_reason=auto_reason,
        ),
        *arms,
    )


def _whisper_note(machine: _Machine) -> str:
    resolved = machine.resolved_model
    if resolved is None:
        return ""
    strongest = "the strongest model this machine's memory and device allow"
    if not machine.allow_model_downloads:
        note = f"'auto' selects {resolved} here: the strongest model whose weights are cached."
        if machine.downloadable_model not in (None, resolved):
            note += f" Allowing model downloads would use {machine.downloadable_model} instead."
        return note
    note = f"'auto' selects {resolved} here: {strongest}."
    if machine.cached_model == resolved:
        return note + " Its weights are already cached."
    if machine.cached_model is None:
        return note + " No weights are cached yet, so the first run downloads them."
    return (
        f"{note} Its weights are not cached yet ({machine.cached_model} is), "
        "so the first run downloads them."
    )


def _whisper_default(machine: _Machine) -> tuple[str, str]:
    """The preselected transcription model and what the preselection means here.

    `auto` when the provider can resolve it, which is the ordinary case. When it
    cannot but a named model still can, the control stays live (see
    `_Machine.transcription_reason`) and something selectable has to be
    preselected, because an available control whose preselected value is greyed
    out is a dead end -- `test_an_available_option_never_preselects_an_unavailable_choice`.
    The note then says plainly that the preselected model is not cached either, so
    the preselection is not read as a promise that it runs.
    """

    if machine.resolved_model is not None:
        return transcription.AUTO_MODEL, _whisper_note(machine)
    fallback = machine.fallback_model
    if fallback is None:
        return transcription.AUTO_MODEL, ""
    return fallback, (
        f"'auto' has nothing to load here, so {fallback} is preselected: it is what 'auto' "
        "would pick once model downloads are allowed. Its weights are not cached either -- "
        "allow downloads, or choose a model you already have weights for."
    )


def _whisper_choice_help(model: str) -> str:
    """Both facts about a model name, not whichever one matches first.

    `distil-small.en` is a distilled model *and* English-only, and the second half
    is the one that ruins a Spanish song. Testing the prefix first and returning
    dropped it: `test_every_english_only_model_says_so` is the check.
    """

    notes = []
    if model.startswith("distil-"):
        notes.append("A distilled model: close to its parent's accuracy at a fraction of the cost.")
    if model.endswith(".en"):
        notes.append("English only. Do not pick this for any other language.")
    if not notes:
        notes.append("Larger models hear more and cost more; weights must be cached or downloaded.")
    return " ".join(notes)


def _audio_source_default(machine: _Machine) -> tuple[str, str]:
    """Pick an audio source that exists here, in the provider's own vocabulary.

    The provider's `DEFAULT_AUDIO_SOURCE` is the vocal stem, and its reasoning is
    the right reasoning -- but it assumes a stem, and without Demucs there is not
    one. Preferring a default that reads well over one that runs is how a form
    ships a preselection that fails at stage three.
    """

    if machine.demucs:
        return (
            transcription.DEFAULT_AUDIO_SOURCE,
            f"'{transcription.DEFAULT_AUDIO_SOURCE}' is used: an isolated voice transcribes "
            "better than a full mix, and it costs one decoding pass rather than two.",
        )
    return (
        transcription.AUDIO_SOURCE_MIX,
        "Demucs is not installed here, so there is no vocal stem to listen to and the full "
        "mix is used.",
    )


def _audio_source_help(value: str) -> str:
    if value == transcription.AUDIO_SOURCE_VOCALS:
        return "The separated vocal stem. Usually the cleanest thing to transcribe."
    if value == transcription.AUDIO_SOURCE_MIX:
        return (
            "The full normalised audio. Better when separation smeared or dropped a line, "
            "which happens on sparse mixes that had little to separate."
        )
    return (
        "Transcribe both and keep the better result. It decodes the song twice, which "
        "roughly doubles the longest stage of the run."
    )


def _lyrics_group(machine: _Machine) -> OptionGroup:
    audio_default, audio_note = _audio_source_default(machine)
    whisper_default, whisper_note = _whisper_default(machine)
    # Which audio the transcriber listens to is not a question on a machine that
    # cannot transcribe: without the second clause this option reads as available,
    # with a resolved note recommending the vocal stem, beside a greyed-out
    # transcription control. `test_what_to_transcribe_dies_with_transcription`.
    audio_reason = machine.transcription_reason
    return OptionGroup(
        id="lyrics",
        label="Lyrics",
        help="Where the words come from, and how they get their timings.",
        options=(
            OptionSpec(
                id="lyrics_source",
                label="Where lyrics come from",
                type="enum",
                default=LYRIC_SOURCE_AUTO,
                stage="lyrics",
                help=(
                    "Leave this automatic unless you know the source has bad captions or "
                    "you have a better file of your own."
                ),
                choices=_lyrics_source_choices(machine),
            ),
            OptionSpec(
                id="lyrics_path",
                label="Lyrics file",
                type="path",
                default=None,
                stage="lyrics",
                help=(
                    "LRC, SRT, WebVTT, JSON, or plain text, up to "
                    f"{MAX_LYRICS_BYTES // (1024 * 1024)} MiB. Timed formats keep their "
                    "timings; plain text is spread across the song unless it is aligned."
                ),
            ),
            OptionSpec(
                id="language",
                label="Language",
                type="text",
                default=transcription.AUTO_LANGUAGE,
                stage="lyrics",
                help=(
                    "'auto' detects the sung language and prefers a caption track to match. "
                    "The list is what the transcription provider publishes for a screen "
                    "without a search box; any BCP 47 tag is accepted, which is why this "
                    "is a text field and not a closed set."
                ),
                choices=tuple(
                    Choice(
                        value=value,
                        label="Detect it" if value == transcription.AUTO_LANGUAGE else value,
                        help=(
                            "Let the model decide, which it is usually right about."
                            if value == transcription.AUTO_LANGUAGE
                            else ""
                        ),
                    )
                    for value in transcription.LANGUAGE_CHOICES
                ),
            ),
            OptionSpec(
                id="whisper_model",
                label="Transcription model",
                type="enum",
                default=whisper_default,
                stage="lyrics",
                help=(
                    "Which faster-whisper model writes the words down. 'auto' picks the "
                    "strongest one this machine can actually run."
                ),
                # The option and the named models carry `transcription_reason`;
                # only `auto` carries `whisper_reason`. That is the whole of the
                # rule against over-greying: "'auto' cannot choose" is a fact
                # about one choice, and the sixteen named models -- twelve of
                # which `auto` never even looks at -- stay live behind it.
                choices=(
                    Choice(
                        value=transcription.AUTO_MODEL,
                        label="Automatic",
                        help="Let this machine's memory, device, and cached weights decide.",
                        available=machine.whisper_reason is None,
                        unavailable_reason=machine.whisper_reason,
                    ),
                    *(
                        Choice(
                            value=model,
                            label=model,
                            help=_whisper_choice_help(model),
                            available=machine.transcription_reason is None,
                            unavailable_reason=machine.transcription_reason,
                        )
                        # Sorted, not frozenset order, or the document reorders
                        # itself between processes: `test_the_model_list_is_ordered`.
                        for model in sorted(transcription.SUPPORTED_MODELS)
                    ),
                ),
                advanced=True,
                available=machine.transcription_reason is None,
                unavailable_reason=machine.transcription_reason,
                default_is_resolved=bool(whisper_note),
                resolved_note=whisper_note,
            ),
            OptionSpec(
                id="audio_source",
                label="What to transcribe",
                type="enum",
                default=audio_default,
                stage="lyrics",
                help="Which audio the transcriber listens to.",
                choices=tuple(
                    Choice(
                        value=value,
                        label=value,
                        help=_audio_source_help(value),
                        available=machine.demucs or value == transcription.AUDIO_SOURCE_MIX,
                        unavailable_reason=(
                            None
                            if machine.demucs or value == transcription.AUDIO_SOURCE_MIX
                            else _DEMUCS_MISSING
                        ),
                    )
                    for value in transcription.AUDIO_SOURCE_CHOICES
                ),
                advanced=True,
                available=audio_reason is None,
                unavailable_reason=audio_reason,
                default_is_resolved=True,
                resolved_note=audio_note,
            ),
            # Not a resolved default and not gated: the force-aligner is part of
            # this package, so it is on every machine that can render this screen.
            # What used to be its resolved note is now the second half of `help`,
            # because the sentence is worth reading and no longer says "here".
            OptionSpec(
                id="align_supplied_text",
                label="Align untimed words to the audio",
                type="bool",
                default=True,
                stage="lyrics",
                help=(
                    "Only affects lyrics that arrive without timings. On, they are placed "
                    "against the audio; off, they are spread evenly across the song, which "
                    "drifts badly."
                ),
                advanced=True,
            ),
        ),
    )


def _tuning_choice(name: str) -> Choice:
    labels = " ".join(tuning_labels(TUNINGS[name]))
    return Choice(value=name, label=name, help=f"Strings, low to high: {labels}.")


def _tablature_group(machine: _Machine) -> OptionGroup:
    guitar_reason = None if machine.guitar else _BASIC_PITCH_MISSING
    return OptionGroup(
        id="tablature",
        label="Guitar tab",
        help="How the notes found in the guitar stem are written onto a fretboard.",
        options=(
            OptionSpec(
                id="tuning",
                label="Tuning",
                type="tuning",
                default="standard",
                stage="tablature",
                help=(
                    "The tuning the tab is written for. The value is a name from the list; "
                    "six MIDI pitches, low to high, are also accepted."
                ),
                choices=tuple(_tuning_choice(name) for name in TUNINGS),
                available=guitar_reason is None,
                unavailable_reason=guitar_reason,
            ),
            OptionSpec(
                id="max_fret",
                label="Highest fret",
                type="int",
                default=20,
                stage="tablature",
                help=(
                    "The highest fret the tab may use. Lower keeps playing near the nut; "
                    "higher allows positions most necks have but few players want."
                ),
                minimum=_MIN_FRET,
                maximum=_MAX_FRET,
                unit="fret",
                advanced=True,
                available=guitar_reason is None,
                unavailable_reason=guitar_reason,
            ),
        ),
    )


def _limits_group(machine: _Machine) -> OptionGroup:
    return OptionGroup(
        id="limits",
        label="Limits and consent",
        help="The ceilings a run may not cross, and the two answers only you can give.",
        options=(
            OptionSpec(
                id="max_duration",
                label="Longest source accepted",
                type="float",
                default=30.0 * 60,
                stage="source",
                help=(
                    "Anything longer is refused before a byte is fetched. Thirty minutes "
                    "covers songs and rejects the concert upload that would run for hours."
                ),
                minimum=_MIN_DURATION,
                maximum=_MAX_DURATION,
                unit="seconds",
                advanced=True,
            ),
            OptionSpec(
                id="allow_model_downloads",
                label="Allow model downloads",
                type="bool",
                default=machine.allow_model_downloads,
                stage="source",
                help=(
                    "Off, model weights that are not already cached are not fetched and the "
                    "run stays entirely offline. On, this run may download the weights it "
                    "needs -- which on a fresh machine is what makes it work at all."
                ),
            ),
            OptionSpec(
                id="rights_confirmed",
                label="I have permission to process this media and its lyrics",
                type="bool",
                default=False,
                stage="source",
                help=(
                    "Required before a run starts. It is off by default because a ticked "
                    "consent box is not consent."
                ),
            ),
        ),
    )


def _greyed_choices(option: OptionSpec) -> OptionSpec:
    """Grey every choice of a greyed option, with the option's own reason.

    A surface renders `available=False` by disabling the control, so a choice
    inside it that still claims `available=True` is a claim nothing can act on --
    and worse, it is how an unavailable option comes to carry a reason whose
    escape ("select a model whose weights you already have") the rendered screen
    forbids. Applying this once, to every option, means the disagreement cannot be
    reintroduced one option at a time;
    `test_a_greyed_control_never_leaves_a_live_choice_behind` is the check.

    This is the *last* step, not a substitute for greying a choice for its own
    reason: a choice that already has one keeps it, because "Demucs is not
    installed" is more use than the option-level sentence that swallowed it.
    """

    if option.available or not option.choices:
        return option
    return replace(
        option,
        choices=tuple(
            choice
            if not choice.available
            else replace(choice, available=False, unavailable_reason=option.unavailable_reason)
            for choice in option.choices
        ),
    )


def build_options_document(*, allow_model_downloads: bool = False) -> OptionsDocument:
    """Describe every backend option as this machine can honour it right now.

    `allow_model_downloads` is the one answer that changes what is available
    rather than only what is chosen: with downloads forbidden, `auto` may have no
    obtainable model at all. Passing it re-resolves the document as it will look
    once the user ticks that box, and the returned document stays self-consistent
    -- its own `allow_model_downloads` default is the value passed here.
    """

    machine = _probe(allow_model_downloads=allow_model_downloads)
    groups = (
        _source_group(machine),
        _separation_group(machine),
        _lyrics_group(machine),
        _tablature_group(machine),
        _limits_group(machine),
    )
    return OptionsDocument(
        groups=tuple(
            replace(group, options=tuple(_greyed_choices(option) for option in group.options))
            for group in groups
        )
    )
