# Kilix Playalong

Kilix Playalong turns a YouTube song URL into a private guitar-practice project:
independently controllable audio stems, timed lyrics, a MIDI transcription of
the isolated guitar stem, a timed guitar tab, and a self-contained printable
song sheet. Processing stays on the local machine after the explicitly
requested download.

The generated tab and lyrics timing are drafts. Source separation and automatic
music transcription are imperfect, especially for dense mixes, doubled guitars,
unusual tunings, bends, slides, and fast polyphony. Verify the result by ear.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for stage contracts, cache
invalidation, fingering inference, player synchronization, and security
boundaries.

## What it provides

- YouTube intake through a locked `yt-dlp`. Non-HTTPS or non-YouTube hosts, URLs
  carrying credentials or a non-standard port, and playlist URLs are rejected
  before any network call at all; the path is percent-decoded, lowercased and
  re-split before that compare, so `//playlist` and `/%70laylist` are caught
  there too, not only a plain `/playlist` or a `/watch?list=` with no `v=`.
  Live and upcoming streams, a source with no finite positive duration, songs
  longer than 30 minutes, and a playlist that reached the extractor by a
  spelling the pre-network gate does not model are rejected from the metadata
  document, before any media is fetched. A `watch?v=ID&list=LIST` URL is
  narrowed to the single requested video by `--no-playlist`, never expanded.
- Downloads carry a 512 MiB `--max-filesize` ceiling, and the finished file is
  re-checked against that same limit on disk. yt-dlp can refuse a transfer up
  front only where the server declares the size; a segmented download is bounded
  while it runs by the 30-minute duration gate and the `bestaudio/best` format
  choice rather than by any byte counter, so an oversized segmented source is
  rejected only once its bytes have been written.
- Six-stem Demucs separation (`vocals`, `drums`, `bass`, `guitar`, `piano`, and
  `other`) with a common playback clock plus mute and level controls for every
  stem.
- Timed YouTube captions when available, user-supplied LRC/SRT/VTT/plain text,
  or local faster-whisper transcription of the isolated vocal stem.
- Basic Pitch ONNX transcription of the isolated guitar stem to MIDI, followed
  by deterministic string/fret inference for standard, Drop D, or DADGAD tuning.
- A local play-along page with synchronized lyrics and tab highlighting,
  selectable lyrics, tempo control, seeking, keyboard control, and a vocals-off
  practice preset.
- Printable HTML and ASCII tab exports. Printed lyrics and tab can each be
  hidden independently.
- Resumable, checksummed stages and private XDG storage backed by the shared
  `kilix-state` library.

## Rights and privacy

Only process media and lyrics you are authorized to use. The command requires an
explicit `--i-have-rights` confirmation and records that decision; it cannot
determine ownership for you. Downloaded media, separated stems, lyrics, MIDI,
tabs, and exports are derivative/private user content. They are never committed,
uploaded, or used as telemetry by this application.

The player binds to `127.0.0.1` only, uses a random per-launch capability in its
URL, validates the HTTP Host header, exposes a read-only route set, and sends a
restrictive Content Security Policy. That capability is the only authenticator,
and it is not a secret the command can keep to itself:

- `serve` always prints the full URL, capability included, on stdout. Anything
  that records the terminal (a logging wrapper, `script`, a scrollback file, a
  pasted transcript) records the capability with it. `--no-open` does not
  change this.
- Without `--no-open`, `serve` also hands the URL to the browser as a
  command-line argument. `/proc` is world-readable on a normal Linux system, so
  any local user can read it out of the browser process's `cmdline` while that
  process lives.
- Once the URL reaches a browser it outlives this process: browser history and
  session-restore data keep it on disk after `serve` exits. The capability stops
  working then, because each launch mints a new one, but the recorded URL does
  not disappear with it.

On a shared machine run `serve --no-open`, paste the printed URL yourself, and
treat terminal logs and browser history as places the capability now lives. The
`--no-open` behaviour is documented but unasserted: no test drives `serve`'s
browser launch, so that mitigation is protected by review only.

URLs and local paths are redacted from provider errors. No cookies, browser
profiles, credentials, or DRM-bypass options are passed to `yt-dlp`. Every
provider receives a minimal allowlisted environment and a disposable private
home directory, so unrelated session credentials and Python import paths do not
cross the worker boundary.

