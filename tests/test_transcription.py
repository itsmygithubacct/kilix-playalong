"""Transcription provider and whisper-worker tests.

WHAT IS AND IS NOT COVERED BY A REAL MODEL, stated plainly because a stand-in
test that reads like a real one is worse than an honest gap:

* Real, no stand-in: the Silero VAD. It ships inside faster-whisper as a local
  ONNX asset, needs no download, and is exercised on synthesised audio in
  ``test_speech_spans_come_from_the_real_silero_vad``.
* Real, no stand-in: the shape of the arguments handed to faster-whisper. The
  worker's option dictionary is bound against the installed
  ``WhisperModel.transcribe`` signature and its VAD dictionary is used to build
  a real ``VadOptions``, so a renamed or removed keyword fails here rather than
  inside a subprocess on a user's machine.
* STAND-IN: everything that would need Whisper's weights -- decoding, language
  detection, segment log-probabilities. No model is downloaded or run. The
  worker is driven with a fake ``faster_whisper`` module injected into
  ``sys.modules``, which is enough to test argument construction, the
  post-filters, the audio-source criterion and the receipt, and is NOT evidence
  about transcription accuracy. Nothing here says the tuned thresholds produce
  better lyrics; they are argued for in the constants' comments and would need
  real audio and a reference transcript to measure.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from kilix_playalong import _whisper_worker as worker
from kilix_playalong import alignment, text
from kilix_playalong.errors import InvalidInputError, ProviderUnavailableError
from kilix_playalong.providers import transcription
from kilix_playalong.runner import CommandResult

_HAS_FASTER_WHISPER = importlib.util.find_spec("faster_whisper") is not None
_needs_faster_whisper = pytest.mark.skipif(
    not _HAS_FASTER_WHISPER, reason="faster-whisper is not installed"
)
_needs_ctranslate2 = pytest.mark.skipif(
    importlib.util.find_spec("ctranslate2") is None,
    # Without it both answers are False for the same reason and the comparison
    # would pass without having compared anything.
    reason="ctranslate2 is not installed, so there is no ungated answer to check",
)


# ---------------------------------------------------------------------------
# Adaptive model resolution (unchanged behaviour, kept under test).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("available_gib", "expected"),
    [
        (16, "large-v3"),
        (8, "large-v3-turbo"),
        (4, "medium"),
        (2, "small"),
    ],
)
def test_auto_model_adapts_to_available_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available_gib: int,
    expected: str,
) -> None:
    monkeypatch.setattr(transcription, "cuda_available", lambda: False)
    monkeypatch.setattr(
        transcription,
        "_available_memory_bytes",
        lambda: available_gib * 1024**3,
    )

    assert (
        transcription.resolve_model(
            "auto",
            device="auto",
            model_cache=tmp_path,
            allow_model_downloads=True,
        )
        == expected
    )


def test_auto_model_prefers_large_v3_on_cuda(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transcription, "cuda_available", lambda: True)
    monkeypatch.setattr(transcription, "_available_memory_bytes", lambda: 1024**3)

    assert (
        transcription.resolve_model(
            "auto",
            device="auto",
            model_cache=tmp_path,
            allow_model_downloads=True,
        )
        == "large-v3"
    )


def test_auto_model_uses_strongest_compatible_cached_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transcription, "cuda_available", lambda: False)
    monkeypatch.setattr(transcription, "_available_memory_bytes", lambda: 16 * 1024**3)
    snapshot = (
        tmp_path / "models--Systran--faster-whisper-medium" / "snapshots" / "fixture-revision"
    )
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"fixture")

    assert (
        transcription.resolve_model(
            "auto",
            device="cpu",
            model_cache=tmp_path,
            allow_model_downloads=False,
        )
        == "medium"
    )


def test_auto_model_requires_download_permission_without_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transcription, "cuda_available", lambda: False)
    monkeypatch.setattr(transcription, "_available_memory_bytes", lambda: 16 * 1024**3)

    with pytest.raises(ProviderUnavailableError, match="--allow-model-downloads"):
        transcription.resolve_model(
            "auto",
            device="cpu",
            model_cache=tmp_path,
            allow_model_downloads=False,
        )


def test_an_unsupported_model_is_a_user_error_not_a_missing_package(tmp_path: Path) -> None:
    """A name outside the closed set is a bad value, wherever it is checked.

    ``errors.py`` separates "input failed validation" from "an optional package or
    model is unavailable", and a model name this build never had is the first of
    those: no install, no download and no ``--allow-model-downloads`` makes
    "larj-v3" resolvable. Both doors into the provider are checked because the
    pipeline reaches ``resolve_model`` directly when it keys the lyrics stage and
    ``transcribe`` when it runs it, and a caller that catches one class at one door
    and the other at the other has no rule it can state.
    """
    with pytest.raises(InvalidInputError, match="unsupported faster-whisper model"):
        transcription.resolve_model(
            "larj-v3",
            device="cpu",
            model_cache=tmp_path,
            allow_model_downloads=True,
        )
    with pytest.raises(InvalidInputError, match="unsupported faster-whisper model"):
        transcription.transcribe(tmp_path / "vocals.wav", tmp_path / "lyrics.json", model="larj-v3")


@_needs_ctranslate2
def test_the_cuda_shortcut_agrees_with_the_real_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one inference in ``_LINUX_CUDA_DEVICE_PATHS``, checked on this machine.

    The shortcut answers "no CUDA" from four filesystem paths instead of from a
    one-second import of ctranslate2, and it is only sound because their joint
    absence is a proof rather than a guess. That proof is about how NVIDIA
    drivers expose themselves to Linux, which is not something this suite can
    derive -- so it is pinned the only honest way there is: whatever machine runs
    these tests must get the same answer with the shortcut as without it.
    Disabling ``_cuda_driver_is_absent`` restores the pre-shortcut function
    exactly, error handling included, so this compares the two answers and
    nothing else. A fifth flavour of Linux the path list does not know about
    fails here, on the machine that has it, rather than silently pinning that
    machine to the CPU.
    """
    gated = transcription.cuda_available()

    monkeypatch.setattr(transcription, "_cuda_driver_is_absent", lambda: False)

    assert gated == transcription.cuda_available()


def test_asking_where_the_weights_live_creates_nothing(private_homes: Path) -> None:
    """``model_cache_path`` is the path; ``transcribe`` is what makes the directory.

    Describing a machine -- which model is already cached, what ``auto`` would run
    here -- has to reach the same directory the worker is handed, and must not
    leave a directory behind for having asked. That is the whole reason this is a
    separate function from the ``ensure_private_directory`` call inside
    ``transcribe``. The other half of the property -- that what the worker is
    handed is what was asked for -- is
    ``test_the_worker_is_handed_the_cache_path_everyone_else_reads``.
    """
    assert not transcription.model_cache_path().exists()


