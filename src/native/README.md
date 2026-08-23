# kilix-playalong-native

The C11 practice surface. It only ever *reads* what the Python pipeline
wrote; nothing here creates, edits or deletes a project.

Rationale and design history live under `~/research`. This file is
implementation-facing: what each file owns, how to build it, where the
security boundary is, and what does not work.

## Files

Each module owns one header in `include/kilix_playalong/`. The headers are
the contract; a module reaches another module only through one.

| File | Owns |
| --- | --- |
| `kpa_json.c` | Bounded read-only RFC 8259 parser. One caller-owned arena, no allocation after `kpa_json_parse` returns. Nodes index the arena rather than pointing into it. |
| `kpa_project.c` | The three project schemas (`project/v1`, `lyrics/v1`, `tab/v1`), the private store listing, id validation, and **artifact resolution** — see below. Also the cue/word/event cursors. |
| `kpa_audio.c` | File-backed streaming multitrack playback with one clock: decode threads, a single-producer ring, the SDL callback, gain/mute/solo, sample-exact looping, WSOLA rate control, and the audible-frame estimate. |
| `kpa_cells.c` | The UTF-8 terminal-cell overlay: validation, width, character-boundary truncation, row erase, synchronised-update framing. |
| `kpa_ui.c` | `kpa_ui_compose` (pure pixel composition), the cell layout, the key table, and `kpa_ui_run` (the event loop, the audio plumbing, the overlay). |
| `main.c` | Argument parsing, the exit codes, `--list`, `--doctor`, `--render`. Decides every exit code *before* the surface starts, because `kpa_ui_run` reports only clean/not-clean. |

Tests are one binary per module under `tests/native/`, discovered by
wildcard. `tests/native/test_ui.c` reaches `kpa_ui_internal_apply_key`,
which `kpa_ui.c` exports deliberately and `kpa_ui.h` does not declare: the
key table has to be testable without a terminal.

## Build and test

```sh
make -f Makefile.native native         # build/native/kilix-playalong-native
make -f Makefile.native native-test    # every suite, one binary each
make -f Makefile.native native-sanitize  # the same suites under ASan+UBSan
make -f Makefile.native native-clean
make -f Makefile.native native-install PREFIX=/usr/local
```

`native-test` also links the command, so a broken `main.c` cannot pass
unnoticed. `native-sanitize` builds into `build/sanitize/` rather than
sharing the object tree, so a later plain `native` cannot silently link
sanitized objects; it runs with `detect_leaks=1` and
`halt_on_error=1`.

Both `-D_POSIX_C_SOURCE=200809L` **and** `-D_DEFAULT_SOURCE` are required
everywhere. `_POSIX_C_SOURCE` alone leaves `S_ISVTX` undeclared in the
vendored `kilix_state.c` and the build fails inside a file that has nothing
to do with the change you made.

Dependencies are probed, not assumed: `native-deps` runs before the first
compile and names the Debian package for whatever is missing
(`libsdl2-dev`, `libsndfile1-dev`, `zlib1g-dev`, `pkg-config`), or tells
you to run `git submodule update --init --recursive`.

Vendored sources build with `-Wno-conversion -Wno-sign-conversion` so their
output cannot bury ours. Every other warning still applies to them. Our own
files are warning-clean under the full set.

## The artifact-resolution boundary

**`kpa_project_open_artifact` is the only sanctioned way to reach project
bytes.** It resolves a manifest's relative path one component at a time
beneath a project-directory descriptor held open for the project's
lifetime, with `O_NOFOLLOW` at each component, and returns a read-only
descriptor the caller owns.

That is what keeps a manifest path from naming `/etc/shadow`. An absolute
path, a `..`, a symlink, or a path swapped between validation and open is
refused rather than followed — the check and the open are the same
operation, so there is no window between them.

The rule for callers: **never build a path and call `open`.** `kpa_ui.c`
opens every stem through `kpa_project_open_artifact` and hands the audio
session a descriptor; `kpa_audio_add_track` takes a borrowed fd and dups
it, and has no path-shaped argument to abuse. The one `open()` in
`kpa_ui.c` is `/dev/tty` for the overlay's own output descriptor, which is
not project content.

## Limits

