# -*- coding: utf-8 -*-
"""
cpsm.cli — headless CLI dispatcher for CPSM.

Spec sections: §1.3, §5.4, §5.5

Subcommands
-----------
    cpsm --version
    cpsm --help
    cpsm validate    [--config PATH] [--json]
    cpsm launch      <connection_id> [--config PATH] [--json]
                     [--isolation shared|per-group --group GROUP_ID]
    cpsm launch-group <group_id>  [--config PATH] [--json]
    cpsm launch-scene <scene_id>  [--config PATH] [--json]
    cpsm import <legacy.yaml> -o <out.yaml> [--force]

Exit codes
----------
    0   Success
    1   Generic failure (unspecified error)
    2   Bad CLI args (argparse default)
    3   Config validation failed
    4   Connection / group / scene not found
    5   Launch failed (ssh/exec error)
    6   Import target file already exists (and --force not given)
    7   Source legacy file not found
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

from cpsm import __version__

__all__ = ["dispatch"]

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_BAD_ARGS = 2
EXIT_VALIDATION_FAILED = 3
EXIT_NOT_FOUND = 4
EXIT_LAUNCH_FAILED = 5
EXIT_TARGET_EXISTS = 6
EXIT_SOURCE_NOT_FOUND = 7

# ---------------------------------------------------------------------------
# Service-stack factory
# ---------------------------------------------------------------------------


def _make_services() -> SimpleNamespace:
    """Build the headless service stack (no Qt, no GUI).

    Returns a namespace with:
      .config     — ConfigService
      .session    — SessionService
      .layout     — LayoutService
      .templates  — TemplateService
      .repository — CpsmRepository

    Each internal import is done at call-time so tests can patch the
    sub-modules (e.g. ``cpsm.platform.tmux_backend.TmuxBackend``) before
    this function is invoked.
    """
    import cpsm.platform.tmux_backend as _tmux_mod
    from cpsm.data.repository import CpsmRepository
    from cpsm.services.config_service import ConfigService
    from cpsm.services.layout_service import LayoutService
    from cpsm.services.session_service import SessionService
    from cpsm.services.template_service import TemplateService

    repository = CpsmRepository()
    config = ConfigService(repository)
    backend = _tmux_mod.TmuxBackend()
    templates = TemplateService()
    layout = LayoutService(backend)
    session = SessionService(config, backend, templates, layout)
    return SimpleNamespace(
        config=config,
        session=session,
        layout=layout,
        templates=templates,
        repository=repository,
    )


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

_EPILOG = """\
Exit codes:
  0   Success
  1   Generic failure
  2   Bad CLI arguments
  3   Config validation failed
  4   Connection / group / scene not found
  5   Launch failed (ssh/exec error)
  6   Import target file already exists (--force not given)
  7   Source legacy file not found
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cpsm",
        description="CPSM — Cross-Platform Session Manager (headless CLI)",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"cpsm {__version__}",
    )

    sub = parser.add_subparsers(dest="subcommand", metavar="COMMAND")

    # ---- validate ----------------------------------------------------------
    p_val = sub.add_parser(
        "validate",
        help="Load and validate the CPSM config file.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_val.add_argument("--config", metavar="PATH", help="Path to .cpsm.yaml")
    p_val.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help='Output {"valid": bool, "issues": [...]} as JSON.',
    )

    # ---- launch ------------------------------------------------------------
    p_launch = sub.add_parser(
        "launch",
        help="Launch a single connection.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_launch.add_argument("connection_id", help="Connection ID to launch.")
    p_launch.add_argument("--config", metavar="PATH", help="Path to .cpsm.yaml")
    p_launch.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help='Output {"success": bool, "connection_id": str, ...} as JSON.',
    )
    p_launch.add_argument(
        "--isolation",
        choices=["shared", "per-group"],
        help="Session isolation mode.",
    )
    p_launch.add_argument(
        "--group",
        metavar="GROUP_ID",
        help="Group ID (required when --isolation per-group is used).",
    )

    # ---- launch-group ------------------------------------------------------
    p_lg = sub.add_parser(
        "launch-group",
        help="Launch all members of a group.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_lg.add_argument("group_id", help="Group ID to launch.")
    p_lg.add_argument("--config", metavar="PATH", help="Path to .cpsm.yaml")
    p_lg.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output per-member status list as JSON.",
    )

    # ---- launch-scene ------------------------------------------------------
    p_ls = sub.add_parser(
        "launch-scene",
        help="Launch all groups in a scene.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_ls.add_argument("scene_id", help="Scene ID to launch.")
    p_ls.add_argument("--config", metavar="PATH", help="Path to .cpsm.yaml")
    p_ls.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output per-group status as JSON.",
    )

    # ---- gui ---------------------------------------------------------------
    p_gui = sub.add_parser(
        "gui",
        help="Launch the CPSM graphical interface.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_gui.add_argument("--config", metavar="PATH", help="Path to .cpsm.yaml")

    # ---- import ------------------------------------------------------------
    p_imp = sub.add_parser(
        "import",
        help="Convert a legacy .claude-projects.yaml to .cpsm.yaml.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_imp.add_argument("source", metavar="legacy.yaml", help="Source legacy YAML file.")
    p_imp.add_argument(
        "-o",
        "--output",
        metavar="out.yaml",
        required=True,
        help="Destination .cpsm.yaml path.",
    )
    p_imp.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the target file if it already exists.",
    )

    # ---- install-desktop ---------------------------------------------------
    p_desk = sub.add_parser(
        "install-desktop",
        help="Install a Linux .desktop launcher for the GUI.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_desk.add_argument(
        "--system",
        action="store_true",
        help="Install system-wide to /usr/local/share/applications (requires root). "
        "Default is per-user under ~/.local/share/applications.",
    )
    p_desk.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .desktop file at the target path.",
    )
    p_desk.add_argument(
        "--executable",
        metavar="PATH",
        help="Override the executable path written into the .desktop Exec= line. "
        "Defaults to the path of the cpsm console script (sys.argv[0] resolved).",
    )
    p_desk.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON.",
    )

    return parser


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    """Handle: cpsm validate [--config PATH] [--json]."""
    from pydantic import ValidationError as PydanticValidationError

    svc = _make_services()
    config_path = Path(args.config) if args.config else None

    try:
        doc = svc.config.load(config_path)
    except FileNotFoundError as exc:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "issues": [{"path": "<config>", "message": str(exc), "severity": "error"}],
                    }
                )
            )
        else:
            print(f"ERROR: Config file not found: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_FAILED
    except PydanticValidationError as exc:
        # Schema parse failure — surface each pydantic error as an issue
        issues_data = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) if err["loc"] else "<root>"
            issues_data.append({"path": loc, "message": err["msg"], "severity": "error"})
        if args.json_output:
            print(json.dumps({"valid": False, "issues": issues_data}))
        else:
            print(f"ERROR: Config schema parse failed ({len(issues_data)} issue(s)):")
            for item in issues_data:
                print(f"  [ERROR] {item['path']}: {item['message']}")
        return EXIT_VALIDATION_FAILED

    issues = svc.config.validate(doc)

    if args.json_output:
        payload = {
            "valid": len(issues) == 0,
            "issues": [
                {"path": i.location, "message": i.message, "severity": i.severity} for i in issues
            ],
        }
        print(json.dumps(payload))
        return EXIT_OK if not issues else EXIT_VALIDATION_FAILED

    if not issues:
        print("Config is valid.")
        return EXIT_OK

    print(f"Config has {len(issues)} issue(s):")
    for issue in issues:
        print(f"  [{issue.severity.upper()}] {issue.location}: {issue.message}")
    return EXIT_VALIDATION_FAILED