def test_explicit_model_is_never_replaced(tmp_path: Path) -> None:
    assert (
        transcription.resolve_model(
            "small",
            device="cuda",
            model_cache=tmp_path,
            allow_model_downloads=True,
        )
        == "small"
    )


# ---------------------------------------------------------------------------
# Real faster-whisper API contract. No weights, no download, no stand-in.
# ---------------------------------------------------------------------------


@_needs_faster_whisper
def test_transcribe_options_bind_to_the_installed_faster_whisper() -> None:
    """Every tuned keyword must exist on the real ``WhisperModel.transcribe``.

    This is the check that a stand-in cannot make: a fake model accepts
    ``**kwargs`` and would happily swallow a keyword that faster-whisper renamed
    or never had.
    """
    import importlib
    import inspect

    from faster_whisper import WhisperModel

    vad = importlib.import_module("faster_whisper.vad")
    options = worker.transcribe_options("en", vad.VadOptions(**worker.VAD_PARAMETERS))
    signature = inspect.signature(WhisperModel.transcribe)
    unknown = set(options) - set(signature.parameters)
    assert unknown == set()
    signature.bind(object(), object(), **options)


@_needs_faster_whisper
def test_vad_parameters_are_real_silero_options() -> None:
    import importlib

    vad = importlib.import_module("faster_whisper.vad")
    options = vad.VadOptions(**worker.VAD_PARAMETERS)

    # The music-specific choice: instrumental gaps, not conversational pauses.
    assert options.min_silence_duration_ms == 700
    assert options.min_silence_duration_ms < vad.VadOptions().min_silence_duration_ms
    assert options.min_speech_duration_ms == 250
    assert options.max_speech_duration_s == 30.0


def _speech_like_audio(amplitude: float = 0.1) -> Any:
    """Two seconds of voiced-sounding audio between two seconds of silence.

    A harmonic stack with a 4 Hz syllabic envelope, which the real Silero model
    classifies as speech. ``amplitude`` is how loud: quiet audio sits nearer the
    detector's decision boundary, which is where a wrong threshold shows up.
    """
    import numpy as np

    sample_rate = worker.SAMPLE_RATE
    times = np.arange(sample_rate * 2) / sample_rate
    buzz = sum(np.sin(2 * np.pi * 130.0 * k * times) / k for k in range(1, 25))
    envelope = 0.5 * (1 + np.sin(2 * np.pi * 4 * times))
    audio: Any = np.concatenate(
        [np.zeros(sample_rate), buzz * envelope * amplitude, np.zeros(sample_rate)]
    ).astype(np.float32)
    return audio


@_needs_faster_whisper
def test_the_vad_dictionary_pins_what_it_says_it_pins() -> None:
    """Three of the six values are the library's defaults; the comments say so.

    This is a truthfulness test, not a behaviour test. ``VAD_PARAMETERS`` marks
    each field CHANGED or PINNED, and a PINNED field that stopped matching the
    installed faster-whisper would make the comment beside it false -- it would
    have become a silent, unargued tuning decision. ``threshold`` and
    ``speech_pad_ms`` are checked against the declared defaults directly;
    ``neg_threshold`` has no declared default -- the library leaves it None and
    derives it inside ``get_speech_timestamps`` -- so it is checked three ways,
    none of which is sufficient alone.
    """
    import importlib
    import inspect

    vad = importlib.import_module("faster_whisper.vad")
    defaults = vad.VadOptions()

    # PINNED: identical to what the library would have used.
    assert worker.VAD_PARAMETERS["threshold"] == defaults.threshold
    assert worker.VAD_PARAMETERS["speech_pad_ms"] == defaults.speech_pad_ms
    # CHANGED: deliberately not the library's value, and the comments argue why.
    assert worker.VAD_PARAMETERS["min_speech_duration_ms"] != defaults.min_speech_duration_ms
    assert worker.VAD_PARAMETERS["max_speech_duration_s"] != defaults.max_speech_duration_s
    assert worker.VAD_PARAMETERS["min_silence_duration_ms"] != defaults.min_silence_duration_ms

    # 1. It is still derived rather than declared. A library that starts shipping
    #    a default has to be re-checked against that default, not against this.
    assert defaults.neg_threshold is None
    # 2. The derivation itself, read out of the installed source. Exact, and
    #    deliberately brittle: an upstream edit to this line is precisely the
    #    event that would turn the pin into an unargued tuning choice, and it
    #    should stop the suite rather than pass unnoticed. (0.5 - 0.15 is exactly
    #    0.35 in binary floating point, so the comparison below is safe.)
    assert "neg_threshold = max(threshold - 0.15, 0.01)" in inspect.getsource(
        vad.get_speech_timestamps
    )
    assert worker.VAD_PARAMETERS["neg_threshold"] == max(
        worker.VAD_PARAMETERS["threshold"] - 0.15, 0.01
    )
    # 3. And the same thing behaviourally, in case the derivation moves somewhere
    #    the two checks above cannot see: the real VAD with the pin and without it
    #    must find the same spans. Loud, quiet and very quiet, because hysteresis
    #    only decides anything where the detector's confidence is near the
    #    boundary; a single loud clip would agree under almost any value.
    derived = {
        name: value for name, value in worker.VAD_PARAMETERS.items() if name != "neg_threshold"
    }
    for amplitude in (0.1, 0.02, 0.008):
        audio = _speech_like_audio(amplitude)

        assert worker.speech_spans(
            vad, audio, vad.VadOptions(**worker.VAD_PARAMETERS)
        ) == worker.speech_spans(vad, audio, vad.VadOptions(**derived))


@_needs_faster_whisper
def test_speech_spans_come_from_the_real_silero_vad(tmp_path: Path) -> None:
    """Run the genuine bundled Silero VAD and filter real cues against it.

    The audio is synthesised, not sung, but the VAD is the real one loaded from
    faster-whisper's own ONNX assets, so this covers the parameter names, the
    sample-index-to-seconds conversion, and the silence rule end to end.
    """
    import importlib

    vad = importlib.import_module("faster_whisper.vad")
    audio = _speech_like_audio()

    spans = worker.speech_spans(vad, audio, vad.VadOptions(**worker.VAD_PARAMETERS))

    assert len(spans) == 1
    start, end = spans[0]
    assert 0.4 <= start <= 1.05
    assert 2.95 <= end <= 3.6

    sung = worker.RawCue(1.4, 1.9, "a real line", (), -0.3)
    over_silence = worker.RawCue(3.7, 3.95, "an invented line", (), -0.3)
    kept = worker.filter_cues([sung, over_silence], spans)
    assert [cue.text for cue in kept] == ["a real line"]


