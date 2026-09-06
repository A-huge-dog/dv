#!/usr/bin/env python3
"""REF-005 ownership checks for the single Project loop."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from runtime.project_loop import (
    CheckpointRepository, ProjectLoop, ResumePolicy, WorkflowState,
)


ROOT = Path(__file__).resolve().parents[2]


class Ref005ProjectLoopArchitectureTests(unittest.TestCase):
    def test_every_auto_state_has_one_explicit_transition(self):
        transitions = ProjectLoop._transitions
        # The transition table is instance-bound because its values are
        # executable handlers; inspect the declared source names here.
        tree = ast.parse((ROOT / "runtime/project_loop.py").read_text(
            encoding="utf-8"))
        literals = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        for state in WorkflowState:
            if CheckpointRepository.policy_for(state) is ResumePolicy.AUTO:
                self.assertIn(state.value, literals, state.value)
        self.assertIsNotNone(transitions)

    def test_repository_does_not_scan_for_latest_authority(self):
        tree = ast.parse((ROOT / "runtime/project_loop.py").read_text(
            encoding="utf-8"))
        repository = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "CheckpointRepository")
        attributes = {
            node.attr for node in ast.walk(repository)
            if isinstance(node, ast.Attribute)
        }
        self.assertFalse({"glob", "rglob", "iterdir"} & attributes)

    def test_cli_has_no_project_transition_or_runtime_wrapper(self):
        tree = ast.parse((ROOT / "scripts/run_project_job.py").read_text(
            encoding="utf-8"))
        functions = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertFalse({"advance_repair_runtime", "advance_commit_runtime"} & functions)
        calls = {
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn("run_until_pause", calls)
        self.assertFalse({"start", "resume", "route_scenarios"} & calls)

    def test_project_loop_does_not_own_agent_protocol(self):
        tree = ast.parse((ROOT / "runtime/project_loop.py").read_text(
            encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertFalse(any(name.startswith("runtime.agent_loop") for name in imported))


if __name__ == "__main__":
    unittest.main()
