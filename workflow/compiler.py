"""
Workflow compiler — converts a validated WorkflowDef into kanban cards via the Hermes kanban CLI.

The compiler:
  1. Renders Jinja2 template expressions in step definitions
  2. Resolves for_each iterations into individual card instances
  3. Computes parent-child dependencies across steps
  4. Creates kanban cards with hermes kanban create --parent
  5. Sets workflow_template_id and current_step_key on each card
  6. Records step execution logs in the state DB for debugging

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
    verbose: bool = False,
) -> tuple[str, list[dict]]:
    """Compile a WorkflowDef into kanban cards.

    Returns (pipeline_id: str, created_cards: list[dict]).
    Each created card dict has: {step_id, card_id, title, assignee, status}
    """
    from .state import (
        create_pipeline_instance,
        set_pipeline_step,
        update_step_outputs,
        start_step_log,
        complete_step_log,
    )

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

        # Start step log
        log_id = start_step_log(
            pipeline_id=pipeline_id,
            step_id=step.id,
            step_type=step.type.value if hasattr(step, "type") else "unknown",
            cycle=1,
            details={"depends_on": step.depends_on},
        )

        try:
            _process_step(
                step=step,
                wf=wf,
                pipeline_id=pipeline_id,
                resolved_vars=resolved_vars,
                step_outputs=step_outputs,
                created_card_map=created_card_map,
                all_cards=all_cards,
                board=board,
                log_id=log_id,
                verbose=verbose,
            )
            # Mark step log as done (for kanban/noop, they complete instantly)
            complete_step_log(log_id, status="done")
        except Exception as e:
            complete_step_log(
                log_id, status="error",
                error_message=f"{type(e).__name__}: {e}",
            )
            raise

    set_pipeline_step(pipeline_id, wf.steps[0].id if wf.steps else None)
    # Strip _goto before persisting — it was consumed during compilation's
    # on_exit branching and should not trigger again in the runner.
    step_outputs.pop("_goto", None)
    update_step_outputs(pipeline_id, step_outputs)
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
    log_id: int,
    verbose: bool = False,
) -> None:
    """Process a single step — script, kanban, loop, or noop."""

    if isinstance(step, ScriptStep):
        _process_script(step, resolved_vars, step_outputs, log_id, verbose)

    elif isinstance(step, KanbanStep):
        _process_kanban(
            step, pipeline_id, resolved_vars,
            created_card_map, all_cards, wf.meta.name, board,
            log_id=log_id, verbose=verbose, step_outputs=step_outputs,
        )

    elif isinstance(step, LoopStep):
        _process_loop(
            step, wf, pipeline_id, resolved_vars,
            step_outputs, created_card_map, all_cards,
            board, log_id, verbose,
        )

    # NoopStep — nothing to do


def _process_script(
    step: ScriptStep,
    vars: dict,
    step_outputs: dict,
    log_id: int,
    verbose: bool = False,
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
    script_stdout = result.stdout.strip()
    if step.output and script_stdout:
        try:
            step_outputs[step.output] = json.loads(script_stdout)
        except (json.JSONDecodeError, ValueError):
            step_outputs[step.output] = script_stdout

    # Handle exit code branches
    goto_target = None
    if step.on_exit and result.returncode != 0:
        branch = step.on_exit.get(str(result.returncode)) or step.on_exit.get("else")
        if branch:
            step_outputs["_goto"] = branch.goto
            goto_target = branch.goto
            print(f"     → exit={result.returncode}, jump to '{branch.goto}'")

    stderr = result.stderr.strip()
    if stderr:
        print(f"     ⚠️  stderr: {stderr[:200]}")

    # Write step log
    from .state import complete_step_log
    complete_step_log(
        log_id,
        status="done" if result.returncode == 0 else "error",
        exit_code=result.returncode,
        stdout=script_stdout,
        stderr=stderr,
    )


def _process_kanban(
    step: KanbanStep,
    pipeline_id: str,
    vars: dict,
    created_card_map: dict,
    all_cards: list[dict],
    template_name: str,
    board: Optional[str],
    log_id: int,
    verbose: bool = False,
    step_outputs: Optional[dict] = None,
) -> None:
    """Create kanban cards for a kanban step (with optional for_each fan-out)."""

    # Merge step_outputs into vars for template resolution.
    # This allows {{ task_data.tasks }} from a prior script step's output.
    render_vars = {**vars}
    if step_outputs:
        render_vars.update(step_outputs)

    # Resolve iteration items
    if step.for_each:
        items = _eval_jinja_expr(step.for_each, render_vars)
        if not isinstance(items, list):
            items = [items]
    else:
        items = [None]

    created_card_ids: list[str] = []

    for item in items:
        item_vars = {**render_vars, "item": item}
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

        # Log the command being run
        if verbose:
            print(f"     $ {' '.join(cmd)}")

        # Execute
        result = _run_hermes(cmd)
        card_id = result.get("id", "?")
        step.card_ids.append(card_id)
        created_card_map.setdefault(step.id, []).append(card_id)
        created_card_ids.append(card_id)
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
        print(f"  📋 {step.id}{item_repr}: {title} → {card_id} (→ {assignee})")

        # Log created card info
        if verbose:
            print(f"       skill={skill}, workspace={workspace}, parents={parent_ids}")

    # Write step log
    from .state import complete_step_log
    complete_step_log(
        log_id,
        status="done",
        card_ids=created_card_ids,
    )


def _tag_card(card_id: str, template_name: str, step_key: str, board: Optional[str]) -> None:
    """Tag a kanban card with workflow_template_id and current_step_key.

    Uses hermes kanban edit to set metadata on the task's current run.
    The workflow_template_id and current_step_key live on the task row.
    We store them via a comment convention since the CLI doesn't expose
    these fields directly yet — the DB columns exist but lack a CLI setter.

    For direct DB access in the future, this would use:
      UPDATE tasks SET workflow_template_id=?, current_step_key=? WHERE id=?
    """
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
    parent_log_id: int,
    verbose: bool = False,
) -> None:
    """Process a loop step — iterates sub-steps."""
    from .state import set_pipeline_cycle, start_step_log, complete_step_log

    for cycle in range(1, step.max_iterations + 1):
        set_pipeline_cycle(pipeline_id, cycle)
        print(f"  🔄 {step.id}: cycle {cycle}/{step.max_iterations}")

        cycle_vars = {**vars, "_cycle": cycle}

        # Clear previous loop card maps so sub-steps create fresh cards
        loop_card_map: dict[str, list[str]] = {}
        cycle_card_ids: list[str] = []

        for sub in step.steps:
            _render_step_vars(sub, cycle_vars, step_outputs)

            # Start log for sub-step (inside loop)
            sub_log_id = start_step_log(
                pipeline_id=pipeline_id,
                step_id=sub.id,
                step_type=sub.type.value if hasattr(sub, "type") else "unknown",
                cycle=cycle,
                details={
                    "depends_on": sub.depends_on,
                    "parent_loop": step.id,
                },
            )

            try:
                _process_step(
                    step=sub,
                    wf=wf,
                    pipeline_id=pipeline_id,
                    resolved_vars=cycle_vars,
                    step_outputs=step_outputs,
                    created_card_map=loop_card_map,
                    all_cards=all_cards,
                    board=board,
                    log_id=sub_log_id,
                    verbose=verbose,
                )
                complete_step_log(sub_log_id, status="done")
            except Exception as e:
                complete_step_log(
                    sub_log_id, status="error",
                    error_message=f"{type(e).__name__}: {e}",
                )
                raise

            # Collect card ids from sub-step
            if sub.id in loop_card_map:
                cycle_card_ids.extend(loop_card_map[sub.id])

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
