# Setup and development

Requirements, setup targets and the development gates. This text was moved
from the README; the [README](../README.md) is the short entry point.

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

Optional songbook browser checks use Node, an installed Playwright package, and
Chromium. They generate original short tones and lyrics in a temporary folder:

```bash
PLAYWRIGHT_MODULE=/absolute/path/to/playwright-core/index.mjs \
CHROMIUM_PATH=/usr/bin/chromium node tests/songbook_browser.mjs
```

With `playwright` and its browser installed on Node's normal module path, both
environment variables can be omitted. This checks audio switching during loading,
sustained-note highlighting, phrase/section loops, large correction round trips, mobile
layout, and printing. The Python gate does not install or run browser tooling.

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
