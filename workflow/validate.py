"""
Workflow validation — comprehensive checks on a WorkflowDef.

Checks performed:
  1. Schema-level: duplicate ids, missing depends_on/goto targets
  2. Graph-level: dependency cycles, control-flow cycles, orphans,
     dead loops, unreachable steps
  3. Semantic: loops without max_iterations, noop-only workflows,
     steps with no effect, empty for_each lists
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .schema import (
    KanbanStep,
    LoopStep,
    NoopStep,
    ScriptStep,
    StepDef,
    StepType,
    WorkflowDef,
)
from .graph import WorkflowGraph


class ValidationIssue:
    """A single validation finding."""

    SEVERITY_ERROR = "error"
    SEVERITY_WARNING = "warning"
    SEVERITY_INFO = "info"

    def __init__(
        self,
        severity: str,
        code: str,
        message: str,
        step_id: Optional[str] = None,
        details: Optional[dict] = None,
    ):
        self.severity = severity
        self.code = code
        self.message = message
        self.step_id = step_id
        self.details = details or {}

    def __repr__(self) -> str:
        prefix = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(
            self.severity, "•"
        )
        loc = f" [{self.step_id}]" if self.step_id else ""
        return f"{prefix} [{self.code}]{loc} {self.message}"

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "step_id": self.step_id,
            "details": self.details,
        }


class ValidationResult:
    """Result of a full workflow validation."""

    def __init__(self, wf: WorkflowDef):
        self.wf = wf
        self.issues: List[ValidationIssue] = []

    def add(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)

    def has_errors(self) -> bool:
        return any(i.severity == ValidationIssue.SEVERITY_ERROR for i in self.issues)

    def has_warnings(self) -> bool:
        return any(i.severity == ValidationIssue.SEVERITY_WARNING for i in self.issues)

    def summary(self) -> dict:
        by_severity: Dict[str, int] = {}
        for i in self.issues:
            by_severity[i.severity] = by_severity.get(i.severity, 0) + 1
        return {
            "total": len(self.issues),
            "errors": by_severity.get(ValidationIssue.SEVERITY_ERROR, 0),
            "warnings": by_severity.get(ValidationIssue.SEVERITY_WARNING, 0),
            "info": by_severity.get(ValidationIssue.SEVERITY_INFO, 0),
            "pass": not self.has_errors(),
        }

    def __repr__(self) -> str:
        lines = [f"Validation: {self.wf.meta.name} v{self.wf.meta.version}"]
        s = self.summary()
        lines.append(
            f"  {s['errors']} errors, {s['warnings']} warnings, "
            f"{s['info']} info — {'✅ PASS' if s['pass'] else '❌ FAIL'}"
        )
        for issue in self.issues:
            lines.append(f"  {issue}")
        return "\n".join(lines)


# ── Schema-level checks ──────────────────────────────────────────


def check_duplicate_ids(wf: WorkflowDef, result: ValidationResult) -> None:
    """Check for duplicate step ids (also handled by loader)."""
    seen: Dict[str, int] = {}
    for step in _all_steps(wf):
        if step.id in seen:
            result.add(ValidationIssue(
                ValidationIssue.SEVERITY_ERROR,
                "duplicate-id",
                f"Duplicate step id '{step.id}'",
                step_id=step.id,
            ))
        seen[step.id] = seen.get(step.id, 0) + 1


def check_missing_dep_refs(wf: WorkflowDef, result: ValidationResult) -> None:
    """Check depends_on references resolve to existing step ids."""
    all_ids = {s.id for s in _all_steps(wf)}
    for step in _all_steps(wf):
        for dep in step.depends_on:
            if dep not in all_ids:
                result.add(ValidationIssue(
                    ValidationIssue.SEVERITY_ERROR,
                    "missing-dep",
                    f"depends_on '{dep}' does not exist",
                    step_id=step.id,
                ))


def check_missing_goto_targets(wf: WorkflowDef, result: ValidationResult) -> None:
    """Check on_exit goto targets resolve to existing step ids."""
    all_ids = {s.id for s in _all_steps(wf)}
    for step in _all_steps(wf):
        if isinstance(step, ScriptStep) and step.on_exit:
            for exit_code, branch in step.on_exit.items():
                if branch.goto not in all_ids:
                    result.add(ValidationIssue(
                        ValidationIssue.SEVERITY_ERROR,
                        "missing-goto",
                        f"on_exit.{exit_code}.goto '{branch.goto}' does not exist",
                        step_id=step.id,
                    ))


def check_unused_vars(wf: WorkflowDef, result: ValidationResult) -> None:
    """Warn about vars declared in YAML but never used in any step."""
    if not wf.vars:
        return

    all_text = _workflow_text(wf)
    for var_name in wf.vars:
        # Allow {{ .varName }} and {{ varName }} syntax
        pattern = f"{{{{ {var_name}"
        alt_pattern = f"{{{{ .{var_name}"
        if pattern not in all_text and alt_pattern not in all_text:
            result.add(ValidationIssue(
                ValidationIssue.SEVERITY_WARNING,
                "unused-var",
                f"Variable '{var_name}' is declared but never referenced in any step",
                details={"var": var_name},
            ))


# ── Graph-level checks ───────────────────────────────────────────


def check_dep_cycles(graph: WorkflowGraph, result: ValidationResult) -> None:
    """Check for circular dependency chains."""
    cycles = graph.find_dep_cycles()
    for cycle in cycles:
        result.add(ValidationIssue(
            ValidationIssue.SEVERITY_ERROR,
            "dep-cycle",
            f"Circular dependency: {' → '.join(cycle)}",
            step_id=cycle[0],
            details={"cycle": cycle},
        ))


def check_cf_cycles(graph: WorkflowGraph, result: ValidationResult) -> None:
    """Check for control-flow cycles (infinite loops without escape).

    We only flag control-flow cycles that are NOT inside a LoopStep
    (those are expected). External control-flow cycles are bugs.
    """
    cycles = graph.find_cf_cycles()
    for cycle in cycles:
        # Skip if the cycle is internal to a LoopStep:
        # all nodes are either the loop parent or children of it
        loop_owners = {graph.loop_parent.get(n) for n in cycle}
        if len(loop_owners) == 1 and None not in loop_owners:
            continue  # All children of one loop — expected
        if len(loop_owners) == 2 and None in loop_owners:
            # One node IS the loop parent, rest are its children
            parent_id = next(n for n in loop_owners if n is not None)
            children = {n for n in cycle if graph.loop_parent.get(n) == parent_id}
            if children and any(n == parent_id for n in cycle):
                continue  # Loop parent + its children — expected
        result.add(ValidationIssue(
            ValidationIssue.SEVERITY_ERROR,
            "cf-cycle",
            f"Control-flow cycle outside loop: {' → '.join(cycle)}",
            step_id=cycle[0],
            details={"cycle": cycle},
        ))


def check_orphans(graph: WorkflowGraph, result: ValidationResult) -> None:
    """Check for steps unreachable from the first step."""
    orphans = graph.find_orphans()
    for orphan in orphans:
        result.add(ValidationIssue(
            ValidationIssue.SEVERITY_ERROR,
            "orphan-step",
            f"Step '{orphan}' is unreachable from the workflow entry point",
            step_id=orphan,
        ))


def check_dead_loops(graph: WorkflowGraph, result: ValidationResult) -> None:
    """Check for loops that can never exit."""
    dead = graph.find_dead_loops()
    for loop_id in dead:
        result.add(ValidationIssue(
            ValidationIssue.SEVERITY_ERROR,
            "dead-loop",
            f"Loop '{loop_id}' has no exit path — will run forever",
            step_id=loop_id,
        ))


def check_self_dep(graph: WorkflowGraph, result: ValidationResult) -> None:
    """Check for steps that depend on themselves."""
    for step in _all_steps(graph.wf):
        if step.id in step.depends_on:
            result.add(ValidationIssue(
                ValidationIssue.SEVERITY_ERROR,
                "self-dep",
                f"Step depends on itself",
                step_id=step.id,
            ))


# ── Semantic checks ─────────────────────────────────────────────


def check_loop_bounds(wf: WorkflowDef, result: ValidationResult) -> None:
    """Check that loops have max_iterations set."""
    for step in wf.steps:
        if isinstance(step, LoopStep):
            if step.max_iterations == 0 and not step.while_condition:
                result.add(ValidationIssue(
                    ValidationIssue.SEVERITY_WARNING,
                    "unbounded-loop",
                    f"Loop has no max_iterations and no while condition — infinite",
                    step_id=step.id,
                ))
            elif step.max_iterations > 20:
                result.add(ValidationIssue(
                    ValidationIssue.SEVERITY_WARNING,
                    "high-loop-limit",
                    f"Loop max_iterations={step.max_iterations} is high "
                    f"(recommended ≤10)",
                    step_id=step.id,
                    details={"max_iterations": step.max_iterations},
                ))


def check_noop_only(wf: WorkflowDef, result: ValidationResult) -> None:
    """Warn if the workflow has only noop steps."""
    non_noop = [s for s in wf.steps if not isinstance(s, NoopStep)]
    if not non_noop:
        result.add(ValidationIssue(
            ValidationIssue.SEVERITY_WARNING,
            "noop-only",
            "Workflow contains only noop steps — nothing will execute",
        ))


def check_script_without_output(wf: WorkflowDef, result: ValidationResult) -> None:
    """Warn about script steps that produce stdout but don't capture it."""
    for step in _all_steps(wf):
        if isinstance(step, ScriptStep) and not step.output:
            if not step.on_exit:
                result.add(ValidationIssue(
                    ValidationIssue.SEVERITY_INFO,
                    "script-no-output",
                    f"Script step has no output capture and no on_exit — "
                    f"side-effect-only",
                    step_id=step.id,
                ))


