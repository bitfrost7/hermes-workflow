"""
Workflow loader — loads and validates YAML workflow definitions from URLs or local files.

Supports three source types:
  - URL (http/https): fetches via urllib
  - Local file (file:// or bare path): reads from disk
  - Built-in template (--template): from templates/ directory
"""

from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

import yaml

from .schema import WorkflowDef, StepDef

# Plugin templates directory
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class LoadError(Exception):
    """Raised when a workflow definition cannot be loaded or validated."""


def load_from_url(url: str) -> WorkflowDef:
    """Fetch a workflow YAML from an HTTP(S) URL and parse it."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-workflow/0.1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise LoadError(f"HTTP {e.code} fetching {url}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise LoadError(f"URL error for {url}: {e.reason}") from e
    except Exception as e:
        raise LoadError(f"Failed to fetch {url}: {e}") from e

    return _parse_yaml(raw, source=url)


def load_from_file(path: str) -> WorkflowDef:
    """Read a workflow YAML from a local file path."""
    resolved = os.path.expanduser(path)
    if not os.path.isfile(resolved):
        raise LoadError(f"File not found: {resolved}")
    try:
        with open(resolved, "r") as f:
            raw = f.read()
    except OSError as e:
        raise LoadError(f"Failed to read {resolved}: {e}") from e

    return _parse_yaml(raw, source=resolved)


def load_from_template(name: str) -> WorkflowDef:
    """Load a built-in template by name (without .yaml suffix)."""
    candidates = [
        _TEMPLATES_DIR / f"{name}.yaml",
        _TEMPLATES_DIR / f"{name}.yml",
    ]
    for path in candidates:
        if path.is_file():
            try:
                with open(path, "r") as f:
                    raw = f.read()
            except OSError as e:
                raise LoadError(f"Failed to read template {name}: {e}") from e
            return _parse_yaml(raw, source=str(path))

    # List available templates
    available = _list_templates()
    raise LoadError(
        f"Template '{name}' not found. Available templates: {', '.join(available) or '(none)'}"
    )


def load(source: str) -> WorkflowDef:
    """Auto-detect source type and load the workflow definition.

    Auto-detection order:
      1. http/https URL → load_from_url
      2. file:// URL → load_from_file
      3. Bare path that exists → load_from_file
      4. Otherwise → try as template name → load_from_template
    """
    if source.startswith(("http://", "https://")):
        return load_from_url(source)
    if source.startswith("file://"):
        return load_from_file(source[7:])

    expanded = os.path.expanduser(source)
    if os.path.isfile(expanded):
        return load_from_file(expanded)

    # Fallback: try as a template name
    return load_from_template(source)


def list_templates() -> list[dict]:
    """List available built-in templates with metadata."""
    results = []
    for path in _TEMPLATES_DIR.glob("*.yaml"):
        try:
            with open(path) as f:
                raw = f.read()
            data = yaml.safe_load(raw)
            meta = data.get("meta", {}) if isinstance(data, dict) else {}
            results.append({
                "name": path.stem,
                "version": meta.get("version", "?"),
                "description": meta.get("description", ""),
                "path": str(path),
            })
        except Exception:
            pass
    # Sort by name, but 'review-loop' first
    results.sort(key=lambda x: (x["name"] != "review-loop", x["name"]))
    return results


def _list_templates() -> list[str]:
    return sorted(p.stem for p in _TEMPLATES_DIR.glob("*.yaml"))


def _parse_yaml(raw: str, source: str) -> WorkflowDef:
    """Parse raw YAML string into a validated WorkflowDef."""
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise LoadError(f"YAML parse error in {source}: {e}") from e

    if not isinstance(data, dict):
        raise LoadError(f"Invalid workflow in {source}: expected a top-level mapping")

    try:
        wf = WorkflowDef.model_validate(data)
    except Exception as e:
        # Provide structured error output for validation failures
        lines = raw.split("\n")
        raise LoadError(
            f"Validation error in {source}:\n  {e}"
        ) from e

    # Validate step id uniqueness
    ids = set()
    for step in wf.steps:
        _validate_ids_unique(step, ids)

    # Validate depends_on references
    all_ids = set(ids)
    for step in wf.steps:
        _validate_dep_refs(step, all_ids)

    return wf


def _validate_ids_unique(step, seen: set) -> None:
    if step.id in seen:
        raise LoadError(f"Duplicate step id: '{step.id}'")
    seen.add(step.id)
    if hasattr(step, "steps"):  # LoopStep
        for sub in step.steps:
            _validate_ids_unique(sub, seen)


def _validate_dep_refs(step, all_ids: set) -> None:
    for dep in step.depends_on:
        if dep not in all_ids:
            raise LoadError(
                f"Step '{step.id}' depends_on '{dep}' but no such step exists"
            )
    if hasattr(step, "steps"):
        for sub in step.steps:
            _validate_dep_refs(sub, all_ids)
