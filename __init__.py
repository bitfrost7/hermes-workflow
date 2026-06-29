"""
Hermes Workflow Plugin — YAML-defined kanban workflow pipelines.

A Hermes Agent plugin that compiles YAML workflow definitions into Hermes
Kanban task cards with parent-child dependency chains, steps forward through
the pipeline as cards complete, and supports review-fix loops with retry.

Usage:
    hermes workflow run --url <URL> [--var key=val ...]
    hermes workflow run --file <path> [--var key=val ...]
    hermes workflow list
    hermes workflow show <pipeline_id>
    hermes workflow cancel <pipeline_id>
    hermes workflow gc
    hermes workflow templates
    hermes workflow template show <name>
"""

from __future__ import annotations

from .cli import register_cli

__all__ = ["register_cli"]
