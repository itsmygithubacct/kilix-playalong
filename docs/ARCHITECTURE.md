# Architecture

Kilix Playalong is a local batch pipeline followed by a read-only loopback web
player. The batch side owns acquisition and analysis; the browser never starts
jobs, reads arbitrary files, or talks to third-party services.

## Project flow

| Stage | Provider | Stable output |
| --- | --- | --- |
| Download | locked `yt-dlp` module | one source media file and optional VTT captions |
| Normalize | FFmpeg | 44.1 kHz stereo PCM WAV |
| Separate | pinned Demucs revision | independently addressable lossless stems |
| Lyrics | captions, supplied text, or faster-whisper | versioned timed-cue JSON |
| Transcribe guitar | Basic Pitch ONNX child | MIDI and stable note-event JSON |
| Tablature | internal deterministic dynamic program | timed string/fret JSON and ASCII tab |
| Export | internal renderer | self-contained printable HTML |

Every stage records its provider, settings fingerprint, timestamps, artifact
size, and SHA-256 digest in `project.state`. Resume accepts a cached stage only
when its fingerprint and every artifact still match. A changed input or setting
invalidates the affected stage and all downstream stages.

Provider calls run as argument vectors without a shell, in separate process
groups with timeouts, incrementally bounded diagnostics, private caches, a
minimal allowlisted environment, a disposable private home, and path/URL
redaction. The optional heavyweight Python providers run in child interpreters
so their imports and failures do not destabilize the command process. Provider
wrappers must pass any required cache or module path explicitly.

## Private project data

Projects use the XDG data directory and have this logical shape:

```text
projects/song-…/
  project.state
  source/
  media/normalized.wav
  stems/{vocals,drums,bass,guitar,piano,other}.wav
  lyrics/lyrics.json
  midi/{guitar.mid,guitar-notes.json}
  tab/guitar-tab.json
  exports/{guitar-tab.txt,playalong.html}
```

Directories are created with mode `0700` and artifacts with mode `0600`.
Manifests are atomically persisted with integrity checking through the shared
`kilix-state` module. The original URL exists only inside this private manifest;
normal CLI inspection omits it.

## Fingering inference

Note events within a short onset window become chord candidates. For each pitch,
the engine enumerates playable string/fret positions for the selected tuning and
fret limit, rejects duplicate-string and excessive-span combinations, and then
uses dynamic programming across time. The cost favors small fret spans, modest
hand positions, compact string use, stable pitch-to-string assignment, and small
position shifts. Identical input and settings therefore produce identical tab.

This is a practical fingering estimate, not score understanding: bends, slides,
harmonics, alternate voices, and performance technique are not inferred.

## Player clock

The web player creates one audio element per stem. One ready stem is the master
timeline; play, pause, seek, and rate changes are applied as a transaction to all
elements. A lightweight correction loop brings stems that exceed the drift
threshold back to the master time. Buffering is treated as a group condition so
one stalled stem cannot quietly leave the mix out of phase.

Lyrics and tab are indexed by their start times and selected against that same
clock. Muting vocals or any individual track changes only its audible level, not
its timeline, preserving synchronization when it is restored.

## Loopback boundary

`serve` binds only to `127.0.0.1` and creates a new high-entropy URL capability
for each launch. The server validates `Host`, accepts only `GET` and `HEAD` on a
small route table, supports byte ranges for stems, disables caching and
referrers, and sends a restrictive Content Security Policy. The browser client
uses same-origin data only and constructs user-controlled text with DOM text
nodes rather than HTML injection.

## Verification

`make check` is the entire gate: `uv lock --check` runs first and nothing else
starts until it passes, confirming that the committed `uv.lock` still matches
`pyproject.toml`; the non-ML test suite, `ruff check`, `ruff format --check`,
and `mypy` follow. The two `ruff` gates cover `src` and `tests`; `mypy` is
given no path and `pyproject.toml` sets `packages = ["kilix_playalong"]`, so
the typing gate reads the package only and never the tests. That ordering lives
in the `check` recipe rather than in a prerequisite list, because sibling
prerequisites are unordered under `make -j`, and `tests/test_build_gate.py`
asserts it by running the Makefile under `-j8` against a stub `uv`.
`uv lock --check` proves only that the lock still satisfies `pyproject.toml`,
not that it is the resolution uv would produce today: a hand-edited `uv.lock`
pinning an older but still in-range transitive passes it. Every uv-invoking
target depends on the kilix-state library target, so a clone made without
`--recurse-submodules` fails with make's submodule build error rather than
with uv's unresolvable path source. The repository ships no CI configuration
and no `[project.urls]` metadata, so nothing runs that gate automatically; it
is a local discipline, and a change that never had `make check` run against it
can still reach the branch. Nothing enforces formatting or the lock outside it
either: there is no pre-commit hook.

