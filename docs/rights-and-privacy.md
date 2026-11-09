# Rights, privacy and models

The rights confirmation, the loopback player capability, provider isolation
and the licensed models. This text was moved from the README; the
[README](../README.md) is the short entry point.

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

YouTube access remains a network operation governed by its provider's current
terms. Model code licenses do not establish the rights of model weights, so this
app never downloads weights itself. Every model it runs is a Kilix Content asset
with a kilix-license record:

| Stage | Asset | Licence |
| --- | --- | --- |
| Stem separation | `demucs-htdemucs-6s` | MIT, Meta Platforms, Inc. and affiliates |
| Guitar notes | `basic-pitch-icassp-2022-onnx` | Apache-2.0, Spotify AB |
| English lyrics (default) | `faster-whisper-medium-en` | MIT, OpenAI and SYSTRAN |
| Lyrics in any language (optional) | `faster-whisper-large-v3` | MIT, OpenAI and SYSTRAN |

`kilix-playalong models install` runs `kilix models install` for each default
asset (add `--large-v3` for the optional one). Kilix shows the record and the
licence text, writes the receipt and fetches the pinned files from their source.
Before each run, the stage verifies its asset with `kilix models verify`, which
re-checks the receipt and re-hashes the files, and then loads that directory
offline. A stage whose asset does not verify refuses and names the install
command. `kilix-playalong models` reports what is installed.
