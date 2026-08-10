"""
Invoke the private moorcheh-agent Docker image.

Secrets (Function URL + Ed25519 private key) are baked into the image at
build time in moorcheh-prod CI — never into this open-source repo.

Requires Docker Desktop (or a running Docker daemon) and
``MOORCHEH_AGENT_IMAGE`` (default ``moorcheh/moorcheh-agent:latest``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from memanto.app.config import settings

logger = logging.getLogger(__name__)

EXIT_END_TURN = 0
EXIT_REQUIRES_CLIENT_TOOL = 10

# Wall-clock ceiling for a single agent invocation. Without one, a container
# that never exits becomes a request that never ends.
STREAM_TIMEOUT_SECONDS = 300.0
JSON_TIMEOUT_SECONDS = 60.0

# ``docker info`` costs a round-trip to the daemon on every call; cache a
# successful probe briefly. Failures are never cached, so the next call picks
# up a daemon that has since started.
DOCKER_CHECK_TTL_SECONDS = 60.0
_docker_ok_until = 0.0


class MoorchehAgentError(Exception):
    """Raised when the moorcheh-agent Docker runtime cannot be invoked."""

    def __init__(self, message: str, *, stderr: str | None = None):
        super().__init__(message)
        self.stderr = stderr


def _ensure_docker() -> None:
    """Probe the Docker daemon. Blocking — call via :func:`_ensure_docker_async`."""
    if not shutil.which("docker"):
        raise MoorchehAgentError(
            "Docker is required for agent tool calling. "
            "Install Docker Desktop, then pull "
            f"{settings.MOORCHEH_AGENT_IMAGE}."
        )
    try:
        r = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MoorchehAgentError(
            "Could not reach the Docker daemon. Start Docker Desktop, then retry. "
            f"(image: {settings.MOORCHEH_AGENT_IMAGE})"
        ) from e
    if r.returncode != 0:
        raise MoorchehAgentError(
            "Docker CLI is installed but the Docker daemon is not running. "
            "Start Docker Desktop, then retry. "
            f"(image: {settings.MOORCHEH_AGENT_IMAGE})"
        )


async def _ensure_docker_async() -> None:
    """Probe Docker off the event loop so one wedged daemon cannot stall the server."""
    global _docker_ok_until
    if time.monotonic() < _docker_ok_until:
        return
    await asyncio.to_thread(_ensure_docker)
    _docker_ok_until = time.monotonic() + DOCKER_CHECK_TTL_SECONDS


def _build_argv(
    subcommand: str,
    *,
    api_key: str,
    args: list[str],
    host_files: list[Path] | None = None,
) -> tuple[list[str], Path | None, str]:
    """
    Build ``docker run`` argv.

    ``host_files`` are mounted read-only into the container at
    ``/tmp/memanto-agent/<filename>`` and matching absolute path args are rewritten.

    Returns (argv, temp_dir_or_none, container_name).
    """
    host_files = host_files or []

    image = (settings.MOORCHEH_AGENT_IMAGE or "").strip()
    if not image:
        raise MoorchehAgentError(
            "MOORCHEH_AGENT_IMAGE is not set. Set it to the moorcheh-agent image "
            "(e.g. moorcheh/moorcheh-agent:latest) and pull it with 'docker pull'."
        )
    # Named so an abandoned run can be stopped: killing the attached ``docker run``
    # client does not stop the container it started.
    container = f"memanto-agent-{uuid.uuid4().hex[:12]}"
    docker: list[str] = ["docker", "run", "--rm", "-i", "--name", container]
    rewritten = list(args)
    work: Path | None = None

    if host_files:
        work = Path(tempfile.mkdtemp(prefix="memanto-agent-"))
        docker.extend(["-v", f"{work.resolve()}:/tmp/memanto-agent:ro"])
        for host in host_files:
            dest = work / host.name
            shutil.copy2(host, dest)
            container_path = f"/tmp/memanto-agent/{host.name}"
            host_s = str(host)
            host_resolved = host.resolve()

            def _match(
                a: str, _host_s: str = host_s, _hr: Path = host_resolved
            ) -> bool:
                if a == _host_s:
                    return True
                try:
                    return Path(a).resolve() == _hr
                except (OSError, ValueError):
                    return False

            rewritten = [container_path if _match(a) else a for a in rewritten]

    argv = [*docker, image, subcommand, "--api-key", api_key, *rewritten]
    return argv, work, container


def _cleanup(work: Path | None) -> None:
    if work is not None:
        shutil.rmtree(work, ignore_errors=True)


def _abort(
    proc: asyncio.subprocess.Process,
    container: str,
    stderr_task: asyncio.Task[None] | None = None,
) -> None:
    """Stop a run that did not finish on its own (disconnect, timeout, error).

    Deliberately synchronous: this runs from ``finally`` blocks that may be
    executing under cancellation, where awaiting is not dependable.
    """
    if stderr_task is not None:
        stderr_task.cancel()
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    try:
        # Fire-and-forget: the daemon removes the container (--rm) once stopped.
        subprocess.Popen(
            ["docker", "kill", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        logger.warning("Could not stop agent container %s", container)


def _redact_argv(argv: list[str]) -> list[str]:
    out: list[str] = []
    hide = False
    for a in argv:
        if hide:
            out.append("***")
            hide = False
            continue
        if a == "--api-key":
            out.append(a)
            hide = True
            continue
        out.append(a)
    return out


async def stream_agent_command(
    subcommand: str,
    *,
    api_key: str,
    args: list[str],
    host_files: list[Path] | None = None,
    timeout: float = STREAM_TIMEOUT_SECONDS,
) -> AsyncIterator[bytes]:
    """Run moorcheh-agent via Docker and yield raw stdout (SSE for run/continue)."""
    await _ensure_docker_async()
    argv, work, container = _build_argv(
        subcommand, api_key=api_key, args=args, host_files=host_files
    )
    logger.info("Invoking moorcheh-agent: %s", " ".join(_redact_argv(argv)))

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "MOORCHEH_API_KEY": api_key},
    )
    assert proc.stdout is not None
    assert proc.stderr is not None

    stderr_chunks: list[bytes] = []
    stderr = proc.stderr

    async def _drain_stderr() -> None:
        while True:
            chunk = await stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)

    stderr_task = asyncio.create_task(_drain_stderr())
    deadline = time.monotonic() + timeout
    finished = False

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MoorchehAgentError(
                    f"moorcheh-agent {subcommand} timed out after {timeout:.0f}s"
                )
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), remaining)
            except asyncio.TimeoutError as e:
                raise MoorchehAgentError(
                    f"moorcheh-agent {subcommand} timed out after {timeout:.0f}s"
                ) from e
            if not line:
                break
            yield line

        await stderr_task
        returncode = await proc.wait()
        finished = True

        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
        if returncode not in (EXIT_END_TURN, EXIT_REQUIRES_CLIENT_TOOL):
            raise MoorchehAgentError(
                f"moorcheh-agent {subcommand} failed (exit {returncode})"
                + (f": {stderr_text}" if stderr_text else ""),
                stderr=stderr_text or None,
            )
    finally:
        # Covers timeouts, errors, and the consumer hanging up mid-stream: without
        # this the container keeps running (and billing) with nobody reading it.
        if not finished:
            _abort(proc, container, stderr_task)
        _cleanup(work)


async def run_agent_json(
    subcommand: str,
    *,
    api_key: str,
    args: list[str],
    host_files: list[Path] | None = None,
    timeout: float = JSON_TIMEOUT_SECONDS,
) -> Any:
    """Run a JSON-producing command (list/get/delete) via Docker and parse stdout."""
    await _ensure_docker_async()
    argv, work, container = _build_argv(
        subcommand, api_key=api_key, args=args, host_files=host_files
    )
    logger.info("Invoking moorcheh-agent: %s", " ".join(_redact_argv(argv)))

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "MOORCHEH_API_KEY": api_key},
    )
    try:
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError as e:
            raise MoorchehAgentError(
                f"moorcheh-agent {subcommand} timed out after {timeout:.0f}s"
            ) from e
        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            raise MoorchehAgentError(
                f"moorcheh-agent {subcommand} failed (exit {proc.returncode})"
                + (f": {stderr or stdout}" if (stderr or stdout) else ""),
                stderr=stderr or None,
            )
        if not stdout:
            raise MoorchehAgentError(
                f"moorcheh-agent {subcommand} returned empty output",
                stderr=stderr or None,
            )
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as e:
            raise MoorchehAgentError(
                f"moorcheh-agent {subcommand} returned invalid JSON: {e}",
                stderr=stdout[:500],
            ) from e
    finally:
        _abort(proc, container)
        _cleanup(work)


def write_temp_json(data: Any, *, suffix: str = ".json") -> Path:
    """Write JSON to a temp file. Caller should unlink when finished."""
    fd, name = tempfile.mkstemp(prefix="memanto-agent-", suffix=suffix)
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path
