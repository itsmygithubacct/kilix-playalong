"""Bounded subprocess execution with process-group teardown and redaction."""

from __future__ import annotations

import math
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
from typing import cast

from .errors import InvalidInputError, ProviderFailedError
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


# Blocked for the duration of a teardown, in this thread only. A second SIGINT
# ("Ctrl-C, Ctrl-C") must not escape the SIGTERM wait below and skip the SIGKILL
# escalation, and a SIGTERM/SIGHUP/SIGQUIT arriving mid-teardown must not kill this
# process while the provider group is still up. Delivery is deferred, not discarded:
# a pending signal fires the moment the mask is restored, at most ~6 seconds (two 3
# second waits) after it arrived. That bound is per teardown, and a single call can
# run two: when neither wait reaps the child -- an uninterruptible provider stuck in
# D state, the case this mask exists for -- ``_bounded_communicate`` runs one masked
# teardown and ``run_command``'s handler runs a second, so an interrupt can take up to
# ~12 seconds to end the call. Two is the maximum, because those are the only two call
# sites and each runs the teardown at most once per call.
_TEARDOWN_SIGNALS = frozenset({signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT})

# Ceiling on one ``selector.select`` wait, so that the deadline computed below -- not the
# selector's argument conversion -- decides how long a quiet provider is given. The
# DefaultSelector here is epoll, whose timeout is an int of milliseconds: measured on this
# interpreter, 2147483 seconds (INT_MAX ms, ~24.8 days) is the largest wait it accepts, and
# anything above raises OverflowError -- not a ProviderFailedError -- instead of waiting.
_MAX_SELECT_WAIT = 3600.0


