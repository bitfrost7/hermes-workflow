# Hermes Workflow Plugin — YAML Schema Reference

## Top-Level Structure

```yaml
meta:
  name: <string>          # Required, ≤64 chars, kebab-case
  version: <string>       # Optional, default "1.0.0"
  description: <string>   # Optional

vars:
  <key>: <value>          # Template variables

settings:                 # Optional
  concurrency: <int>      # Default: 3
  poll_interval: <int>    # Default: 15 (seconds)
  timeout_per_step: <str> # Default: "30m"
  cleanup_on_error: <bool> # Default: true

steps:                    # Required, ≥1 step
  - id: <string>
    type: <step_type>
    # ...
```

## Step Types

### ScriptStep

Runs a shell command. The output can be captured into a template variable for downstream steps.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `id` | string | ✅ | — | Unique step identifier |
| `type` | string | ✅ | — | Must be `"script"` |
| `script` | string | ✅ | — | Shell command (supports `{{ var }}` templates) |
| `depends_on` | list[string] | ❌ | `[]` | Parent step ids |
| `output` | string | ❌ | — | Variable name for captured stdout |
| `on_exit` | map | ❌ | — | Exit code → goto mapping |

**`on_exit` format**:
```yaml
on_exit:
  <exit_code>:          # e.g., "0", "1", or "else" for default
    goto: <step_id>     # Jump target step id
```

### KanbanStep

Creates kanban task cards. With `for_each`, creates one card per iteration item.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `id` | string | ✅ | — | Unique step identifier |
| `type` | string | ✅ | — | Must be `"kanban"` |
| `depends_on` | list[string] | ❌ | `[]` | Parent step ids (→ `--parent`) |
| `for_each` | string | ❌ | — | `{{ expr }}` to iterate over (e.g., `{{ l1_json.actions }}`) |
| `template` | object | ✅ | — | Card template (see below) |

**`template` fields**:

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `title` | string | ✅ | — | Card title (supports `{{ var }}`) |
| `assignee` | string | ✅ | — | Profile name |
| `skill` | string | ❌ | — | Skill to force-load (`--skill`) |
| `workspace` | string | ❌ | `scratch` | Workspace type |
| `body` | string | ❌ | — | Card body (supports `{{ var }}`) |

### LoopStep

Repeats a block of sub-steps.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `id` | string | ✅ | — | Unique step identifier |
| `type` | string | ✅ | — | Must be `"loop"` |
| `max_iterations` | int | ❌ | `10` | Maximum loop iterations (safety valve) |
| `while` | string | ❌ | — | Condition expression to continue looping |
| `steps` | list[step] | ✅ | — | Sub-steps to repeat |

**`while` condition format**:

```
steps.<step_id>.exit_code != 0
steps.<step_id>.exit_code == 0
```

### NoopStep

Terminal step with no action.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `id` | string | ✅ | — | Unique step identifier |
| `type` | string | ✅ | — | Must be `"noop"` |
| `depends_on` | list[string] | ❌ | `[]` | Not typically used for noop |
| `summary` | string | ❌ | — | Completion message |

## Template Expressions

Expressions use `{{ expression }}` syntax with dot-path resolution into the
variable context.

### Variable Sources

| Source | Example | Description |
|--------|---------|-------------|
| `vars:` block | `{{ VAULT }}` | YAML-defined vars |
| `--var` CLI | `{{ SERVICE }}` | CLI overrides |
| Step output | `{{ l1_json.actions }}` | Output from prior `output:` step |
| Loop item | `{{ item }}` | Current for_each iteration value |
| Loop cycle | `{{ _cycle }}` | Current loop iteration number |

### Dot-Path Resolution

| Expression | Resolves to |
|-----------|-------------|
| `{{ VAULT }}` | `vars["VAULT"]` |
| `{{ l1_json.actions }}` | `step_outputs["l1_json"]["actions"]` |
| `{{ l1_json.actions.0 }}` | `l1_json["actions"][0]` |

### Literals

| Expression | Resolves to |
|-----------|-------------|
| `42` | `42` (int) |
| `true` | `True` |
| `false` | `False` |

## Built-in Variables

The following variables are auto-injected into every pipeline:

| Variable | Source | Description |
|----------|--------|-------------|
| `_cycle` | Loop step | Current iteration (1-based) |
| `item` | for_each | Current iteration value |
| `<step_id>.exit_code` | Script step | Exit code of completed script |

## Workflow Validation Rules

| Rule | Error |
|------|-------|
| Step ids must be unique | `Duplicate step id: 'xxx'` |
| `depends_on` must reference existing steps | `Step 'xxx' depends_on 'yyy' but no such step exists` |
| At least one step required | List validation |
| Step type must be valid | Pydantic discriminated union |

## Complete Example

```yaml
meta:
  name: code-doc-pipeline
  version: 1.0.0
  description: "Analyze code → generate docs → review → fix cycle"

vars:
  VAULT: "~/Documents/Code/work/mywiki"
  SERVICE: "{{ .service }}"

settings:
  concurrency: 3
  poll_interval: 15
  cleanup_on_error: true

steps:
  - id: analyze
    type: script
    script: |
      python3 bin/analyze.py \
        raw/ast/{{ SERVICE }}/graph.json
    output: l1_json

  - id: writer
    type: kanban
    depends_on: [analyze]
    for_each: "{{ l1_json.actions }}"
    template:
      title: "writer: interfaces/{{ item }}"
      assignee: writer
      skill: interface-sk
      workspace: "dir:{{ VAULT }}"
      body: |
        action={{ item }}
        output_dir={{ VAULT }}/Wiki/privatelink/{{ SERVICE }}

  - id: reviewer
    type: kanban
    depends_on: [writer]
    for_each: "{{ l1_json.actions }}"
    template:
      title: "review: interfaces/{{ item }}"
      assignee: reviewer
      skill: review-sk
      workspace: "dir:{{ VAULT }}"

  - id: verify
    type: script
    depends_on: [reviewer]
    script: "bin/verify.sh {{ VAULT }}/review/{{ SERVICE }}"
    on_exit:
      0:
        goto: done
      else:
        goto: fix_loop

  - id: fix_loop
    type: loop
    max_iterations: 10
    while: "steps.verify.exit_code != 0"
    steps:
      - id: fix
        type: kanban
        template:
          title: "fix: {{ SERVICE }}"
          assignee: writer
          skill: fix-sk

      - id: re_review
        type: kanban
        depends_on: [fix]
        template:
          title: "re-review: {{ SERVICE }}"
          assignee: reviewer
          skill: review-sk

      - id: check_again
        type: script
        depends_on: [re_review]
        script: "bin/verify.sh {{ VAULT }}/review/{{ SERVICE }}"
        on_exit:
          0:
            goto: done

  - id: done
    type: noop
    summary: "Pipeline complete for {{ SERVICE }}"
```