def check_kanban_without_assignee(wf: WorkflowDef, result: ValidationResult) -> None:
    """Warn about kanban steps with empty assignee."""
    for step in _all_steps(wf):
        if isinstance(step, KanbanStep):
            if not step.template.assignee:
                result.add(ValidationIssue(
                    ValidationIssue.SEVERITY_ERROR,
                    "no-assignee",
                    "Kanban step has no assignee — dispatcher cannot route",
                    step_id=step.id,
                ))
            if not step.template.title:
                result.add(ValidationIssue(
                    ValidationIssue.SEVERITY_ERROR,
                    "no-title",
                    "Kanban step has no title",
                    step_id=step.id,
                ))


# ── Top-level validator ─────────────────────────────────────────


def validate(wf: WorkflowDef) -> ValidationResult:
    """Run all validation checks on a WorkflowDef.

    Returns a ValidationResult with all findings (errors, warnings, info).
    """
    result = ValidationResult(wf)
    graph = WorkflowGraph(wf)

    # Schema checks
    check_duplicate_ids(wf, result)
    check_missing_dep_refs(wf, result)
    check_missing_goto_targets(wf, result)
    check_unused_vars(wf, result)

    # Graph checks
    check_dep_cycles(graph, result)
    check_cf_cycles(graph, result)
    check_orphans(graph, result)
    check_dead_loops(graph, result)
    check_self_dep(graph, result)

    # Semantic checks
    check_loop_bounds(wf, result)
    check_noop_only(wf, result)
    check_script_without_output(wf, result)
    check_kanban_without_assignee(wf, result)

    return result


# ── Helpers ──────────────────────────────────────────────────────


def _all_steps(wf: WorkflowDef) -> List[StepDef]:
    result: List[StepDef] = []
    for step in wf.steps:
        result.append(step)
        if isinstance(step, LoopStep):
            result.extend(step.steps)
    return result


def _workflow_text(wf: WorkflowDef) -> str:
    """Concatenate all step text for pattern matching."""
    parts: List[str] = []
    for step in _all_steps(wf):
        parts.append(step.id if hasattr(step, "id") else "")
        if isinstance(step, ScriptStep):
            parts.append(step.script)
        elif isinstance(step, KanbanStep):
            parts.append(step.template.title)
            parts.append(step.template.body or "")
            parts.append(step.template.assignee)
        elif isinstance(step, NoopStep):
            parts.append(step.summary or "")
        if hasattr(step, "depends_on"):
            parts.extend(step.depends_on)
    return " ".join(parts)