Provider isolation is verified only on the non-ML path. Unit tests drive
`run_command` with a plain Python child and assert the allowlisted environment,
the rejection of unsafe explicit overrides, the disposable private home, the
bounded diagnostics, and error redaction.

The runner tears the child's process group down on every abnormal exit from the
call once the child exists, not on timeout alone: the wall-clock deadline, the
per-stream diagnostic cap, a `KeyboardInterrupt`, and any other exception raised
while the child runs all reach the same teardown, which signals the child's
whole session with `SIGTERM` and then `SIGKILL`. A second interrupt does not
escape it. The teardown blocks `SIGINT`, `SIGTERM`, `SIGHUP`, and `SIGQUIT` in
the calling thread for its duration and escalates to `SIGKILL` from a `finally`,
so the escalation runs even if the `SIGTERM` wait raises; blocking defers
delivery rather than discarding it, and a held signal fires as soon as the mask
is restored. What the teardown bounds is its own wait, not the group's lifetime:
it allows three seconds at each step and waits only on the group leader, so it
can return while a signalled grandchild is still dying.

`runner.py` names three residuals that stay outside that guarantee, each at the
point it arises. A `SIGTERM`, `SIGHUP`, `SIGQUIT`, or `SIGKILL` delivered to the
command process itself outside a teardown window runs no teardown at all:
`SIGKILL` cannot be handled, an importable library must not install
process-wide handlers for the rest, and the kernel-level alternative
`PR_SET_PDEATHSIG` is rejected because reaching it means `preexec_fn`, which is
documented as unsafe in a threaded caller; a supervisor that needs that
guarantee should use a cgroup. A grandchild that calls `setsid` has left the
process group the teardown addresses, bounded only by every argv here being
built by this package's own provider modules, none of which daemonises. And one
instruction boundary inside `Popen._execute_child` is not closable from outside
CPython: `_posixsubprocess.fork_exec` forks the child and `self.pid` is stored
on the very next bytecode, so a signal delivered between the two leaves a live
child that this process cannot name. The much wider window that follows, CPython
reading the exec errpipe at a PEP 475 handler point, is closed by constructing
the `Popen` in two steps and guarding teardown on `pid is not None`.

Coverage of the teardown is pinned by observation, not by inference. The suite
watches a real provider pid disappear on every interrupt path it claims: a
single interrupt to the parent, a second interrupt arriving during the teardown
of a `SIGTERM`-ignoring child, another teardown signal landing inside the
teardown window, and an interrupt raised out of a real `_execute_child` once
the fork has already happened. The signalling itself is pinned separately: an
`os.killpg` spy asserts the escalation is exactly `SIGTERM` then `SIGKILL`
against a single process group, that the diagnostic-cap path signals through
the same route, and that a provider which exited on its own is never signalled
at all; a further test asserts the caller's signal mask comes back exactly as
it was. No teardown path is now asserted only through the error it raises.

That is a claim about what the suite pins, deliberately not a count of tests.
An earlier version of this paragraph enumerated them and went stale three times
against a test file that was still moving.

No ML-marked test reaches a model runtime through `run_command`: `make smoke-ml`
starts the Basic Pitch worker with `subprocess.run` under the ambient
environment, so it verifies the worker's own contract and nothing about the
boundary the pipeline actually crosses. Until an ML-marked test invokes a
provider wrapper through `run_command`, the ML path of that boundary is
unverified by the test suite. Demucs and faster-whisper are weaker still: no
test loads either runtime. Separation is stubbed out entirely, and the
faster-whisper wrapper is driven only by non-ML tests that replace
`run_command` itself with a stub worker, so `make setup-all && make smoke-ml`
passing says nothing about separation or transcription.

The loopback boundary's own mitigation is undertested in the same way. `serve`
prints the capability URL unconditionally and, without `--no-open`, also passes
it to `webbrowser.open`; no test drives that launch or the flag, so the
documented shared-machine mitigation is protected by review rather than by the
gate. The suite's one mention of `webbrowser` is a denylist of names the
request handler must not reference, which is a different property.
