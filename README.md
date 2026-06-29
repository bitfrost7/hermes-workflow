# Hermes Workflow Plugin

**YAML-defined kanban workflow pipelines for Hermes Agent.**

A plugin that compiles YAML workflow definitions into Hermes Kanban task cards with parent-child dependency chains, steps forward through the pipeline as cards complete, and supports review-fix loops with retry cycles.

> 🚧 **Version 0.1.0 — MVP**  
> Core features work: YAML loading (URL/file/template), kanban card creation with parent deps, step advancement, review-fix loops, and pipeline state tracking. Dashboard tab coming in v0.2.

## Quick Start

```bash
# 1. Install the plugin
hermes plugins install /path/to/hermes-workflow

# 2. Run a built-in template
hermes workflow run --template review-loop --var service=apisvr

# 3. Run from a URL
hermes workflow run \
  --url https://raw.github.com/bitfrost7/pipelines/main/deploy.yaml \
  --var env=staging

# 4. Run from a local file
hermes workflow run --file ./my-pipeline.yaml --var service=apisvr

# 5. See what's running
hermes workflow list
```

## Features

### YAML-Defined Pipelines

Define multi-step pipelines in YAML with five step types:

| Type | Purpose |
|------|---------|
| `script` | Run a deterministic shell command (analyze, verify) |
| `kanban` | Create one or more kanban task cards in parallel |
| `loop` | Repeat a block of steps with max-iterations protection |
| `noop` | Terminal marker with completion message |

### URL / Local File / Template Sources

Load pipeline definitions from anywhere:

- `--url https://...` — HTTP/S URL
- `--file ./pipeline.yaml` — Local file path
- `--template review-loop` — Built-in template (bundled with the plugin)

### Automatic Step Advancement

After all kanban cards in a step complete (or a script exits), the pipeline automatically advances to the next step. No manual promotion needed.

### Review-Fix Loop

The review-loop template demonstrates the Writer → Reviewer → Fix → Re-review cycle, with up to 10 retry cycles. The `on_exit` directive on script steps supports conditional jumps based on exit codes.

### Compatible with Hermes Kanban

- Uses `hermes kanban create --parent` for dependency chains
- Tags cards with structured comments for step tracking
- Fully compatible with the kanban dashboard, dispatcher, and worker lifecycle
- Existing kanban tools (`kanban_show`, `kanban_complete`, etc.) work unchanged

## Installation

```bash
# Via git URL
hermes plugins install git@github.com:bitfrost7/hermes-workflow.git

# Or local path during development
hermes plugins install /path/to/hermes-workflow

# Verify
hermes workflow list
```

## CLI Reference

### `hermes workflow run`

Run a pipeline from a YAML definition.

```bash
hermes workflow run \
  --url <URL> | --file <path> | --template <name> \
  [--var key=val ...] \
  [--board <slug>] \
  [--poll-interval <seconds>]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--url` | — | HTTP/S URL to workflow YAML |
| `--file` | — | Local workflow YAML file |
| `--template` | — | Built-in template name |
| `--var` | — | Pipeline variable (repeatable: `key=val`) |
| `--board` | `default` | Kanban board slug |
| `--poll-interval` | `15` | Step watcher poll interval (seconds) |

### `hermes workflow list`

List all pipeline instances (newest first).

```bash
hermes workflow list
```

### `hermes workflow show`

Show a pipeline instance with metadata.

```bash
hermes workflow show pipe_<id>
```

### `hermes workflow cancel`

Cancel a running pipeline. Archives all kanban cards created by this pipeline.

```bash
hermes workflow cancel pipe_<id>
```

### `hermes workflow gc`

Clean up old pipeline instances.

```bash
hermes workflow gc [--older-than <days>]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--older-than` | `7` | Delete instances older than N days |

### `hermes workflow templates`

List available built-in workflow templates.

```bash
hermes workflow templates
```

### `hermes workflow template show`

Show a template's YAML content.

```bash
hermes workflow template show review-loop
```

## Workflow YAML Reference

### Minimal Example

```yaml
meta:
  name: hello-pipeline
  version: 1.0.0
  description: "A minimal hello-world pipeline"

vars:
  GREETING: "Hello"

steps:
  - id: greet
    type: noop
    summary: "{{ GREETING }}, world!"
```

### Pipeline Structure

```yaml
meta:
  name: <string>             # Required — workflow template identifier
  version: <string>          # Default: 1.0.0
  description: <string>      # Optional

vars:
  <key>: <value>             # Template variables (overridden by --var)

settings:                    # Optional
  concurrency: 3             # Max parallel cards per profile
  poll_interval: 15          # Step watcher poll interval (seconds)
  timeout_per_step: 30m      # Per-step timeout
  cleanup_on_error: true     # Archive cards on error

steps:
  - id: <string>             # Required — unique step identifier
    type: <script|kanban|loop|noop>
    depends_on: [<step_id>]  # Optional — parent step dependencies
    output: <varname>        # Optional — capture output into context
    # ... type-specific fields ...
```

### Step Types

#### `script`

Runs a shell command. Output can be captured into a variable.

