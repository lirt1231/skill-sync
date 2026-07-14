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
    return f"Detected Agents: {detected}\nIssues: {len(result['issues'])}"


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


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _names(value: Any) -> str:
    if not value:
        return "none"
    return ", ".join(str(item) for item in value)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
