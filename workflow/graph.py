"""
Workflow graph analysis — directed graph of step relationships.

Builds a graph from a WorkflowDef and runs:
  - Cycle detection (DFS-based)
  - Reachability analysis (orphan / unreachable nodes)
  - Dependency deadlock detection
  - Infinite loop detection

The graph has two kinds of edges:

  1. **dependency edges** — from ``depends_on``
     (A.depends_on=[B] → B must finish before A starts → B → A)
  2. **control-flow edges** — sequential order + ``on_exit.goto``
     (step N → N+1 by default, or step N → goto_target when
      ``on_exit`` branches)
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple

from .schema import (
    KanbanStep,
    LoopStep,
    NoopStep,
    OnExitBranch,
    ScriptStep,
    StepDef,
    WorkflowDef,
)


class WorkflowGraph:
    """Directed graph of step relationships within a WorkflowDef."""

    def __init__(self, wf: WorkflowDef):
        self.wf = wf
        self.step_ids: List[str] = [s.id for s in wf.steps]

        # Edge sets
        self.dep_edges: Dict[str, Set[str]] = defaultdict(set)   # depends_on
        self.cf_edges: Dict[str, Set[str]] = defaultdict(set)    # control flow
        self.loop_parent: Dict[str, str] = {}                     # step → parent loop id

        self._build()

    def _build(self) -> None:
        """Build the graph from the WorkflowDef."""
        step_map = {s.id: s for s in self._all_steps(self.wf)}

        # Control-flow edges (sequential + goto + loop internals)
        ordered = self.wf.steps
        for i, step in enumerate(ordered):
            # Sequential: step[i] → step[i+1] (unless overridden by goto)
            if i + 1 < len(ordered):
                self.cf_edges[step.id].add(ordered[i + 1].id)

            # Goto edges from on_exit (override sequential)
            if isinstance(step, ScriptStep) and step.on_exit:
                for exit_code, branch in step.on_exit.items():
                    self.cf_edges[step.id].add(branch.goto)

            # Loop internals: parent → first sub-step, sub-step chain,
            # and last sub-step → parent (for iteration back to top)
            if isinstance(step, LoopStep) and step.steps:
                first_sub = step.steps[0].id
                self.cf_edges[step.id].add(first_sub)
                for j, sub in enumerate(step.steps):
                    if j + 1 < len(step.steps):
                        self.cf_edges[sub.id].add(step.steps[j + 1].id)
                # Last sub-step loops back to parent
                last_sub = step.steps[-1].id
                self.cf_edges[last_sub].add(step.id)

        # Dependency edges
        for step in self._all_steps(self.wf):
            for dep_id in step.depends_on:
                self.dep_edges[dep_id].add(step.id)

        # Loop parent tracking
        for step in self.wf.steps:
            if isinstance(step, LoopStep):
                for sub in step.steps:
                    self.loop_parent[sub.id] = step.id

    # ── Cycle detection ────────────────────────────────────────────

    def find_dep_cycles(self) -> List[List[str]]:
        """Find all cycles in the dependency graph (depends_on edges).

        Returns list of cycles, each as a list of step_ids forming the cycle.
        """
        return self._find_cycles(self.dep_edges)

    def find_cf_cycles(self) -> List[List[str]]:
        """Find all cycles in the control-flow graph (sequential + goto edges).

        These represent potential infinite loops in step progression.
        """
        return self._find_cycles(self.cf_edges)

    def find_all_cycles(self) -> Dict[str, List[List[str]]]:
        """Return all cycles, keyed by graph type."""
        return {
            "dependency": self.find_dep_cycles(),
            "control_flow": self.find_cf_cycles(),
        }

    def _find_cycles(self, edges: Dict[str, Set[str]]) -> List[List[str]]:
        """Tarjan-like DFS-based cycle detection.

        Returns elementary cycles (each node appears once per cycle).
        """
        all_nodes = set(edges.keys())
        for targets in edges.values():
            all_nodes.update(targets)

        cycles = []
        visited: Set[str] = set()
        rec_stack: List[str] = []
        rec_set: Set[str] = set()

        def dfs(node: str, path: List[str]) -> None:
            visited.add(node)
            rec_stack.append(node)
            rec_set.add(node)

            for neighbor in edges.get(node, set()):
                if neighbor not in visited:
                    dfs(neighbor, path + [neighbor])
                elif neighbor in rec_set:
                    # Found a cycle: slice from neighbor to end of rec_stack
                    idx = rec_stack.index(neighbor)
                    cycle = rec_stack[idx:] + [neighbor]
                    cycles.append(cycle)

            rec_stack.pop()
            rec_set.discard(node)

        for node in sorted(all_nodes):
            if node not in visited:
                dfs(node, [])

        return cycles

    # ── Reachability ───────────────────────────────────────────────

    def find_orphans(self) -> List[str]:
        """Find steps that are not reachable from the first step via
        control-flow edges.

        These steps will never execute because no path leads to them.
        """
        start = self.wf.steps[0].id if self.wf.steps else None
        if start is None:
            return []

        reachable = self._bfs(start, self.cf_edges)

        all_ids = set()
        for s in self._all_steps(self.wf):
            all_ids.add(s.id)

        orphans = sorted(all_ids - reachable)
        return orphans

    def find_sinks(self) -> List[str]:
        """Find steps that have no outgoing control-flow edges (terminal)."""
        all_ids = set()
        for s in self._all_steps(self.wf):
            all_ids.add(s.id)

        sinks = []
        for sid in all_ids:
            targets = self.cf_edges.get(sid, set())
            if not targets:
                sinks.append(sid)
        return sorted(sinks)

    def find_dead_loops(self) -> List[str]:
        """Find loops that can never exit (no goto escape, while always true).

        A loop is dead if:
          1. It has no sub-step with an on_exit that goes to a step
             outside the loop.
          2. Its ``while`` condition, if present, unconditionally
             evaluates to a truthy value.
          3. All possible code paths inside the loop eventually lead
             back to the loop start rather than exiting.
        """
        dead: List[str] = []
        for step in self.wf.steps:
            if isinstance(step, LoopStep):
                if self._is_loop_infinite(step):
                    dead.append(step.id)
        return dead

    def _is_loop_infinite(self, loop: LoopStep) -> bool:
        """Check if a loop step can ever exit."""
        # If max_iterations is set, it's bounded (not infinite)
        if loop.max_iterations >= 1:
            return False

        # Check if any sub-step has an on_exit that reaches outside
        for sub in self._all_sub_steps(loop):
            if isinstance(sub, ScriptStep) and sub.on_exit:
                for branch in sub.on_exit.values():
                    # Goto target outside the loop means an exit path
                    if branch.goto not in self._all_sub_step_ids(loop):
                        return False  # Found an exit path

        # No exit path found
        return True

    # ── Helpers ────────────────────────────────────────────────────

    def _all_steps(self, wf: WorkflowDef) -> List[StepDef]:
        """Return all steps including those nested inside loops."""
        result: List[StepDef] = []
        for step in wf.steps:
            result.append(step)
            if isinstance(step, LoopStep):
                result.extend(step.steps)
        return result

    def _all_sub_steps(self, loop: LoopStep) -> List[StepDef]:
        """Return all steps inside a loop."""
        return list(loop.steps)

    def _all_sub_step_ids(self, loop: LoopStep) -> Set[str]:
        return {s.id for s in loop.steps}

    def _bfs(self, start: str, edges: Dict[str, Set[str]]) -> Set[str]:
        """BFS to find all reachable nodes from start."""
        visited: Set[str] = set()
        queue = deque([start])
        while queue:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            for neighbor in edges.get(node, set()):
                if neighbor not in visited:
                    queue.append(neighbor)
        return visited

    # ── Summary ────────────────────────────────────────────────────

    def summary(self) -> dict:
        """Return a summary of the graph analysis."""
        dep_cycles = self.find_dep_cycles()
        cf_cycles = self.find_cf_cycles()
        orphans = self.find_orphans()
        dead_loops = self.find_dead_loops()

        return {
            "node_count": len(self.step_ids),
            "dependency_edges": sum(len(v) for v in self.dep_edges.values()),
            "control_flow_edges": sum(len(v) for v in self.cf_edges.values()),
            "dep_cycles": [list(c) for c in dep_cycles],
            "cf_cycles": [list(c) for c in cf_cycles],
            "orphan_nodes": orphans,
            "dead_loops": dead_loops,
            "sink_nodes": self.find_sinks(),
            "has_issues": bool(dep_cycles or cf_cycles or orphans or dead_loops),
        }
