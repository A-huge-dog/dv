#!/usr/bin/env python3
"""REF-006 physical cutover, dependency, and ownership qualification."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from scripts.capture_refactor_baseline import capture


ROOT = Path(__file__).resolve().parents[2]
ACTIVE_ROOTS = (
    "adapters", "agents", "application", "contracts", "domain",
    "infrastructure", "runtime", "scripts",
)
MOVED_OWNERS = {
    "AgentLoop": "runtime/agent_loop.py",
    "CheckpointRepository": "runtime/project_loop.py",
    "ProjectLoop": "runtime/project_loop.py",
    "ProjectJobWorkflow": "runtime/project_job.py",
    "ProjectJobRuntimeIntegration": "runtime/job_runtime.py",
    "ProjectRepairRuntime": "runtime/repair_runtime.py",
    "ProjectCommitRuntime": "runtime/commit_runtime.py",
    "StagedProjectWorkflow": "runtime/staged_workflow.py",
    "StandaloneStage3Workflow": "runtime/standalone_stage3.py",
    "StandaloneStage3ReviewerWorkflow": "runtime/standalone_reviewer.py",
    "ProjectReadModel": "agents/project_tools.py",
    "RepairRecordStore": "infrastructure/persistence/repair_records.py",
    "SerialSessionScheduler": "runtime/session_scheduler.py",
}


def _active_python() -> list[Path]:
    return sorted(
        path for root in ACTIVE_ROOTS for path in (ROOT / root).rglob("*.py")
        if "__pycache__" not in path.parts
    )


class Ref006PhysicalCleanupTests(unittest.TestCase):
    def test_legacy_core_tree_and_import_paths_are_absent(self):
        self.assertFalse((ROOT / "core").exists())
        for path in _active_python():
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("core.project_", source, path.as_posix())
            self.assertNotIn("core/project_", source, path.as_posix())

    def test_runtime_owners_are_unique_and_not_reexported(self):
        owners = {name: [] for name in MOVED_OWNERS}
        for path in _active_python():
            tree = ast.parse(path.read_text(encoding="utf-8"), path.as_posix())
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and node.name in owners:
                    owners[node.name].append(path.relative_to(ROOT).as_posix())
        for name, expected in MOVED_OWNERS.items():
            self.assertEqual([expected], owners[name], name)
        for relative in (
            "agents/__init__.py", "runtime/__init__.py",
            "infrastructure/persistence/__init__.py",
        ):
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            self.assertFalse(
                any(isinstance(node, (ast.Import, ast.ImportFrom))
                    for node in tree.body),
                relative,
            )

    def test_dependency_graph_has_no_cycle_or_reverse_layer_import(self):
        architecture = capture()
        self.assertEqual([], architecture["circular_import_components"])
        for path in _active_python():
            relative = path.relative_to(ROOT)
            if relative.parts[0] not in {"application", "infrastructure"}:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), path.as_posix())
            for node in ast.walk(tree):
                module = ""
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".", 1)[0] == "runtime":
                            self.fail("{} imports runtime".format(relative))
                if module.split(".", 1)[0] == "runtime":
                    self.fail("{} imports runtime".format(relative))

    def test_ref005_to_ref006_structure_comparison(self):
        import json

        before = json.loads((
            ROOT / "tests/baselines/ref005_architecture.json"
        ).read_text(encoding="utf-8"))
        after = capture()
        before_core = {
            item["module"] for item in before["modules"]
            if item["module"] == "core" or item["module"].startswith("core.")
        }
        after_core = {
            item["module"] for item in after["modules"]
            if item["module"] == "core" or item["module"].startswith("core.")
        }
        self.assertGreater(len(before_core), 0)
        self.assertEqual(set(), after_core)
        self.assertGreater(len(before["circular_import_components"]), 0)
        self.assertEqual([], after["circular_import_components"])


if __name__ == "__main__":
    unittest.main()