# ---------------------------------------------------------------------------
# Post-filters. Pure functions, no model needed at all.
# ---------------------------------------------------------------------------


def _cue(text: str, start: float = 0.0, end: float = 2.0, logprob: float = -0.3) -> worker.RawCue:
    return worker.RawCue(start=start, end=end, text=text, words=(), avg_logprob=logprob)


@pytest.mark.parametrize(
    "text",
    [
        "Thanks for watching!",
        "thanks for watching",
        "Subtitles by the Amara.org community",
        "Sous-titres réalisés par la communauté d'Amara.org",
        "ご視聴ありがとうございました",
        "Untertitelung aufgrund der Amara.org-Community",
    ],
)
def test_known_phantom_cues_are_dropped(text: str) -> None:
    assert worker.filter_cues([_cue(text)], []) == []


@pytest.mark.parametrize(
    "text",
    [
        "Thank you for the love you gave me",
        "Say goodbye, say goodbye",
        "You",
        "The end of the line",
    ],
)
def test_plausible_lyrics_are_not_mistaken_for_phantoms(text: str) -> None:
    assert [cue.text for cue in worker.filter_cues([_cue(text)], [])] == [text]


@pytest.mark.parametrize("mark", ["'", "\u2019", "\u2018", "\u00b4", "\u0060", "\u02bc"])
def test_a_phantom_is_caught_under_every_spelling_of_its_apostrophe(mark: str) -> None:
    r"""One character decides between "filtered artefact" and "lyric".

    U+02BC MODIFIER LETTER APOSTROPHE is the reason this exists: it is category
    Lm, so ``\w`` calls it a word character, and ``fold``'s punctuation rule left it
    in place while removing all five other spellings. Whisper's stock
    "Don't forget to subscribe" was therefore filtered under five spellings and
    shipped as a lyric under the sixth.
    """
    assert worker.filter_cues([_cue("Don" + mark + "t forget to subscribe")], []) == []


def test_the_two_folds_normalise_on_one_shared_basis() -> None:
    """The filter and the aligner fold case, accents and apostrophes identically.

    ``worker.fold`` and ``alignment.comparison_tokens`` are deliberately different
    functions -- see ``fold``'s docstring for why the worker must not expand digits
    and contractions -- but "which characters are an apostrophe" is one question,
    and answering it twice is how the U+02BC hole above got in. ``text.fold_accents``
    is now the one answer, and this pins that both really route through it rather
    than merely resembling it: the worker's fold is that function followed by
    collapsing punctuation, exactly, over a corpus that includes every apostrophe
    spelling and a spread of accented and cased forms.

    The scan stops at U+3000 because the class is spelled inside the Latin and
    General Punctuation blocks.
    """
    spellings = [chr(code) for code in range(0x3000) if text._APOSTROPHE.fullmatch(chr(code))]

    assert "\u02bc" in spellings
    for mark in spellings:
        assert worker.fold("Don" + mark + "t") == "don t", f"U+{ord(mark):04X} is not folded"

    corpus = ["Don" + mark + "t stop" for mark in [*spellings, "'"]] + [
        "CAFÉ",
        "cafe\u0301",
        "Stra\u00dfe",
        "1985 ain't",
        "R&B",
        "",
        "  ",
        "İstanbul",
    ]
    for value in corpus:
        collapsed = " ".join(worker._PUNCTUATION.sub(" ", text.fold_accents(value)).split())
        assert worker.fold(value) == collapsed, repr(value)
        # And the aligner starts from the same string, before its own tail runs.
        assert alignment.comparison_tokens(value) == alignment.comparison_tokens(
            text.fold_accents(value)
        ), repr(value)


def test_runaway_token_repetition_is_dropped_but_a_real_hook_survives() -> None:
    loop = _cue(" ".join(["stop"] * 40))
    hook = _cue("na na na na na na na na, hey Jude")

    kept = worker.filter_cues([loop, hook], [])

    assert [cue.text for cue in kept] == [hook.text]


def test_a_repeated_phrase_is_caught_where_a_repeated_token_rule_sees_nothing() -> None:
    """The measured failure: a two-word loop with no token ever repeating twice.

    Produced verbatim by the real `tiny` model on a loop-prone recording, where
    no token follows itself and no token is more than half of the cue, so every
    word-level test passes it.
    """
    loop_text = ", ".join(["hold on"] * 40)
    tokens = worker.fold(loop_text).split()

    assert worker.max_phrase_repeats(tokens, 1) == 1
    assert max(tokens.count(token) for token in set(tokens)) / len(tokens) == pytest.approx(0.5)
    assert worker.filter_cues([_cue(loop_text)], []) == []


def test_a_short_repeated_phrase_within_the_bound_survives() -> None:
    chant = _cue(", ".join(["hold on"] * 6) + ", to me tonight")
    assert worker.filter_cues([chant], []) == [chant]


def test_a_chant_is_kept_to_exactly_the_declared_bound() -> None:
    """Both sides of ``MAX_PHRASE_REPEATS``, at the boundary rather than near it.

    The constant's comment claims a specific trade -- sixteen repeats kept, a
    seventeenth loses the cue -- and until this test the longest chant under test
    was eight, so the constant could have been anything from nine upwards with
    the suite green and the comment silently wrong. A bound that is only tested
    an octave below where it sits is not a tested bound.
    """
    # Literal counts, deliberately: written as MAX_PHRASE_REPEATS and
    # MAX_PHRASE_REPEATS + 1 the test would follow the constant anywhere and
    # check only that *some* boundary exists. The comment claims sixteen and
    # seventeen, so sixteen and seventeen are what is checked, and moving the
    # constant means saying so here and in the comment that argues for it.
    kept = _cue(", ".join(["hold on"] * 16))
    one_too_many = _cue(", ".join(["hold on"] * 17))

    assert worker.filter_cues([kept], []) == [kept]
    assert worker.filter_cues([one_too_many], []) == []
    assert worker.MAX_PHRASE_REPEATS == 16


def test_a_cue_inside_a_silent_span_is_dropped() -> None:
    spans = [(0.0, 5.0), (20.0, 30.0)]
    inside_silence = _cue("invented over the solo", start=9.0, end=12.0)
    overlapping = _cue("last line of the verse", start=4.5, end=6.0)

    kept = worker.filter_cues([overlapping, inside_silence], spans)

    assert [cue.text for cue in kept] == ["last line of the verse"]


def test_an_empty_span_list_does_not_delete_the_transcript() -> None:
    """A VAD that found nothing must not be read as "every cue is a phantom"."""
    cues = [_cue("a line", start=1.0, end=2.0)]
    assert worker.filter_cues(cues, []) == cues


