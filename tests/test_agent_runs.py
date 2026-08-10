"""
Contract tests for the agent tool-calling routes (/api/v2/agents/{id}/run…).

These never reach Docker: they cover the guards in front of it (session scope,
namespace, server API key) and the failure path when Docker is unavailable —
which is what every caller hits on a machine without a running daemon.
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from memanto.app.config import settings
from memanto.app.main import app
from memanto.app.models.session import Session
from memanto.app.routes.auth_deps import get_current_session
from memanto.app.services import moorcheh_agent_binary
from memanto.app.services.moorcheh_agent_binary import MoorchehAgentError, _build_argv

AGENT_ID = "test-agent-runs"
NAMESPACE = f"memanto_agent_{AGENT_ID}"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def session_override():
    """Authenticate every request as an active session scoped to AGENT_ID."""
    app.dependency_overrides[get_current_session] = lambda: Session(
        session_id="sess-test",
        session_token="token-test",
        agent_id=AGENT_ID,
        namespace=NAMESPACE,
        started_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def no_docker():
    """Make the Docker probe fail the way a machine without Docker does."""
    moorcheh_agent_binary._docker_ok_until = 0.0
    with patch.object(moorcheh_agent_binary.shutil, "which", return_value=None):
        yield
    moorcheh_agent_binary._docker_ok_until = 0.0


class TestSessionScope:
    """A session may only drive the agent it was issued for."""

    async def test_run_rejects_other_agent(self, client, session_override):
        response = await client.post(
            "/api/v2/agents/some-other-agent/run",
            json={"query": "hello"},
        )
        assert response.status_code == 403

    async def test_list_runs_rejects_other_agent(self, client, session_override):
        response = await client.get("/api/v2/agents/some-other-agent/runs")
        assert response.status_code == 403

    async def test_delete_run_rejects_other_agent(self, client, session_override):
        response = await client.delete(
            "/api/v2/agents/some-other-agent/runs/run_abc",
        )
        assert response.status_code == 403

    async def test_run_requires_session_token(self, client):
        """No dependency override here: the real auth dependency must reject."""
        response = await client.post(
            f"/api/v2/agents/{AGENT_ID}/run",
            json={"query": "hello"},
        )
        assert response.status_code == 401


class TestNamespaceGuard:
    async def test_list_runs_rejects_foreign_namespace(self, client, session_override):
        response = await client.get(
            f"/api/v2/agents/{AGENT_ID}/runs",
            params={"namespace": "memanto_agent_someone_else"},
        )
        assert response.status_code == 400
        assert NAMESPACE in response.json()["detail"]

    async def test_list_runs_accepts_own_namespace(
        self, client, session_override, no_docker
    ):
        """The namespace guard passes, so the call proceeds to Docker (502)."""
        response = await client.get(
            f"/api/v2/agents/{AGENT_ID}/runs",
            params={"namespace": NAMESPACE},
        )
        assert response.status_code == 502


class TestServerApiKey:
    async def test_run_returns_503_without_server_key(self, client, session_override):
        with patch.object(settings, "MOORCHEH_API_KEY", ""):
            response = await client.post(
                f"/api/v2/agents/{AGENT_ID}/run",
                json={"query": "hello"},
            )
        assert response.status_code == 503
        assert "MOORCHEH_API_KEY" in response.json()["detail"]

    async def test_list_runs_returns_503_without_server_key(
        self, client, session_override
    ):
        with patch.object(settings, "MOORCHEH_API_KEY", ""):
            response = await client.get(f"/api/v2/agents/{AGENT_ID}/runs")
        assert response.status_code == 503


class TestDockerUnavailable:
    """Docker down must degrade cleanly, not 500 or hang."""

    async def test_run_streams_sse_error(self, client, session_override, no_docker):
        response = await client.post(
            f"/api/v2/agents/{AGENT_ID}/run",
            json={"query": "hello"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        body = response.text
        assert "event: error" in body
        assert "Docker is required" in body
        assert '"stop_reason": "error"' in body

    async def test_continue_streams_sse_error(
        self, client, session_override, no_docker
    ):
        response = await client.post(
            f"/api/v2/agents/{AGENT_ID}/runs/run_abc/continue",
            json={"tool_result": {"tool_use_id": "tooluse_1", "content": "{}"}},
        )
        assert response.status_code == 200
        assert "event: error" in response.text
        assert '"run_id": "run_abc"' in response.text

    async def test_get_run_returns_502(self, client, session_override, no_docker):
        response = await client.get(f"/api/v2/agents/{AGENT_ID}/runs/run_abc")
        assert response.status_code == 502
        assert "Docker is required" in response.json()["detail"]


class TestStreamCleanup:
    """A run that does not finish on its own must not leave the container alive."""

    @staticmethod
    def _fake_agent(*, emit_line: bool):
        """A stand-in for `docker run` that writes at most one line, then hangs."""
        script = "import time" + (
            "; print('event: run_started', flush=True)" if emit_line else ""
        )
        return [sys.executable, "-c", script + "; time.sleep(30)"]

    @staticmethod
    def _patches(argv):
        return (
            patch.object(moorcheh_agent_binary, "_ensure_docker_async", AsyncMock()),
            patch.object(
                moorcheh_agent_binary,
                "_build_argv",
                return_value=(argv, None, "memanto-agent-testcontainer"),
            ),
            patch.object(moorcheh_agent_binary.subprocess, "Popen"),
        )

    async def test_timeout_raises_and_kills_the_process(self):
        argv = self._fake_agent(emit_line=False)
        ensure, build, popen = self._patches(argv)

        with ensure, build, popen as mock_popen:
            stream = moorcheh_agent_binary.stream_agent_command(
                "run", api_key="k", args=[], timeout=0.5
            )
            with pytest.raises(MoorchehAgentError, match="timed out after 0s"):
                async for _ in stream:
                    pass

        # The container is stopped by name, not just the attached client process.
        assert mock_popen.call_args[0][0] == [
            "docker",
            "kill",
            "memanto-agent-testcontainer",
        ]

    async def test_consumer_disconnect_kills_the_process(self):
        argv = self._fake_agent(emit_line=True)
        ensure, build, popen = self._patches(argv)
        spawned = []

        real_exec = asyncio.create_subprocess_exec

        async def capture(*a, **kw):
            proc = await real_exec(*a, **kw)
            spawned.append(proc)
            return proc

        with ensure, build, popen as mock_popen:
            with patch.object(asyncio, "create_subprocess_exec", capture):
                stream = moorcheh_agent_binary.stream_agent_command(
                    "run", api_key="k", args=[]
                )
                first = await stream.__anext__()
                assert first.startswith(b"event: run_started")
                # Hang up mid-stream, exactly as an SSE client walking away does.
                await stream.aclose()

        proc = spawned[0]
        await asyncio.wait_for(proc.wait(), timeout=5)
        assert proc.returncode is not None
        assert mock_popen.call_args[0][0][:2] == ["docker", "kill"]
        # NOTE: on Windows this test emits a ResourceWarning for the killed
        # process's pipe transports — the test's event loop closes before their
        # finalizers run. Harmless in the server, where the loop outlives them.


class TestBuildArgv:
    def test_missing_image_raises(self):
        with patch.object(settings, "MOORCHEH_AGENT_IMAGE", ""):
            with pytest.raises(MoorchehAgentError, match="MOORCHEH_AGENT_IMAGE"):
                _build_argv("run", api_key="k", args=[])

    def test_container_is_named_so_it_can_be_stopped(self):
        argv, work, container = _build_argv("run", api_key="k", args=["--top-k", "5"])
        assert work is None
        assert container.startswith("memanto-agent-")
        assert argv[:6] == ["docker", "run", "--rm", "-i", "--name", container]
        assert argv[-2:] == ["--top-k", "5"]

    def test_api_key_is_redacted_in_logs(self):
        argv, _, _ = _build_argv("run", api_key="super-secret", args=[])
        redacted = moorcheh_agent_binary._redact_argv(argv)
        assert "super-secret" not in redacted
        assert redacted[redacted.index("--api-key") + 1] == "***"

    def test_host_file_is_mounted_and_path_rewritten(self, tmp_path: Path):
        tools = tmp_path / "tools.json"
        tools.write_text("[]", encoding="utf-8")

        argv, work, _ = _build_argv(
            "run",
            api_key="k",
            args=["--tools", str(tools)],
            host_files=[tools],
        )
        try:
            assert work is not None
            assert (work / "tools.json").exists()
            assert argv[argv.index("--tools") + 1] == "/tmp/memanto-agent/tools.json"
            assert any(a.endswith(":/tmp/memanto-agent:ro") for a in argv)
        finally:
            moorcheh_agent_binary._cleanup(work)
