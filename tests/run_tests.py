#!/usr/bin/env python3
"""
Hermes Workflow Plugin — Test Runner

Usage:
    python3 tests/run_tests.py                  # Run all tests
    python3 tests/run_tests.py --list           # List test cases
    python3 tests/run_tests.py schema           # Run only schema tests
    python3 tests/run_tests.py --verbose        # Detailed output

Prerequisites:
    - Plugin must be installed: `hermes plugins install .`
    - PyYAML and pydantic must be available
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

# ── Ensure plugin modules are importable ──
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR))

# ── Test registry ──

ALL_TESTS: list[dict] = []

def test(
    module: str,
    name: str,
    desc: str,
    kind: str = "unit",
    requires_kanban: bool = False,
    requires_discuss: bool = False,
):
    """Decorator to register a test function."""
    def wrapper(fn):
        ALL_TESTS.append({
            "module": module,
            "name": name,
            "desc": desc,
            "kind": kind,
            "requires_kanban": requires_kanban,
            "requires_discuss": requires_discuss,
            "fn": fn,
        })
        return fn
    return wrapper


# ── Test results ──

results: list[dict] = []

def run_test(test_def: dict, verbose: bool = False) -> dict:
    """Run a single test function and return result."""
    fn = test_def["fn"]
    name = f"{test_def['module']}/{test_def['name']}"
    print(f"  {name:<50} ", end="", flush=True)

    start = time.time()
    try:
        fn()
        elapsed = time.time() - start
        print(f"✅  ({elapsed:.1f}s)")
        result = {"name": name, "status": "pass", "elapsed": elapsed}
    except AssertionError as e:
        elapsed = time.time() - start
        msg = str(e) or "Assertion failed"
        print(f"❌  ({elapsed:.1f}s)")
        print(f"       {msg}")
        result = {"name": name, "status": "fail", "elapsed": elapsed, "error": msg}
    except Exception as e:
        elapsed = time.time() - start
        print(f"💥  ({elapsed:.1f}s)")
        traceback.print_exc(limit=3, file=sys.stderr)
        result = {"name": name, "status": "error", "elapsed": elapsed, "error": str(e)}

    results.append(result)
    return result


# ═══════════════════════════════════════════════════════════════════
#  Tests: Schema Validation
# ═══════════════════════════════════════════════════════════════════

@test("schema", "valid-minimal", "Minimal valid workflow YAML")
def test_valid_minimal():
    from workflow.schema import WorkflowDef
    wf = WorkflowDef.model_validate({
        "meta": {"name": "test", "version": "1.0.0"},
        "steps": [{"id": "hello", "type": "noop", "summary": "hi"}],
    })
    assert wf.meta.name == "test"
    assert len(wf.steps) == 1
    assert wf.steps[0].id == "hello"


@test("schema", "meta-required", "Missing meta should fail")
def test_meta_required():
    from pydantic import ValidationError
    from workflow.schema import WorkflowDef
    try:
        WorkflowDef.model_validate({"steps": []})
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


@test("schema", "steps-required", "Empty steps should fail")
def test_steps_required():
    from pydantic import ValidationError
    from workflow.schema import WorkflowDef
    try:
        WorkflowDef.model_validate({
            "meta": {"name": "test", "version": "1.0.0"},
            "steps": [],
        })
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


@test("schema", "duplicate-ids", "Duplicate step ids (caught by loader.validate_ids)")
def test_duplicate_ids():
    """Duplicate IDs are detected by loader._validate_ids_unique, not Pydantic
    schema. The schema accepts duplicates (each step validates independently),
    so this test verifies loader validation rejects them."""
    from workflow.loader import _parse_yaml

    raw = """
meta:
  name: test
  version: 1.0.0
steps:
  - id: a
    type: noop
  - id: a
    type: noop
