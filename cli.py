"""
``hermes workflow ...`` CLI subcommands — registered by the plugin via
``ctx.register_cli_command()``.

Subcommands:

    run          Run a pipeline from a YAML definition (URL, file, or template)
    list         List pipeline instances (active, done, all)
    show         Show a pipeline instance with its step and card status
    cancel       Cancel a running pipeline and archive its cards
    gc           Clean up old pipeline instances
    templates    List available built-in workflow templates
    template     Show a template's content
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Optional

from .workflow import loader
from .workflow.compiler import compile_workflow
from .workflow.runner import run_pipeline
from .workflow.state import (
    get_pipeline,
    list_pipelines,
    set_pipeline_status,
    delete_old_pipelines,
)
from .workflow.schema import PipelineStatus

# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def register_cli(parser: argparse.ArgumentParser) -> None:
    """Wire up `hermes workflow ...` subcommands."""
    subs = parser.add_subparsers(dest="workflow_cmd", required=False)

    # run
    p_run = subs.add_parser(
        "run",
        help="Run a pipeline from a YAML definition (URL, file, or template)",
        description=textwrap.dedent("""\
            Run a workflow pipeline. Sources:
              --url <URL>     HTTP(S) URL to a workflow YAML
              --file <path>   Local file path
              --template <name>  Built-in template name

            Pass variables with --var key=val (repeatable).
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_run.add_argument("--url", default=None, help="URL to workflow YAML")
    p_run.add_argument("--file", default=None, help="Local workflow YAML file")
    p_run.add_argument("--template", default=None, help="Built-in template name")
    p_run.add_argument("--var", action="append", default=[], help="Variable: key=val")
    p_run.add_argument("--board", default=None, help="Kanban board slug")
    p_run.add_argument("--poll-interval", type=int, default=15, help="Poll interval (seconds)")

    # list
    subs.add_parser(
        "list",
        help="List pipeline instances",
        description="List all pipeline instances, newest first.",
    )

    # show
    p_show = subs.add_parser(
        "show",
        help="Show a pipeline instance with step and card status",
    )
    p_show.add_argument("pipeline_id", help="Pipeline ID (pipe_...)")

    # cancel
    p_cancel = subs.add_parser(
        "cancel",
        help="Cancel a running pipeline and archive its cards",
    )
    p_cancel.add_argument("pipeline_id", help="Pipeline ID to cancel")

    # gc
    p_gc = subs.add_parser(
        "gc",
        help="Clean up old pipeline instances",
    )
    p_gc.add_argument("--older-than", type=int, default=7, help="Delete instances older than N days")

    # templates
    subs.add_parser(
        "templates",
        help="List available built-in workflow templates",
    )

    # template show
    p_tmpl = subs.add_parser(
        "template",
        help="Show a template's YAML content",
    )
    p_tmpl.add_argument("action", choices=["show"], help="Sub-action")
    p_tmpl.add_argument("name", help="Template name")

    # version
    parser.set_defaults(func=dispatch)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def dispatch(args: argparse.Namespace) -> int:
    cmd = getattr(args, "workflow_cmd", None)
    if cmd is None:
        print("Usage: hermes workflow <command> [options]")
        print()
        print("Commands:")
        print("  run            Run a pipeline from a YAML definition")
        print("  list           List pipeline instances")
        print("  show           Show a pipeline instance")
        print("  cancel         Cancel a running pipeline")
        print("  gc             Clean up old pipeline instances")
        print("  templates      List available built-in templates")
        print("  template show  Show a template's YAML content")
        print()
        print("Run 'hermes workflow <command> --help' for more details.")
        return 0

    try:
        if cmd == "run":
            return _cmd_run(args)
        elif cmd == "list":
            return _cmd_list(args)
        elif cmd == "show":
            return _cmd_show(args)
        elif cmd == "cancel":
            return _cmd_cancel(args)
        elif cmd == "gc":
            return _cmd_gc(args)
        elif cmd == "templates":
            return _cmd_templates()
        elif cmd == "template":
            return _cmd_template_show(args)
        else:
            print(f"Unknown workflow command: {cmd}", file=sys.stderr)
            return 2
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_run(args: argparse.Namespace) -> int:
    # Resolve source
    source = args.url or args.file or args.template
    if not source:
        print("Error: specify one of --url, --file, or --template", file=sys.stderr)
        return 1

    # Parse --var key=val
    user_vars = {}
    for v in args.var:
        if "=" not in v:
            print(f"Error: --var must be key=val, got: {v}", file=sys.stderr)
            return 1
        key, _, val = v.partition("=")
        user_vars[key.strip()] = val.strip()

    # Load workflow
    try:
        if args.url:
            wf = loader.load_from_url(args.url)
        elif args.file:
            wf = loader.load_from_file(args.file)
        else:
            wf = loader.load_from_template(args.template)
    except loader.LoadError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Compile
    print(f"Workflow: {wf.meta.name} v{wf.meta.version}")
    print(f"Steps: {len(wf.steps)}")
    if wf.meta.description:
        print(f"Description: {wf.meta.description}")
    print()

    try:
        pipeline_id, cards = compile_workflow(
            wf=wf,
            user_vars=user_vars,
            board=args.board,
        )
    except Exception as e:
        print(f"Compilation failed: {e}", file=sys.stderr)
        return 1

    print(f"\nPipeline: {pipeline_id}")
    print(f"Cards created: {len(cards)}\n")

    # Run
    rc = run_pipeline(
        wf=wf,
        pipeline_id=pipeline_id,
        board=args.board,
        poll_interval=args.poll_interval,
    )

    return rc