```yaml
- id: analyze
  type: script
  script: "bash bin/analyze.sh input.json"
  output: l1_json              # stdout captured as variable
  on_exit:                     # Conditional jump on exit code
    0:
      goto: done
    else:
      goto: fix_loop
```

#### `kanban`

Creates one or more kanban task cards. Supports for_each for fan-out.

```yaml
- id: writer
  type: kanban
  for_each: "{{ l1_json.actions }}"    # Iterate to create parallel cards
  template:
    title: "writer: interfaces/{{ item }}"
    assignee: writer
    skill: interface-sk
    workspace: "dir:{{ VAULT }}"
    body: |
      action={{ item }}
      output_dir={{ VAULT }}/output
  depends_on: [analyze]                # Parent dependency chain
```

#### `loop`

Repeats sub-steps with max iterations and optional while condition.

```yaml
- id: fix_loop
  type: loop
  max_iterations: 10
  while: "steps.verify.exit_code != 0"
  steps:
    - id: fix
      type: kanban
      template:
        title: "fix: {{ item }}"
        assignee: writer
        skill: fix-sk
    - id: re_review
      type: kanban
      depends_on: [fix]
      template:
        title: "re-review: {{ item }}"
        assignee: reviewer
        skill: review-sk
```

#### `noop`

Terminal step. Does nothing but provides a summary message.

```yaml
- id: done
  type: noop
  summary: "Pipeline complete for {{ SERVICE }}"
```

### Template Variables

Variables use `{{ var.name }}` syntax with dot-path resolution:

```yaml
vars:
  VAULT: "~/Documents/Code/work/mywiki"
  SERVICE: "{{ .service }}"       # CLI override via --var service=xxx

steps:
  - id: example
    type: noop
    summary: "Working on {{ SERVICE }} in {{ VAULT }}"
```

Available variable sources:
- `vars:` block from the YAML
- `--var key=val` from CLI (overrides YAML defaults)
- `output` from prior steps (e.g., `{{ l1_json.actions }}`)
- `{{ item }}` within a `for_each` iteration
- `{{ _cycle }}` within a loop step

## Built-in Templates

| Template | Description |
|----------|-------------|
| `review-loop` | Writer → Reviewer → Fix → Re-review with discuss-verify exit gating |

## Architecture

```
┌──────────────────────────────────────────────────┐
│              hermes workflow CLI                  │
├─────────┬──────────┬────────┬────────┬──────────┤
│  run    │  list    │  show  │ cancel │  gc      │
└────┬────┴──────────┴────────┴────────┴──────────┘
     │
     ▼
┌──────────────────────────────────────────────────┐
│               Plugin Core Layer                    │
├─────────────┬─────────────┬──────────────────────┤
│  loader.py  │ compiler.py │      runner.py        │
│  URL/file   │ YAML →      │      Step watcher     │
│  → YAML     │ kanban      │      + advancement    │
│  → schema   │ create cmd  │      + loop support   │
├─────────────┴─────────────┴──────────────────────┤
│  schema.py (Pydantic)  │  state.py (SQLite)       │
└──────────────────────────────────────────────────┘
     │
     ▼
┌──────────────────────────────────────────────────┐
│           Hermes Kanban Infrastructure            │
│  kanban.db  │  kanban_* tools  │  dispatcher     │
└──────────────────────────────────────────────────┘
```

### How It Works

1. **Load**: YAML from URL/file/template is parsed and validated against Pydantic schema.
2. **Compile**: Each step is converted into kanban cards with proper parent-child dependencies. `for_each` creates parallel fan-out cards. `workflow_template_id` and `current_step_key` metadata is embedded in cards.
3. **Run**: The runner monitors kanban state by polling card status. When all cards in a step complete, the pipeline advances to the next step. Loops repeat sub-steps up to `max_iterations`.
4. **State**: Pipeline instances are persisted in an isolated SQLite DB at `~/.hermes/workflow/state.db`.

## Development

### Project Structure

```
hermes-workflow/
├── manifest.json           # Plugin manifest
├── __init__.py             # Plugin entry point
├── cli.py                  # hermes workflow CLI commands
├── workflow/
│   ├── __init__.py
│   ├── schema.py           # Pydantic models
│   ├── loader.py           # YAML loading (URL/file/template)
│   ├── compiler.py         # YAML → kanban cards
│   ├── runner.py           # Step watcher + advancement
│   └── state.py            # Pipeline instance persistence
├── templates/
│   ├── review-loop.yaml    # Built-in review-fix template
├── docs/
│   ├── overview.md         # This document
│   ├── yaml-reference.md   # Full YAML schema reference
│   ├── cli-reference.md    # CLI commands in detail
│   └── architecture.md     # Design and internals
└── README.md
```

### Testing

```bash
# Validate schema loading
python3 -c "
from workflow.schema import WorkflowDef
from workflow.loader import load_from_template
wf = load_from_template('review-loop')
print(f'OK: {wf.meta.name} v{wf.meta.version}, {len(wf.steps)} steps')
"

# Test compiler (dry run — needs running hermes kanban)
python3 -c "
from workflow.compiler import _render_str
result = _render_str('hello {{ name }}', {'name': 'world'})
assert result == 'hello world', f'Expected hello world, got {result}'
print(f'Template rendering OK')
"
```

## License

MIT