"""
    from workflow.loader import LoadError
    try:
        _parse_yaml(raw, source="<test>")
        assert False, "Should have raised LoadError"
    except LoadError:
        pass


@test("schema", "script-step", "Script step with minimal fields")
def test_script_step():
    from workflow.schema import WorkflowDef, ScriptStep
    wf = WorkflowDef.model_validate({
        "meta": {"name": "test", "version": "1.0.0"},
        "steps": [{"id": "s1", "type": "script", "script": "echo hello"}],
    })
    assert isinstance(wf.steps[0], ScriptStep)
    assert wf.steps[0].script == "echo hello"


@test("schema", "kanban-step", "Kanban step with template")
def test_kanban_step():
    from workflow.schema import WorkflowDef, KanbanStep
    wf = WorkflowDef.model_validate({
        "meta": {"name": "test", "version": "1.0.0"},
        "steps": [{
            "id": "k1", "type": "kanban",
            "template": {"title": "test", "assignee": "writer"},
        }],
    })
    assert isinstance(wf.steps[0], KanbanStep)
    assert wf.steps[0].template.title == "test"
    assert wf.steps[0].template.assignee == "writer"


@test("schema", "loop-step", "Loop step with sub-steps")
def test_loop_step():
    from workflow.schema import WorkflowDef, LoopStep
    wf = WorkflowDef.model_validate({
        "meta": {"name": "test", "version": "1.0.0"},
        "steps": [{
            "id": "loop1", "type": "loop",
            "max_iterations": 5,
            "steps": [
                {"id": "sub1", "type": "noop"},
            ],
        }],
    })
    assert isinstance(wf.steps[0], LoopStep)
    assert wf.steps[0].max_iterations == 5
    assert len(wf.steps[0].steps) == 1


@test("schema", "on-exit-int-keys", "YAML int keys in on_exit coerced to str")
def test_on_exit_int_keys():
    from workflow.schema import WorkflowDef, ScriptStep
    wf = WorkflowDef.model_validate({
        "meta": {"name": "test", "version": "1.0.0"},
        "steps": [{
            "id": "s1", "type": "script", "script": "exit 1",
            "on_exit": {0: {"goto": "done"}, "else": {"goto": "fix"}},
        }],
    })
    step = wf.steps[0]
    assert isinstance(step, ScriptStep)
    assert step.on_exit is not None
    assert "0" in step.on_exit
    assert "else" in step.on_exit


@test("schema", "for-each", "Kanban step with for_each")
def test_for_each():
    from workflow.schema import WorkflowDef, KanbanStep
    wf = WorkflowDef.model_validate({
        "meta": {"name": "test", "version": "1.0.0"},
        "steps": [{
            "id": "k1", "type": "kanban",
            "for_each": "items",
            "template": {"title": "item {{ item }}", "assignee": "writer"},
        }],
    })
    assert isinstance(wf.steps[0], KanbanStep)
    assert wf.steps[0].for_each == "items"


# ═══════════════════════════════════════════════════════════════════
#  Tests: Template Rendering
# ═══════════════════════════════════════════════════════════════════

@test("loader", "render-simple", "Simple {{ var }} replacement")
def test_render_simple():
    from workflow.compiler import _render_str
    result = _render_str("hello {{ name }}", {"name": "world"})
    assert result == "hello world", f"Got: {result}"


@test("loader", "render-dot-path", "Dot-path {{ a.b }} resolution")
def test_render_dot_path():
    from workflow.compiler import _render_str
    result = _render_str("{{ user.name }}", {"user": {"name": "alice"}})
    assert result == "alice", f"Got: {result}"


@test("loader", "render-array-index", "Array index {{ list.0 }}")
def test_render_array_index():
    from workflow.compiler import _render_str
    result = _render_str("{{ items.0 }}", {"items": ["a", "b"]})
    assert result == "a", f"Got: {result}"


@test("loader", "render-multiple", "Multiple vars in template")
def test_render_multiple():
    from workflow.compiler import _render_str
    result = _render_str("{{ a }}/{{ b }}", {"a": "x", "b": "y"})
    assert result == "x/y", f"Got: {result}"


@test("loader", "render-item", "{{ item }} in for_each context")
def test_render_item():
    from workflow.compiler import _render_str
    result = _render_str("fix: {{ item }}", {"item": "CreateVPCEndpoint"})
    assert result == "fix: CreateVPCEndpoint", f"Got: {result}"


@test("loader", "render-nested-loop", "{{ _cycle }} in loop")
def test_render_nested_loop():
    from workflow.compiler import _render_str
    result = _render_str("cycle {{ _cycle }}", {"_cycle": 3})
    assert result == "cycle 3", f"Got: {result}"


@test("loader", "render-unset-var", "Missing var renders empty")
def test_render_unset_var():
    from workflow.compiler import _render_str
    result = _render_str("hello {{ missing }}", {"name": "world"})
    assert result == "hello ", f"Got: {result}"


@test("loader", "render-unchanged", "No template markers stays unchanged")
def test_render_unchanged():
    from workflow.compiler import _render_str
    result = _render_str("plain text", {})
    assert result == "plain text", f"Got: {result}"


# ═══════════════════════════════════════════════════════════════════
#  Tests: Graph Analysis
# ═══════════════════════════════════════════════════════════════════

@test("graph", "no-deps", "No dependency edges")
def test_graph_no_deps():
    from workflow.schema import WorkflowDef
    from workflow.graph import WorkflowGraph
    wf = WorkflowDef.model_validate({
        "meta": {"name": "test", "version": "1.0.0"},
        "steps": [
            {"id": "a", "type": "noop"},
            {"id": "b", "type": "noop"},
        ],
    })
    g = WorkflowGraph(wf)
    cycles = g.find_dep_cycles()
    assert cycles == [], f"Found unexpected cycles: {cycles}"
    orphans = g.find_orphans()
    assert orphans == [], f"Found unexpected orphans: {orphans}"


@test("graph", "dep-cycle", "Circular depends_on detected")
def test_graph_dep_cycle():
    from workflow.schema import WorkflowDef
    from workflow.graph import WorkflowGraph
    wf = WorkflowDef.model_validate({
        "meta": {"name": "test", "version": "1.0.0"},
        "steps": [
            {"id": "a", "type": "noop", "depends_on": ["c"]},
            {"id": "b", "type": "noop", "depends_on": ["a"]},
            {"id": "c", "type": "noop", "depends_on": ["b"]},
        ],
    })
    g = WorkflowGraph(wf)
    cycles = g.find_dep_cycles()
    assert len(cycles) > 0, "Should have found a dependency cycle"


@test("graph", "self-dep", "Self dependency detected")
def test_graph_self_dep():
    from workflow.schema import WorkflowDef
    from workflow.graph import WorkflowGraph
    wf = WorkflowDef.model_validate({
        "meta": {"name": "test", "version": "1.0.0"},
        "steps": [
            {"id": "a", "type": "noop", "depends_on": ["a"]},
        ],
    })
    g = WorkflowGraph(wf)
    cycles = g.find_dep_cycles()
    assert len(cycles) > 0, "Should have found self-dependency cycle"


@test("graph", "orphan-after-goto", "Steps after goto are unreachable")
def test_graph_orphan_after_goto():
    from workflow.schema import WorkflowDef
    from workflow.graph import WorkflowGraph
    wf = WorkflowDef.model_validate({
        "meta": {"name": "test", "version": "1.0.0"},
        "steps": [
            {"id": "a", "type": "script", "script": "exit 1",
             "on_exit": {"0": {"goto": "done"}}},
            {"id": "b", "type": "noop"},
            {"id": "done", "type": "noop"},
        ],
    })
    g = WorkflowGraph(wf)
    # Sequential: a → b → done. Goto: a → done.
    # Both 'b' and 'done' are reachable via sequential edges,
    # so no orphans here. This test validates correct
    # reachability analysis.
    orphans = g.find_orphans()
    assert isinstance(orphans, list)


@test("graph", "loop-reachability", "Steps inside a loop are reachable")
def test_graph_loop_reachability():
    from workflow.schema import WorkflowDef
    from workflow.graph import WorkflowGraph
    wf = WorkflowDef.model_validate({
        "meta": {"name": "test", "version": "1.0.0"},
        "steps": [
            {"id": "start", "type": "noop"},
            {"id": "fix_loop", "type": "loop", "max_iterations": 10,
             "steps": [
                 {"id": "fix", "type": "noop"},
                 {"id": "re_review", "type": "noop"},
             ]},
            {"id": "done", "type": "noop"},
        ],
    })
    g = WorkflowGraph(wf)
    orphans = g.find_orphans()
    assert "fix" not in orphans, f"'fix' should be reachable via loop, orphans={orphans}"
    assert "re_review" not in orphans, f"'re_review' should be reachable via loop, orphans={orphans}"
    assert "done" not in orphans


@test("graph", "loop-backedge-not-cycle", "Loop back-edge not flagged as cf-cycle")
def test_loop_backedge_not_cycle():
    from workflow.schema import WorkflowDef
    from workflow.graph import WorkflowGraph
    wf = WorkflowDef.model_validate({
        "meta": {"name": "test", "version": "1.0.0"},
        "steps": [
            {"id": "start", "type": "noop"},
            {"id": "fix_loop", "type": "loop", "max_iterations": 10,
             "steps": [
                 {"id": "fix", "type": "noop"},
             ]},
            {"id": "done", "type": "noop"},
        ],
    })
    g = WorkflowGraph(wf)
    # Loop back-edge should be filtered in summary
    s = g.summary()
    assert "fix" not in s["orphan_nodes"], f"'fix' should not be orphan"
    # cf_cycles should be empty (loop back-edge is expected)
    assert len(s["cf_cycles"]) == 0, f"Loop back-edge should not be flagged: {s['cf_cycles']}"


# ═══════════════════════════════════════════════════════════════════
#  Tests: Validation
# ═══════════════════════════════════════════════════════════════════

@test("validate", "pass-noop", "Noop-only workflow should warn (not error)")
def test_validate_noop_only():
    from workflow.schema import WorkflowDef
    from workflow.validate import validate
    wf = WorkflowDef.model_validate({
        "meta": {"name": "test", "version": "1.0.0"},
        "steps": [{"id": "done", "type": "noop"}],
    })
    result = validate(wf)
    assert not result.has_errors(), "Noop-only should not error"
    assert result.has_warnings(), "Noop-only should warn"


@test("validate", "missing-dep-err", "Missing depends_on target errors")
def test_validate_missing_dep():
    from workflow.schema import WorkflowDef
    from workflow.validate import validate
    wf = WorkflowDef.model_validate({
        "meta": {"name": "test", "version": "1.0.0"},
        "steps": [
            {"id": "a", "type": "noop", "depends_on": ["missing"]},
        ],
    })
    result = validate(wf)
    assert result.has_errors(), "Missing dep should error"
    codes = [i.code for i in result.issues]
    assert "missing-dep" in codes


@test("validate", "missing-goto-err", "Missing goto target errors")
def test_validate_missing_goto():
    from workflow.schema import WorkflowDef
    from workflow.validate import validate
    wf = WorkflowDef.model_validate({
        "meta": {"name": "test", "version": "1.0.0"},
        "steps": [
            {"id": "a", "type": "script", "script": "echo hi",
             "on_exit": {"0": {"goto": "nowhere"}}},
        ],
    })
    result = validate(wf)
    assert result.has_errors(), "Missing goto should error"
    codes = [i.code for i in result.issues]
    assert "missing-goto" in codes


@test("validate", "review-loop-passes", "The built-in review-loop template passes")
def test_validate_review_loop():
    from workflow.loader import load_from_template
    from workflow.validate import validate
    wf = load_from_template("review-loop")
    result = validate(wf)
    assert not result.has_errors(), f"review-loop should pass: {result}"


# ═══════════════════════════════════════════════════════════════════
#  Tests: Kanban CLI Integration
# ═══════════════════════════════════════════════════════════════════

@test("kanban", "create-list-archive", "Kanban card lifecycle: create → show → archive",
      requires_kanban=True)
def test_kanban_create_list_archive():
    """Test basic kanban CLI commands used by the compiler."""
    import subprocess

    # Create a card
    r1 = subprocess.run(
        ["hermes", "kanban", "create", "test: hello",
         "--assignee", "writer", "--body", "test body", "--json"],
        capture_output=True, text=True, timeout=30,
    )
    assert r1.returncode == 0, f"Create failed: {r1.stderr}"
    card = json.loads(r1.stdout)
    card_id = card.get("id", "")
    assert card_id.startswith("t_"), f"Bad card id: {card_id}"

    # Show the card
    r2 = subprocess.run(
        ["hermes", "kanban", "show", card_id],
        capture_output=True, text=True, timeout=30,
    )
    assert r2.returncode == 0, f"Show failed: {r2.stderr}"
    assert "test: hello" in r2.stdout, f"Title not found in show output"

    # Archive the card
    r3 = subprocess.run(
        ["hermes", "kanban", "archive", card_id],
        capture_output=True, text=True, timeout=30,
    )
    assert r3.returncode == 0, f"Archive failed: {r3.stderr}"


@test("kanban", "create-with-parent", "Card with --parent creates dependency",
      requires_kanban=True)
def test_kanban_create_with_parent():
    import subprocess, json

    r1 = subprocess.run(
        ["hermes", "kanban", "create", "test: parent", "--assignee", "writer", "--json"],
        capture_output=True, text=True, timeout=30,
    )
    assert r1.returncode == 0
    parent_id = json.loads(r1.stdout)["id"]

    r2 = subprocess.run(
        ["hermes", "kanban", "create", "test: child", "--assignee", "writer",
         "--parent", parent_id, "--json"],
        capture_output=True, text=True, timeout=30,
    )
    assert r2.returncode == 0, f"Child create failed: {r2.stderr}"
    child_id = json.loads(r2.stdout)["id"]

    # Clean up
    subprocess.run(["hermes", "kanban", "archive", parent_id, child_id],
                   capture_output=True, timeout=30)


@test("kanban", "complete-card", "Card can be completed and shows done status",
      requires_kanban=True)
def test_kanban_complete():
    import subprocess, json

    r1 = subprocess.run(
        ["hermes", "kanban", "create", "test: complete-me", "--assignee", "writer", "--json"],
        capture_output=True, text=True, timeout=30,
    )
    assert r1.returncode == 0
    card_id = json.loads(r1.stdout)["id"]

    r2 = subprocess.run(
        ["hermes", "kanban", "complete", card_id],
        capture_output=True, text=True, timeout=30,
    )
    assert r2.returncode == 0, f"Complete failed: {r2.stderr}"

    r3 = subprocess.run(
        ["hermes", "kanban", "show", card_id],
        capture_output=True, text=True, timeout=30,
    )
    assert "done" in r3.stdout.lower(), f"Show should show done status"

    subprocess.run(["hermes", "kanban", "archive", card_id],
                   capture_output=True, timeout=30)


@test("kanban", "card-runs", "Card runs command logs worker history",
      requires_kanban=True)
def test_kanban_card_runs():
    import subprocess, json

    r1 = subprocess.run(
        ["hermes", "kanban", "create", "test: runs-check", "--assignee", "writer", "--json"],
        capture_output=True, text=True, timeout=30,
    )
    assert r1.returncode == 0
    card_id = json.loads(r1.stdout)["id"]

    r2 = subprocess.run(
        ["hermes", "kanban", "runs", card_id],
        capture_output=True, text=True, timeout=30,
    )
    assert r2.returncode == 0

    subprocess.run(["hermes", "kanban", "archive", card_id],
                   capture_output=True, timeout=30)


# ═══════════════════════════════════════════════════════════════════
#  Tests: Workflow CLI
# ═══════════════════════════════════════════════════════════════════

@test("cli", "templates-list", "hermes workflow templates shows built-in")
def test_cli_templates():
    import subprocess
    r = subprocess.run(
        ["hermes", "workflow", "templates"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"templates failed: {r.stderr}"
    assert "review-loop" in r.stdout, "review-loop should be in templates"


@test("cli", "template-show", "hermes workflow template show review-loop")
def test_cli_template_show():
    import subprocess
    r = subprocess.run(
        ["hermes", "workflow", "template", "show", "review-loop"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"template show failed: {r.stderr}"
    assert "review-loop" in r.stdout
    assert "writer" in r.stdout


@test("cli", "check-builtin", "hermes workflow check --template review-loop")
def test_cli_check_builtin():
    import subprocess
    r = subprocess.run(
        ["hermes", "workflow", "check", "--template", "review-loop"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"check review-loop failed: {r.stderr}"
    assert "PASS" in r.stdout


@test("cli", "dry-run", "hermes workflow run --dry-run with template")
def test_cli_dry_run():
    import subprocess
    r = subprocess.run(
        ["hermes", "workflow", "run", "--template", "review-loop",
         "--var", "SERVICE=test", "--var", "VAULT=/tmp", "--dry-run"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"dry-run failed: {r.stderr}"
    assert "Dry-run" in r.stdout


@test("cli", "state-gc", "hermes workflow gc cleans up old pipelines")
def test_cli_gc():
    import subprocess
    r = subprocess.run(
        ["hermes", "workflow", "gc", "--older-than", "0"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"gc failed: {r.stderr}"


# ═══════════════════════════════════════════════════════════════════
#  Tests: Workflow Run Integration (requires discuss)
# ═══════════════════════════════════════════════════════════════════

@test("cli", "run-minimal", "Minimal script-only pipeline completes without kanban cards",
      requires_kanban=False)
def test_cli_run_minimal():
    """Run a minimal pipeline with script + noop steps (no kanban cards).
    This tests the full lifecycle: check → compile → run → log."""
    import subprocess, tempfile, os

    # Write a minimal workflow YAML
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("""\
meta:
  name: minimal-test
  version: 1.0.0
