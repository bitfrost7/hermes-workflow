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

from . import cli as _cli


def register(ctx) -> None:
    """Plugin entry point — called by Hermes plugin manager with a PluginContext.

    Registers the ``hermes workflow ...`` CLI subcommand tree.
    """
    ctx.register_cli_command(
        name="workflow",
        help="Run, manage, and inspect YAML-defined kanban workflow pipelines",
        description=(
            "Compile YAML workflow definitions into Hermes Kanban task cards "
            "with parent-child dependency chains. Supports URL/file/template "
            "sources, review-fix loops, and automatic step advancement."
        ),
        setup_fn=_cli.register_cli,
        handler_fn=_cli.dispatch,
    )
