"""
Pydantic schema models for Hermes Workflow YAML definitions.

Validates workflow YAML files at load time before any kanban operations run.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


class StepType(str, Enum):
    SCRIPT = "script"
    KANBAN = "kanban"
    LOOP = "loop"
    NOOP = "noop"


class OnExitBranch(BaseModel):
    """Conditional jump target based on a script's exit code."""

    goto: str = Field(description="Step id to jump to")


class BaseStep(BaseModel):
    """Common fields shared by all step types."""

    id: str = Field(description="Unique step identifier within this workflow")
    depends_on: List[str] = Field(
        default_factory=list,
        description="Step ids this step depends on (resolved to parent cards)",
    )
    output: Optional[str] = Field(
        default=None,
        description="Variable name to store script/card output into step context",
    )


class ScriptStep(BaseStep):
    """A deterministic script step — runs a shell command, not a kanban card."""

    type: Literal[StepType.SCRIPT] = Field(default=StepType.SCRIPT)
    script: str = Field(description="Shell command to run")
    on_exit: Optional[Dict[str, OnExitBranch]] = Field(
        default=None,
        description="Exit code → jump target mapping, e.g. {0: {goto: next}, else: {goto: fix}}",
    )


class KanbanCardTemplate(BaseModel):
    """Template for creating a single kanban task card."""

    title: str = Field(description="Card title (Jinja2 template)")
    assignee: str = Field(description="Profile name to assign the card to")
    skill: Optional[str] = Field(default=None, description="Skill to force-load")
    workspace: str = Field(default="scratch", description="Workspace type: scratch|dir:<path>")
    body: Optional[str] = Field(default=None, description="Card body text")


class KanbanStep(BaseStep):
    """A kanban card step — creates one or more kanban task cards."""

    type: Literal[StepType.KANBAN] = Field(default=StepType.KANBAN)
    for_each: Optional[str] = Field(
        default=None,
        description="Jinja2 expression to iterate over (e.g. 'l1_json.actions')",
    )
    template: KanbanCardTemplate = Field(description="Card template")

    # Internal: resolved at runtime
    card_ids: List[str] = Field(default_factory=list, exclude=True)


class LoopStep(BaseStep):
    """A loop step — repeats a block of sub-steps until a condition is met."""

    type: Literal[StepType.LOOP] = Field(default=StepType.LOOP)
    max_iterations: int = Field(default=10, ge=1, description="Max loop iterations")
    while_condition: Optional[str] = Field(
        default=None,
        alias="while",
        description="Jinja2 condition string; empty/null = infinite until on_exit breaks",
    )
    steps: List[Union["ScriptStep", "KanbanStep", "LoopStep", "NoopStep"]] = Field(
        description="Sub-steps to repeat each iteration"
    )


class NoopStep(BaseStep):
    """A no-op step — terminal marker with no action."""

    type: Literal[StepType.NOOP] = Field(default=StepType.NOOP)
    summary: Optional[str] = Field(default=None, description="Completion message")


StepDef = Union[ScriptStep, KanbanStep, LoopStep, NoopStep]


class WorkflowMeta(BaseModel):
    """Workflow metadata — maps to workflow_template_id in kanban."""

    name: str = Field(description="Workflow name (used as workflow_template_id)")
    version: str = Field(default="1.0.0", description="Semantic version")
    description: Optional[str] = Field(default=None, description="Human-readable description")


class WorkflowSettings(BaseModel):
    """Runtime settings for a workflow pipeline."""

    concurrency: int = Field(default=3, ge=1, description="Max parallel cards per profile")
    poll_interval: int = Field(default=15, ge=5, description="Step watcher poll interval (seconds)")
    timeout_per_step: str = Field(default="30m", description="Per-step timeout")
    cleanup_on_error: bool = Field(default=True, description="Archive cards on error/cancel")


class WorkflowDef(BaseModel):
    """Top-level workflow definition — validated YAML content."""

    meta: WorkflowMeta = Field(description="Workflow metadata")
    vars: Dict[str, str] = Field(default_factory=dict, description="Template variables")
    steps: List[StepDef] = Field(min_length=1, description="Ordered step definitions")
    settings: WorkflowSettings = Field(default_factory=WorkflowSettings)


class PipelineStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"
    ERROR = "error"


class PipelineInstance(BaseModel):
    """A running pipeline instance — persisted in the state DB."""

    id: str = Field(description="Unique pipeline id (pipe_<uuid>)")
    template_name: str = Field(description="meta.name from workflow def")
    template_version: str = Field(description="meta.version from workflow def")
    status: PipelineStatus = Field(default=PipelineStatus.RUNNING)
    current_step_id: Optional[str] = Field(default=None)
    current_cycle: int = Field(default=1)
    max_cycles: int = Field(default=1)
    vars: Dict[str, str] = Field(default_factory=dict)
    step_outputs: Dict[str, Any] = Field(default_factory=dict)
    created_at: int = Field(description="Unix timestamp")
    updated_at: int = Field(description="Unix timestamp")
    error: Optional[str] = Field(default=None)