def test_a_long_run_of_identical_cues_is_trimmed_to_the_bound() -> None:
    run = [_cue("same line", start=float(index), end=index + 1.0) for index in range(20)]

    kept = worker.filter_cues(run, [])

    assert len(kept) == worker.MAX_IDENTICAL_CUE_RUN
    assert kept == run[: worker.MAX_IDENTICAL_CUE_RUN]


def test_a_repeated_chorus_line_survives_when_other_lines_break_the_run() -> None:
    cues = []
    for index in range(12):
        text = "hold on tight" if index % 2 == 0 else f"verse line {index}"
        cues.append(_cue(text, start=float(index), end=index + 1.0))

    assert worker.filter_cues(cues, []) == cues


# ---------------------------------------------------------------------------
# Audio-source criterion.
# ---------------------------------------------------------------------------


def _attempt(
    label: str,
    cues: Sequence[tuple[float, float, float]],
) -> worker.Attempt:
    return worker.Attempt(
        audio_source=label,
        cues=tuple(
            worker.RawCue(start, end, f"line {index}", (), logprob)
            for index, (start, end, logprob) in enumerate(cues)
        ),
        language="en",
        language_from="detected",
        language_confidence=0.9,
    )


def test_a_boolean_confidence_is_no_confidence_rather_than_the_best_possible() -> None:
    """``bool`` is the only thing ``float()`` turns into a plausible number silently.

    Every other unusable field a third-party segment could carry raises and lands
    on the default; ``float(True)`` is 1.0, which is not a log-probability any
    decoder can produce and would make the cue carrying it the most confident in
    the transcript -- enough to win an ``auto`` comparison on its own.
    """
    assert worker._finite(True, worker.LOG_PROB_THRESHOLD) == worker.LOG_PROB_THRESHOLD
    assert worker._finite(False, worker.LOG_PROB_THRESHOLD) == worker.LOG_PROB_THRESHOLD
    # Not a blanket rejection of int: a real integer bound is still a number.
    assert worker._finite(0, -1.0) == 0.0


def test_score_is_log_probability_weighted_by_covered_duration() -> None:
    attempt = _attempt("vocals", [(0.0, 10.0, -0.1), (10.0, 12.0, -1.1)])

    # (-0.1 * 10 + -1.1 * 2) / 12
    assert attempt.covered == pytest.approx(12.0)
    assert attempt.score == pytest.approx((-1.0 + -2.2) / 12.0)


def test_auto_prefers_the_clearly_better_scoring_source() -> None:
    vocals = _attempt("vocals", [(0.0, 30.0, -0.9)])
    mix = _attempt("mix", [(0.0, 30.0, -0.2)])

    assert worker.choose_attempt([vocals, mix]) == (mix, "score")


def test_auto_keeps_the_stem_when_the_mix_is_only_marginally_better() -> None:
    vocals = _attempt("vocals", [(0.0, 30.0, -0.30)])
    mix = _attempt("mix", [(0.0, 30.0, -0.28)])

    assert worker.choose_attempt([vocals, mix]) == (vocals, "tie")


def test_auto_rejects_a_confident_fragment_that_missed_most_of_the_song() -> None:
    vocals = _attempt("vocals", [(0.0, 120.0, -0.6)])
    mix = _attempt("mix", [(0.0, 20.0, -0.05)])

    assert worker.choose_attempt([vocals, mix]) == (vocals, "coverage")


def test_auto_falls_back_to_whichever_source_produced_anything() -> None:
    vocals = _attempt("vocals", [])
    mix = _attempt("mix", [(0.0, 30.0, -0.7)])

    assert worker.choose_attempt([vocals, mix]) == (mix, "empty")


def test_a_single_source_is_reported_as_such() -> None:
    vocals = _attempt("vocals", [(0.0, 30.0, -0.7)])

    assert worker.choose_attempt([vocals]) == (vocals, "single")


# ---------------------------------------------------------------------------
# Receipt.
# ---------------------------------------------------------------------------


def test_receipt_round_trips() -> None:
    rendered = transcription.format_receipt(
        model="large-v3",
        audio_source="mix",
        audio_from="auto:score",
        language="de",
        language_from="detected",
        language_confidence=0.9312,
    )

    assert rendered == (
        "faster-whisper:large-v3;audio=mix;audio_from=auto:score"
        ";lang=de;lang_from=detected;lang_confidence=0.93"
    )
    parsed = transcription.parse_receipt(rendered)
    assert parsed == transcription.Receipt(
        model="large-v3",
        audio_source="mix",
        audio_from="auto:score",
        language="de",
        language_from="detected",
        language_confidence=0.93,
    )


def test_a_receipt_from_an_older_release_still_names_its_model() -> None:
    parsed = transcription.parse_receipt("faster-whisper:small")

    assert parsed is not None
    assert parsed.model == "small"
    assert parsed.audio_source == "vocals"


def test_a_non_whisper_source_is_not_a_receipt() -> None:
    assert transcription.parse_receipt("imported-lrc") is None
    assert transcription.parse_receipt("faster-whisper:") is None


# Every reason ``choose_attempt`` returns, reachable and unreachable alike.
_CHOOSE_ATTEMPT_REASONS = ("single", "empty", "coverage", "score", "tie")

# Every ``audio_from`` the worker can write, and no more. ``main`` writes
# "requested" whenever the caller pinned the audio source, and ``auto:<reason>``
# only under ``--audio-source auto`` -- where ``_audio_plan`` always gives two
# passes, so "single", which ``choose_attempt`` returns only at one pass, cannot
# reach a receipt. Both halves are driven end to end below: the two-pass plan by
# ``test_automatic_selection_runs_both_sources_and_records_the_winner``, the
# one-pass wording by ``test_the_mix_alone_transcribes_only_the_mix``.
_AUTO_REASONS = tuple(reason for reason in _CHOOSE_ATTEMPT_REASONS if reason != "single")
_AUDIO_FROM_VALUES = ("requested", *(f"auto:{reason}" for reason in _AUTO_REASONS))

# The winner's own label. Never "auto": "auto" is what was *asked for*, and it is
# recorded in ``audio_from``, so the two fields together say both.
_AUDIO_SOURCE_VALUES = ("vocals", "mix")

# ``_resolve_language`` returns exactly these three, and the language beside them
# is a base subtag -- ``normalize_language`` has already reduced any tag by then.
_LANGUAGE_VALUES = (("en", "model"), ("de", "detected"), ("fr", "requested"))


