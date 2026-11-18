# Kilix Playalong

Kilix Playalong turns a YouTube song URL into a private guitar-practice project:
independently controllable audio stems, timed lyrics, a MIDI transcription of
the isolated guitar stem, a timed guitar tab, and a self-contained printable
song sheet. Processing stays on the local machine after the explicitly
requested download; the one other request is an optional LRCLIB lyrics lookup by
title and artist, which `--no-lyrics-lookup` turns off.

Installable on demand from the Kilix content catalog as `playalong`
(`kilix install playalong`). `kilix playalong [ARGS]` installs the pinned
build on first use and runs the command in the current pane; the catalog
builds it with `make setup` and runs `.venv/bin/kilix-playalong`.

The generated tab and lyrics timing are drafts. Source separation and automatic
music transcription are imperfect, especially for dense mixes, doubled guitars,
unusual tunings, bends, slides, and fast polyphony. Verify the result by ear.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for stage contracts, cache
invalidation, fingering inference, player synchronization, and security
boundaries.

## What it does

- Takes a YouTube link or a local audio or video file, after an explicit
  `--i-have-rights` confirmation.
- Separates six stems with Demucs, times lyrics from captions, supplied text,
  LRCLIB's published lyrics or faster-whisper, and transcribes the guitar stem
  to MIDI with Basic Pitch.
- Infers string and fret positions for standard, Drop D or DADGAD tuning and
  writes a timed tab.
- Plays the stems together in a loopback web player with synchronized lyrics
  and tab, mute and level per stem, tempo control and a vocals-off preset.
- Exports printable HTML and ASCII tab, and an offline songbook folder.
- Resumes interrupted projects from checksummed stage artifacts.

The full list, including the intake and download limits, is in
[docs/features.md](docs/features.md).

## Models

This repository never downloads model weights. Every model it runs is a Kilix
Content asset with a kilix-license record:

| Stage | Asset | Licence |
| --- | --- | --- |
| Stem separation | `demucs-htdemucs-6s` | MIT |
| Guitar notes | `basic-pitch-icassp-2022-onnx` | Apache-2.0 |
| English lyrics (default) | `faster-whisper-medium-en` | MIT |
| Lyrics in any language (optional) | `faster-whisper-large-v3` | MIT |

Copyright holders are listed in
[docs/rights-and-privacy.md](docs/rights-and-privacy.md).
`kilix-playalong models install` runs `kilix models install` for each of the
first three (`--large-v3` adds the fourth); Kilix shows the licence, writes the
receipt and fetches the pinned files. Before a stage runs, it checks its asset
with `kilix models verify` and refuses with the install command if the check
fails. `kilix-playalong models` reports what is installed. Rights, privacy and
the player's loopback capability are described in
[docs/rights-and-privacy.md](docs/rights-and-privacy.md).

## Quick start

Requires `uv` 0.12.x, FFmpeg and ffprobe, and the Git submodules; `uv` fetches
the pinned Python 3.10. See [docs/setup-and-development.md](docs/setup-and-development.md).

```bash
git clone --recurse-submodules https://github.com/itsmygithubacct/kilix-playalong.git
cd kilix-playalong
make setup-all
make check
uv run --frozen kilix-playalong doctor
uv run --frozen kilix-playalong models install
uv run --frozen kilix-playalong create \
  'https://www.youtube.com/watch?v=VIDEO_ID' \
  --i-have-rights
uv run --frozen kilix-playalong serve song-PROJECT_ID
```

`make setup` installs only the command, test and state-storage runtime;
`make setup-all` adds the Demucs, Basic Pitch ONNX and faster-whisper
providers.

## Usage

| Command | What it does |
| --- | --- |
| `create SOURCE --i-have-rights` | build a project from a link, or `--file PATH` |
| `resume PROJECT` | continue an interrupted project from verified artifacts |
| `list`, `show PROJECT` | list projects; show stage and artifact status |
| `serve PROJECT [--no-open]` | open the timed multitrack player on `127.0.0.1` |
| `songbook PROJECT [--output DIR]` | export an offline lyrics, notes and tab page |
| `models [status\|install]` | report or install the licensed models |
| `options` | print every backend option, its default and availability |
| `doctor` | inspect the locked runtime and optional providers |

Common `create` options include `--lyrics FILE`, `--tuning`, `--max-fret`
and `--whisper-model`. Worked examples are in [docs/usage.md](docs/usage.md).

## Keyboard controls for the multitrack player

| Key | Action |
| --- | --- |
| Space | Play or pause |
| Left / Right | Seek five seconds |
| `V` | Toggle vocals |
| `L` | Show or hide lyrics |


## How it works

```
link or file --> download (yt-dlp) --> normalize (FFmpeg) --> separate (Demucs)
                                                                  |
        songbook / HTML / ASCII <-- tab <-- guitar MIDI (Basic Pitch)
                                     ^
                 lyrics (captions, text, LRCLIB, faster-whisper)

project.state: provider, settings fingerprint and SHA-256 for every stage
```

Each stage writes its artifacts into a private project directory and records
its provider, settings fingerprint, sizes and SHA-256 digests; resume reuses a
stage only when everything it depends on still matches. Providers run as
argument vectors in child processes with timeouts, a minimal environment and a
disposable home. The player is a read-only server on `127.0.0.1` with a random
per-launch capability in its URL; the browser never starts jobs. A separate
C11 practice surface under `src/native` reads the same projects without
changing them. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Documentation

- [docs/features.md](docs/features.md): the full feature list and intake limits.
- [docs/rights-and-privacy.md](docs/rights-and-privacy.md): rights, the player
  capability, provider isolation and the model table.
- [docs/usage.md](docs/usage.md): create, resume, serve, songbook export,
  player keys, storage and recovery.
- [docs/setup-and-development.md](docs/setup-and-development.md): requirements,
  setup and the development gates.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): stage contracts, caching,
  fingering inference, player clock and security boundaries.
- [docs/FRETBOARD.md](docs/FRETBOARD.md): the fretboard contract shared by
  the native and browser surfaces.
- [src/native/README.md](src/native/README.md): the native C11 surface.

## Related repositories

- [kilix-state](https://github.com/itsmygithubacct/kilix-state#readme),
  [soft-raster](https://github.com/itsmygithubacct/soft-raster#readme) and
  [kitty-terminal-session](https://github.com/itsmygithubacct/kitty-terminal-session#readme):
  submodules under `third_party/`.
- [kilix-content](https://github.com/itsmygithubacct/kilix-content#readme) and
  [kilix-license](https://github.com/itsmygithubacct/kilix-license#readme):
  the catalog entry, the model assets and their receipts.
- [kilix](https://github.com/itsmygithubacct/kilix#readme): the
  `kilix playalong` and `kilix models` verbs.

## Part of Plebian-OS

Kilix Playalong is one of the repositories behind
[Plebian-OS](https://github.com/itsmygithubacct/plebian-os#readme), a Debian
install whose desktop is [Kilix](https://github.com/itsmygithubacct/kilix#readme)
running the [Kilix 95](https://github.com/itsmygithubacct/kilix-95#readme)
desktop through [Pleb](https://github.com/itsmygithubacct/pleb#readme).
It is installed on demand from the Kilix content catalog, not shipped in the
installer image. The map of every repository and how it ships is on the
[components page](https://github.com/itsmygithubacct/plebian-os/blob/main/docs/components.md).

## License

MIT; see [LICENSE](LICENSE). Model weights are not part of this repository and
keep their own licences.