Stated plainly, because a limit that is not written down is a bug report
later.

**Rate control** is available in this build. The WSOLA engine is qualified
by measurement, not by assertion: `tests/native/test_audio.c` renders a
sine and measures its fundamental at 0.75x, 1.0x and 1.25x (currently
440.000 Hz, 440.000 Hz and 440.003 Hz — 0.000%, 0.000% and 0.001% off).
`KPA_RATE_ENGINE_QUALIFIED` in `kpa_audio.c` is the single switch: if that
measurement ever stops passing, set it false and `kpa_audio_set_rate`
returns `KPA_AUDIO_RATE_UNAVAILABLE` and changes nothing, because a guitar
part transposed by a semitone is worse than one that plays at full speed.
The range is 0.5x to 2.0x. `kpa_audio_rate_available` is what the UI shows;
it is false when there is no session at all.

**Unicode width is an approximation**, generated from Unicode 15.1.0
(Python 3.13's `unicodedata`). Where it ends:

- No grapheme clustering. Width is summed per code point, so an emoji ZWJ
  sequence measures as the sum of its parts — a two-person family emoji
  reports 4 columns, not 2 — and a regional-indicator flag reports 2.
  Terminals disagree with each other about clusters; per code point is the
  only reproducible answer, and it is what `kpa_cells_fit` is written
  against.
- Category Mc (spacing combining marks, e.g. U+0903) counts one column.
- The rest of category Cf — U+00AD and the Arabic/Kaithi prepended
  concatenation marks — counts one column, matching xterm and kitty.
- Unassigned code points outside the CJK default-wide blocks count one
  column. A later Unicode release can assign a wide character there, and
  the table is one column short for it until it is regenerated.

Lyrics, titles and artists are UTF-8 and go through `kpa_cells.c`. The
embedded raster font is ASCII bitmaps, so anything drawn in the pixel layer
is ASCII-only by construction — see the next point.

**Cell-only fallback.** When the terminal has no graphics path,
`kittyts_start` is retried with the probe off and the model's `cell_only`
is set. `kpa_ui_compose` then draws *nothing at all* and the whole surface
arrives as terminal cells. Against the pixel layer, cell-only omits:

- the timeline bar, its playhead and its two loop markers (the loop is
  shown as `loop m:ss-m:ss` text on the transport line instead);
- the numeric gain value — the mixer row keeps the `[=====.....]` meter,
  the `M`/`S` flags and the selection marker, but not the `1.00`;
- the proportional-time tab lane. The cell lane is fixed at 4 columns per
  second with the playhead a quarter of the way in, and it does **not**
  brighten the notes currently sounding — every fret on a string row is
  drawn in one colour;
- the dimming that marks a stem as inaudible because another stem is
  soloed.

Lyrics, the transport, the mixer, the tab and both the library and help
views all reach the player in cell-only.

**Non-ASCII in the pixel layer.** The title row and the lyric band are
cells and render any script the terminal can. The library list, the mixer
labels and the tab gutter in the *pixel* layer go through the ASCII raster
font, so a non-ASCII stem label or project title degrades there. It is
correct in cell-only and in the title row, which is where a title is
actually read.

**Frame tearing between the two layers.** `kitty-framebuffer` encodes and
writes its frames on its own thread and exposes no output lock, so a cell
row written by the overlay can land inside a graphics packet. The cost is a
glitched frame, not a wedged terminal — both layers are rewritten on the
next redraw. A real fix needs an output lock in `kitty-framebuffer` and is
still open (`kpa_ui.c`, `draw_frame`).

**No audio device means no surface.** `main.c` probes a real
`SDL_OpenAudioDevice` before starting and exits 4 if it fails, so a
headless machine cannot use the player to read tab or lyrics.
`SDL_AUDIODRIVER=dummy` opens successfully and is enough to run the surface
without hardware.

## Exit codes

`0` ok · `1` unexpected failure · `2` invalid input · `3` no such project ·
`4` no audio device · `5` no usable terminal.

`5` is only the no-terminal case: a terminal without a graphics path falls
back to cell-only, so a plain xterm is supported and only a pipe has no
fallback left. `--doctor` exits 0 whenever it produced a report — read the
fields for the verdict, not the status.