def test_the_receipt_shapes_under_test_are_every_shape_the_worker_can_write() -> None:
    """Keeps the parametrisation below exhaustive rather than merely long.

    ``choose_attempt``'s reason is the only open-ended part of ``audio_from``, so
    the enumerated shapes are exhaustive exactly while these are the reasons it
    can return. That needs both halves: driving the function proves each listed
    reason is really reachable, and it takes a scan of the function's own returns
    -- the one thing that fails when a *sixth* reason is added -- to prove there
    is no reason missing from the list. Neither half alone would notice.

    Reachable from ``choose_attempt`` is not the same as reachable in a receipt:
    "single" is returned only for a one-pass run, and a one-pass run is one the
    caller pinned, which ``main`` records as "requested". So ``auto:single`` is a
    shape the worker cannot write, and the last two assertions pin the fact that
    keeps it out -- ``auto`` is always a two-pass plan -- rather than leaving the
    exclusion as a remark in a comment.
    """
    import argparse
    import inspect
    import re

    returned = set(
        re.findall(r'return [a-z_]+, "([a-z:]+)"', inspect.getsource(worker.choose_attempt))
    )
    assert returned == set(_CHOOSE_ATTEMPT_REASONS)

    both = ([(0.0, 30.0, -0.9)], [(0.0, 30.0, -0.2)])
    produced = {
        worker.choose_attempt([_attempt("vocals", [(0.0, 30.0, -0.7)])])[1],
        worker.choose_attempt([_attempt("vocals", []), _attempt("mix", both[1])])[1],
        worker.choose_attempt(
            [_attempt("vocals", [(0.0, 120.0, -0.6)]), _attempt("mix", [(0.0, 20.0, -0.05)])]
        )[1],
        worker.choose_attempt([_attempt("vocals", both[0]), _attempt("mix", both[1])])[1],
        worker.choose_attempt(
            [_attempt("vocals", [(0.0, 30.0, -0.30)]), _attempt("mix", [(0.0, 30.0, -0.28)])]
        )[1],
    }

    assert produced == set(_CHOOSE_ATTEMPT_REASONS)

    plan = worker._audio_plan(
        argparse.Namespace(
            audio_source=transcription.AUDIO_SOURCE_AUTO,
            source=Path("vocals.wav"),
            mix=Path("normalized.wav"),
        )
    )
    assert len(plan) == 2
    assert set(_AUDIO_FROM_VALUES) == {"requested"} | {
        f"auto:{reason}" for reason in returned - {"single"}
    }


@pytest.mark.parametrize("model", sorted(transcription.SUPPORTED_MODELS))
@pytest.mark.parametrize("audio_from", _AUDIO_FROM_VALUES)
def test_the_model_is_recoverable_from_every_receipt_shape(model: str, audio_from: str) -> None:
    """``parse_receipt(source).model`` is the model, over the whole receipt space.

    This is the property the pipeline's stage keying stands on, so it is tested
    over every model this release can resolve and every ``audio_from`` the worker
    can write rather than on one example. Model names carry ``.`` and ``-``
    (``small.en``, ``distil-large-v3``, ``large-v3-turbo``) and the model is the
    one field with no closed vocabulary on the way back in, so it is the field a
    parser gets wrong.
    """
    for audio_source in _AUDIO_SOURCE_VALUES:
        for language, language_from in _LANGUAGE_VALUES:
            rendered = transcription.format_receipt(
                model=model,
                audio_source=audio_source,
                audio_from=audio_from,
                language=language,
                language_from=language_from,
                language_confidence=0.5,
            )

            assert transcription.parse_receipt(rendered) == transcription.Receipt(
                model=model,
                audio_source=audio_source,
                audio_from=audio_from,
                language=language,
                language_from=language_from,
                language_confidence=0.5,
            )


@pytest.mark.parametrize("model", sorted(transcription.SUPPORTED_MODELS))
def test_a_receipt_recorded_before_this_release_still_names_its_model(model: str) -> None:
    """Every project already on this machine carries the bare form.

    Backward compatibility here is not a nicety: the previous release wrote
    ``f"faster-whisper:{model}"`` and nothing else, and those manifests are on
    disk now. The defaults the missing fields read back with are that release's
    actual behaviour -- it transcribed the vocal stem, because that is the only
    path the pipeline handed it. Its *receipt* said nothing about the language
    (the document's separate ``language`` field did), so both language fields
    read back as unknown rather than as a confident-sounding guess.
    """
    parsed = transcription.parse_receipt(f"faster-whisper:{model}")

    assert parsed == transcription.Receipt(model=model)
    assert (parsed.audio_source, parsed.audio_from) == ("vocals", "requested")
    assert (parsed.language, parsed.language_from) == ("unknown", "unknown")
    assert parsed.language_confidence == 0.0


def test_a_receipt_from_a_later_release_keeps_the_fields_this_one_knows() -> None:
    """An added field must not cost a reader the fields it does understand.

    The forward half of the same problem: this release has to read a receipt
    written by the next one, because a project is opened by whatever is
    installed, not by whatever wrote it.
    """
    parsed = transcription.parse_receipt(
        "faster-whisper:small;audio=mix;audio_from=auto:score;lang=en"
        ";lang_from=detected;lang_confidence=0.80;beam=5;prompt_hash=abc123"
    )

    assert parsed is not None
    assert parsed.model == "small"
    assert parsed.audio_source == "mix"
    assert parsed.language_confidence == pytest.approx(0.80)


def test_a_receipt_that_names_no_model_is_not_a_receipt() -> None:
    """The empty model is the failure the pipeline cannot see.

    ``parse_receipt`` returning ``Receipt(model="")`` would put an empty string
    into the lyrics stage key as if it were a model name, which is the same class
    of bug as handing it a whole receipt: a key naming a model that does not
    exist. "Not a receipt" is the only honest answer to a string with no model.
    """
    assert transcription.parse_receipt("faster-whisper:;lang=en;lang_confidence=0.99") is None
    assert transcription.parse_receipt("faster-whisper:;") is None


@pytest.mark.parametrize("text", ["nan", "-nan", "inf", "-inf", "Infinity"])
def test_an_unusable_confidence_reads_as_no_confidence_not_as_certainty(text: str) -> None:
    """A corrupt number must not come back as the strongest claim the field makes.

    ``float("nan")`` and ``float("inf")`` both survive a bare
    ``max(0.0, min(1.0, x))`` as **1.00**, because ``min(1.0, nan)`` is 1.0 --
    so a truncated or hand-edited manifest would read as maximum confidence in
    whatever language it names. Reached only from disk (the worker runs every
    number through ``_finite`` first), and this module's whole job is that the
    receipt does not overstate what is known.
    """
    parsed = transcription.parse_receipt(f"faster-whisper:small;lang=en;lang_confidence={text}")

    assert parsed is not None
    assert parsed.language_confidence == 0.0


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        (float("nan"), "0.00"),
        (float("inf"), "0.00"),
        (float("-inf"), "0.00"),
        (-0.5, "0.00"),
        (1.5, "1.00"),
        (0.917, "0.92"),
    ],
)
def test_a_written_confidence_is_a_real_probability(confidence: float, expected: str) -> None:
    rendered = transcription.format_receipt(
        model="small",
        audio_source="vocals",
        audio_from="requested",
        language="en",
        language_from="detected",
        language_confidence=confidence,
    )

    assert rendered.endswith(f";lang_confidence={expected}")