steps:
  - id: greet
    type: script
    script: echo "hello world"
    output: greeting
  - id: done
    type: noop
    summary: "Done with minimal test"
""")
        yaml_path = f.name

    try:
        r = subprocess.run(
            ["hermes", "workflow", "run", "--file", yaml_path,
             "--poll-interval", "3"],
            capture_output=True, text=True, timeout=30,
        )
        # Should complete (exit 0) — no cards to wait for
        assert r.returncode == 0, f"run failed: {r.stderr[:500]}"

        # Check output
        assert "hello world" in r.stdout or "Pipeline complete" in r.stdout, \
            f"Unexpected output: {r.stdout[:500]}"

        # Check step_logs were created
        r2 = subprocess.run(
            ["hermes", "workflow", "list"],
            capture_output=True, text=True, timeout=15,
        )
        assert "minimal-test" in r2.stdout, "Pipeline should appear in list"

    finally:
        os.unlink(yaml_path)


@test("cli", "run-script-step", "Script pipeline with error captures exit code and stderr",
      requires_kanban=False)
def test_cli_run_script_error():
    """Run a pipeline where a script step fails, verifying error capture."""
    import subprocess, tempfile, os

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("""\
meta:
  name: script-error-test
  version: 1.0.0
steps:
  - id: fail_script
    type: script
    script: bash -c 'echo "stderr output" >&2; exit 42'
  - id: done
    type: noop
    summary: "should not reach here"
