from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kilix_playalong.errors import CorruptProjectError
from kilix_playalong.paths import project_artifact
from kilix_playalong.server import PlayalongServer
from kilix_playalong.state import load_manifest, new_manifest, save_manifest
from kilix_playalong.util import private_write


def _ready_project(root: Path) -> Path:
    project = root / "song-server123"
    project.mkdir(mode=0o700, parents=True)
    manifest = new_manifest(
        "song-server123",
        url_sha256="0" * 64,
        rights_statement="confirmed for a synthetic fixture",
    )
    manifest["title"] = "Server <Test>"
    manifest["artist"] = "Fixture Artist"
    manifest["source"]["duration"] = 4.0

    stem = project / "stems" / "vocals.wav"
    private_write(stem, b"0123456789")
    manifest["tracks"] = [
        {
            "id": "vocals",
            "label": "Vocals",
            "kind": "vocals",
            "path": "stems/vocals.wav",
            "sha256": "0" * 64,
            "size": 10,
            "default_muted": False,
        }
    ]
    private_write(
        project / "lyrics" / "lyrics.json",
        json.dumps({"schema": "kilix.playalong.lyrics/v1", "cues": []}).encode(),
    )
    private_write(
        project / "tab" / "guitar-tab.json",
        json.dumps(
            {
                "schema": "kilix.playalong.tab/v1",
                "tuning": {"midi": [40, 45, 50, 55, 59, 64], "labels": []},
                "events": [],
            }
        ).encode(),
    )
    private_write(project / "exports" / "playalong.html", b"<!doctype html><title>print</title>")
    private_write(project / "exports" / "guitar-tab.txt", b"tab")
    private_write(project / "midi" / "guitar.mid", b"midi")
    manifest["lyrics"] = {
        "path": "lyrics/lyrics.json",
        "source": "fixture",
        "language": "en",
        "visible": True,
    }
    manifest["tablature"] = {
        "path": "tab/guitar-tab.json",
        "ascii_path": "exports/guitar-tab.txt",
        "midi_path": "midi/guitar.mid",
        "visible": True,
        "tuning": [40, 45, 50, 55, 59, 64],
        "max_fret": 20,
    }
    save_manifest(project, manifest)
    return project


def test_state_round_trip_and_corruption_detection(tmp_path: Path) -> None:
    project = _ready_project(tmp_path)
    assert load_manifest(project)["title"] == "Server <Test>"
    state = project / "project.state"
    payload = bytearray(state.read_bytes())
    payload[-1] ^= 0xFF
    state.write_bytes(payload)
    with pytest.raises(CorruptProjectError, match="integrity"):
        load_manifest(project)


def test_project_artifacts_cannot_escape_private_project(tmp_path: Path) -> None:
    project = tmp_path / "song-contained"
    project.mkdir()
    assert project_artifact(project, "stems/guitar.wav") == project / "stems" / "guitar.wav"
    with pytest.raises(CorruptProjectError, match="escapes"):
        project_artifact(project, "../../outside")
    with pytest.raises(CorruptProjectError, match="absolute"):
        project_artifact(project, "/etc/passwd")


def test_loopback_server_capability_routes_and_ranges(tmp_path: Path) -> None:
    project = _ready_project(tmp_path)
    with PlayalongServer(project) as server:
        with urllib.request.urlopen(server.url, timeout=3) as response:
            page = response.read().decode()
            assert response.headers["Content-Security-Policy"]
            assert "Kilix Playalong" in page

        with urllib.request.urlopen(server.url + "api/project", timeout=3) as response:
            payload = json.load(response)
            assert payload["schema"] == "kilix.playalong.web/v1"
            assert payload["project"]["title"] == "Server <Test>"
            assert payload["tracks"][0]["url"].startswith(f"/{server.token}/")

        with urllib.request.urlopen(server.url + "export/print", timeout=3) as response:
            policy = response.headers["Content-Security-Policy"]
            assert "default-src 'none'" in policy
            assert "style-src 'unsafe-inline'" in policy
            assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"

        request = urllib.request.Request(
            server.url + "media/vocals",
            headers={"Range": "bytes=2-5"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 206
            assert response.headers["Content-Range"] == "bytes 2-5/10"
            assert response.read() == b"2345"

        wrong = server.url.replace(server.token, "wrong-capability")
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(wrong, timeout=3)
        assert raised.value.code == 404

        post = urllib.request.Request(server.url + "api/project", method="POST")
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(post, timeout=3)
        assert raised.value.code == 405
