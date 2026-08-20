"""Bounded subprocess execution with process-group teardown and redaction."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .errors import ProviderFailedError
from .util import private_write, public_error


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str


def run_command(
    arguments: Sequence[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    log_path: Path | None = None,
    redact: tuple[str, ...] = (),
    max_output: int = 4 * 1024 * 1024,
) -> CommandResult:
    if not arguments or any("\x00" in item for item in arguments):
        raise ValueError("command arguments must be non-empty strings without NUL bytes")
    child_env = os.environ.copy()
    if env is not None:
        child_env.update(env)
    process = subprocess.Popen(
        list(arguments),
        cwd=cwd,
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=True,
    )
    try:
        stdout_bytes, stderr_bytes = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise ProviderFailedError(f"provider timed out after {timeout:g} seconds") from None

    if len(stdout_bytes) > max_output or len(stderr_bytes) > max_output:
        raise ProviderFailedError("provider produced more diagnostic output than allowed")
    stdout = stdout_bytes.decode("utf-8", "replace")
    stderr = stderr_bytes.decode("utf-8", "replace")
    if log_path is not None:
        safe_log = public_error(stdout + "\n" + stderr, secrets=redact)
        private_write(log_path, (safe_log + "\n").encode("utf-8"))
    if process.returncode != 0:
        detail = public_error(stderr or stdout or "provider failed", secrets=redact)
        raise ProviderFailedError(detail)
    return CommandResult(stdout=stdout, stderr=stderr)