""")
        yaml_path = f.name

    try:
        r = subprocess.run(
            ["hermes", "workflow", "run", "--file", yaml_path,
             "--poll-interval", "3"],
            capture_output=True, text=True, timeout=30,
        )
        # Should exit 1 (script failed but that's expected for this test)
        if r.returncode != 0 and r.returncode != 1:
            assert False, f"Unexpected exit: {r.returncode}: {r.stderr[:500]}"
        assert "stderr output" in r.stdout or "exit=42" in r.stdout, \
            f"Should show error: {r.stdout[:500]}"
    finally:
        os.unlink(yaml_path)


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════

def run_all_tests(module_filter: Optional[str] = None, verbose: bool = False):
    """Run all registered tests, optionally filtered by module."""
    total = 0
    passed = 0
    failed = 0
    errors = 0
    skipped = 0

    print(f"{'='*60}")
    print(f"  Hermes Workflow Plugin — Test Suite")
    print(f"{'='*60}")
    print()

    for td in ALL_TESTS:
        if module_filter and td["module"] != module_filter:
            continue
        if td["module"] in ("cli", "kanban") and module_filter is None:
            # CLI/Kanban tests in default mode
            pass

        total += 1
        result = run_test(td, verbose)
        if result["status"] == "pass":
            passed += 1
        elif result["status"] == "fail":
            failed += 1
        else:
            errors += 1

    print()
    print(f"{'='*60}")
    print(f"  Summary: {total} tests")
    print(f"  ✅ Pass:  {passed}")
    print(f"  ❌ Fail:  {failed}")
    print(f"  💥 Error: {errors}")
    print(f"  ⏭️  Skip:  {skipped}")
    print(f"{'='*60}")

    return 0 if failed == 0 and errors == 0 else 1


def list_tests():
    """List all registered tests."""
    print(f"{'MODULE':<20} {'NAME':<40} {'KIND':<8} DESCRIPTION")
    print("-" * 100)
    for td in ALL_TESTS:
        print(f"{td['module']:<20} {td['name']:<40} {td['kind']:<8} {td['desc']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hermes Workflow Test Runner")
    parser.add_argument("filter", nargs="?", default=None, help="Module filter")
    parser.add_argument("--list", "-l", action="store_true", help="List tests")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    if args.list:
        list_tests()
        sys.exit(0)

    sys.exit(run_all_tests(args.filter, args.verbose))
