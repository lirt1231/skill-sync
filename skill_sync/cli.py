"""Command-line interface for skill-sync."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from skill_sync import core
from skill_sync.errors import SkillSyncError
from skill_sync.protocol import error_envelope, success_envelope
from skill_sync.version import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Run the skill-sync CLI and return a process exit code."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    json_mode = bool(getattr(args, "json", False))
    command_name = getattr(args, "protocol_command", args.command)

    try:
        result = args.handler(args)
    except SkillSyncError as exc:
        if json_mode:
            print(_json(error_envelope(command_name, exc)), file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code

    if result is not None:
        if json_mode:
            print(_json(success_envelope(command_name, result)))
        else:
            print(result)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skill-sync")
    parser.add_argument(
        "--config",
        help="path to the local skill-sync config file",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="initialize skill-sync state")
    init_parser.add_argument("--repo", required=True, help="Git URL or local repository path")
    init_parser.add_argument("--sync-dir", help="local sync repository directory")
    init_parser.add_argument("--branch", default="main", help="Git branch to synchronize")
    init_parser.add_argument("--skills-root", help="canonical Skill directory (default: ~/.agents/skills)")
    init_parser.add_argument("--platform", help=argparse.SUPPRESS)
    init_parser.set_defaults(handler=_handle_init)

    version_parser = subparsers.add_parser("version", help="show the installed skill-sync version")
    version_parser.add_argument("--json", action="store_true", help="print JSON output")
    version_parser.set_defaults(handler=_handle_version)

    scan_parser = subparsers.add_parser("scan", help="list candidate local Skills")
    scan_parser.add_argument("--platform", help=argparse.SUPPRESS)
    scan_parser.add_argument("--json", action="store_true", help="print JSON output")
    scan_parser.set_defaults(handler=_handle_scan)

    select_parser = subparsers.add_parser("select", help="select local Skills for sync")
    select_parser.add_argument("--platform", help=argparse.SUPPRESS)
    select_parser.add_argument("items", nargs="+", help="Skill names or paths")
    select_parser.add_argument(
        "--allow-external",
        action="store_true",
        help="allow selecting a Skill outside the platform root",
    )
    select_parser.set_defaults(handler=_handle_select)

    deselect_parser = subparsers.add_parser("deselect", help="deselect Skills")
    deselect_parser.add_argument("names", nargs="+", help="Skill names")
    deselect_parser.set_defaults(handler=_handle_deselect)

    status_parser = subparsers.add_parser("status", help="show synchronization status")
    status_parser.add_argument("--json", action="store_true", help="print JSON output")
    _add_skill_filter(status_parser)
    status_parser.set_defaults(handler=_handle_status)

    preview_parser = subparsers.add_parser("preview", help="show the next safe synchronization action")
    preview_parser.add_argument("--json", action="store_true", help="print JSON output")
    _add_skill_filter(preview_parser)
    preview_parser.set_defaults(handler=_handle_preview)

    pull_parser = subparsers.add_parser("pull", help="pull and install remote Skill changes")
    _add_skill_filter(pull_parser)
    pull_parser.set_defaults(handler=_handle_pull)

    push_parser = subparsers.add_parser("push", help="push local Skill changes")
    _add_skill_filter(push_parser)
    push_parser.add_argument("--message", help="commit message")
    push_parser.set_defaults(handler=_handle_push)

    sync_parser = subparsers.add_parser("sync", help="run the safe default sync workflow")
    _add_skill_filter(sync_parser)
    sync_parser.set_defaults(handler=_handle_sync)

    import_parser = subparsers.add_parser("import", help="import Agent-local Skills into the global root")
    import_parser.add_argument("items", nargs="+", help="Skill names to import")
    import_parser.add_argument("--agent", required=True, choices=("codex", "claude", "workbuddy"))
    import_parser.set_defaults(handler=_handle_import)

    copy_parser = subparsers.add_parser("copy", help="copy global Skills into an Agent without linking")
    _add_skill_filter(copy_parser)
    _add_agent_filter(copy_parser)
    copy_parser.set_defaults(handler=_handle_copy)

    link_parser = subparsers.add_parser("link", help="link selected Skills into detected Agents")
    _add_skill_filter(link_parser)
    _add_agent_filter(link_parser)
    link_parser.set_defaults(handler=_handle_link)

    unlink_parser = subparsers.add_parser("unlink", help="remove managed Agent links")
    _add_skill_filter(unlink_parser)
    _add_agent_filter(unlink_parser)
    unlink_parser.set_defaults(handler=_handle_unlink)

    doctor_parser = subparsers.add_parser("doctor", help="diagnose Agent links and global Skills")
    doctor_parser.add_argument("--json", action="store_true", help="print JSON output")
    doctor_parser.set_defaults(handler=_handle_doctor)

    managed_parser = subparsers.add_parser("managed", help="inspect managed Skill ownership")
    managed_subparsers = managed_parser.add_subparsers(dest="managed_action", required=True)
    managed_check_parser = managed_subparsers.add_parser(
        "check", help="check whether a Skill path is managed"
    )
    managed_check_parser.add_argument("path_or_name", help="Skill path or selected Skill name")
    managed_check_parser.add_argument("--client", help="concrete client or Agent family ID")
    managed_check_parser.add_argument("--json", action="store_true", help="print JSON output")
    managed_check_parser.set_defaults(
        handler=_handle_managed_check,
        protocol_command="managed check",
    )

    edit_parser = subparsers.add_parser("edit", help="manage safe Skill edit sessions")
    edit_subparsers = edit_parser.add_subparsers(dest="edit_action", required=True)
    edit_list_parser = edit_subparsers.add_parser(
        "list", help="list machine-local edit sessions"
    )
    edit_list_parser.add_argument("--json", action="store_true", help="print JSON output")
    edit_list_parser.set_defaults(
        handler=_handle_edit_list,
        protocol_command="edit list",
    )
    edit_status_parser = edit_subparsers.add_parser(
        "status", help="show one machine-local edit session"
    )
    edit_status_parser.add_argument("session_id", help="edit session UUID")
    edit_status_parser.add_argument("--json", action="store_true", help="print JSON output")
    edit_status_parser.set_defaults(
        handler=_handle_edit_status,
        protocol_command="edit status",
    )
    edit_begin_parser = edit_subparsers.add_parser(
        "begin", help="create a Base edit workspace"
    )
    edit_begin_parser.add_argument("skill", help="selected logical Skill name")
    edit_begin_parser.add_argument(
        "--base",
        action="store_true",
        required=True,
        help="edit the canonical Base Skill",
    )
    edit_begin_parser.add_argument("--actor", help="client or Agent starting the edit")
    edit_begin_parser.add_argument("--json", action="store_true", help="print JSON output")
    edit_begin_parser.set_defaults(
        handler=_handle_edit_begin,
        protocol_command="edit begin",
    )
    edit_abort_parser = edit_subparsers.add_parser(
        "abort", help="discard a managed edit workspace"
    )
    edit_abort_parser.add_argument("session_id", help="edit session UUID")
    edit_abort_parser.add_argument("--json", action="store_true", help="print JSON output")
    edit_abort_parser.set_defaults(
        handler=_handle_edit_abort,
        protocol_command="edit abort",
    )
    edit_diff_parser = edit_subparsers.add_parser(
        "diff", help="show changes in an active Base edit workspace"
    )
    edit_diff_parser.add_argument("session_id", help="edit session UUID")
    edit_diff_parser.add_argument("--json", action="store_true", help="print JSON output")
    edit_diff_parser.set_defaults(
        handler=_handle_edit_diff,
        protocol_command="edit diff",
    )
    edit_validate_parser = edit_subparsers.add_parser(
        "validate", help="validate an active Base edit workspace"
    )
    edit_validate_parser.add_argument("session_id", help="edit session UUID")
    edit_validate_parser.add_argument(
        "--json", action="store_true", help="print JSON output"
    )
    edit_validate_parser.set_defaults(
        handler=_handle_edit_validate,
        protocol_command="edit validate",
    )
    edit_impact_parser = edit_subparsers.add_parser(
        "impact", help="preview Base deployment impact"
    )
    edit_impact_parser.add_argument("session_id", help="edit session UUID")
    edit_impact_parser.add_argument("--json", action="store_true", help="print JSON output")
    edit_impact_parser.set_defaults(
        handler=_handle_edit_impact,
        protocol_command="edit impact",
    )

    deploy_parser = subparsers.add_parser(
        "deploy", help="inspect and migrate rendered Skill deployments"
    )
    deploy_subparsers = deploy_parser.add_subparsers(dest="deploy_action", required=True)
    deploy_preview_parser = deploy_subparsers.add_parser(
        "preview", help="preview migration to rendered deployments"
    )
    deploy_preview_parser.set_defaults(
        handler=_handle_deploy_preview,
        protocol_command="deploy preview",
    )
    deploy_status_parser = deploy_subparsers.add_parser(
        "status", help="show rendered deployment status"
    )
    deploy_status_parser.add_argument("--json", action="store_true", help="print JSON output")
    deploy_status_parser.set_defaults(
        handler=_handle_deploy_status,
        protocol_command="deploy status",
    )
    deploy_migrate_parser = deploy_subparsers.add_parser(
        "migrate", help="migrate managed links to rendered deployments"
    )
    deploy_migrate_parser.set_defaults(
        handler=_handle_deploy_migrate,
        protocol_command="deploy migrate",
    )
    deploy_gc_parser = deploy_subparsers.add_parser(
        "gc", help="remove verified unreferenced rendered deployments"
    )
    deploy_gc_parser.add_argument(
        "--dry-run", action="store_true", help="list removable deployments only"
    )
    deploy_gc_parser.add_argument("--json", action="store_true", help="print JSON output")
    deploy_gc_parser.set_defaults(
        handler=_handle_deploy_gc,
        protocol_command="deploy gc",
    )

    web_parser = subparsers.add_parser("web", help="start the local management Web UI")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", type=int, default=8765)
    web_parser.add_argument("--no-browser", action="store_true")
    web_parser.set_defaults(handler=_handle_web)

    agent_parser = subparsers.add_parser("agent", help="enable or disable an Agent sync target")
    agent_parser.add_argument("action", choices=("enable", "disable"))
    agent_parser.add_argument(
        "name",
        choices=("codex", "workbuddy", "kimi", "claude"),
    )
    agent_parser.set_defaults(handler=_handle_agent)

    return parser


def _add_skill_filter(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--skill",
        action="append",
        dest="skills",
        help="restrict the command to one selected Skill; repeatable",
    )


def _add_agent_filter(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--agent", action="append", dest="agents", help="restrict to an Agent; repeatable")


def _handle_init(args: argparse.Namespace) -> str:
    kwargs = {"sync_dir": args.sync_dir, "branch": args.branch, "platform": args.platform, "config_path": args.config}
    if args.skills_root is not None:
        kwargs["skills_root"] = args.skills_root
    result = core.init_sync(args.repo, **kwargs)
    location = result.get("skills_root") or result.get("platform", "global")
    return (
        f"Initialized skill-sync repo: {result['sync_repo_path']} "
        f"(branch {result['branch']}, skills {location})"
    )


def _handle_version(args: argparse.Namespace) -> str | dict[str, str]:
    if args.json:
        return {"version": __version__}
    return f"skill-sync {__version__}"


def _handle_scan(args: argparse.Namespace) -> Any:
    result = core.scan_skills(platform=args.platform, config_path=args.config)
    if args.json:
        return result
    if not result:
        return "No Skills found."
    lines = []
    for item in result:
        flags = []
        if item.get("selected"):
            flags.append("selected")
        if item.get("external"):
            flags.append("external")
        suffix = f" ({', '.join(flags)})" if flags else ""
        lines.append(f"{item['name']}{suffix}: {item['path']}")
    return "\n".join(lines)


def _handle_select(args: argparse.Namespace) -> str:
    result = core.select_skills(
        args.items,
        platform=args.platform,
        allow_external=args.allow_external,
        config_path=args.config,
    )
    return f"Selected: {_names(result.get('selected'))}"


def _handle_deselect(args: argparse.Namespace) -> str:
    result = core.deselect_skills(args.names, config_path=args.config)
    return f"Deselected: {_names(result.get('deselected'))}"


def _handle_status(args: argparse.Namespace) -> Any:
    result = core.status(skill_names=args.skills, config_path=args.config)
    if args.json:
        return result
    return _format_status(result)


def _handle_preview(args: argparse.Namespace) -> Any:
    result = core.sync_preview(skill_names=args.skills, config_path=args.config, fetch_remote=False)
    if args.json:
        return result
    return f"Next action: {result['action']}\n{result['summary']}"


def _handle_pull(args: argparse.Namespace) -> str:
    result = core.pull(skill_names=args.skills, config_path=args.config)
    return f"Pulled: {_names(result.get('pulled'))}"


def _handle_push(args: argparse.Namespace) -> str:
    result = core.push(skill_names=args.skills, config_path=args.config, message=args.message)
    suffix = " (committed)" if result.get("committed") else " (no commit needed)"
    return f"Pushed: {_names(result.get('pushed'))}{suffix}"


def _handle_sync(args: argparse.Namespace) -> str:
    result = core.sync(skill_names=args.skills, config_path=args.config)
    if result.get("noop"):
        return "No changes to sync."
    if "pulled" in result:
        return f"Pulled: {_names(result.get('pulled'))}"
    if "pushed" in result:
        suffix = " (committed)" if result.get("committed") else " (no commit needed)"
        return f"Pushed: {_names(result.get('pushed'))}{suffix}"
    return f"Synced: {_names(result.get('synced'))}"


def _handle_import(args: argparse.Namespace) -> str:
    result = core.import_agent_skills(args.items, args.agent, config_path=args.config)
    return f"Imported: {_names(item['name'] for item in result['imported'])}"


def _handle_copy(args: argparse.Namespace) -> str:
    if not args.skills or not args.agents:
        raise SkillSyncError("copy requires at least one --skill and one --agent")
    result = core.copy_global_skills_to_agents(args.skills, args.agents, config_path=args.config)
    return f"Copied: {len(result['copied'])} Agent Skill directories"


def _handle_link(args: argparse.Namespace) -> str:
    result = core.link_skills(skill_names=args.skills, agent_names=args.agents, config_path=args.config)
    return f"Links checked: {len(result['links'])}"


def _handle_unlink(args: argparse.Namespace) -> str:
    result = core.unlink_skills(skill_names=args.skills, agent_names=args.agents, config_path=args.config)
    return f"Links removed: {len(result['unlinked'])}"


def _handle_doctor(args: argparse.Namespace) -> Any:
    result = core.doctor(config_path=args.config)
    if args.json:
        return result
    detected = ", ".join(a["display_name"] for a in result["agents"] if a["detected"]) or "none"
    lines = [f"Detected Agents: {detected}", f"Issues: {len(result['issues'])}"]
    for operation in result.get("deployment_operations", []):
        detail = f"{operation['status']}: {operation['path']}"
        if operation.get("in_flight"):
            detail += f" (in flight: {operation['in_flight']})"
        lines.append(f"Recovery required: {detail}")
    return "\n".join(lines)


def _handle_managed_check(args: argparse.Namespace) -> Any:
    result = core.managed_check(
        args.path_or_name,
        client=args.client,
        config_path=args.config,
    )
    if args.json:
        return result
    ownership = "managed" if result["managed"] else "unmanaged"
    health = "healthy" if result["healthy"] else "unhealthy"
    source = result.get("source_path") or "none"
    client = result.get("client") or "none"
    if result["managed"] and result["healthy"]:
        action = "Do not edit this path directly; use the managed edit workflow."
    elif result["managed"]:
        action = "Stop and repair the managed Skill state before editing."
    else:
        action = "Skill Sync does not manage this path; use the client's normal edit workflow."
    return "\n".join(
        (
            f"Ownership: {ownership} ({result['role']})",
            f"Health: {health} ({result['state']})",
            f"Source: {source}",
            f"Client: {client}",
            f"Recommended action: {action}",
        )
    )


def _handle_edit_begin(args: argparse.Namespace) -> Any:
    result = core.edit_begin(
        args.skill,
        actor=args.actor,
        config_path=args.config,
    )
    if args.json:
        return result
    return "\n".join(
        (
            f"Edit session: {result['session_id']} ({result['skill']}, Base)",
            f"Baseline: {result['baseline_hash']}",
            f"Workspace: {result['workspace_path']}",
        )
    )


def _handle_edit_abort(args: argparse.Namespace) -> Any:
    result = core.edit_abort(args.session_id, config_path=args.config)
    if args.json:
        return result
    return f"Aborted edit session: {result['session_id']} ({result['skill']})"


def _handle_edit_diff(args: argparse.Namespace) -> Any:
    result = core.edit_diff(args.session_id, config_path=args.config)
    if args.json:
        return result
    lines = [f"Edit diff: {result['session_id']} ({result['skill']}, Base)"]
    if not result["changed"]:
        lines.append("No changes.")
        return "\n".join(lines)
    summary = result["summary"]
    lines.append(
        f"Summary: {summary['added']} added, {summary['modified']} modified, "
        f"{summary['deleted']} deleted"
    )
    for item in result["files"]:
        lines.append(f"- {item['change']} {item['kind']}: {item['path']}")
        lines.append(
            f"  hashes: {item['old_hash'] or 'none'} -> {item['new_hash'] or 'none'}; "
            f"bytes: {item['old_size'] if item['old_size'] is not None else 'none'} -> "
            f"{item['new_size'] if item['new_size'] is not None else 'none'}"
        )
        if item["kind"] == "text" and item["diff"]:
            lines.append(item["diff"].rstrip("\n"))
    return "\n".join(lines)


def _handle_edit_validate(args: argparse.Namespace) -> Any:
    result = core.edit_validate(args.session_id, config_path=args.config)
    if args.json:
        return result
    state = "valid" if result["valid"] else "invalid"
    lines = [
        f"Validation: {state} ({result['session_id']}, {result['skill']}, Base)",
        f"Workspace: {result['workspace_hash'] or 'unsafe'}",
        f"Changes: {'yes' if result['changed'] else 'no'}",
        f"Issues: {len(result['issues'])}",
    ]
    lines.extend(
        f"- {issue['code']} {issue['path']}: {issue['message']}"
        for issue in result["issues"]
    )
    return "\n".join(lines)


def _handle_edit_impact(args: argparse.Namespace) -> Any:
    result = core.edit_impact(args.session_id, config_path=args.config)
    if args.json:
        return result
    lines = [
        f"Impact: {result['session_id']} ({result['skill']}, Base)",
        f"Stale baseline: {'yes' if result['stale_baseline'] else 'no'}",
        (
            f"Blocked: {'yes' if result['blocked'] else 'no'}"
            + (f" ({result['blocked_reason']})" if result['blocked_reason'] else "")
        ),
        (
            f"Affected clients: {result['summary']['affected']}; "
            f"rebuilds: {result['summary']['requires_rebuild']}"
        ),
    ]
    for client in result["clients"]:
        lines.append(
            f"- {client['client']} [{client['agent']}, {client['availability']}]: "
            f"{client['action']}; deployment {client['current_deployment_state']} "
            f"-> {client['proposed_deployment_state']}"
        )
    return "\n".join(lines)


def _handle_deploy_preview(args: argparse.Namespace) -> str:
    result = core.deploy_preview(config_path=args.config)
    return _format_deploy_preview(result)


def _handle_deploy_status(args: argparse.Namespace) -> Any:
    result = core.deploy_status(config_path=args.config)
    if args.json:
        return result
    return _format_deploy_status(result)


def _handle_deploy_migrate(args: argparse.Namespace) -> str:
    result = core.deploy_migrate(config_path=args.config)
    return _format_deploy_migrate(result)


def _handle_deploy_gc(args: argparse.Namespace) -> Any:
    result = core.deploy_gc(config_path=args.config, dry_run=args.dry_run)
    if args.json:
        return result
    action = "Would remove" if args.dry_run else "Removed"
    paths = result["candidates"] if args.dry_run else result["removed"]
    lines = [f"{action}: {len(paths)} deployments"]
    lines.extend(f"- {path}" for path in paths)
    if result.get("skipped"):
        lines.append(f"Skipped: {len(result['skipped'])} unsafe or referenced entries")
    return "\n".join(lines)


def _handle_edit_list(args: argparse.Namespace) -> Any:
    result = core.list_edit_sessions(config_path=args.config)
    if args.json:
        return result
    sessions = result["sessions"]
    if not sessions:
        return "No edit sessions."
    return "\n".join(
        f"- {item['session_id']} [{item['status']}] {item['logical_skill']} "
        f"(actor {item['actor'] or 'none'}, updated {item['updated_at']})"
        for item in sessions
    )


def _handle_edit_status(args: argparse.Namespace) -> Any:
    result = core.edit_session_status(args.session_id, config_path=args.config)
    if args.json:
        return result
    return "\n".join(
        (
            f"Session: {result['session_id']}",
            f"Skill: {result['logical_skill']}",
            f"Status: {result['status']}",
            f"Actor: {result['actor'] or 'none'}",
            f"Baseline: {result['baseline_hash']}",
            f"Created: {result['created_at']}",
            f"Updated: {result['updated_at']}",
        )
    )


def _handle_web(args: argparse.Namespace) -> None:
    from skill_sync.web import serve

    serve(host=args.host, port=args.port, config_path=args.config, open_browser=not args.no_browser)
    return None


def _handle_agent(args: argparse.Namespace) -> str:
    if args.action == "disable":
        result = core.disable_agent_sync(args.name, config_path=args.config)
        return f"Disabled Agent sync: {result['disabled']} (removed {len(result['unlinked'])} links)"
    result = core.enable_agent_sync(args.name, config_path=args.config)
    return f"Enabled Agent sync: {result['enabled']}"


def _format_status(result: dict[str, Any]) -> str:
    repo = result["repo"]
    clean_label = "clean" if repo["clean"] else "dirty"
    lines = [
        (
            f"Repo: {repo['path']} "
            f"(branch {repo['branch']}, {clean_label}, "
            f"ahead {repo['ahead']}, behind {repo['behind']}, diverged {repo['diverged']})"
        )
    ]
    skills = result.get("skills", [])
    if not skills:
        lines.append("No selected Skills.")
        return "\n".join(lines)

    for skill in skills:
        change_label = "changed" if skill.get("changed_local") else "unchanged"
        lines.append(
            f"- {skill['name']} [{skill['platform']}] {change_label}: {skill['local_path']}"
        )
    return "\n".join(lines)


def _format_deploy_preview(result: dict[str, Any]) -> str:
    lines = [f"Rendered root: {result['rendered_root']}"]
    skills = result.get("skills", [])
    if not skills:
        lines.append("No managed Skill deployments to preview.")
    for skill in skills:
        lines.append(f"- {skill['name']} (source {skill['source_path']})")
        for client in skill.get("clients", []):
            lines.append(
                f"  - {client['client']} [{client['agent']}]: "
                f"{client['current_state']} -> {client['action']}; "
                f"{client['destination']} -> {client['deployment_path']}"
            )
    if result.get("blocked"):
        lines.append("Migration blocked: resolve the reported deployment state first.")
    return "\n".join(lines)


def _format_deploy_status(result: dict[str, Any]) -> str:
    lines = [f"Rendered root: {result['rendered_root']}"]
    for operation in result.get("operations", []):
        detail = f"{operation['status']}: {operation['path']}"
        if operation.get("in_flight"):
            detail += f" (in flight: {operation['in_flight']})"
        lines.append(f"Recovery required: {detail}")
    skills = result.get("skills", [])
    if not skills:
        lines.append("No managed Skill deployments.")
        return "\n".join(lines)
    for skill in skills:
        lines.append(f"- {skill['name']} (source {skill['source_path']})")
        for client in skill.get("clients", []):
            migration = "migration required" if client["migration_required"] else "current"
            lines.append(
                f"  - {client['client']} [{client['agent']}]: "
                f"deployment {client['deployment_state']}, link {client['link_state']}, {migration}; "
                f"{client['destination']} -> {client['deployment_path']}"
            )
    return "\n".join(lines)


def _format_deploy_migrate(result: dict[str, Any]) -> str:
    lines = [f"Rendered root: {result['rendered_root']}"]
    migrated = result.get("migrated", [])
    if result.get("noop") or not migrated:
        lines.append("No deployment migrations needed.")
        return "\n".join(lines)
    lines.append(f"Migrated: {len(migrated)} Skill/client links")
    for item in migrated:
        lines.append(
            f"- {item['skill']} / {item['client']}: {item['state']}; "
            f"{item['from']} -> {item['to']}"
        )
    return "\n".join(lines)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _names(value: Any) -> str:
    if not value:
        return "none"
    return ", ".join(str(item) for item in value)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
