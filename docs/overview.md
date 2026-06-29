# Hermes Workflow Plugin — Overview

## What Is It?

Hermes Workflow is a Hermes Agent plugin that lets you define multi-step
automation pipelines as YAML files, compile them into Hermes Kanban task
cards, and watch them execute step by step.

## Why Use It?

Hermes Kanban is excellent for individual task management and simple parent-child
dependencies, but it lacks:

- A concrete **pipeline definition language** (YAML with steps, loops, conditions)
- **Multi-step orchestration** (advance to the next step when all cards complete)
- **Review-fix cycles** (loop cards back through fix → re-review until passing)
- **External workflow sources** (load pipeline definitions from URLs or templates)

This plugin fills those gaps without reinventing kanban — it compiles to kanban
cards and uses the existing dispatcher, worker lifecycle, and dashboard.

## Who Is It For?

| Role | Use Case |
|------|----------|
| **Kanban orchestrator user** | You already use `kanban_create` to fan out work. This plugin lets you define the entire pipeline in YAML so you don't have to manually create cards for each step. |
| **Multi-profile pipeline runner** | You have writer, reviewer, and fix profiles and want a reproducible pipeline that can run unattended with automatic retry on failure. |
| **CI/CD or scheduled workflow user** | You want to trigger complex multi-step pipelines from cron or webhooks, with progress tracked in the kanban dashboard. |
| **Template author** | You want to share reusable pipeline templates with your team. |

## Core Concepts

### Pipeline

A **pipeline** is a single run of a workflow definition. Each pipeline has a
unique ID (`pipe_<uuid>`) and is tracked in the plugin's state database.

### Workflow Definition

A **workflow** is a YAML file describing the steps, variables, and settings for
a pipeline. Workflows can be loaded from:

- **URL**: `--url https://raw.github.com/.../pipeline.yaml`
- **Local file**: `--file ./my-pipeline.yaml`
- **Built-in template**: `--template review-loop`

### Step

A **step** is one unit of work in a pipeline. There are five types:

| Type | What It Does | How It Completes |
|------|--------------|-----------------|
| `script` | Runs a shell command | Command exits (subprocess) |
| `kanban` | Creates one or more kanban cards | All cards reach `done` status |
| `loop` | Repeats sub-steps | Condition met or max iterations |
| `noop` | Terminal marker | Instant |

### Card Fan-Out

A `kanban` step with `for_each` creates multiple parallel cards, one per
iteration item. Each card gets its own `{{ item }}` context.

### Step Advancement

After a step completes (all cards done / script exits / loop finishes), the
runner advances to the next step in the list — or jumps to a `goto` target
if the step specified `on_exit` branches.

### Review-Fix Loop

The review-loop template demonstrates the full cycle:

```
writer → reviewer → verify_discuss
                          │
                    exit_code == 0? ──→ done
                          │
                    else ──→ fix_loop
                              │
                              ├─ fix → re_review → check_again
                              │                         │
                              │                   exit_code == 0? ──→ done
                              │                         │
                              └────────── loop again ────┘
```

## Typical Workflow

```bash
# 1. Create your profile set (writer, reviewer, fixer)
hermes profile create writer --description "Documentation writer"
hermes profile create reviewer --description "Code/documentation reviewer"

# 2. Write a pipeline YAML
cat > my-pipeline.yaml << 'EOF'
meta:
  name: my-pipeline
  version: 1.0.0
steps:
  - id: writer
    type: kanban
    template:
      title: "Write docs"
      assignee: writer
      skill: interface-sk
  - id: reviewer
    type: kanban
    depends_on: [writer]
    template:
      title: "Review docs"
      assignee: reviewer
      skill: review-sk
  - id: done
    type: noop
    summary: "Done!"
EOF

# 3. Run it
hermes workflow run --file my-pipeline.yaml

# 4. Check progress
hermes workflow list
hermes workflow show pipe_<id>

# 5. After completion, review in kanban
hermes kanban list --status done
```

## Relationship to `hermes kanban swarm`

| | `swarm` | `workflow` plugin |
|---|---|---|
| Pipeline shape | Fixed: parallel workers → verifier → synthesizer | Arbitrary DAG with loops and conditions |
| Review-fix cycles | ❌ | ✅ |
| YAML definition | ❌ (CLI args only) | ✅ |
| External sources | ❌ | ✅ URL/file/template |
| Step advancement | ❌ one-shot | ✅ watcher + goto |
| When to use | Quick one-shot parallel workflows | Complex multi-step pipelines |

## Relationship to `hermes kanban decompose`

`decompose` is LLM-driven task decomposition (AI decides how to split work).
This plugin is deterministic (YAML defines the exact steps). Use decompose for
exploratory work, this plugin for repeatable pipelines.

## Installation

```bash
# From GitHub
hermes plugins install https://github.com/bitfrost7/hermes-workflow

# Local development
cd /path/to/hermes-workflow
hermes plugins install .

# Verify
hermes workflow --help
```

## Quick Start

```bash
# Run the built-in review-loop template
hermes workflow run --template review-loop --var service=apisvr
```
