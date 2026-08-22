"""Bounded subprocess execution with process-group teardown and redaction."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import tempfile
import time
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


_INHERITED_ENVIRONMENT = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "LD_LIBRARY_PATH",
    "CUDA_VISIBLE_DEVICES",
    "NVIDIA_VISIBLE_DEVICES",
    "ROCR_VISIBLE_DEVICES",
    "HIP_VISIBLE_DEVICES",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
)
_PROTECTED_ENVIRONMENT = frozenset(
    {
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "PYTHONNOUSERSITE",
        "PYTHONSAFEPATH",
    }
)


def _valid_environment_name(name: str) -> bool:
    return (
        bool(name)
        and name.isascii()
        and (name[0].isalpha() or name[0] == "_")
        and all(character.isalnum() or character == "_" for character in name)
    )


def _child_environment(home: str, overrides: Mapping[str, str] | None) -> dict[str, str]:
    environment: dict[str, str] = {}
    for name in _INHERITED_ENVIRONMENT:
        value = os.environ.get(name)
        if value is not None and "\x00" not in value:
            environment[name] = value
    environment.setdefault("PATH", os.defpath)
    for name, value in (overrides or {}).items():
        if not isinstance(name, str) or not _valid_environment_name(name):
            raise ValueError("provider environment contains an invalid name")
        if name in _PROTECTED_ENVIRONMENT:
            raise ValueError("provider environment cannot replace private runtime paths")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("provider environment values must be NUL-free strings")
        environment[name] = value
    environment.update(
        HOME=home,
        XDG_CONFIG_HOME=str(Path(home) / "config"),
        XDG_CACHE_HOME=str(Path(home) / "cache"),
        XDG_DATA_HOME=str(Path(home) / "data"),
        XDG_STATE_HOME=str(Path(home) / "state"),
        PYTHONNOUSERSITE="1",
        PYTHONSAFEPATH="1",
    )
    return environment


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=3)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=3)


def _bounded_communicate(
    process: subprocess.Popen[bytes], *, timeout: float, max_output: int
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("provider pipes were not created")
    streams = (process.stdout, process.stderr)
    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    output = {stdout_fd: bytearray(), stderr_fd: bytearray()}
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(process)
                raise ProviderFailedError(f"provider timed out after {timeout:g} seconds")
            events = selector.select(remaining)
            if not events:
                _terminate_process_group(process)
                raise ProviderFailedError(f"provider timed out after {timeout:g} seconds")
            for key, _mask in events:
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                buffer = output[key.fd]
                if len(buffer) + len(chunk) > max_output:
                    _terminate_process_group(process)
                    raise ProviderFailedError(
                        "provider produced more diagnostic output than allowed"
                    )
                buffer.extend(chunk)
        remaining = deadline - time.monotonic()
        try:
            process.wait(timeout=max(0.0, remaining))
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            raise ProviderFailedError(f"provider timed out after {timeout:g} seconds") from None
    finally:
        selector.close()
        for stream in streams:
            with suppress(OSError):
                stream.close()
    return bytes(output[stdout_fd]), bytes(output[stderr_fd])


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
    if timeout <= 0 or max_output <= 0:
        raise ValueError("provider timeout and output bound must be positive")
    with tempfile.TemporaryDirectory(prefix="kilix-playalong-provider-") as provider_home:
        process = subprocess.Popen(
            list(arguments),
            cwd=cwd,
            env=_child_environment(provider_home, env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=True,
        )
        stdout_bytes, stderr_bytes = _bounded_communicate(
            process, timeout=timeout, max_output=max_output
        )
    stdout = stdout_bytes.decode("utf-8", "replace")
    stderr = stderr_bytes.decode("utf-8", "replace")
    if log_path is not None:
        safe_log = public_error(stdout + "\n" + stderr, secrets=redact)
        private_write(log_path, (safe_log + "\n").encode("utf-8"))
    if process.returncode != 0:
        detail = public_error(stderr or stdout or "provider failed", secrets=redact)
        raise ProviderFailedError(detail)
    return CommandResult(stdout=stdout, stderr=stderr)
