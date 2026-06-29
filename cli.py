"""
``hermes workflow ...`` CLI subcommands — registered by the plugin via
``ctx.register_cli_command()``.

Subcommands:

    run          Run a pipeline from a YAML definition (URL, file, or template)
    list         List pipeline instances (active, done, all)
    show         Show a pipeline instance with its step and card status
    cancel       Cancel a running pipeline and archive its cards
    gc           Clean up old pipeline instances
    check        Validate a workflow YAML for logical errors (graph analysis)
    templates    List available built-in workflow templates
    template     Show a template's content
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time
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
    get_step_logs,
    get_step_log,
    get_worker_log,
    get_log_by_id,
)
from .workflow.schema import PipelineStatus
from .workflow.graph import WorkflowGraph
from .workflow.validate import validate as run_validation

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
            Use --dry-run to validate without creating any cards or running.
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_run.add_argument("--url", default=None, help="URL to workflow YAML")
    p_run.add_argument("--file", default=None, help="Local workflow YAML file")
    p_run.add_argument("--template", default=None, help="Built-in template name")
    p_run.add_argument("--var", action="append", default=[], help="Variable: key=val")
    p_run.add_argument("--board", default=None, help="Kanban board slug")
    p_run.add_argument("--poll-interval", type=int, default=15, help="Poll interval (seconds)")
    p_run.add_argument("--dry-run", action="store_true", help="Validate only — no cards created, no execution")
    p_run.add_argument("--verbose", "-v", action="store_true", help="Detailed per-step debug output")

    # check
    p_check = subs.add_parser(
        "check",
        help="Validate a workflow YAML for logical errors (graph analysis)",
        description=textwrap.dedent("""\
            Run comprehensive validation on a workflow YAML:

              Graph analysis:
                - Circular dependency detection
                - Control-flow cycle detection
                - Orphan/unreachable step detection
                - Dead loop detection (no exit path)

              Schema checks:
                - Duplicate step ids
                - Missing depends_on / goto targets
                - Unused variables
                - Unbounded loops

              Semantic checks:
                - Kanban steps without assignee/title
                - Noop-only workflows

            Exits with code 0 only when no errors are found.
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_check.add_argument("--url", default=None, help="URL to workflow YAML")
    p_check.add_argument("--file", default=None, help="Local workflow YAML file")
    p_check.add_argument("--template", default=None, help="Built-in template name")

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

    # logs — list step execution logs for a pipeline
    p_logs = subs.add_parser(
        "logs",
        help="Show step execution logs for a completed pipeline",
        description=textwrap.dedent("""\
            Show all step execution logs for a pipeline. Each row shows
            one step's status, duration, exit code, and card count.

            Use `hermes workflow log <pipeline_id> <step_id>` for
            detailed info including worker agent logs.
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_logs.add_argument("pipeline_id", help="Pipeline ID (pipe_...)")

    # log — detailed step log with worker agent output
    p_log = subs.add_parser(
        "log",
        help="Show detailed execution log for one step (with profile agent output)",
        description=textwrap.dedent("""\
            Show the detailed execution log for a specific step within
            a pipeline. Includes script stdout/stderr, kanban card details,
            and profile agent worker logs (via `hermes kanban runs`).

            Use --card to target a specific kanban card's worker log.
            Use --tail to show the last N bytes of the worker log.
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_log.add_argument("pipeline_id", help="Pipeline ID (pipe_...)")
    p_log.add_argument("step_id", help="Step id within the pipeline")
    p_log.add_argument("--cycle", type=int, default=0,
                       help="Loop cycle (default: most recent)")
    p_log.add_argument("--card", default=None,
                       help="Show worker log for a specific kanban card")
    p_log.add_argument("--tail", type=int, default=0,
                       help="Show last N chars of worker log (0 = all)")

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
        print("  check          Validate a workflow YAML (graph analysis)")
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
        elif cmd == "check":
            return _cmd_check(args)
        elif cmd == "list":
            return _cmd_list(args)
        elif cmd == "show":
            return _cmd_show(args)
        elif cmd == "cancel":
            return _cmd_cancel(args)
        elif cmd == "gc":
            return _cmd_gc(args)
        elif cmd == "logs":
            return _cmd_logs(args)
        elif cmd == "log":
            return _cmd_log(args)
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
# Shared: resolve source + load workflow
# ---------------------------------------------------------------------------

def _load_workflow(args) -> tuple:
    """Load a WorkflowDef from args.url / args.file / args.template.

    Returns (wf, source_label) or exits on error.
    """
    source = args.url or args.file or args.template
    if not source:
        print("Error: specify one of --url, --file, or --template", file=sys.stderr)
        return None, None

    try:
        if args.url:
            wf = loader.load_from_url(args.url)
        elif args.file:
            wf = loader.load_from_file(args.file)
        else:
            wf = loader.load_from_template(args.template)
    except loader.LoadError as e:
        print(f"Error: {e}", file=sys.stderr)
        return None, None

    return wf, source


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_run(args: argparse.Namespace) -> int:
    wf, source = _load_workflow(args)
    if wf is None:
        return 1

    user_vars = _parse_vars(args.var)

    # Print workflow info
    print(f"Workflow: {wf.meta.name} v{wf.meta.version}")
    print(f"Steps: {len(wf.steps)}")
    if wf.meta.description:
        print(f"Description: {wf.meta.description}")

    # Run validation first
    print()
    print("── Validation ──")
    validation = run_validation(wf)
    print(validation)
    print()

    if validation.has_errors():
        print("❌ Validation failed — aborting. Use `hermes workflow check` for details.")
        return 1

    # ── Dry-run: show graph + plan, but don't execute ──
    if args.dry_run:
        print("── Dry Run (no cards created, no execution) ──")
        graph = WorkflowGraph(wf)
        gs = graph.summary()
        print(f"  Nodes: {gs['node_count']}")
        print(f"  Dependency edges: {gs['dependency_edges']}")
        print(f"  Control-flow edges: {gs['control_flow_edges']}")
        print(f"  Sink nodes (terminals): {gs['sink_nodes']}")
        print()
        print("── Planned Steps ──")
        for step in wf.steps:
            _print_planned_step(step, indent=2)
        print()
        print("✅ Dry-run complete — no errors.")
        return 0

    # ── Full run ──
    print()
    try:
        pipeline_id, cards = compile_workflow(
            wf=wf,
            user_vars=user_vars,
            board=args.board,
            verbose=args.verbose,
        )
    except Exception as e:
        print(f"Compilation failed: {e}", file=sys.stderr)
        return 1

    print(f"\nPipeline: {pipeline_id}")
    print(f"Cards created: {len(cards)}\n")

    rc = run_pipeline(
        wf=wf,
        pipeline_id=pipeline_id,
        board=args.board,
        poll_interval=args.poll_interval,
        verbose=args.verbose,
    )

    return rc


def _cmd_check(args: argparse.Namespace) -> int:
    """Validate a workflow YAML — comprehensive logical analysis."""
    wf, source = _load_workflow(args)
    if wf is None:
        return 1

    print(f"Workflow: {wf.meta.name} v{wf.meta.version}")
    print(f"Steps: {len(wf.steps)}")
    if wf.meta.description:
        print(f"Description: {wf.meta.description}")
    print()

    # ── 1. Validation rules ──
    val = run_validation(wf)
    print(val)
    print()

    # ── 2. Graph analysis ──
    graph = WorkflowGraph(wf)
    gs = graph.summary()

    print(f"── Graph Analysis ──")
    print(f"  Nodes: {gs['node_count']}")
    print(f"  Control-flow edges: {gs['control_flow_edges']}")
    print(f"  Dependency edges: {gs['dependency_edges']}")
    print()

    # Dependency cycles
    if gs["dep_cycles"]:
        print(f"  ❌ Dependency cycles ({len(gs['dep_cycles'])}):")
        for c in gs["dep_cycles"]:
            print(f"     {' → '.join(c)}")
        print()
    else:
        print(f"  ✅ No dependency cycles")
        print()

    # Control-flow cycles
    if gs["cf_cycles"]:
        print(f"  ⚠️  Control-flow cycles ({len(gs['cf_cycles'])}):")
        for c in gs["cf_cycles"]:
            print(f"     {' → '.join(c)}")
        print()
    else:
        print(f"  ✅ No control-flow cycles")
        print()

    # Orphans
    if gs["orphan_nodes"]:
        print(f"  ❌ Unreachable steps ({len(gs['orphan_nodes'])}):")
        for o in gs["orphan_nodes"]:
            print(f"     '{o}' — no path from the entry step")
        print()
    else:
        print(f"  ✅ All steps reachable from entry point")
        print()

    # Dead loops
    if gs["dead_loops"]:
        print(f"  ❌ Dead loops (no exit path, {len(gs['dead_loops'])}):")
        for l in gs["dead_loops"]:
            print(f"     '{l}'")
        print()
    else:
        print(f"  ✅ No dead loops")
        print()

    # Sinks
    print(f"  ℹ️  Terminal steps: {gs['sink_nodes']}")
    print()

    # ── 3. Step-by-step walkthrough ──
    print("── Step Walkthrough ──")
    for i, step in enumerate(wf.steps):
        _print_step_walkthrough(step, i, wf)
    print()

    # ── 4. Summary verdict ──
    if gs["has_issues"] or val.has_errors():
        print("❌ FAIL — Issues found that may prevent execution.")
        return 1
    elif val.has_warnings():
        print("⚠️  PASS with warnings — review recommendations above.")
        return 0
    else:
        print("✅ PASS — Workflow looks good.")
        return 0


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


def _cmd_logs(args: argparse.Namespace) -> int:
    """Show all step execution logs for a pipeline."""
    logs = get_step_logs(args.pipeline_id)
    if not logs:
        print(f"No step logs found for pipeline: {args.pipeline_id}")
        print("  (pipeline may not have been run yet)")
        return 1

    print(f"Step logs for: {args.pipeline_id}")
    print()
    print(f"{'STEP':<20} {'TYPE':<10} {'STATUS':<12} {'DURATION':<12} {'EXIT':<6} {'CARDS':<8}")
    print("-" * 68)
    for log in logs:
        duration = ""
        if log["started_at"] and log["ended_at"]:
            d = log["ended_at"] - log["started_at"]
            duration = f"{d}s" if d < 60 else f"{d // 60}m{d % 60}s"
        exit_code = str(log["exit_code"]) if log["exit_code"] is not None else "-"
        card_count = len(log["card_ids"])
        worker_count = len(log["worker_logs"])
        cards_str = f"{card_count} (+{worker_count}w)" if worker_count else str(card_count)

        step_label = log["step_id"]
        if log.get("cycle", 1) > 1:
            step_label += f" (c{log['cycle']})"

        print(f"{step_label:<20} {log['step_type']:<10} {log['status']:<12} {duration:<12} {exit_code:<6} {cards_str:<8}")

    print()
    print(f"Total: {len(logs)} step log(s)")
    print(f"Use `hermes workflow log <pipeline_id> <step_id>` for detail.")
    return 0


def _cmd_log(args: argparse.Namespace) -> int:
    """Show detailed execution log for one step."""
    cycle = args.cycle or 0  # 0 = most recent

    if args.card:
        # Show worker log for a specific card
        worker = get_worker_log(args.pipeline_id, args.step_id, args.card, cycle)
        if worker is None:
            print(f"No worker log for card '{args.card}' in step '{args.step_id}'")
            return 1

        print(f"Worker log for card: {args.card}")
        print(f"  Profile:       {worker.get('profile', '?')}")
        print(f"  Status:        {worker.get('status', '?')}")
        print(f"  Outcome:       {worker.get('outcome', '?')}")
        print(f"  Elapsed:       {worker.get('elapsed_seconds', '?')}s")
        if worker.get("summary"):
            print(f"  Summary:       {worker['summary']}")
        print()

        log_text = worker.get("log_preview") or "(no worker log available)"
        if args.tail > 0:
            log_text = log_text[-args.tail:]
        elif len(log_text) > 2000:
            log_text = log_text[:1000] + "\n  ... (truncated) ...\n" + log_text[-1000:]

        print("── Worker Log ──")
        print(log_text)
        if worker.get("log_length", 0) > 5000:
            hidden = worker["log_length"] - 5000
            print(f"\n  ... ({hidden} more bytes — use --tail to see more) ...")
        if worker.get("error"):
            print(f"\n  ⚠️  Fetch error: {worker['error']}")
        return 0

    # Show full step detail
    log = get_step_log(args.pipeline_id, args.step_id, cycle)
    if log is None:
        print(f"No log found for step '{args.step_id}' in pipeline '{args.pipeline_id}'")
        return 1

    print(f"Step:          {log['step_id']}")
    print(f"Type:          {log['step_type']}")
    print(f"Status:        {log['status']}")
    print(f"Cycle:         {log['cycle']}")
    duration = ""
    if log["started_at"] and log["ended_at"]:
        d = log["ended_at"] - log["started_at"]
        duration = f"{d}s" if d < 60 else f"{d // 60}m{d % 60}s"
        print(f"Duration:      {duration}")
    print(f"Exit code:     {log['exit_code'] or '-'}")
    print(f"Cards:         {len(log['card_ids'])}")
    print(f"Worker logs:   {len(log['worker_logs'])}")

    if log.get("error_message"):
        print(f"Error:         {log['error_message']}")

    if log.get("details"):
        print(f"Details:       {json.dumps(log['details'])}")

    # Print script stdout/stderr
    if log["stdout"]:
        print()
        print("── Script stdout ──")
        print(log["stdout"][:2000])
        if len(log["stdout"]) > 2000:
            print("  ... (truncated) ...")
    if log["stderr"]:
        print()
        print("── Script stderr ──")
        print(log["stderr"][:2000])
        if len(log["stderr"]) > 2000:
            print("  ... (truncated) ...")

    # Print worker logs summary
    if log["worker_logs"]:
        print()
        print("── Worker Runs ──")
        for w in log["worker_logs"]:
            profile = w.get("profile", "?")
            outcome = w.get("outcome", "?")
            elapsed = w.get("elapsed_seconds", "?")
            summary = w.get("summary", "")
            print(f"  {w.get('card_id', '?'):<20} profile={profile:<12} outcome={outcome:<12} "
                  f"elapsed={elapsed}s")
            if summary:
                print(f"    summary: {summary[:200]}")
        print()
        print("  Use `hermes workflow log <pipeline_id> <step_id> --card <id>` for full log.")

    print()
    print("  Card IDs:", ", ".join(log["card_ids"]) if log["card_ids"] else "(none)")

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
        print(f"    type: step.type.value")
        if step.depends_on:
            print(f"    depends_on: {step.depends_on}")
        print()
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_vars(var_list: list) -> dict:
    """Parse --var key=val arguments into a dict."""
    result = {}
    for v in var_list:
        if "=" not in v:
            print(f"Warning: ignoring --var without '=': {v}", file=sys.stderr)
            continue
        key, _, val = v.partition("=")
        result[key.strip()] = val.strip()
    return result


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


def _print_planned_step(step, indent: int = 2) -> None:
    """Print a planned step in the dry-run plan view."""
    prefix = " " * indent
    from .workflow.schema import ScriptStep, KanbanStep, LoopStep, NoopStep, StepType

    if isinstance(step, ScriptStep):
        print(f"{prefix}📜 {step.id}: script — {step.script[:80]}...")
    elif isinstance(step, KanbanStep):
        title = step.template.title
        assignee = step.template.assignee
        skill = step.template.skill or "(default)"
        for_each = f" [for_each: {step.for_each}]" if step.for_each else ""
        print(f"{prefix}📋 {step.id}: kanban → {assignee} ({skill}){for_each}")
        print(f"{prefix}   title: {title}")
    elif isinstance(step, LoopStep):
        cond = f" while: {step.while_condition}" if step.while_condition else ""
        print(f"{prefix}🔄 {step.id}: loop (max={step.max_iterations}{cond})")
        for sub in step.steps:
            _print_planned_step(sub, indent + 4)
    elif isinstance(step, NoopStep):
        summary = f" — {step.summary}" if step.summary else ""
        print(f"{prefix}⏹️  {step.id}: noop{summary}")


def _print_step_walkthrough(step, index: int, wf) -> None:
    """Print step info in the check command walkthrough."""
    from .workflow.schema import ScriptStep, KanbanStep, LoopStep, NoopStep

    deps = f", depends_on: {step.depends_on}" if step.depends_on else ""
    if isinstance(step, ScriptStep):
        nxt = _next_step_text(step.id, wf)
        gotos = ""
        if step.on_exit:
            branches = "; ".join(f"exit {k} → {b.goto}" for k, b in step.on_exit.items())
            gotos = f" [{branches}]"
        print(f"  [{index}] 📜 {step.id}: script{deps}{gotos} → {nxt}")
    elif isinstance(step, KanbanStep):
        print(f"  [{index}] 📋 {step.id}: kanban → {step.template.assignee}{deps}")
    elif isinstance(step, LoopStep):
        sub_ids = [s.id for s in step.steps]
        cond = f" while: \"{step.while_condition}\"" if step.while_condition else ""
        print(f"  [{index}] 🔄 {step.id}: loop(max={step.max_iterations}{cond}) "
              f"→ sub-steps: {sub_ids}")
    elif isinstance(step, NoopStep):
        print(f"  [{index}] ⏹️  {step.id}: noop{deps}")


def _next_step_text(step_id: str, wf) -> str:
    """Return text describing what follows a step in the default flow."""
    ids = [s.id for s in wf.steps]
    try:
        idx = ids.index(step_id)
        if idx + 1 < len(ids):
            return f"→ {ids[idx + 1]}"
        return "→ (end)"
    except ValueError:
        return "(not in top-level flow)"
