# Hermes Workflow Plugin — CLI Reference

## `hermes workflow run`

Run a pipeline from a YAML definition.

### Synopsis

```bash
hermes workflow run \
  (--url <URL> | --file <path> | --template <name>) \
  [--var <key>=<val> ...] \
  [--board <slug>] \
  [--poll-interval <seconds>]
```

### Examples

```bash
# Run from a built-in template
hermes workflow run --template review-loop --var service=apisvr

# Run from a GitHub raw URL
hermes workflow run \
  --url https://raw.githubusercontent.com/user/repo/main/pipeline.yaml \
  --var env=staging

# Run from a local YAML
hermes workflow run --file ./deploy.yaml --var cluster=prod

# Multiple variables
hermes workflow run --file ./pipeline.yaml \
  --var service=apisvr \
  --var VAULT=~/Documents/Code/work/mywiki

# Use a specific kanban board
hermes workflow run --template review-loop \
  --var service=apisvr \
  --board mywiki
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Pipeline completed successfully |
| 1 | Pipeline error or compilation failure |
| 130 | Interrupted (Ctrl-C) |

## `hermes workflow list`

List pipeline instances, newest first.

### Synopsis

```bash
hermes workflow list
```

### Output

```
ID                       TEMPLATE                 STEP                 STATUS       AGE
pipe_abc12345abc         review-loop              verify               running      12m
pipe_def67890def         hello-pipeline           done                 done         3h
```

## `hermes workflow show`

Show a pipeline instance with metadata.

### Synopsis

```bash
hermes workflow show <pipeline_id>
```

### Example

```bash
$ hermes workflow show pipe_abc12345abc
Pipeline:    pipe_abc12345abc
Template:    review-loop v1.0.0
Status:      running
Step:        verify
Cycle:       1
Created:     12m ago
Updated:     30s ago
```

## `hermes workflow cancel`

Cancel a running pipeline. Updates pipeline status to `cancelled` and archives
all kanban cards created by this pipeline.

### Synopsis

```bash
hermes workflow cancel <pipeline_id>
```

### Notes

- Only cancels `running` pipelines
- Does NOT affect already-completed cards
- Cards are archived (recoverable from dashboard with "show archived")
- To resume a cancelled pipeline, re-run with the same arguments

## `hermes workflow gc`

Clean up old pipeline instances from the state DB.

### Synopsis

```bash
hermes workflow gc [--older-than <days>]
```

### Default

`--older-than 7` — deletes instances older than 7 days. Does NOT affect kanban
cards (those are managed by `hermes kanban gc`).

## `hermes workflow templates`

List available built-in workflow templates.

### Synopsis

```bash
hermes workflow templates
```

### Output

```
NAME                     VERSION    DESCRIPTION
review-loop              1.0.0      Writer → Reviewer → Fix → Re-review pipeline with up to 10 fix cycles

Use: hermes workflow run --template <name>
```

## `hermes workflow template show`

Show a template's YAML content.

### Synopsis

```bash
hermes workflow template show <name>
```

### Example

```bash
hermes workflow template show review-loop
```