def cmd_launch(args: argparse.Namespace) -> int:
    """Handle: cpsm launch <connection_id> [--config PATH] [--json] [--isolation ...]."""
    svc = _make_services()
    config_path = Path(args.config) if args.config else None

    try:
        doc = svc.config.load(config_path)
    except FileNotFoundError as exc:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "success": False,
                        "connection_id": args.connection_id,
                        "session_name": "",
                        "errors": [str(exc)],
                        "warnings": [],
                    }
                )
            )
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_GENERIC

    # Validate connection exists before launching
    conn = svc.config.find_connection(doc, args.connection_id)
    if conn is None:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "success": False,
                        "connection_id": args.connection_id,
                        "session_name": "",
                        "errors": [f"Connection '{args.connection_id}' not found."],
                        "warnings": [],
                    }
                )
            )
        else:
            print(f"ERROR: Connection '{args.connection_id}' not found.", file=sys.stderr)
        return EXIT_NOT_FOUND

    group_id: str | None = getattr(args, "group", None)
    isolation: str | None = getattr(args, "isolation", None)
    # Only pass group_id to session service if isolation mode requests it
    effective_group_id: str | None = group_id if isolation == "per-group" else None

    try:
        result = svc.session.launch(doc, args.connection_id, group_id=effective_group_id)
    except Exception as exc:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "success": False,
                        "connection_id": args.connection_id,
                        "session_name": "",
                        "errors": [str(exc)],
                        "warnings": [],
                    }
                )
            )
        else:
            print(f"ERROR: Launch failed: {exc}", file=sys.stderr)
        return EXIT_LAUNCH_FAILED

    if args.json_output:
        payload = {
            "success": result.success,
            "connection_id": result.connection_id,
            "session_name": result.session_name,
            "errors": result.errors,
            "warnings": result.warnings,
        }
        print(json.dumps(payload))
    else:
        if result.success:
            print(f"Launched '{result.connection_id}' in session '{result.session_name}'.")
        else:
            for err in result.errors:
                print(f"ERROR: {err}", file=sys.stderr)

    return EXIT_OK if result.success else EXIT_LAUNCH_FAILED


