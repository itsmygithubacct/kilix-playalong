"""Assertions about the shape of the `make check` gate itself.

`make check` is the only thing that runs `uv lock --check`, `make setup` is the
only thing that refuses a stale lock at install time, and the repository ships
no CI configuration (docs/ARCHITECTURE.md, "Verification"), so an edit that
dropped the lock gate, let it race the rest of the suite under `make -j`, or
turned a `--locked` sync back into a `--frozen` one would otherwise fail
nothing. These tests drive the real Makefile with a stub `uv` and a stub
submodule so they assert the gate's behaviour without running any gate.

Scope: this file is about the build gate only. It deliberately does not import
the package.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = REPO_ROOT / "Makefile"
STATE_LIBRARY = "libkilix-state.so"


def _shell_clock_is_usable() -> bool:
    """Probe the two coreutils behaviours `_write_stubs` bakes into its stubs.

    The stubs timestamp with `date +%s.%N` and pace themselves with a fractional
    `sleep`. Both are GNU behaviour: busybox and BSD `date` echo a literal `%N`,
    and POSIX `sleep` takes whole seconds. Without this probe `_parse_log` turns
    the literal `%N` into a `ValueError` mid-test, reporting a missing dependency
    as a failure instead of a skip.
    """
    try:
        probe = subprocess.run(
            ["sh", "-c", "sleep 0.01 && date +%s.%N"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except OSError:
        return False
    if probe.returncode != 0:
        return False
    try:
        float(probe.stdout.strip())
    except ValueError:
        return False
    return True


requires_make = pytest.mark.skipif(
    shutil.which("make") is None or shutil.which("sh") is None or not _shell_clock_is_usable(),
    reason="needs GNU make, a POSIX shell, and GNU `date +%N` with a fractional `sleep`",
)


@dataclass(frozen=True)
class Event:
    """One stubbed command, with the wall-clock instants it ran between."""

    command: str
    start: float
    end: float


def _write_stubs(tmp_path: Path, *, prebuilt: bool, delay: str) -> tuple[Path, Path, Path]:
    """Create a stub `uv`, a stub kilix-state submodule, and their shared log."""
    log = tmp_path / "gate.log"
    log.touch()

    uv = tmp_path / "uv-stub"
    uv.write_text(
        "#!/bin/sh\n"
        f'printf "%s START uv %s\\n" "$(date +%s.%N)" "$*" >> {log}\n'
        f"sleep {delay}\n"
        f'printf "%s END uv %s\\n" "$(date +%s.%N)" "$*" >> {log}\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)

    state_dir = tmp_path / "kilix-state"
    state_dir.mkdir()
    (state_dir / "Makefile").write_text(
        "all:\n"
        f'\t@printf "%s START submodule-build\\n" "$$(date +%s.%N)" >> {log}\n'
        "\t@mkdir -p build\n"
        f"\t@touch build/{STATE_LIBRARY}\n"
        f'\t@printf "%s END submodule-build\\n" "$$(date +%s.%N)" >> {log}\n',
        encoding="utf-8",
    )
    if prebuilt:
        (state_dir / "build").mkdir()
        (state_dir / "build" / STATE_LIBRARY).touch()
    return uv, state_dir, log


def _parse_log(log: Path) -> list[Event]:
    started: dict[str, float] = {}
    events: list[Event] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        stamp, phase, command = line.split(" ", 2)
        if phase == "START":
            started[command] = float(stamp)
        else:
            events.append(Event(command=command, start=started[command], end=float(stamp)))
    return events


def _run_make(
    tmp_path: Path,
    targets: list[str],
    *,
    jobs: int | None = None,
    prebuilt: bool = True,
    delay: str = "0.25",
) -> tuple[subprocess.CompletedProcess[str], list[Event]]:
    uv, state_dir, log = _write_stubs(tmp_path, prebuilt=prebuilt, delay=delay)
    environment = dict(os.environ)
    # An outer `make test` would otherwise hand this make its jobserver and its
    # own flags, which would decide the very thing these tests measure.
    for name in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKE_TERMOUT", "MAKE_TERMERR"):
        environment.pop(name, None)
    command = ["make", "-f", str(MAKEFILE)]
    if jobs is not None:
        command.append(f"-j{jobs}")
    command += [f"UV={uv}", f"KILIX_STATE_DIR={state_dir}", *targets]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return completed, _parse_log(log)


def _recipes() -> dict[str, list[str]]:
    """Map each explicit target to its recipe lines, textually."""
    recipes: dict[str, list[str]] = {}
    current: str | None = None
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("\t"):
            if current is not None:
                recipes[current].append(line.strip())
            continue
        match = re.match(r"^([^\s:=#][^:=]*):(?!=)", line)
        if match is None:
            current = None
            continue
        current = match.group(1).strip()
        recipes.setdefault(current, [])
    return recipes


def _prerequisites() -> dict[str, list[str]]:
    prerequisites: dict[str, list[str]] = {}
    text = MAKEFILE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("\t"):
            continue
        match = re.match(r"^([^\s:=#][^:=]*):(?!=)(.*)$", line)
        if match is None:
            continue
        name = match.group(1).strip()
        if name.startswith("."):
            continue
        prerequisites.setdefault(name, []).extend(match.group(2).split())
    return prerequisites


def _requires_state_library(target: str) -> bool:
    prerequisites = _prerequisites()
    seen: set[str] = set()
    pending = [target]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if STATE_LIBRARY in name:
            return True
        pending.extend(prerequisites.get(name, []))
    return False


@requires_make
def test_check_finishes_the_lock_gate_before_any_other_gate_starts(tmp_path: Path) -> None:
    """F17: a stale lock must stop the suite, not merely fail alongside it.

    Run under `-j8` on purpose: sibling prerequisites are unordered under a
    parallel make, so `check: lock-check test lint …` would satisfy a purely
    textual test while still running the whole suite against a stale
    environment.
    """
    completed, events = _run_make(tmp_path, ["check"], jobs=8)
    assert completed.returncode == 0, completed.stderr
    lock = [event for event in events if "lock --check" in event.command]
    assert lock, f"`make check` never ran `uv lock --check`; it ran {[e.command for e in events]}"
    assert len(lock) == 1
    others = [event for event in events if event is not lock[0]]
    early = [event.command for event in others if event.start < lock[0].end]
    assert not early, f"these ran before the lock gate finished: {early}"


@requires_make
def test_check_still_runs_every_gate(tmp_path: Path) -> None:
    """Ordering must not be bought by dropping a gate."""
    completed, events = _run_make(tmp_path, ["check"], jobs=8, delay="0")
    assert completed.returncode == 0, completed.stderr
    commands = " | ".join(event.command for event in events)
    for fragment in ("lock --check", "pytest", "ruff check", "ruff format --check", "mypy"):
        assert fragment in commands, f"`make check` no longer runs {fragment}: {commands}"


@requires_make
def test_lock_gate_builds_the_submodule_before_invoking_uv(tmp_path: Path) -> None:
    """On a clone made without --recurse-submodules, make's error must come first.

    `uv lock --check` resolves the `kilix-state-py` path source out of the
    submodule, so without this prerequisite the first failure a newcomer sees
    is uv's "Distribution not found at: …" rather than the submodule build.
    """
    completed, events = _run_make(tmp_path, ["lock-check"], prebuilt=False, delay="0")
    assert completed.returncode == 0, completed.stderr
    ordered = [event.command for event in sorted(events, key=lambda event: event.start)]
    assert ordered and ordered[0] == "submodule-build", ordered


def test_every_uv_target_requires_the_state_submodule() -> None:
    """Same diagnosability property, stated over the whole Makefile.

    Every `uv` invocation needs the synced environment, and the environment
    cannot resolve without the submodule; `check` itself is exempt because it
    only re-enters make.
    """
    missing = [
        target
        for target, recipe in _recipes().items()
        if any("$(UV)" in line for line in recipe) and not _requires_state_library(target)
    ]
    assert not missing, f"these run uv with no submodule prerequisite: {missing}"


def test_check_orders_the_lock_gate_in_its_recipe() -> None:
    """The ordering is a recipe line, not a prerequisite list; keep it that way."""
    recipe = _recipes().get("check")
    assert recipe, "the Makefile has no `check` recipe"
    assert recipe[0] == "$(MAKE) lock-check", recipe
    assert not _prerequisites().get("check"), (
        "`check` gained prerequisites; sibling prerequisites are unordered under `make -j`"
    )


def test_sync_targets_refuse_a_stale_lock() -> None:
    """`uv sync --frozen` installs a stale lock silently; `--locked` fails."""
    syncs = re.findall(r"^\t\$\(UV\) sync (.*)$", MAKEFILE.read_text(encoding="utf-8"), re.M)
    assert syncs, "no `uv sync` recipe found"
    for flags in syncs:
        assert "--locked" in flags, flags
        assert "--frozen" not in flags, flags


def test_no_target_rewrites_the_committed_lock() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    for flags in re.findall(r"^\t\$\(UV\) run (.*)$", text, re.M):
        assert flags.startswith("--frozen "), flags
    for flags in re.findall(r"^\t\$\(UV\) lock(.*)$", text, re.M):
        assert flags.strip() == "--check", flags
