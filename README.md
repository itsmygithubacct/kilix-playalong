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

- YouTube intake through a locked `yt-dlp`, with playlists, live streams,
  credentials, custom ports, oversized files, and overlong songs rejected.
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
restrictive Content Security Policy. URLs and local paths are redacted from
provider errors. No cookies, browser profiles, credentials, or DRM-bypass options
are passed to `yt-dlp`.

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

The project pins Python 3.10 in `.python-version`. `uv` obtains that interpreter
when it is not already installed.

## Setup

```bash
git clone --recurse-submodules https://github.com/itsmygithubacct/kilix-playalong.git
cd kilix-playalong
make setup-all
make check
uv run kilix-playalong doctor
```

`make setup` installs only the small command, test, and state-storage runtime.
`make setup-all` additionally installs the locked Demucs, Basic Pitch ONNX, and
faster-whisper providers. Both use the committed `uv.lock`; no `pip` workflow is
supported.

## Create and play

```bash
uv run kilix-playalong create 'https://www.youtube.com/watch?v=VIDEO_ID' \
  --i-have-rights \
  --allow-model-downloads

uv run kilix-playalong list
uv run kilix-playalong serve song-PROJECT_ID
```

Supply known lyrics or choose another tuning when appropriate:

```bash
uv run kilix-playalong create 'https://youtu.be/VIDEO_ID' \
  --i-have-rights \
  --lyrics song.lrc \
  --tuning drop-d \
  --max-fret 22 \
  --allow-model-downloads
```

The create command prints the project id and printable export path. An
interrupted project resumes from verified artifacts:

```bash
uv run kilix-playalong resume song-PROJECT_ID --allow-model-downloads
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
uv run pytest
uv run ruff check src tests
uv run mypy
```

Tests use generated metadata, MIDI, lyrics, and tiny original waveforms only.
No downloaded song, lyric corpus, stem, model weight, or credential belongs in
this repository.