def cmd_launch_group(args: argparse.Namespace) -> int:
    """Handle: cpsm launch-group <group_id> [--config PATH] [--json]."""
    svc = _make_services()
    config_path = Path(args.config) if args.config else None

    try:
        doc = svc.config.load(config_path)
    except FileNotFoundError as exc:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "success": False,
                        "group_id": args.group_id,
                        "session_name": "",
                        "member_statuses": [],
                        "errors": [str(exc)],
                        "warnings": [],
                    }
                )
            )
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_GENERIC

    # Validate group exists
    grp = svc.config.find_group(doc, args.group_id)
    if grp is None:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "success": False,
                        "group_id": args.group_id,
                        "session_name": "",
                        "member_statuses": [],
                        "errors": [f"Group '{args.group_id}' not found."],
                        "warnings": [],
                    }
                )
            )
        else:
            print(f"ERROR: Group '{args.group_id}' not found.", file=sys.stderr)
        return EXIT_NOT_FOUND

    try:
        result = svc.session.launch_group(doc, args.group_id)
    except Exception as exc:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "success": False,
                        "group_id": args.group_id,
                        "session_name": "",
                        "member_statuses": [],
                        "errors": [str(exc)],
                        "warnings": [],
                    }
                )
            )
        else:
            print(f"ERROR: Launch group failed: {exc}", file=sys.stderr)
        return EXIT_LAUNCH_FAILED

    if args.json_output:
        member_statuses = [
            {
                "connection_id": mr.connection_id,
                "session_name": mr.session_name,
                "success": mr.success,
                "errors": mr.errors,
                "warnings": mr.warnings,
            }
            for mr in result.member_results
        ]
        payload = {
            "success": result.success,
            "group_id": result.group_id,
            "session_name": result.session_name,
            "member_statuses": member_statuses,
            "errors": result.errors,
            "warnings": result.warnings,
        }
        print(json.dumps(payload))
    else:
        for mr in result.member_results:
            status = "OK" if mr.success else "FAIL"
            print(f"  [{status}] {mr.connection_id}")
        if result.warnings:
            for w in result.warnings:
                print(f"  [WARN] {w}")

    # Partial failure: if any member failed we still return 0 with warnings
    # (documented: partial success yields EXIT_OK; individual statuses in output)
    return EXIT_OK


def cmd_launch_scene(args: argparse.Namespace) -> int:
    """Handle: cpsm launch-scene <scene_id> [--config PATH] [--json]."""
    svc = _make_services()
    config_path = Path(args.config) if args.config else None

    try:
        doc = svc.config.load(config_path)
    except FileNotFoundError as exc:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "success": False,
                        "scene_id": args.scene_id,
                        "group_statuses": [],
                        "errors": [str(exc)],
                        "warnings": [],
                    }
                )
            )
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_GENERIC

    # Validate scene exists
    scene = svc.config.find_scene(doc, args.scene_id)
    if scene is None:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "success": False,
                        "scene_id": args.scene_id,
                        "group_statuses": [],
                        "errors": [f"Scene '{args.scene_id}' not found."],
                        "warnings": [],
                    }
                )
            )
        else:
            print(f"ERROR: Scene '{args.scene_id}' not found.", file=sys.stderr)
        return EXIT_NOT_FOUND

    try:
        result = svc.session.launch_scene(doc, args.scene_id)
    except Exception as exc:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "success": False,
                        "scene_id": args.scene_id,
                        "group_statuses": [],
                        "errors": [str(exc)],
                        "warnings": [],
                    }
                )
            )
        else:
            print(f"ERROR: Launch scene failed: {exc}", file=sys.stderr)
        return EXIT_LAUNCH_FAILED

    if args.json_output:
        group_statuses = [
            {
                "group_id": gr.group_id,
                "session_name": gr.session_name,
                "success": gr.success,
                "errors": gr.errors,
                "warnings": gr.warnings,
            }
            for gr in result.group_results
        ]
        payload = {
            "success": result.success,
            "scene_id": result.scene_id,
            "group_statuses": group_statuses,
            "errors": result.errors,
            "warnings": result.warnings,
        }
        print(json.dumps(payload))
    else:
        for gr in result.group_results:
            status = "OK" if gr.success else "FAIL"
            print(f"  [{status}] group '{gr.group_id}'")
        if result.warnings:
            for w in result.warnings:
                print(f"  [WARN] {w}")

    return EXIT_OK if result.success else EXIT_LAUNCH_FAILED


