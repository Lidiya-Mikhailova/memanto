"""
Agent tool-calling routes (Moorcheh agent via private Docker image).

Session-authenticated. Proxies to moorcheh-agent (SSE for run/continue, JSON for
list/get/delete). Does not expose Function URL or signing keys.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from memanto.app.config import settings
from memanto.app.models.session import Session
from memanto.app.routes.auth_deps import get_current_session
from memanto.app.services.moorcheh_agent_binary import (
    MoorchehAgentError,
    run_agent_json,
    stream_agent_command,
    write_temp_json,
)
from memanto.app.utils.errors import AuthorizationError, map_error_to_http_exception

logger = logging.getLogger(__name__)

router = APIRouter()


class AgentRunRequest(BaseModel):
    query: str = Field(..., min_length=1, description="User query for the agent")
    tools: list[dict[str, Any]] | None = Field(
        None, description="Optional client tool definitions (JSON Schema tools array)"
    )
    top_k: int = Field(5, ge=1, le=50)
    temperature: float = Field(0.3, ge=0.0, le=2.0)
    ai_model: str | None = None


class AgentContinueRequest(BaseModel):
    tool_result: dict[str, Any] = Field(
        ...,
        description="Tool result payload: tool_use_id, content, optional status",
    )


def enforce_session_scope(session: Session, agent_id: str) -> None:
    if session.agent_id != agent_id:
        raise map_error_to_http_exception(
            AuthorizationError(
                f"Session is scoped to agent '{session.agent_id}', not '{agent_id}'"
            )
        )


def _require_api_key() -> str:
    key = (settings.MOORCHEH_API_KEY or "").strip()
    if not key:
        raise HTTPException(
            status_code=503,
            detail="MOORCHEH_API_KEY is not configured on the Memanto server",
        )
    return key


def _sse_error_event(message: str, *, run_id: str | None = None) -> bytes:
    payload = {"code": "agent_error", "message": message}
    if run_id:
        payload["run_id"] = run_id
    import json

    return (
        f"event: error\ndata: {json.dumps(payload)}\n\n"
        f"event: done\ndata: {json.dumps({'stop_reason': 'error'})}\n\n"
    ).encode()


@router.post("/{agent_id}/run")
async def agent_run(
    agent_id: str,
    request: AgentRunRequest = Body(...),
    session: Session = Depends(get_current_session),
):
    """Start an agent run. Streams SSE from the moorcheh-agent Docker image.

    Public Memanto path is ``/run`` (not Moorcheh Function URL ``/agent/run``).
    """
    enforce_session_scope(session, agent_id)
    api_key = _require_api_key()
    namespace = session.namespace

    args = [
        "--namespace",
        namespace,
        "--query",
        request.query,
        "--top-k",
        str(request.top_k),
        "--temperature",
        str(request.temperature),
    ]
    if request.ai_model:
        args.extend(["--ai-model", request.ai_model])

    host_files = []
    tools_path = None
    if request.tools is not None:
        tools_path = write_temp_json(request.tools, suffix="-tools.json")
        host_files.append(tools_path)
        args.extend(["--tools", str(tools_path)])

    async def event_stream():
        try:
            async for chunk in stream_agent_command(
                "run",
                api_key=api_key,
                args=args,
                host_files=host_files or None,
            ):
                yield chunk
        except MoorchehAgentError as e:
            logger.exception("agent run failed")
            yield _sse_error_event(str(e))
        finally:
            if tools_path is not None:
                tools_path.unlink(missing_ok=True)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{agent_id}/runs/{run_id}/continue")
async def agent_continue(
    agent_id: str,
    run_id: str,
    request: AgentContinueRequest = Body(...),
    session: Session = Depends(get_current_session),
):
    """Continue a run after a client tool result. Streams SSE."""
    enforce_session_scope(session, agent_id)
    api_key = _require_api_key()

    result_path = write_temp_json(request.tool_result, suffix="-tool-result.json")
    args = ["--run-id", run_id, "--tool-result", str(result_path)]

    async def event_stream():
        try:
            async for chunk in stream_agent_command(
                "continue",
                api_key=api_key,
                args=args,
                host_files=[result_path],
            ):
                yield chunk
        except MoorchehAgentError as e:
            logger.exception("agent continue failed")
            yield _sse_error_event(str(e), run_id=run_id)
        finally:
            result_path.unlink(missing_ok=True)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{agent_id}/runs")
async def agent_list_runs(
    agent_id: str,
    namespace: str | None = Query(
        None,
        description="Must match the session namespace (memanto_agent_{agent_id})",
    ),
    limit: int | None = Query(None, ge=1, le=100),
    session: Session = Depends(get_current_session),
):
    """List agent runs for this agent's namespace (JSON)."""
    enforce_session_scope(session, agent_id)
    api_key = _require_api_key()
    ns = session.namespace
    if namespace is not None and namespace != ns:
        raise HTTPException(
            status_code=400,
            detail=f"namespace must be '{ns}' for this session",
        )

    args = ["--namespace", ns]
    if limit is not None:
        args.extend(["--limit", str(limit)])

    try:
        data = await run_agent_json("list", api_key=api_key, args=args)
    except MoorchehAgentError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return JSONResponse(content=data)


@router.get("/{agent_id}/runs/{run_id}")
async def agent_get_run(
    agent_id: str,
    run_id: str,
    include: str | None = Query(
        None, description="Pass 'messages' to include full transcript"
    ),
    session: Session = Depends(get_current_session),
):
    """Get one agent run (JSON)."""
    enforce_session_scope(session, agent_id)
    api_key = _require_api_key()

    args = ["--run-id", run_id]
    if include == "messages":
        args.append("--include-messages")

    try:
        data = await run_agent_json("get", api_key=api_key, args=args)
    except MoorchehAgentError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return JSONResponse(content=data)


@router.delete("/{agent_id}/runs/{run_id}")
async def agent_delete_run(
    agent_id: str,
    run_id: str,
    session: Session = Depends(get_current_session),
):
    """Hard-delete one agent run (JSON)."""
    enforce_session_scope(session, agent_id)
    api_key = _require_api_key()

    try:
        data = await run_agent_json(
            "delete", api_key=api_key, args=["--run-id", run_id]
        )
    except MoorchehAgentError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return JSONResponse(content=data)
