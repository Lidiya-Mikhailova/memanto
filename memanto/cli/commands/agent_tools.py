"""
MEMANTO CLI — Agent tool-calling (run / continue / runs list|get|delete).

Invokes the private moorcheh-agent Docker image. Requires an active Memanto agent
session and MOORCHEH_API_KEY. Does not print Function URL or signing secrets.

Note: ``memanto agent list`` / ``delete`` remain agent lifecycle commands.
Run CRUD lives under ``memanto agent runs …``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import typer

from memanto.app.core import agent_namespace
from memanto.app.services.moorcheh_agent_binary import (
    EXIT_END_TURN,
    EXIT_REQUIRES_CLIENT_TOOL,
    MoorchehAgentError,
    run_agent_json,
    stream_agent_command,
)
from memanto.cli.commands._shared import (
    PRIMARY,
    _error,
    agent_app,
    config_manager,
    console,
    get_client,
)

agent_runs_app = typer.Typer(help="List / get / delete Moorcheh agent runs")
agent_app.add_typer(agent_runs_app, name="runs")


def _print_json(data: Any) -> None:
    """Print JSON safely on Windows consoles (emoji / non-cp1252 chars)."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))


def _require_active_agent(agent_id: str | None) -> tuple[str, str]:
    """Return (agent_id, api_key)."""
    active_id, active_token = config_manager.get_active_session()
    resolved = agent_id or active_id
    if not resolved or not active_token:
        _error(
            "No active agent.",
            hint="Run 'memanto agent activate <agent-id>' first.",
        )
    if agent_id and active_id and agent_id != active_id:
        _error(
            f"Active session is for '{active_id}', not '{agent_id}'.",
            hint=f"Run 'memanto agent activate {agent_id}' first.",
        )
    # Ensure client session is loaded (validates token)
    client = get_client()
    if client.agent_id and client.agent_id != resolved:
        _error(f"Client agent mismatch: expected {resolved}, got {client.agent_id}")

    api_key = config_manager.get_api_key()
    if not api_key:
        _error(
            "MOORCHEH_API_KEY not configured.",
            hint="Run 'memanto config set-api-key' or set MOORCHEH_API_KEY.",
        )
    return resolved, api_key


def _namespace_for(agent_id: str) -> str:
    return agent_namespace(agent_id)


async def _stream_to_stdout(
    subcommand: str,
    *,
    api_key: str,
    args: list[str],
    host_files: list[Path] | None = None,
) -> int:
    """Stream SSE to stdout; return process-style exit code."""
    exit_code = EXIT_END_TURN
    try:
        async for chunk in stream_agent_command(
            subcommand,
            api_key=api_key,
            args=args,
            host_files=host_files,
        ):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            text = chunk.decode("utf-8", errors="replace")
            if "requires_client_tool" in text and "stop_reason" in text:
                exit_code = EXIT_REQUIRES_CLIENT_TOOL
    except MoorchehAgentError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    return exit_code


@agent_app.command("run")
def agent_tool_run(
    query: str = typer.Argument(..., help="Query for the agent"),
    agent_id: str | None = typer.Option(
        None, "--agent-id", "-a", help="Agent id (default: active session)"
    ),
    tools: Path | None = typer.Option(
        None, "--tools", help="JSON file with client tool definitions (array)"
    ),
    top_k: int = typer.Option(5, "--top-k", help="Search top_k"),
    temperature: float = typer.Option(0.3, "--temperature"),
    ai_model: str | None = typer.Option(None, "--ai-model"),
    quiet: bool = typer.Option(
        False, "--quiet", help="Suppress status spinner (SSE still prints)"
    ),
):
    """Start a Moorcheh agent run (SSE). Uses namespace memanto_agent_{id}."""
    resolved_id, api_key = _require_active_agent(agent_id)
    ns = _namespace_for(resolved_id)

    args = [
        "--namespace",
        ns,
        "--query",
        query,
        "--top-k",
        str(top_k),
        "--temperature",
        str(temperature),
    ]
    if ai_model:
        args.extend(["--ai-model", ai_model])

    host_files: list[Path] = []
    if tools is not None:
        if not tools.is_file():
            _error(f"Tools file not found: {tools}")
        args.extend(["--tools", str(tools.resolve())])
        host_files.append(tools.resolve())

    if not quiet:
        console.print(
            f"[dim]Agent run[/dim] [{PRIMARY}]{resolved_id}[/{PRIMARY}] "
            f"[dim]namespace={ns}[/dim]"
        )

    code = asyncio.run(
        _stream_to_stdout(
            "run", api_key=api_key, args=args, host_files=host_files or None
        )
    )
    raise typer.Exit(code)


@agent_app.command("continue")
def agent_tool_continue(
    run_id: str = typer.Option(..., "--run-id", help="Run id from previous run"),
    tool_result: Path = typer.Option(
        ..., "--tool-result", help="JSON file with tool_use_id + content"
    ),
    agent_id: str | None = typer.Option(
        None, "--agent-id", "-a", help="Agent id (default: active session)"
    ),
):
    """Continue a run after executing a client tool (SSE)."""
    _, api_key = _require_active_agent(agent_id)

    if not tool_result.is_file():
        _error(f"Tool result file not found: {tool_result}")

    path = tool_result.resolve()
    args = ["--run-id", run_id, "--tool-result", str(path)]
    code = asyncio.run(
        _stream_to_stdout("continue", api_key=api_key, args=args, host_files=[path])
    )
    raise typer.Exit(code)


@agent_runs_app.command("list")
def agent_runs_list(
    agent_id: str | None = typer.Option(
        None, "--agent-id", "-a", help="Agent id (default: active session)"
    ),
    limit: int | None = typer.Option(None, "--limit", "-n", min=1, max=100),
):
    """List Moorcheh agent runs for this agent's namespace (JSON)."""
    resolved_id, api_key = _require_active_agent(agent_id)
    ns = _namespace_for(resolved_id)
    args = ["--namespace", ns]
    if limit is not None:
        args.extend(["--limit", str(limit)])

    try:
        data = asyncio.run(run_agent_json("list", api_key=api_key, args=args))
    except MoorchehAgentError as e:
        _error(str(e))
    _print_json(data)


@agent_runs_app.command("get")
def agent_runs_get(
    run_id: str = typer.Argument(..., help="Run id"),
    agent_id: str | None = typer.Option(
        None, "--agent-id", "-a", help="Agent id (default: active session)"
    ),
    include_messages: bool = typer.Option(
        False, "--include-messages", help="Include full transcript"
    ),
):
    """Get one Moorcheh agent run (JSON)."""
    _, api_key = _require_active_agent(agent_id)
    args = ["--run-id", run_id]
    if include_messages:
        args.append("--include-messages")
    try:
        data = asyncio.run(run_agent_json("get", api_key=api_key, args=args))
    except MoorchehAgentError as e:
        _error(str(e))
    _print_json(data)


@agent_runs_app.command("delete")
def agent_runs_delete(
    run_id: str = typer.Argument(..., help="Run id"),
    agent_id: str | None = typer.Option(
        None, "--agent-id", "-a", help="Agent id (default: active session)"
    ),
):
    """Hard-delete one Moorcheh agent run (JSON)."""
    _, api_key = _require_active_agent(agent_id)
    try:
        data = asyncio.run(
            run_agent_json("delete", api_key=api_key, args=["--run-id", run_id])
        )
    except MoorchehAgentError as e:
        _error(str(e))
    _print_json(data)
