"""
MEMANTO CLI - Memory expiry policy commands.

Policies decide when a memory stops being active. Nothing expires on its own:
``memanto policy apply`` runs the sweep that stamps matching memories.
"""

import typer
from rich.panel import Panel
from rich.table import Table

from memanto.cli.commands._shared import (
    BOLD_PRIMARY,
    BRIGHT,
    DIM,
    PRIMARY,
    SUCCESS,
    WARNING,
    _error,
    config_manager,
    console,
    format_local_time,
    get_client,
    policy_app,
)


def _resolve_agent(agent_id: str | None) -> str:
    """Return the requested agent, falling back to the active one."""
    if agent_id:
        return agent_id

    active_agent_id, active_session_token = config_manager.get_active_session()
    if not active_agent_id or not active_session_token:
        _error(
            "No agent specified and no active agent.",
            hint="Provide --agent or run 'memanto agent activate <agent-id>' first.",
        )
    return active_agent_id


def _render_policy(policy: dict, agent_id: str) -> None:
    """Print a policy's retention table and rules."""
    retention = policy.get("retention") or {}
    rules = policy.get("rules") or []
    purge = policy.get("purge_expired_after", "never")

    if retention:
        table = Table(
            show_header=True, header_style=BOLD_PRIMARY, title="Retention by type"
        )
        table.add_column("Type", style=BRIGHT)
        table.add_column("Expires after", justify="right", style="white")
        for memory_type, duration in sorted(retention.items()):
            style = DIM if duration == "never" else "white"
            table.add_row(memory_type, f"[{style}]{duration}[/{style}]")
        console.print()
        console.print(table)
    else:
        console.print("\n[dim]No per-type retention set.[/dim]")

    if rules:
        rule_table = Table(show_header=True, header_style=BOLD_PRIMARY, title="Rules")
        rule_table.add_column("Name", style=BRIGHT)
        rule_table.add_column("Matches", style="white")
        rule_table.add_column("Expires after", justify="right", style="white")
        for rule in rules:
            match = rule.get("match") or {}
            conditions = [
                f"{key}={value}" for key, value in match.items() if value is not None
            ]
            expire_after = rule.get("expire_after", "never")
            style = DIM if expire_after == "never" else "white"
            rule_table.add_row(
                rule.get("name", "?"),
                ", ".join(conditions) or "[dim]everything[/dim]",
                f"[{style}]{expire_after}[/{style}]",
            )
        console.print()
        console.print(rule_table)
    else:
        console.print("[dim]No rules set.[/dim]")

    purge_label = (
        f"[{WARNING}]{purge}[/{WARNING}]"
        if purge != "never"
        else f"[{DIM}]never[/{DIM}]"
    )
    console.print(f"\n[dim]Purge expired after:[/dim] {purge_label}")
    console.print(f"[dim]Agent: {agent_id}[/dim]")


@policy_app.command("show")
def policy_show(
    agent_id: str | None = typer.Option(
        None, "--agent", "-a", help="Agent identifier (defaults to active agent)"
    ),
):
    """Show the agent's current expiry policy.

    Examples:
        memanto policy show
        memanto policy show --agent my-agent
    """
    agent_id = _resolve_agent(agent_id)
    client = get_client()

    try:
        result = client.get_policy(agent_id)
    except Exception as e:
        _error(f"Failed to load policy: {e}")

    console.print(
        Panel.fit(
            f"[{BOLD_PRIMARY}]Expiry Policy[/{BOLD_PRIMARY}]\n"
            f"Agent: [bold]{agent_id}[/bold]",
            border_style=PRIMARY,
        )
    )

    if result.get("is_empty"):
        console.print(
            "\n[yellow]No policy set — nothing will ever expire.[/yellow]\n"
            "[dim]Start from a preset: 'memanto policy preset balanced'[/dim]"
        )
        return

    _render_policy(result.get("policy") or {}, agent_id)


