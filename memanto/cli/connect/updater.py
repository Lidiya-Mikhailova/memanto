import re
from pathlib import Path
from typing import Any

from memanto.cli.connect.agent_registry import list_agents
from memanto.cli.connect.engine import install_agent
from memanto.cli.connect.templates import TEMPLATE_VERSION


def _extract_version(file_path: Path) -> str | None:
    """Extracts the memanto template version from a file."""
    if not file_path.exists():
        return None

    try:
        content = file_path.read_text(encoding="utf-8")
        match = re.search(r"<!-- memanto-template-version: ([\d\.]+) -->", content)
        if match:
            return match.group(1)
        if "<!-- MEMANTO-MANAGED-SECTION -->" in content:
            return "0.0.0"
        return None
    except Exception:
        return None


def _is_version_lower(v1: str, v2: str) -> bool:
    """Simple semver comparison."""
    try:
        return [int(x) for x in v1.split(".")] < [int(x) for x in v2.split(".")]
    except ValueError:
        return False


def check_for_updates(
    project_dir: str = ".",
) -> dict[str, Any]:
    """
    Scans the workspace and globally for active integrations and checks if their templates are outdated.

    Returns:
        dict: {
            "outdated": bool,
            "installed_version": str | None,
            "latest_version": str,
            "active_agents": list[str],
            "active_local": list[str],
            "active_global": list[str]
        }
    """
    project_path = Path(project_dir).expanduser().resolve()
    lowest_version = None
    active_local = []
    active_global = []

    for agent in list_agents():
        # Check Local
        local_has_files = False
        inst_path_local = agent.resolve_instruction_file(project_path, False)
        if inst_path_local and inst_path_local.exists():
            local_has_files = True
            v = _extract_version(inst_path_local)
            if v and (lowest_version is None or _is_version_lower(v, lowest_version)):
                lowest_version = v

        skill_dir_local = agent.resolve_skill_local(project_path)
        skill_path_local = skill_dir_local / "SKILL.md"
        if skill_path_local.exists():
            local_has_files = True
            v = _extract_version(skill_path_local)
            if v and (lowest_version is None or _is_version_lower(v, lowest_version)):
                lowest_version = v

        if local_has_files:
            active_local.append(agent.name)

        # Check Global
        global_has_files = False
        inst_path_global = agent.resolve_instruction_file(project_path, True)
        if inst_path_global and inst_path_global.exists():
            global_has_files = True
            v = _extract_version(inst_path_global)
            if v and (lowest_version is None or _is_version_lower(v, lowest_version)):
                lowest_version = v

        skill_dir_global = agent.resolve_skill_global()
        skill_path_global = skill_dir_global / "SKILL.md"
        if skill_path_global.exists():
            global_has_files = True
            v = _extract_version(skill_path_global)
            if v and (lowest_version is None or _is_version_lower(v, lowest_version)):
                lowest_version = v

        if global_has_files:
            active_global.append(agent.name)

    is_outdated = False
    if lowest_version and _is_version_lower(lowest_version, TEMPLATE_VERSION):
        is_outdated = True

    return {
        "outdated": is_outdated,
        "installed_version": lowest_version,
        "latest_version": TEMPLATE_VERSION,
        "active_agents": list(set(active_local + active_global)),
        "active_local": active_local,
        "active_global": active_global,
    }


def update_all_agents(
    project_dir: str = ".", update_global: bool = True, update_local: bool = True
) -> list[str]:
    """
    Updates all currently installed agents to the latest template version.
    Returns a list of messages.
    """
    status = check_for_updates(project_dir)
    messages = []

    if not status.get("active_local") and not status.get("active_global"):
        return ["No active Memanto integrations found to update."]

    if update_local:
        for agent_name in status.get("active_local", []):
            res = install_agent(agent_name, project_dir, is_global=False)
            messages.extend(res.get("steps", []))
            if res.get("errors"):
                messages.extend(
                    [f"Error updating local {agent_name}: {e}" for e in res["errors"]]
                )

    if update_global:
        for agent_name in status.get("active_global", []):
            res = install_agent(agent_name, project_dir, is_global=True)
            messages.extend(res.get("steps", []))
            if res.get("errors"):
                messages.extend(
                    [f"Error updating global {agent_name}: {e}" for e in res["errors"]]
                )

    messages.append(
        f"\n🎉 Successfully updated all active templates to v{TEMPLATE_VERSION}!"
    )
    return messages