def test_a_garbled_confidence_does_not_cost_the_rest_of_the_receipt() -> None:
    parsed = transcription.parse_receipt(
        "faster-whisper:medium;audio=mix;audio_from=auto:empty;lang=es"
        ";lang_from=requested;lang_confidence=high"
    )

    assert parsed is not None
    assert parsed.model == "medium"
    assert parsed.language == "es"
    assert parsed.language_confidence == 0.0


@pytest.mark.parametrize(
    "value",
    [
        "",
        "imported-lrc",
        "manual",
        "whisper:small",
        "Faster-Whisper:small",
        " faster-whisper:small",
    ],
)
def test_a_source_that_is_not_a_whisper_receipt_parses_as_none(value: str) -> None:
    """None means "some other lyrics source", which a caller has to be able to see.

    The pipeline distinguishes "these lyrics came from a Whisper model, keyed on
    which one" from "these lyrics were imported", and an imported ``.lrc`` must
    never be mistaken for a receipt with a strange model name.
    """
    assert transcription.parse_receipt(value) is None


# ---------------------------------------------------------------------------
# Provider boundary: argv, timeouts, path and language validation.
# ---------------------------------------------------------------------------


class _RecordingRunner:
    def __init__(self) -> None:
        self.arguments: list[str] = []
        self.timeout: float = 0.0
        self.env: dict[str, str] = {}
        self.calls = 0

    def __call__(
        self,
        arguments: Sequence[str],
        *,
        timeout: float,
        env: dict[str, str] | None = None,
        **_kwargs: object,
    ) -> CommandResult:
        self.calls += 1
        self.arguments = list(arguments)
        self.timeout = timeout
        self.env = dict(env or {})
        return CommandResult(stdout="", stderr="")


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> _RecordingRunner:
    recorder = _RecordingRunner()
    monkeypatch.setattr(transcription, "is_available", lambda: True)
    monkeypatch.setattr(transcription, "run_command", recorder)
    return recorder


def _audio(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"synthetic audio")
    return path


def test_transcribe_defaults_to_the_vocal_stem(
    tmp_path: Path, private_homes: Path, runner: _RecordingRunner
) -> None:
    output = tmp_path / "lyrics.json"
    output.write_text("{}")

    transcription.transcribe(_audio(tmp_path, "vocals.wav"), output, model="small")

    assert runner.arguments[runner.arguments.index("--audio-source") + 1] == "vocals"
    assert "--mix" not in runner.arguments
    assert runner.timeout == pytest.approx(60 * 60)


def test_transcribe_passes_both_sources_for_automatic_selection(
    tmp_path: Path, private_homes: Path, runner: _RecordingRunner
) -> None:
    output = tmp_path / "lyrics.json"
    output.write_text("{}")
    mix = _audio(tmp_path, "normalized.wav")

    transcription.transcribe(
        _audio(tmp_path, "vocals.wav"),
        output,
        model="small",
        audio_source="auto",
        mix=mix,
        timeout=600,
    )

    assert runner.arguments[runner.arguments.index("--audio-source") + 1] == "auto"
    assert runner.arguments[runner.arguments.index("--mix") + 1] == str(mix.resolve())
    # Two decoding passes get two passes' worth of wall clock, rather than the
    # second one running into the first one's deadline.
    assert runner.timeout == pytest.approx(1200)


def test_automatic_selection_needs_the_full_mix(
    tmp_path: Path, private_homes: Path, runner: _RecordingRunner
) -> None:
    with pytest.raises(InvalidInputError, match="full mix"):
        transcription.transcribe(
            _audio(tmp_path, "vocals.wav"),
            tmp_path / "lyrics.json",
            model="small",
            audio_source="auto",
        )
    assert runner.calls == 0


def test_an_unknown_audio_source_is_rejected(
    tmp_path: Path, private_homes: Path, runner: _RecordingRunner
) -> None:
    with pytest.raises(InvalidInputError, match="audio source"):
        transcription.transcribe(
            _audio(tmp_path, "vocals.wav"),
            tmp_path / "lyrics.json",
            model="small",
            audio_source="karaoke",
        )
    assert runner.calls == 0


def test_the_worker_argv_keeps_the_model_directly_after_the_output_path(
    tmp_path: Path, private_homes: Path, runner: _RecordingRunner
) -> None:
    output = tmp_path / "lyrics.json"
    output.write_text("{}")
    source = _audio(tmp_path, "vocals.wav")

    transcription.transcribe(source, output, model="small")

    model_index = runner.arguments.index("--model")
    assert runner.arguments[model_index - 1] == str(output.resolve())
    assert runner.arguments[model_index - 2] == str(source.resolve())
    assert runner.arguments[:3] == [sys.executable, "-m", "kilix_playalong._whisper_worker"]


def test_the_worker_is_handed_the_cache_path_everyone_else_reads(
    tmp_path: Path, private_homes: Path, runner: _RecordingRunner
) -> None:
    """The other half of ``test_asking_where_the_weights_live_creates_nothing``.

    ``--cache`` and ``HF_HOME`` are where the weights land, so a describer probing
    ``model_cache_path`` is only telling the truth while these are that path.
    """
    output = tmp_path / "lyrics.json"
    output.write_text("{}")

    transcription.transcribe(_audio(tmp_path, "vocals.wav"), output, model="small")

    expected = str(transcription.model_cache_path())
    assert runner.arguments[runner.arguments.index("--cache") + 1] == expected
    assert runner.env["HF_HOME"] == expected
    assert transcription.model_cache_path().is_dir()


@pytest.mark.parametrize(
    ("requested", "expected"),
    [("en", "en"), ("EN", "en"), ("en-GB", "en"), ("pt-BR", "pt")],
)
def test_a_language_tag_is_reduced_to_the_subtag_whisper_understands(
    tmp_path: Path,
    private_homes: Path,
    runner: _RecordingRunner,
    requested: str,
    expected: str,
) -> None:
    output = tmp_path / "lyrics.json"
    output.write_text("{}")

    transcription.transcribe(
        _audio(tmp_path, "vocals.wav"), output, model="small", language=requested
    )

    assert runner.arguments[runner.arguments.index("--language") + 1] == expected


@pytest.mark.parametrize("value", ["--device", "-x", "en GB", "", "e" * 40])
def test_a_language_that_could_be_read_as_an_option_never_reaches_the_worker(
    tmp_path: Path, private_homes: Path, runner: _RecordingRunner, value: str
) -> None:
    with pytest.raises(InvalidInputError, match="BCP 47"):
        transcription.transcribe(
            _audio(tmp_path, "vocals.wav"),
            tmp_path / "lyrics.json",
            model="small",
            language=value,
        )
    assert runner.calls == 0