@policy_app.command("presets")
def policy_presets():
    """List the predefined policy bundles.

    Examples:
        memanto policy presets
    """
    client = get_client()

    try:
        presets = client.list_policy_presets()
    except Exception as e:
        _error(f"Failed to list presets: {e}")

    table = Table(show_header=True, header_style=BOLD_PRIMARY, title="Policy presets")
    table.add_column("Name", style=BRIGHT)
    table.add_column("Description", style="white")
    table.add_column("Types", justify="right", style="white")
    table.add_column("Rules", justify="right", style="white")
    table.add_column("Purge", justify="right", style="white")

    for preset in presets:
        purge = preset.get("purge_expired_after", "never")
        table.add_row(
            preset["name"],
            preset["description"],
            str(len(preset.get("retention") or {})),
            str(preset.get("rule_count", 0)),
            purge,
        )

    console.print()
    console.print(table)
    console.print("\n[dim]Adopt one with: memanto policy preset <name>[/dim]")


@policy_app.command("preset")
def policy_preset(
    name: str = typer.Argument(
        ..., help="Preset name (conservative/balanced/aggressive)"
    ),
    agent_id: str | None = typer.Option(
        None, "--agent", "-a", help="Agent identifier (defaults to active agent)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
):
    """Adopt a predefined policy bundle, replacing the current policy.

    Nothing expires until you run 'memanto policy apply'.

    Examples:
        memanto policy preset balanced
        memanto policy preset aggressive --agent my-agent
    """
    agent_id = _resolve_agent(agent_id)
    client = get_client()

    if not yes:
        console.print(
            f"\n[{WARNING}]This replaces the entire policy for "
            f"'{agent_id}'.[/{WARNING}]"
        )
        if not typer.confirm(f"Adopt preset '{name}'?", default=False):
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        result = client.apply_policy_preset(agent_id, name)
    except ValueError as e:
        _error(str(e))
    except Exception as e:
        _error(f"Failed to adopt preset: {e}")

    console.print(f"\n[green]Adopted preset '{name}' for '{agent_id}'.[/green]")
    _render_policy(result.get("policy") or {}, agent_id)
    console.print(
        "\n[dim]Preview what this would expire: memanto policy apply --dry-run[/dim]"
    )


@policy_app.command("apply")
def policy_apply(
    agent_id: str | None = typer.Option(
        None, "--agent", "-a", help="Agent identifier (defaults to active agent)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be expired without writing"
    ),
    limit: int = typer.Option(
        20, "--limit", "-n", help="Max matched memories to list (default 20)"
    ),
):
    """Sweep the agent's memories and expire everything the policy matches.

    Run with --dry-run first to see exactly what would change.

    Examples:
        memanto policy apply --dry-run
        memanto policy apply
    """
    agent_id = _resolve_agent(agent_id)
    client = get_client()

    mode = "Dry run" if dry_run else "Applying"
    console.print(
        Panel.fit(
            f"[{BOLD_PRIMARY}]Policy Sweep[/{BOLD_PRIMARY}]\n"
            f"Agent: [bold]{agent_id}[/bold]  •  {mode}",
            border_style=PRIMARY,
        )
    )

    with console.status(f"[{PRIMARY}]Evaluating policy...", spinner="dots"):
        try:
            report = client.apply_policy(agent_id, dry_run=dry_run)
        except Exception as e:
            _error(f"Policy sweep failed: {e}")

    if report.get("policy_is_empty"):
        console.print(
            "\n[yellow]No policy set — nothing will ever expire.[/yellow]\n"
            "[dim]Start from a preset: 'memanto policy preset balanced'[/dim]"
        )
        return

    matched = report.get("matched", 0)
    scanned = report.get("scanned", 0)

    if matched == 0:
        console.print(
            f"\n[green]Nothing to expire.[/green] "
            f"[dim]{scanned} active memories scanned.[/dim]"
        )
        return

    per_rule = report.get("per_rule") or {}
    table = Table(show_header=True, header_style=BOLD_PRIMARY, title="Matched by rule")
    table.add_column("Rule", style=BRIGHT)
    table.add_column("Count", justify="right", style="white")
    for rule_name, count in sorted(per_rule.items(), key=lambda kv: -kv[1]):
        table.add_row(rule_name, str(count))
    console.print()
    console.print(table)

    memories = report.get("memories") or []
    if memories:
        detail = Table(show_header=True, header_style=BOLD_PRIMARY, title="Memories")
        detail.add_column("Title", style=BRIGHT)
        detail.add_column("Type", style="white")
        detail.add_column("Rule", style="white")
        detail.add_column("Last updated", style="white")
        for item in memories[:limit]:
            updated = item.get("updated_at") or item.get("created_at")
            detail.add_row(
                (item.get("title") or "Untitled")[:50],
                item.get("type") or "—",
                str(item.get("expired_by")),
                format_local_time(updated) if updated else "—",
            )
        console.print()
        console.print(detail)
        if len(memories) > limit:
            console.print(f"[dim]... and {len(memories) - limit} more[/dim]")

    if dry_run:
        console.print(
            f"\n[{WARNING}]Dry run — nothing was changed.[/{WARNING}] "
            f"[dim]{matched} of {scanned} memories would be expired.[/dim]"
        )
        console.print("[dim]Run without --dry-run to apply.[/dim]")
    else:
        console.print(
            f"\n[{SUCCESS}]Expired {report.get('expired', 0)} "
            f"of {scanned} memories.[/{SUCCESS}]"
        )
        console.print(
            "[dim]They remain recallable and labelled [EXPIRED]. "
            "Restore one with 'memanto memory restore <id>'.[/dim]"
        )

    errors = report.get("errors") or []
    if errors:
        console.print(f"\n[red]{len(errors)} memory/memories failed to expire:[/red]")
        for err in errors[:5]:
            console.print(f"[red]  {err.get('id')}: {err.get('error')}[/red]")


@policy_app.command("purge")
def policy_purge(
    agent_id: str | None = typer.Option(
        None, "--agent", "-a", help="Agent identifier (defaults to active agent)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be deleted without deleting"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
):
    """Permanently delete memories expired longer than the purge window.

    Destructive and irreversible. Disabled unless the policy sets
    'purge_expired_after'.

    Examples:
        memanto policy purge --dry-run
        memanto policy purge
    """
    agent_id = _resolve_agent(agent_id)
    client = get_client()

    with console.status(f"[{PRIMARY}]Finding purgeable memories...", spinner="dots"):
        try:
            preview = client.purge_expired(agent_id, dry_run=True)
        except Exception as e:
            _error(f"Purge failed: {e}")

    if not preview.get("enabled"):
        console.print(
            "\n[yellow]Purging is disabled for this agent.[/yellow]\n"
            "[dim]Set 'purge_expired_after' in the policy to enable it.[/dim]"
        )
        return

    matched = preview.get("matched", 0)
    if matched == 0:
        console.print("\n[green]Nothing to purge.[/green]")
        return

    window = preview.get("purge_expired_after")
    console.print(
        f"\n[{WARNING}]{matched} memory/memories expired more than {window} ago."
        f"[/{WARNING}]"
    )

    for item in (preview.get("memories") or [])[:10]:
        console.print(f"[dim]  {item.get('id')} — {item.get('title')}[/dim]")
    if matched > 10:
        console.print(f"[dim]  ... and {matched - 10} more[/dim]")

    if dry_run:
        console.print(
            f"\n[{WARNING}]Dry run — nothing was deleted.[/{WARNING}]\n"
            "[dim]Run without --dry-run to purge.[/dim]"
        )
        return

    if not yes:
        console.print(
            f"\n[red]This permanently deletes {matched} memory/memories. "
            "They cannot be restored.[/red]"
        )
        if not typer.confirm("Purge them?", default=False):
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    with console.status(f"[{PRIMARY}]Purging...", spinner="dots"):
        try:
            report = client.purge_expired(agent_id, dry_run=False)
        except Exception as e:
            _error(f"Purge failed: {e}")

    console.print(f"\n[green]Purged {report.get('purged', 0)} memories.[/green]")

    errors = report.get("errors") or []
    if errors:
        console.print(f"\n[red]{len(errors)} failed to delete:[/red]")
        for err in errors[:5]:
            console.print(f"[red]  {err.get('id')}: {err.get('error')}[/red]")
