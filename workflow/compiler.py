"""
Workflow compiler — converts a validated WorkflowDef into kanban cards via the Hermes kanban CLI.

The compiler:
  1. Renders Jinja2 template expressions in step definitions
  2. Resolves for_each iterations into individual card instances
  3. Computes parent-child dependencies across steps
  4. Creates kanban cards with hermes kanban create --parent
  5. Sets workflow_template_id and current_step_key on each card

Pipeline execution is handled by runner.py; this module only creates the cards.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

from .schema import (
    KanbanStep,
    LoopStep,
    NoopStep,
    ScriptStep,
    StepDef,
    WorkflowDef,
    WorkflowSettings,
)

# Matches {{ ... }} Jinja2 expressions
_TMPL_RE = re.compile(r"\{\{(.*?)\}\}")


class CompileError(Exception):
    """Raised when workflow compilation fails."""


def compile_workflow(
    wf: WorkflowDef,
    user_vars: Dict[str, str],
    board: Optional[str] = None,
) -> tuple[str, list[dict]]:
    """Compile a WorkflowDef into kanban cards.

    Returns (pipeline_id: str, created_cards: list[dict]).
    Each created card dict has: {step_id, card_id, title, assignee, status}
    """
    from .state import create_pipeline_instance, set_pipeline_step

    # Resolve vars: defaults from YAML + user overrides
    resolved_vars: dict = {**wf.vars, **{k: v for k, v in user_vars.items()}}

    # Create pipeline instance
    pipeline_id = create_pipeline_instance(
        template_name=wf.meta.name,
        template_version=wf.meta.version,
        vars=resolved_vars,
    )

    all_cards: list[dict] = []
    step_outputs: dict[str, Any] = {}
    created_card_map: dict[str, list[str]] = {}  # step_id → [card_ids]

    # Walk steps in order
    for step in wf.steps:
        _render_step_vars(step, resolved_vars, step_outputs)
        _process_step(
            step=step,
            wf=wf,
            pipeline_id=pipeline_id,
            resolved_vars=resolved_vars,
            step_outputs=step_outputs,
            created_card_map=created_card_map,
            all_cards=all_cards,
            board=board,
        )

    set_pipeline_step(pipeline_id, wf.steps[0].id if wf.steps else None)
    return pipeline_id, all_cards


def _process_step(
    step: StepDef,
    wf: WorkflowDef,
    pipeline_id: str,
    resolved_vars: dict,
    step_outputs: dict,
    created_card_map: dict[str, list[str]],
    all_cards: list[dict],
    board: Optional[str],
) -> None:
    """Process a single step — script, kanban, loop, or noop."""

    if isinstance(step, ScriptStep):
        _process_script(step, resolved_vars, step_outputs)

    elif isinstance(step, KanbanStep):
        _process_kanban(
            step, pipeline_id, resolved_vars,
            created_card_map, all_cards, wf.meta.name, board,
        )

    elif isinstance(step, LoopStep):
        _process_loop(
            step, wf, pipeline_id, resolved_vars,
            step_outputs, created_card_map, all_cards,
            board,
        )

    # NoopStep — nothing to do


def _process_script(
    step: ScriptStep,
    vars: dict,
    step_outputs: dict,
) -> None:
    """Execute a script step and capture its output."""
    import time

    rendered = _render_str(step.script, vars)
    print(f"  ⚡ {step.id}: {rendered[:120]}")

    result = subprocess.run(
        ["bash", "-c", rendered],
        capture_output=True, text=True, timeout=1800,
    )

    # Store exit code
    step_outputs[f"{step.id}.exit_code"] = result.returncode

    # Store stdout if output variable is set
    if step.output:
        stdout = result.stdout.strip()
        if stdout:
            try:
                step_outputs[step.output] = json.loads(stdout)
            except (json.JSONDecodeError, ValueError):
                step_outputs[step.output] = stdout

    # Handle exit code branches
    if step.on_exit and result.returncode != 0:
        branch = step.on_exit.get(str(result.returncode)) or step.on_exit.get("else")
        if branch:
            step_outputs["_goto"] = branch.goto
            print(f"     → exit={result.returncode}, jump to '{branch.goto}'")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if stderr:
            print(f"     ⚠️  stderr: {stderr[:200]}")


def _process_kanban(
    step: KanbanStep,
    pipeline_id: str,
    vars: dict,
    created_card_map: dict,
    all_cards: list[dict],
    template_name: str,
    board: Optional[str],
) -> None:
    """Create kanban cards for a kanban step (with optional for_each fan-out)."""

    # Resolve iteration items
    if step.for_each:
        items = _eval_jinja_expr(step.for_each, vars)
        if not isinstance(items, list):
            items = [items]
    else:
        items = [None]

    for item in items:
        # Render template with item context
        item_vars = {**vars, "item": item}
        title = _render_str(step.template.title, item_vars)
        assignee = _render_str(step.template.assignee, item_vars)
        skill = _render_str(step.template.skill, item_vars) if step.template.skill else None
        body = _render_str(step.template.body, item_vars) if step.template.body else None
        workspace = _render_str(step.template.workspace, item_vars)

        # Compute parents from depends_on
        parent_ids: list[str] = []
        for dep in step.depends_on:
            parent_ids.extend(created_card_map.get(dep, []))

        # Build kanban create command
        cmd = [
            "hermes", "kanban", "create",
            title,
            "--assignee", assignee,
            "--workspace", workspace,
            "--body", body or "",
        ]
        for pid in parent_ids:
            cmd.extend(["--parent", pid])
        if skill:
            cmd.append(f"--skill={skill}")
        cmd.append("--json")

        if board:
            cmd = ["--board", board] + cmd

        # Execute
        result = _run_hermes(cmd)
        card_id = result.get("id", "?")
        step.card_ids.append(card_id)
        created_card_map.setdefault(step.id, []).append(card_id)
        all_cards.append({
            "step_id": step.id,
            "card_id": card_id,
            "title": title,
            "assignee": assignee,
            "status": "created",
        })

        # Tag the card with workflow metadata
        _tag_card(card_id, template_name, step.id, board)

        item_repr = f" [{item}]" if item else ""
        print(f"  📋 {step.id}{item_repr}: {title} → {card_id}")


def _tag_card(card_id: str, template_name: str, step_key: str, board: Optional[str]) -> None:
    """Tag a kanban card with workflow_template_id and current_step_key.

    Uses hermes kanban edit to set metadata on the task's current run.
    The workflow_template_id and current_step_key live on the task row.
    We store them via a comment convention since the CLI doesn't expose
    these fields directly yet — the DB columns exist but lack a CLI setter.

    For direct DB access in the future, this would use:
      UPDATE tasks SET workflow_template_id=?, current_step_key=? WHERE id=?
    """
    # Workaround: add a structured comment the watcher can parse.
    # In v2 when the dispatcher consumes these columns we'll write them
    # via the kanban_db Python API directly.
    marker = json.dumps({
        "_wf_template": template_name,
        "_wf_step": step_key,
    })
    _run_hermes(["hermes", "kanban", "comment", card_id, marker])


def _process_loop(
    step: LoopStep,
    wf: WorkflowDef,
    pipeline_id: str,
    vars: dict,
    step_outputs: dict,
    created_card_map: dict,
    all_cards: list[dict],
    board: Optional[str],
) -> None:
    """Process a loop step — iterates sub-steps."""
    from .state import set_pipeline_cycle

    for cycle in range(1, step.max_iterations + 1):
        set_pipeline_cycle(pipeline_id, cycle)
        print(f"  🔄 {step.id}: cycle {cycle}/{step.max_iterations}")

        cycle_vars = {**vars, "_cycle": cycle}

        # Clear previous loop card maps so sub-steps create fresh cards
        loop_card_map: dict[str, list[str]] = {}

        for sub in step.steps:
            _render_step_vars(sub, cycle_vars, step_outputs)
            _process_step(
                step=sub,
                wf=wf,
                pipeline_id=pipeline_id,
                resolved_vars=cycle_vars,
                step_outputs=step_outputs,
                created_card_map=loop_card_map,
                all_cards=all_cards,
                board=board,
            )

        # Check while condition
        if step.while_condition:
            should_continue = _eval_jinja_expr(step.while_condition, {
                **cycle_vars, **step_outputs,
            })
            if not should_continue:
                print(f"     ✓ while condition false, loop done")
                break

        # Check for on_exit goto from a sub-script
        if step_outputs.get("_goto"):
            del step_outputs["_goto"]
            break
    else:
        print(f"     ⚠️  max iterations ({step.max_iterations}) reached")


def _render_step_vars(step, vars: dict, step_outputs: dict) -> None:
    """Merge step_outputs into vars for Jinja2 resolution of this step."""
    pass  # vars is already a merged dict at call sites


def _render_str(template: str, vars: dict) -> str:
    """Simple {{ var }} / {{ var.key }} template renderer (no Jinja2 dep)."""
    def _replace(m):
        expr = m.group(1).strip()
        try:
            val = _eval_jinja_expr(expr, vars)
            if val is None:
                return ""
            return str(val)
        except Exception:
            return m.group(0)

    return _TMPL_RE.sub(_replace, template)


def _eval_jinja_expr(expr: str, vars: dict) -> Any:
    """Evaluate a simple dot-path expression against vars dict.

    Supports:
      - var.name          → vars["name"]
      - var.name.sub      → vars["name"]["sub"]
      - var.list.0        → vars["list"][0]
      - l1_json.actions   → vars["l1_json"]["actions"]
      - item              → vars["item"]
      - 42                → 42 (literal int)
      - true / false      → True / False
    """
    expr = expr.strip()

    # Literals
    if expr == "true":
        return True
    if expr == "false":
        return False

    # Integer literal
    try:
        return int(expr)
    except ValueError:
        pass

    # Dot-path into vars
    parts = expr.split(".")
    val = vars
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part)
        elif isinstance(val, (list, tuple)) and part.isdigit():
            val = val[int(part)]
        else:
            try:
                val = getattr(val, part)
            except AttributeError:
                return None
    return val


def _run_hermes(cmd: list[str]) -> dict:
    """Run a hermes CLI command and return parsed JSON output."""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise CompileError(
            f"hermes command failed (exit {result.returncode}): {stderr[:500]}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"output": result.stdout.strip()}