def cmd_gui(args: argparse.Namespace, _test_mode: bool = False) -> int:
    """Handle: cpsm gui [--config PATH].

    In test mode the QApplication is not started so tests can assert dispatch
    is wired without triggering an event loop.
    """
    if _test_mode:
        return EXIT_OK

    from cpsm.app import run_gui

    config_path = Path(args.config).expanduser() if getattr(args, "config", None) else None
    return run_gui(config_path=config_path)


def cmd_install_desktop(args: argparse.Namespace) -> int:
    """Handle: cpsm install-desktop.

    Generates a freedesktop.org-compliant `.desktop` launcher entry under
    ``~/.local/share/applications/`` so CPSM appears in GNOME / KDE app menus.
    Idempotent — safe to re-run.
    """
    from cpsm.platform.desktop_entry import install_desktop_entry

    try:
        path = install_desktop_entry(
            user_only=not args.system,
            force=args.force,
            executable=args.executable,
        )
    except FileExistsError as exc:
        print(f"ERROR: {exc}\nUse --force to overwrite.", file=sys.stderr)
        return EXIT_TARGET_EXISTS
    except Exception as exc:
        print(f"ERROR: install-desktop failed: {exc}", file=sys.stderr)
        return EXIT_GENERIC

    if getattr(args, "json", False):
        print(json.dumps({"installed": True, "path": str(path)}))
    else:
        print(f"Installed CPSM launcher: {path}")
        print("It should appear in your application menu within ~30 seconds.")
        print("If it doesn't, run: update-desktop-database ~/.local/share/applications")
    return EXIT_OK


def cmd_import(args: argparse.Namespace) -> int:
    """Handle: cpsm import <legacy.yaml> -o <out.yaml> [--force]."""
    from cpsm.services.import_service import ImportService

    source = Path(args.source)
    target = Path(args.output)

    # Check source exists
    if not source.expanduser().resolve().exists():
        print(f"ERROR: Source file not found: {source}", file=sys.stderr)
        return EXIT_SOURCE_NOT_FOUND

    # Check target exists (without --force)
    if target.expanduser().resolve().exists() and not args.force:
        print(
            f"ERROR: Target file already exists: {target}\nUse --force to overwrite.",
            file=sys.stderr,
        )
        return EXIT_TARGET_EXISTS

    try:
        preview = ImportService.import_legacy_to(source, target)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_SOURCE_NOT_FOUND
    except Exception as exc:
        print(f"ERROR: Import failed: {exc}", file=sys.stderr)
        return EXIT_GENERIC

    transforms_out = [
        {"kind": t.kind, "target_path": t.target_path, "detail": t.detail}
        for t in preview.transforms
    ]

    payload = {
        "source": str(preview.source_path),
        "target": str(target.expanduser().resolve()),
        "transforms": transforms_out,
        "wrote": True,
    }
    print(json.dumps(payload))
    return EXIT_OK


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_HANDLERS = {
    "validate": cmd_validate,
    "launch": cmd_launch,
    "launch-group": cmd_launch_group,
    "launch-scene": cmd_launch_scene,
    "gui": cmd_gui,
    "import": cmd_import,
    "install-desktop": cmd_install_desktop,
}


def dispatch(argv: Sequence[str] | None = None) -> int:
    """Parse *argv* and dispatch to the appropriate subcommand handler.

    Returns an integer exit code.

    Exit codes:
        0   Success
        1   Generic failure
        2   Bad CLI arguments (argparse default; also returned for unknown subcommand)
        3   Config validation failed
        4   Connection / group / scene not found
        5   Launch failed
        6   Import target file already exists (no --force)
        7   Source legacy file not found
    """
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.subcommand is None:
        # No subcommand given — show help (treat as successful usage display)
        parser.print_help()
        return EXIT_BAD_ARGS

    handler = _HANDLERS.get(args.subcommand)
    if handler is None:
        parser.print_help()
        return EXIT_BAD_ARGS

    return handler(args)