def usable_seconds(value: float) -> float | None:
    """Return ``value`` as a float duration, or None if it cannot be one.

    A precondition that raises something other than its own error is not a precondition, and
    this one has two ways to do that: ``int`` is a legal argument wherever ``float`` is
    annotated (PEP 484), and both ``float(10 ** 400)`` and ``math.isfinite(10 ** 400)`` raise
    OverflowError rather than answering. So the conversion happens here, once, and callers
    get a plain None for every value they cannot use. ``providers/youtube.py`` calls this too,
    so the check at the provider entry points and the check here cannot drift apart.
    """
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def require_seconds(value: float, description: str) -> None:
    """``usable_seconds`` as a precondition: refuse the value rather than report it.

    Here rather than at the two entry points that need it, for the reason the
    predicate itself is here. A ``timeout`` reaches ``run_command``, which rejects an
    unusable one with a bare ``ValueError``; a ``max_duration`` reaches nothing but a
    comparison, and a NaN would not raise there at all -- it would compare False and
    switch the duration gate off in silence. Both entry points owe the user the same
    sentence for the same value, and they were writing it out separately: two identical
    two-line wrappers around this predicate, in ``source`` and in ``providers/youtube``,
    with identical messages, is a message that can drift while the predicate cannot.

    ``InvalidInputError`` rather than this module's own ``ProviderFailedError``: an
    unusable bound is a bad argument, and it is refused before any process exists.
    """
    if usable_seconds(value) is None:
        raise InvalidInputError(f"{description} must be a positive, finite number of seconds")


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Tear the provider's process group down: SIGTERM, then SIGKILL, then reap.

    Safe to call twice -- both ``_bounded_communicate`` and ``run_command``'s handler
    call it -- and uninterruptible by the signals that would otherwise leave the group up.
    Twice is not free, and it is not quite a no-op either: after the child was reaped this
    returns without signalling anything (the guard below), but after a pass that failed to
    reap it, the second pass re-sends both signals -- to a pid that is still ours, because
    an unreaped child cannot be recycled -- and can add another ~6 seconds to the call.

    Adjacent properties of "every abnormal exit tears the group down":
    * A second interrupt during teardown: ENFORCED, by the mask plus the ``finally``.
    * A repeat call after the child was reaped: ENFORCED, by the ``returncode`` guard.
    * Parent SIGKILL (and any signal outside this window): OUT OF SCOPE. A library must
      not install process-wide handlers, and SIGKILL cannot be handled at all. The
      kernel-level alternative (PR_SET_PDEATHSIG via ``preexec_fn``) is rejected because
      ``preexec_fn`` is documented as unsafe in the presence of threads: the CLI is
      single-threaded today, but ``run_command`` is importable and cannot promise that
      of its caller. A supervisor that needs the guarantee should use a cgroup.
    * A grandchild that calls ``setsid``: OUT OF SCOPE, it leaves the group by definition.
      BOUNDED BY: every argv this package passes to ``run_command`` is built by its own
      provider modules, and none of the locked providers (yt-dlp, ffmpeg, demucs,
      faster-whisper) daemonises. An outside caller of ``run_command`` gets no such bound.
    """
    if process.returncode is not None:
        # Already reaped. Its pid is free for the kernel to reuse, possibly as an
        # unrelated group leader, so a repeat pass must not signal it again.
        return
    # Read the caller's mask before changing anything: blocking the empty set is a query.
    # Taking it here rather than from the SIG_BLOCK below means there is no instruction
    # boundary at which a handler that had already tripped can leave the mask changed and
    # the restore value unassigned -- restoring is then always correct and idempotent.
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, frozenset())
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, _TEARDOWN_SIGNALS)
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=3)
    finally:
        try:
            # Escalation runs even if the wait above raised. This still sweeps the group
            # when the leader has just been reaped, which is deliberate: it is how a
            # grandchild that outlived its parent's SIGTERM dies. Linux holds a pgid
            # allocated while any member of that group survives, so whenever there is
            # something left to sweep this reaches it and nothing else. When the group is
            # already empty the wait above freed the pid and this is an ESRCH no-op --
            # unless pid allocation wrapped the whole space inside that window and handed
            # the number to a process that then made itself a group leader. NOT CLOSED:
            # skipping the sweep would orphan the grandchild this line exists for, and
            # holding the zombie across it (``os.waitid`` with ``WNOWAIT``) buys a second
            # reaping path for a race one pid wraparound wide.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=3)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _bounded_communicate(
    process: subprocess.Popen[bytes], *, timeout: float, max_output_per_stream: int
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
            # An empty result is not a timeout by itself once the wait is capped: it means
            # the cap or the deadline elapsed, and only the loop head can tell them apart.
            events = selector.select(min(remaining, _MAX_SELECT_WAIT))
            if not events:
                continue
            for key, _mask in events:
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                buffer = output[key.fd]
                if len(buffer) + len(chunk) > max_output_per_stream:
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
    max_output_per_stream: int = 4 * 1024 * 1024,
) -> CommandResult:
    """Run a provider to completion under a wall-clock and diagnostic-output bound.

    ``max_output_per_stream`` bounds stdout and stderr independently, so a provider
    may retain at most twice that many bytes in total before it is torn down.

    Both bounds are preconditions rather than runtime policy: a ``timeout`` that is not a
    positive, finite, float-representable number of seconds, or a non-positive
    ``max_output_per_stream``, raises ``ValueError`` here instead of reaching ``selectors``
    (where a NaN or an infinity surfaces as a bare ValueError/OverflowError from the C
    timeout conversion, and an ``int`` past the float ceiling does so from the deadline
    arithmetic). ``ValueError`` is not a ``PlayalongError``, so a caller that forwards a
    caller-supplied value into either keyword has to validate it first;
    ``providers/youtube.py`` and ``source.acquire`` do exactly that, with
    ``require_seconds``.

    Every abnormal exit from the window in which an unreaped child exists -- timeout,
    output bound, in-process exception, one interrupt or several -- tears the provider's
    process group down before propagating; see ``_terminate_process_group`` for the
    properties that are out of scope. Outside that window there is nothing left to tear
    down: ``_bounded_communicate`` returns only after ``process.wait`` has reaped.

    One window remains open, and it is not closable from outside CPython: in
    ``Popen._execute_child`` the child is forked by ``_posixsubprocess.fork_exec`` and
    ``self.pid`` is stored on the very next bytecode. A signal delivered at that one
    instruction boundary raises before the store, so the child exists and nothing in this
    process knows its pid. The much wider window that follows -- CPython reading the exec
    errpipe with ``os.read``, a PEP 475 handler point -- is closed by the split
    construction below.
    """
    if not arguments or any("\x00" in item for item in arguments):
        # Non-empty sequence, NUL-free members: an empty member is a legal argv entry and
        # is not checked here, so the message says sequence rather than strings.
        raise ValueError("command arguments must be a non-empty sequence of NUL-free strings")
    seconds = usable_seconds(timeout)
    if seconds is None:
        raise ValueError("provider timeout must be a positive, finite number of seconds")
    if max_output_per_stream <= 0:
        raise ValueError("provider output bound must be positive")
    with tempfile.TemporaryDirectory(prefix="kilix-playalong-provider-") as provider_home:
        # Construction is split so that an interrupt *inside* Popen.__init__ still leaves
        # us holding the child. CPython sets self.pid after the fork and then reads the
        # exec errpipe with os.read, which PEP 475 makes a signal-handler point; its own
        # ``except:`` closes the fds and re-raises without killing the child, so a plain
        # ``process = Popen(...)`` never binds and the child is orphaned. __init__ sets
        # self.pid = None before forking and _execute_child overwrites it with the real
        # pid, so "pid is not None" is exactly "a child exists" -- and on a CPython that
        # stopped doing either, this degrades to the old behaviour instead of misfiring.
        process = cast("subprocess.Popen[bytes]", subprocess.Popen.__new__(subprocess.Popen))
        try:
            subprocess.Popen.__init__(
                process,
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
                process, timeout=seconds, max_output_per_stream=max_output_per_stream
            )
        except BaseException:
            if getattr(process, "pid", None) is not None:
                _terminate_process_group(process)
            raise
    stdout = stdout_bytes.decode("utf-8", "replace")
    stderr = stderr_bytes.decode("utf-8", "replace")
    if log_path is not None:
        safe_log = public_error(stdout + "\n" + stderr, secrets=redact)
        private_write(log_path, (safe_log + "\n").encode("utf-8"))
    if process.returncode != 0:
        detail = public_error(stderr or stdout or "provider failed", secrets=redact)
        raise ProviderFailedError(detail)
    return CommandResult(stdout=stdout, stderr=stderr)