YouTube access and model downloads remain network operations governed by their
providers' current terms. Model code licenses do not automatically establish
the rights of model weights or source media. `--allow-model-downloads` is an
explicit per-run decision; without it, missing Hugging Face/Demucs weights fail
offline instead of downloading silently.

## Requirements

- Linux or another POSIX platform
- `uv` 0.12.x
- FFmpeg and ffprobe
- Git submodules
- Enough free storage for the source, normalized PCM, and six lossless stems
- For the complete pipeline, a supported CPU or CUDA GPU and several gigabytes
  for PyTorch, model runtimes, and weights
- For the ML providers, network access to `github.com`: `demucs` is pinned to a
  source commit of `adefossez/demucs` rather than to a PyPI release, so
  `make setup-all` builds it from that repository, the pinned commit hash is
  its only integrity anchor, and installation fails if the repository or that
  commit becomes unreachable

The project pins Python 3.10 in `.python-version`. `uv` obtains that interpreter
when it is not already installed.

## Setup

```bash
git clone --recurse-submodules https://github.com/itsmygithubacct/kilix-playalong.git
cd kilix-playalong
make setup-all
make check
uv run --frozen kilix-playalong doctor
```

`make setup` installs only the small command, test, and state-storage runtime.
`make setup-all` additionally installs the locked Demucs, Basic Pitch ONNX, and
faster-whisper providers. Both use the committed `uv.lock`; no `pip` workflow is
supported.

## Create and play

```bash
uv run --frozen kilix-playalong create \
  'https://www.youtube.com/watch?v=VIDEO_ID' \
  --i-have-rights \
  --allow-model-downloads

uv run --frozen kilix-playalong list
uv run --frozen kilix-playalong serve song-PROJECT_ID
```

Supply known lyrics or choose another tuning when appropriate:

```bash
uv run --frozen kilix-playalong create 'https://youtu.be/VIDEO_ID' \
  --i-have-rights \
  --lyrics song.lrc \
  --tuning drop-d \
  --max-fret 22 \
  --allow-model-downloads
```

The default `--whisper-model auto` favors `large-v3` when CUDA or at least
10 GiB of memory is available, then steps down through `large-v3-turbo`,
`medium`, and `small` on tighter systems. Without model-download permission it
uses the strongest compatible model already in the private cache. An explicit
model name always overrides this selection.

The create command prints the project id and printable export path. An
interrupted project resumes from verified artifacts:

```bash
uv run --frozen kilix-playalong resume song-PROJECT_ID --allow-model-downloads
```

## Keyboard controls

| Key | Action |
| --- | --- |
| Space | Play or pause |
| Left / Right | Seek five seconds |
| `V` | Toggle vocals |
| `L` | Show or hide lyrics |

## Storage and recovery

Projects live below `${XDG_DATA_HOME:-$HOME/.local/share}/kilix-playalong/projects`.
Model caches use `${XDG_CACHE_HOME:-$HOME/.cache}/kilix-playalong`. Directories
are private to the current user. Each stage records its provider, timestamps,
artifact sizes, and SHA-256 digests. Resume skips only artifacts that still
match; a changed artifact invalidates every downstream stage.

The project manifest uses `kilix.playalong.project/v1`. Timed lyrics and tab use
`kilix.playalong.lyrics/v1` and `kilix.playalong.tab/v1`. Uninstalling the code
does not delete projects or exports.

## Development

```bash
make setup
make check
make smoke-ml
uv run --frozen pytest
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen mypy
```

`make check` runs `uv lock --check` first and lets nothing else start until it
passes, so a `pyproject.toml` edit that was never re-locked fails the gate
instead of silently resolving against a stale `uv.lock`. That ordering is a
recipe rather than a prerequisite list, because prerequisites are unordered
under `make -j`, and `tests/test_build_gate.py` asserts it by running this
Makefile under `-j8`. Every `uv run` in this README passes `--frozen`, so no
documented command rewrites the committed lock; run `uv lock` deliberately when
a dependency really changes. `uv lock --check` only proves the lock still
satisfies `pyproject.toml`, not that it is the resolution uv would produce
today.

Tests use generated metadata, MIDI, lyrics, and tiny original waveforms only.
No downloaded song, lyric corpus, stem, model weight, or credential belongs in
this repository.
