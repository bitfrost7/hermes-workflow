"""
Workflow runner — watches pipeline steps and advances them through the kanban board.

The runner operates in two modes:
  1. Interactive (foreground): runs synchronously, blocks until pipeline completes.
  2. Background (daemon): runs as a background thread/process, watching step by step.

It polls kanban cards for completion and advances current_step_key.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from .schema import (
    KanbanStep,
    LoopStep,
    NoopStep,
    PipelineStatus,
    ScriptStep,
    StepDef,
    WorkflowDef,
)

# Matches structured workflow marker in kanban comments
_WF_MARKER_RE = re.compile(r'\{"_wf_template":\s*"[^"]+",\s*"_wf_step":\s*"[^"]+"\}')


class RunnerError(Exception):
    """Raised when pipeline execution fails."""


def run_pipeline(
    wf: WorkflowDef,
    pipeline_id: str,
    board: Optional[str] = None,
    poll_interval: int = 15,
) -> int:
    """Run a pipeline to completion in the foreground.

    Returns 0 on success, 1 on error.
    """
    from .state import (
        get_pipeline,
        list_pipelines,
        set_pipeline_status,
        update_step_outputs,
    )

    print(f"\n{'='*60}")
    print(f"  Pipeline: {pipeline_id}")
    print(f"  Template: {wf.meta.name} v{wf.meta.version}")
    print(f"{'='*60}\n")

    step_outputs: Dict[str, Any] = {}
    created_card_map: Dict[str, List[str]] = {}

    current_step = wf.steps[0] if wf.steps else None
    if not current_step:
        print("  (no steps defined)")
        set_pipeline_status(pipeline_id, PipelineStatus.DONE)
        return 0

    # Main execution loop — walks through steps, advancing on completion
    from .state import set_pipeline_step, set_pipeline_cycle, update_step_outputs, get_pipeline

    while current_step:
        # Reload step outputs from state (scripts run during compilation)
        pipe = get_pipeline(pipeline_id)
        if pipe:
            step_outputs.update(pipe.step_outputs)
        set_pipeline_step(pipeline_id, current_step.id)
        print(f"\n── Step: {current_step.id} ──")

        try:
            _execute_step(
                step=current_step,
                wf=wf,
                pipeline_id=pipeline_id,
                step_outputs=step_outputs,
                created_card_map=created_card_map,
                board=board,
                poll_interval=poll_interval,
            )
        except KeyboardInterrupt:
            print("\n  ⚠️  Interrupted")
            set_pipeline_status(pipeline_id, PipelineStatus.CANCELLED, "Interrupted")
            _cleanup_cards(created_card_map)
            return 1
        except RunnerError as e:
            print(f"\n  ❌ {e}")
            set_pipeline_status(pipeline_id, PipelineStatus.ERROR, str(e))
            return 1

        # Check for goto (from on_exit)
        goto_target = step_outputs.pop("_goto", None)
        if goto_target:
            next_step = _find_step_by_id(wf, goto_target)
            if not next_step:
                print(f"\n  ❌ goto target '{goto_target}' not found")
                set_pipeline_status(pipeline_id, PipelineStatus.ERROR, f"goto target {goto_target} not found")
                return 1
            current_step = next_step
            continue

        # Advance to next step
        next_idx = _step_index(wf, current_step.id) + 1
        if next_idx < len(wf.steps):
            current_step = wf.steps[next_idx]
        else:
            current_step = None

    print(f"\n{'='*60}")
    print(f"  ✅ Pipeline complete: {pipeline_id}")
    print(f"{'='*60}\n")
    set_pipeline_status(pipeline_id, PipelineStatus.DONE)
    return 0


def _execute_step(
    step: StepDef,
    wf: WorkflowDef,
    pipeline_id: str,
    step_outputs: dict,
    created_card_map: dict,
    board: Optional[str],
    poll_interval: int,
) -> None:
    """Execute a single step — dispatch or wait for kanban cards."""

    if isinstance(step, ScriptStep):
        _execute_script(step, step_outputs)

    elif isinstance(step, KanbanStep):
        _wait_kanban_cards(step, created_card_map, poll_interval)

    elif isinstance(step, LoopStep):
        _execute_loop(
            step, wf, pipeline_id, step_outputs,
            created_card_map, board, poll_interval,
        )

    elif isinstance(step, NoopStep):
        if step.summary:
            print(f"  {step.summary}")


def _execute_script(step: ScriptStep, step_outputs: dict) -> None:
    """Execute a script step from the compiler step context.

    Scripts are already compiled by compiler.py. If the script was already
    run during compilation (as a deterministic step that produces card
    inputs like l1_json), we skip re-execution here and just wait if needed.
    """
    # Scripts are run during compilation. In the runner phase we only
    # need to process kanban card steps (waiting for completion).
    # Script steps are passive — they already completed or produced output.
    pass


def _wait_kanban_cards(
    step: KanbanStep,
    created_card_map: dict,
    poll_interval: int,
) -> None:
    """Wait for all kanban cards in a step to complete."""

    card_ids = created_card_map.get(step.id, [])
    if not card_ids:
        print(f"     (no cards to wait for)")
        return

    print(f"     waiting for {len(card_ids)} card(s): {', '.join(card_ids)}")
    timeout = 7200  # 2 hours default
    waited = 0

    while waited < timeout:
        all_done = True
        for cid in card_ids:
            status = _card_status(cid)
            if status not in ("done", "archived"):
                all_done = False
                break

        if all_done:
            print(f"     ✓ all cards done")
            return

        time.sleep(poll_interval)
        waited += poll_interval

    raise RunnerError(f"Step '{step.id}': timeout waiting for kanban cards")


def _execute_loop(
    step: LoopStep,
    wf: WorkflowDef,
    pipeline_id: str,
    step_outputs: dict,
    created_card_map: dict,
    board: Optional[str],
    poll_interval: int,
) -> None:
    """Execute a loop step — iterates sub-steps until condition or max_iterations."""

    for cycle in range(1, step.max_iterations + 1):
        print(f"\n  ┌─ Loop cycle {cycle}/{step.max_iterations} ─┐")
        from .state import set_pipeline_cycle
        set_pipeline_cycle(pipeline_id, cycle)

        # Run sub-steps
        for sub in step.steps:
            _execute_step(sub, wf, pipeline_id, step_outputs, created_card_map, board, poll_interval)

        # Check while condition
        if step.while_condition:
            should_continue = _eval_expr(step.while_condition, step_outputs)
            print(f"  └─ while: {should_continue}")
            if not should_continue:
                return

        # Check for goto from on_exit
        if step_outputs.get("_goto"):
            return

    print(f"  ⚠️  max iterations ({step.max_iterations}) reached")


def _card_status(card_id: str) -> str:
    """Get a card's status via hermes kanban show."""
    result = subprocess.run(
        ["hermes", "kanban", "show", card_id],
        capture_output=True, text=True, timeout=30,
    )
    for line in result.stdout.split("\n"):
        line = line.strip()
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def _cleanup_cards(created_card_map: dict) -> None:
    """Archive all created cards on error/cancel."""
    all_ids = set()
    for ids in created_card_map.values():
        all_ids.update(ids)

    if not all_ids:
        return

    print(f"  🧹 Archiving {len(all_ids)} cards...")
    for cid in all_ids:
        subprocess.run(
            ["hermes", "kanban", "archive", cid, "--force"],
            capture_output=True, timeout=30,
        )


def _find_step_by_id(wf: WorkflowDef, step_id: str) -> Optional[StepDef]:
    """Find a step by its id, recursively searching loops."""
    for step in wf.steps:
        if step.id == step_id:
            return step
        if isinstance(step, LoopStep):
            for sub in step.steps:
                if sub.id == step_id:
                    return sub
    return None


def _step_index(wf: WorkflowDef, step_id: str) -> int:
    for i, s in enumerate(wf.steps):
        if s.id == step_id:
            return i
    return -1


def _eval_expr(expr: str, step_outputs: dict) -> bool:
    """Evaluate a simple while condition expression."""
    # Handle: steps.<id>.exit_code != 0
    # Handle: steps.<id>.exit_code == 0
    # Simple key presence checks
    m = re.match(r"steps\.(\w+)\.exit_code\s*(!=|==)\s*(\d+)", expr)
    if m:
        step_id = m.group(1)
        op = m.group(2)
        val = int(m.group(3))
        actual = step_outputs.get(f"{step_id}.exit_code", -1)
        if op == "!=":
            return actual != val
        else:
            return actual == val

    # Fallback: treat non-empty/truthy as true
    return True