def test_missing_audio_is_refused_before_a_subprocess_is_spawned(
    tmp_path: Path, private_homes: Path, runner: _RecordingRunner
) -> None:
    with pytest.raises(InvalidInputError, match="vocal stem"):
        transcription.transcribe(tmp_path / "absent.wav", tmp_path / "lyrics.json", model="small")
    assert runner.calls == 0


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), 10**308])
def test_an_unusable_timeout_is_a_user_error_not_a_value_error(
    tmp_path: Path, private_homes: Path, runner: _RecordingRunner, timeout: float
) -> None:
    """Including the value that only becomes unusable once it is doubled."""
    with pytest.raises(InvalidInputError, match="timeout"):
        transcription.transcribe(
            _audio(tmp_path, "vocals.wav"),
            tmp_path / "lyrics.json",
            model="small",
            audio_source="auto",
            mix=_audio(tmp_path, "normalized.wav"),
            timeout=timeout,
        )
    assert runner.calls == 0


# ---------------------------------------------------------------------------
# Worker end to end, against a STAND-IN faster-whisper. No weights are loaded.
# ---------------------------------------------------------------------------


class _FakeWord:
    def __init__(self, start: float, end: float, word: str) -> None:
        self.start = start
        self.end = end
        self.word = word


class _FakeSegment:
    def __init__(
        self,
        start: float,
        end: float,
        text: str,
        avg_logprob: float = -0.3,
        words: Sequence[_FakeWord] = (),
    ) -> None:
        self.start = start
        self.end = end
        self.text = text
        self.avg_logprob = avg_logprob
        self.words = list(words)


class _FakeModel:
    """Stand-in for ``WhisperModel``. Loads nothing, decodes nothing."""

    def __init__(
        self,
        segments: dict[str, list[_FakeSegment]],
        detection: tuple[str, float, list[tuple[str, float]]],
        languages: Sequence[str],
    ) -> None:
        self._segments = segments
        self._detection = detection
        self._languages = list(languages)
        self.transcribe_calls: list[tuple[str, dict[str, Any]]] = []
        self.detect_calls: list[dict[str, Any]] = []
        self.construction: dict[str, Any] = {}

    @property
    def supported_languages(self) -> list[str]:
        return self._languages

    def detect_language(self, **kwargs: Any) -> tuple[str, float, list[tuple[str, float]]]:
        self.detect_calls.append(kwargs)
        return self._detection

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[list[_FakeSegment], object]:
        self.transcribe_calls.append((str(audio), kwargs))
        return self._segments.get(str(audio), []), SimpleNamespace(language=kwargs["language"])


class _FakeVadOptions:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _install_stand_in(
    monkeypatch: pytest.MonkeyPatch,
    *,
    segments: dict[str, list[_FakeSegment]],
    spans: dict[str, list[dict[str, int]]] | None = None,
    detection: tuple[str, float, list[tuple[str, float]]] = ("en", 0.97, [("en", 0.97)]),
    languages: Sequence[str] = ("en", "de", "fr"),
) -> _FakeModel:
    """Inject a fake ``faster_whisper`` package for the duration of one test."""
    model = _FakeModel(segments, detection, languages)

    def build_model(name: str, **kwargs: Any) -> _FakeModel:
        model.construction = {"model": name, **kwargs}
        return model

    package = ModuleType("faster_whisper")
    package.WhisperModel = build_model  # type: ignore[attr-defined]
    package.decode_audio = lambda path, **_kwargs: f"audio:{path}"  # type: ignore[attr-defined]

    vad = ModuleType("faster_whisper.vad")
    vad.VadOptions = _FakeVadOptions  # type: ignore[attr-defined]
    vad.get_speech_timestamps = lambda audio, _options: (  # type: ignore[attr-defined]
        (spans or {}).get(str(audio), [])
    )

    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", vad)
    return model


def _run_worker(tmp_path: Path, *extra: str, source: str = "vocals.wav") -> dict[str, Any]:
    output = tmp_path / "lyrics" / "lyrics.json"
    assert (
        worker.main(
            [
                str(tmp_path / source),
                str(output),
                "--model",
                "large-v3",
                "--device",
                "cpu",
                "--cache",
                str(tmp_path / "cache"),
                *extra,
            ]
        )
        == 0
    )
    document: dict[str, Any] = json.loads(output.read_text())
    return document


def test_worker_pins_transcription_and_the_singing_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install_stand_in(
        monkeypatch,
        segments={f"audio:{tmp_path / 'vocals.wav'}": [_FakeSegment(0.0, 2.0, "a line")]},
    )

    _run_worker(tmp_path)

    (_audio, options) = model.transcribe_calls[0]
    # The product bug this closes: Whisper will otherwise translate a
    # foreign-language song into English and report success.
    assert options["task"] == "transcribe"
    assert options["language"] == "en"
    # The repetition-loop cause.
    assert options["condition_on_previous_text"] is False
    assert options["temperature"] == worker.TEMPERATURE_LADDER
    assert options["no_speech_threshold"] == worker.NO_SPEECH_THRESHOLD
    assert options["log_prob_threshold"] == worker.LOG_PROB_THRESHOLD
    assert options["compression_ratio_threshold"] == worker.COMPRESSION_RATIO_THRESHOLD
    assert options["hallucination_silence_threshold"] == worker.HALLUCINATION_SILENCE_THRESHOLD
    assert options["initial_prompt"] is None
    assert options["word_timestamps"] is True
    assert options["vad_filter"] is True
    assert isinstance(options["vad_parameters"], _FakeVadOptions)
    assert options["vad_parameters"].kwargs == worker.VAD_PARAMETERS


def test_worker_records_the_detected_language_and_its_confidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stand_in(
        monkeypatch,
        segments={f"audio:{tmp_path / 'vocals.wav'}": [_FakeSegment(0.0, 2.0, "eine Zeile")]},
        detection=("de", 0.84, [("de", 0.84), ("en", 0.10)]),
    )

    document = _run_worker(tmp_path)

    assert document["language"] == "de"
    receipt = transcription.parse_receipt(document["source"])
    assert receipt is not None
    assert receipt.model == "large-v3"
    assert receipt.language == "de"
    assert receipt.language_from == "detected"
    assert receipt.language_confidence == pytest.approx(0.84)
    assert receipt.audio_source == "vocals"
    assert receipt.audio_from == "requested"


