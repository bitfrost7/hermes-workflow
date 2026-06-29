# Hermes Workflow Plugin — Architecture

## Design Principles

### Deterministic Over AI-Driven

Workflow steps are explicit YAML declarations, not LLM-generated plan. The pipeline knows exactly which steps to run and in what order. The only AI involvement is what you explicitly delegate to kanban worker profiles (writer, reviewer, etc.) through card creation.

### Use Kanban, Don't Replace It

The plugin does not replace or bypass Hermes Kanban — it compiles to kanban cards and lets the existing dispatcher, worker lifecycle, and dashboard handle execution. The plugin only observes and advances.

### Minimal Dependencies

The plugin uses only:
- Python stdlib (`subprocess`, `urllib`, `sqlite3`, `json`, `re`, `uuid`)
- `PyYAML` (for YAML parsing — already installed in Hermes env)
- `pydantic` (for schema validation — already installed in Hermes env)

No external workflow engines (Temporal, Prefect, Airflow) are needed.

## Component Architecture

### loader.py

**Responsibility**: Parse YAML from any source and validate against Pydantic schema.

```
Source (URL / file / template)
    │
    ▼
urllib / open()
    │
    ▼
yaml.safe_load()
    │
    ▼
WorkflowDef.model_validate()     ← Pydantic validation
    │
    ▼
Validated WorkflowDef object
```

**Key design decisions**:
- Validates at load time, before any kanban operations
- Rejects duplicate step ids and unresolved `depends_on` references
- Auto-detects source type: URL starts with `http`, file path exists, else try template

### compiler.py

**Responsibility**: Convert a validated WorkflowDef into kanban cards and run script steps.

**Key design decisions**:
- Script steps run during compile phase (they produce deterministic output like `l1_json` needed by downstream kanban steps)
- Kanban steps create cards via `hermes kanban create --parent`
- `for_each` iterates over a list expression (e.g., `{{ l1_json.actions }}`) and creates one card per item
- Template variables use a simple `{{ var.key }}` dot-path substitution (not full Jinja2 — avoids an external dependency)
- Cards are tagged with workflow metadata (template name + step key) via structured comments

### runner.py

**Responsibility**: Watch pipeline steps and advance through them.

**Key design decisions**:
- Runs synchronously in the foreground (blocks until pipeline completes)
- Polls `hermes kanban show` for card status
- Handles `on_exit` conditional jumps (goto) from script exit codes
- Loops run sub-steps up to `max_iterations`, respecting `while` conditions
- Ctrl-C triggers cleanup: archives all created cards

### state.py

**Responsibility**: Persist pipeline instance metadata.

**Key design decisions**:
- Separate SQLite DB from kanban (`~/.hermes/workflow/state.db`)
- Tracks pipeline: template name, current step, current cycle, step outputs, error
- Does NOT duplicate kanban state (card status lives in `kanban.db`)
- Pipeline instances are GC'd after configurable TTL (default 7 days)

## Data Flow

```
hermes workflow run
    │
    ▼
loader.load(source) ──────────────────┐
    │                                  │
    ▼                                  │
WorkflowDef (Pydantic validated)       │
    │                                  │
    ▼                                  │
compiler.compile_workflow()            │
    │                                  │
    ├─ ScriptStep: subprocess.run()    │
    ├─ KanbanStep: hermes kanban create │
    ├─ LoopStep:   iterate sub-steps   │
    └─ NoopStep:   (no-op)            │
    │                                  │
    ▼                                  │
state.create_pipeline_instance()       │
    │                                  │
    ▼                                  │
runner.run_pipeline()                  │
    │                                  │
    ├─ Loop: for each step:           │
    │   ├─ KanbanStep: poll cards      │
    │   ├─ LoopStep: sub-steps + poll │
    │   └─ on_exit: conditional goto  │
    │                                  │
    ▼                                  │
state.set_pipeline_status(DONE) ──────┘
```

## Kanban Integration Points

| Point | How |
|-------|-----|
| Card creation | `hermes kanban create --parent --assignee --skill --workspace --body --json` |
| Card tagging | Structured comment: `{"_wf_template": "name", "_wf_step": "step_id"}` |
| Card polling | `hermes kanban show <id>` → parse `status:` line |
| Card cleanup | `hermes kanban archive <id>` |
| Board isolation | `--board <slug>` via hermes CLI |
| Dispatcher | Cards are auto-promoted when parent cards complete |

## Future Directions

### v0.2 — Dashboard Tab
- Pipeline visualisation (DAG of steps, current position, card states)
- Per-pipeline progress bar
- Cancel/rerun buttons

### v0.3 — Kanban DB Direct Access
- Write `workflow_template_id` and `current_step_key` directly to `kanban.db` tasks table (bypasses comment convention)
- Use `step_key` in `task_runs` table (v2 schema compatibility)

### v0.4 — Pipeline Chaining
- `context_from` between pipelines (output of pipeline A feeds pipeline B)
- Cron-triggered pipelines

### v1.0 — Hermes Core Integration
- Proposal: merge YAML workflow schema into `kanban_db.create_task()` as first-class `workflow_template_id` support
- Dispatcher natively advances `current_step_key`
