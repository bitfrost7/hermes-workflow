"""
Workflow runner — watches pipeline steps and advances them through the kanban board.

The runner operates in foreground mode: it polls kanban cards for completion
and advances current_step_key. All step activity is logged to the state DB
(step_logs table) for post-mortem debugging.

Profile agent logs are fetched from the kanban system (hermes kanban runs / log)
when a kanban card completes, and attached to the step log record.
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
    verbose: bool = False,
) -> int:
    """Run a pipeline to completion in the foreground.

    Returns 0 on success, 1 on error.
    """
    from .state import (
        get_pipeline,
        set_pipeline_status,
        update_step_outputs,
        start_step_log,
        complete_step_log,
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
    from .state import set_pipeline_step, set_pipeline_cycle

    while current_step:
        # Reload step outputs from state (scripts run during compilation)
        pipe = get_pipeline(pipeline_id)
        if pipe:
            step_outputs.update(pipe.step_outputs)

        set_pipeline_step(pipeline_id, current_step.id)
        print(f"\n── Step: {current_step.id} ──")

        # Start step log in runner phase
        log_id = start_step_log(
            pipeline_id=pipeline_id,
            step_id=current_step.id,
            step_type=current_step.type.value if hasattr(current_step, "type") else "unknown",
            cycle=getattr(pipe, "current_cycle", 1) if pipe else 1,
            details={"phase": "runner", "depends_on": current_step.depends_on},
        )

        try:
            _execute_step(
                step=current_step,
                wf=wf,
                pipeline_id=pipeline_id,
                step_outputs=step_outputs,
                created_card_map=created_card_map,
                board=board,
                poll_interval=poll_interval,
                log_id=log_id,
                verbose=verbose,
            )

            # After step completes, fetch profile agent logs for each card
            _attach_worker_logs(log_id, created_card_map.get(current_step.id, []), verbose)

            complete_step_log(log_id, status="done")

        except KeyboardInterrupt:
            print("\n  ⚠️  Interrupted")
            complete_step_log(log_id, status="cancelled", error_message="Interrupted")
            set_pipeline_status(pipeline_id, PipelineStatus.CANCELLED, "Interrupted")
            _cleanup_cards(created_card_map)
            return 1

        except RunnerError as e:
            print(f"\n  ❌ {e}")
            complete_step_log(log_id, status="error", error_message=str(e))
            set_pipeline_status(pipeline_id, PipelineStatus.ERROR, str(e))
            return 1

        # Check for goto (from on_exit)
        goto_target = step_outputs.pop("_goto", None)
        if goto_target:
            next_step = _find_step_by_id(wf, goto_target)
            if not next_step:
                print(f"\n  ❌ goto target '{goto_target}' not found")
                set_pipeline_status(
                    pipeline_id, PipelineStatus.ERROR,
                    f"goto target {goto_target} not found",
                )
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
    log_id: int,
    verbose: bool = False,
) -> None:
    """Execute a single step — dispatch or wait for kanban cards."""

    if isinstance(step, ScriptStep):
        _execute_script(step, step_outputs)

    elif isinstance(step, KanbanStep):
        _wait_kanban_cards(step, created_card_map, poll_interval, log_id, verbose)

    elif isinstance(step, LoopStep):
        _execute_loop(
            step, wf, pipeline_id, step_outputs,
            created_card_map, board, poll_interval,
            log_id, verbose,
        )

    elif isinstance(step, NoopStep):
        if step.summary:
            print(f"  {step.summary}")


def _execute_script(step: ScriptStep, step_outputs: dict) -> None:
    """Script steps are already executed during compilation — no-op in runner."""
    pass


def _wait_kanban_cards(
    step: KanbanStep,
    created_card_map: dict,
    poll_interval: int,
    log_id: int,
    verbose: bool = False,
) -> None:
    """Wait for all kanban cards in a step to complete.

    After all cards complete, fetches profile agent run logs for each card
    and attaches them to the step log.
    """
    from .state import append_worker_logs, complete_step_log

    card_ids = created_card_map.get(step.id, [])
    if not card_ids:
        print(f"     (no cards to wait for)")
        return

    print(f"     waiting for {len(card_ids)} card(s): {', '.join(card_ids)}")
    timeout = 7200  # 2 hours default
    waited = 0

    while waited < timeout:
        all_done = True
        card_statuses = {}
        for cid in card_ids:
            status = _card_status(cid)
            card_statuses[cid] = status
            if status not in ("done", "archived"):
                all_done = False

        if all_done:
            print(f"     ✓ all cards done")
            # Fetch worker logs for each completed card
            for cid in card_ids:
                run_info = _fetch_card_worker_log(cid, verbose)
                if run_info:
                    append_worker_logs(log_id, cid, run_info)
            return

        if verbose:
            status_line = ", ".join(f"{cid}={s}" for cid, s in card_statuses.items())
            print(f"     [{waited}s] {status_line}")

        time.sleep(poll_interval)
        waited += poll_interval

    raise RunnerError(f"Step '{step.id}': timeout waiting for kanban cards")


def _fetch_card_worker_log(card_id: str, verbose: bool = False) -> Optional[dict]:
    """Fetch the profile agent's run log for a completed kanban card.

    Returns a dict with run metadata if available, or None.
    Uses:
      - hermes kanban runs <card_id> --json  → run history
      - hermes kanban log <card_id>           → worker stdout/stderr
    """
    result: dict = {
        "card_id": card_id,
        "status": "unknown",
        "outcome": None,
        "summary": None,
        "profile": None,
        "elapsed_seconds": None,
        "log_preview": None,
        "error": None,
    }

    # Fetch run history
    try:
        r = subprocess.run(
            ["hermes", "kanban", "runs", card_id, "--json"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            runs_data = json.loads(r.stdout)
            # Runs can be a list or a single dict
            if isinstance(runs_data, list) and runs_data:
                latest = runs_data[0]
            elif isinstance(runs_data, dict):
                latest = runs_data
            else:
                latest = None

            if latest:
                result["profile"] = latest.get("profile")
                result["outcome"] = latest.get("outcome")
                result["summary"] = latest.get("summary")
                result["status"] = latest.get("status", "unknown")
                if latest.get("started_at") and latest.get("ended_at"):
                    result["elapsed_seconds"] = (
                        latest["ended_at"] - latest["started_at"]
                    )
    except Exception as e:
        result["error"] = f"runs fetch: {e}"

    # Fetch worker log (profile agent's terminal output)
    try:
        r = subprocess.run(
            ["hermes", "kanban", "log", card_id],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            log_text = r.stdout.strip()
            # Truncate to first 5KB for storage
            result["log_preview"] = log_text[:5000]
            result["log_length"] = len(log_text)
    except Exception as e:
        if not result.get("error"):
            result["error"] = f"log fetch: {e}"

    return result


def _attach_worker_logs(
    log_id: int,
    card_ids: list[str],
    verbose: bool = False,
) -> None:
    """Post-step: fetch and attach worker logs for all cards in a step."""
    from .state import append_worker_logs

    for cid in card_ids:
        run_info = _fetch_card_worker_log(cid, verbose)
        if run_info:
            append_worker_logs(log_id, cid, run_info)


def _execute_loop(
    step: LoopStep,
    wf: WorkflowDef,
    pipeline_id: str,
    step_outputs: dict,
    created_card_map: dict,
    board: Optional[str],
    poll_interval: int,
    parent_log_id: int,
    verbose: bool = False,
) -> None:
    """Execute a loop step — iterates sub-steps until condition or max_iterations."""
    from .state import set_pipeline_cycle, start_step_log, complete_step_log

    for cycle in range(1, step.max_iterations + 1):
        print(f"\n  ┌─ Loop cycle {cycle}/{step.max_iterations} ─┐")
        set_pipeline_cycle(pipeline_id, cycle)

        for sub in step.steps:
            sub_log_id = start_step_log(
                pipeline_id=pipeline_id,
                step_id=sub.id,
                step_type=sub.type.value if hasattr(sub, "type") else "unknown",
                cycle=cycle,
                details={"phase": "runner/loop", "parent_loop": step.id},
            )

            try:
                _execute_step(
                    step=sub,
                    wf=wf,
                    pipeline_id=pipeline_id,
                    step_outputs=step_outputs,
                    created_card_map=created_card_map,
                    board=board,
                    poll_interval=poll_interval,
                    log_id=sub_log_id,
                    verbose=verbose,
                )
                # Attach worker logs for kanban cards in loop sub-steps
                if isinstance(sub, KanbanStep):
                    _attach_worker_logs(
                        sub_log_id,
                        created_card_map.get(sub.id, []),
                        verbose,
                    )
                complete_step_log(sub_log_id, status="done")
            except KeyboardInterrupt:
                complete_step_log(sub_log_id, status="cancelled")
                raise
            except RunnerError as e:
                complete_step_log(sub_log_id, status="error", error_message=str(e))
                raise

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