def test_an_explicit_language_overrides_detection_and_records_its_own_confidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install_stand_in(
        monkeypatch,
        segments={f"audio:{tmp_path / 'vocals.wav'}": [_FakeSegment(0.0, 2.0, "une ligne")]},
        detection=("en", 0.55, [("en", 0.55), ("fr", 0.31)]),
    )

    document = _run_worker(tmp_path, "--language", "fr")

    assert model.transcribe_calls[0][1]["language"] == "fr"
    assert document["language"] == "fr"
    receipt = transcription.parse_receipt(document["source"])
    assert receipt is not None
    assert receipt.language == "fr"
    assert receipt.language_from == "requested"
    # The detector disagreed; the receipt says so instead of hiding it.
    assert receipt.language_confidence == pytest.approx(0.31)


def test_language_detection_looks_past_an_instrumental_intro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install_stand_in(
        monkeypatch,
        segments={f"audio:{tmp_path / 'vocals.wav'}": [_FakeSegment(0.0, 2.0, "a line")]},
    )

    _run_worker(tmp_path)

    assert model.detect_calls[0]["language_detection_segments"] == 4
    assert model.detect_calls[0]["vad_filter"] is True


def test_an_english_only_model_reports_where_its_language_came_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install_stand_in(
        monkeypatch,
        segments={f"audio:{tmp_path / 'vocals.wav'}": [_FakeSegment(0.0, 2.0, "a line")]},
        languages=("en",),
    )

    document = _run_worker(tmp_path)

    assert model.detect_calls == []
    receipt = transcription.parse_receipt(document["source"])
    assert receipt is not None
    assert receipt.language == "en"
    assert receipt.language_from == "model"


def test_a_language_the_model_cannot_do_fails_instead_of_transcribing_anyway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install_stand_in(
        monkeypatch,
        segments={f"audio:{tmp_path / 'vocals.wav'}": [_FakeSegment(0.0, 2.0, "a line")]},
        languages=("en",),
    )

    exit_code = worker.main(
        [
            str(tmp_path / "vocals.wav"),
            str(tmp_path / "lyrics.json"),
            "--model",
            "small.en",
            "--cache",
            str(tmp_path / "cache"),
            "--language",
            "de",
        ]
    )

    assert exit_code == 2
    assert model.transcribe_calls == []
    assert not (tmp_path / "lyrics.json").exists()


def test_worker_drops_artefacts_before_writing_the_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = f"audio:{tmp_path / 'vocals.wav'}"
    _install_stand_in(
        monkeypatch,
        segments={
            marker: [
                _FakeSegment(0.5, 3.0, "the only real line", words=[_FakeWord(0.5, 1.0, " the")]),
                _FakeSegment(3.2, 8.0, "Thanks for watching!"),
                _FakeSegment(8.1, 12.0, " ".join(["run"] * 30)),
                _FakeSegment(40.0, 44.0, "invented over the outro"),
            ]
        },
        spans={marker: [{"start": 0, "end": 16000 * 20}]},
    )

    document = _run_worker(tmp_path)

    assert [cue["text"] for cue in document["cues"]] == ["the only real line"]
    assert document["cues"][0]["words"] == [{"start": 0.5, "end": 1.0, "text": "the"}]


def test_the_document_is_private_from_the_first_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lyrics are user content; there must be no world-readable window.

    The umask is set for the duration of the run, and that is the whole point of
    the test rather than housekeeping: on a machine whose umask is already 0077
    a plain ``write_text`` also lands at 0600, so this assertion would hold with
    the privacy removed and would only fail for whoever ran the suite under a
    permissive umask. Forcing 0022 makes the difference between ``private_write``
    and a plain write observable everywhere, which is what the assertion claims.
    """
    _install_stand_in(
        monkeypatch,
        segments={f"audio:{tmp_path / 'vocals.wav'}": [_FakeSegment(0.0, 2.0, "a line")]},
    )

    previous = os.umask(0o022)
    try:
        _run_worker(tmp_path)
    finally:
        os.umask(previous)

    assert (tmp_path / "lyrics" / "lyrics.json").stat().st_mode & 0o777 == 0o600


def test_automatic_selection_runs_both_sources_and_records_the_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vocals = f"audio:{tmp_path / 'vocals.wav'}"
    mix = f"audio:{tmp_path / 'normalized.wav'}"
    model = _install_stand_in(
        monkeypatch,
        segments={
            vocals: [_FakeSegment(0.0, 30.0, "smeared stem line", avg_logprob=-0.9)],
            mix: [_FakeSegment(0.0, 30.0, "clear mix line", avg_logprob=-0.15)],
        },
    )

    document = _run_worker(
        tmp_path, "--audio-source", "auto", "--mix", str(tmp_path / "normalized.wav")
    )

    assert len(model.transcribe_calls) == 2
    assert [cue["text"] for cue in document["cues"]] == ["clear mix line"]
    receipt = transcription.parse_receipt(document["source"])
    assert receipt is not None
    assert receipt.audio_source == "mix"
    assert receipt.audio_from == "auto:score"


def test_automatic_selection_keeps_the_stem_on_a_tie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vocals = f"audio:{tmp_path / 'vocals.wav'}"
    mix = f"audio:{tmp_path / 'normalized.wav'}"
    _install_stand_in(
        monkeypatch,
        segments={
            vocals: [_FakeSegment(0.0, 30.0, "stem line", avg_logprob=-0.30)],
            mix: [_FakeSegment(0.0, 30.0, "mix line", avg_logprob=-0.28)],
        },
    )

    document = _run_worker(
        tmp_path, "--audio-source", "auto", "--mix", str(tmp_path / "normalized.wav")
    )

    assert [cue["text"] for cue in document["cues"]] == ["stem line"]
    receipt = transcription.parse_receipt(document["source"])
    assert receipt is not None
    assert receipt.audio_source == "vocals"
    assert receipt.audio_from == "auto:tie"


def test_the_mix_alone_transcribes_only_the_mix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mix = f"audio:{tmp_path / 'normalized.wav'}"
    model = _install_stand_in(
        monkeypatch,
        segments={mix: [_FakeSegment(0.0, 30.0, "mix line")]},
    )

    document = _run_worker(
        tmp_path, "--audio-source", "mix", "--mix", str(tmp_path / "normalized.wav")
    )

    assert [call[0] for call in model.transcribe_calls] == [mix]
    receipt = transcription.parse_receipt(document["source"])
    assert receipt is not None
    assert receipt.audio_source == "mix"
    assert receipt.audio_from == "requested"


def test_the_worker_refuses_a_second_source_it_was_not_given(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as failure:
        worker.main(
            [
                str(tmp_path / "vocals.wav"),
                str(tmp_path / "lyrics.json"),
                "--model",
                "small",
                "--cache",
                str(tmp_path / "cache"),
                "--audio-source",
                "auto",
            ]
        )

    assert failure.value.code == 2