def _cmd_list(args: argparse.Namespace) -> int:
    pipelines = list_pipelines()
    if not pipelines:
        print("No pipelines.")
        return 0

    print(f"{'ID':<24} {'TEMPLATE':<24} {'STEP':<20} {'STATUS':<12} {'AGE':<10}")
    print("-" * 90)
    for p in pipelines:
        age = _fmt_age(p.created_at)
        step = p.current_step_id or "-"
        print(f"{p.id:<24} {p.template_name:<24} {step:<20} {p.status.value:<12} {age:<10}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    p = get_pipeline(args.pipeline_id)
    if p is None:
        print(f"Pipeline not found: {args.pipeline_id}", file=sys.stderr)
        return 1

    print(f"Pipeline:    {p.id}")
    print(f"Template:    {p.template_name} v{p.template_version}")
    print(f"Status:      {p.status.value}")
    print(f"Step:        {p.current_step_id or '-'}")
    print(f"Cycle:       {p.current_cycle}")
    print(f"Created:     {_fmt_age(p.created_at)} ago")
    print(f"Updated:     {_fmt_age(p.updated_at)} ago")
    if p.error:
        print(f"Error:       {p.error}")
    return 0


def _cmd_cancel(args: argparse.Namespace) -> int:
    p = get_pipeline(args.pipeline_id)
    if p is None:
        print(f"Pipeline not found: {args.pipeline_id}", file=sys.stderr)
        return 1
    if p.status != PipelineStatus.RUNNING:
        print(f"Pipeline is already {p.status.value}", file=sys.stderr)
        return 1

    set_pipeline_status(p.id, PipelineStatus.CANCELLED, "Cancelled by user")
    print(f"Cancelled: {args.pipeline_id}")
    return 0


def _cmd_gc(args: argparse.Namespace) -> int:
    deleted = delete_old_pipelines(older_than_days=args.older_than)
    print(f"Deleted {deleted} pipeline(s) older than {args.older_than} days.")
    return 0


def _cmd_templates() -> int:
    templates = loader.list_templates()
    if not templates:
        print("No built-in templates found.")
        return 0

    print(f"{'NAME':<24} {'VERSION':<10} DESCRIPTION")
    print("-" * 70)
    for t in templates:
        desc = (t["description"][:60] + "...") if len(t.get("description", "")) > 60 else t.get("description", "")
        print(f"{t['name']:<24} {t['version']:<10} {desc}")
    print()
    print("Use: hermes workflow run --template <name>")
    return 0


def _cmd_template_show(args: argparse.Namespace) -> int:
    try:
        wf = loader.load_from_template(args.name)
    except loader.LoadError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"---\nname: {wf.meta.name}")
    print(f"version: {wf.meta.version}")
    if wf.meta.description:
        print(f"description: {wf.meta.description}")
    print("---")
    print()
    for step in wf.steps:
        print(f"  - id: {step.id}")
        print(f"    type: {step.type.value}")
        if step.depends_on:
            print(f"    depends_on: {step.depends_on}")
        print()
    return 0


def _fmt_age(ts: int) -> str:
    seconds = int(time.time()) - ts
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    elif seconds < 86400:
        return f"{seconds // 3600}h"
    else:
        return f"{seconds // 86400}d"


import time  # noqa: E402 — used by _fmt_age
