#!/usr/bin/env python3
"""REF-004 application-handler ownership and dependency qualification."""
from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from scripts.capture_refactor_baseline import capture


ROOT = Path(__file__).resolve().parents[2]
HANDLERS = {
    "bootstrap.py": {"BootstrapHandler"},
    "generation.py": {
        "GenerateStage1Handler", "GenerateStage2Handler",
        "GenerateStage3Handler",
    },
    "review.py": {"InitialReviewHandler", "FinalReviewHandler"},
    "repair.py": {
        "CreateRepairPlanHandler", "ValidateRepairPlanHandler",
        "ScopedReplacementHandler",
    },
    "compile_candidate.py": {"CompileCandidateHandler"},
    "commit_group.py": {"CommitGroupHandler"},
    "recompute_impact.py": {"RecomputeImpactHandler"},
    "human_gate.py": {"CreateHumanGateHandler"},
}
REMOVED_CORE_ACTIONS = {
    "bootstrap", "_generate_stage1", "_generate_stage2", "_generate_stage3",
    "_review", "_human_review_gate", "_approval_gate", "_compile",
    "_record_stage_success", "_record_stage_failure",
    "_generate_candidate", "_review_until_human_gate", "_human_gate",
    "_approve", "_latest_checkpoint",
}


class Ref004ApplicationArchitectureTests(unittest.TestCase):
    def test_all_required_handlers_have_one_application_owner(self):
        owners: dict[str, list[str]] = {
            name: [] for names in HANDLERS.values() for name in names}
        for path in sorted((ROOT / "application").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), path.as_posix())
            classes = {
                node.name for node in tree.body if isinstance(node, ast.ClassDef)}
            for name in owners:
                if name in classes:
                    owners[name].append(path.name)
        for filename, names in HANDLERS.items():
            for name in names:
                self.assertEqual([filename], owners[name], name)

    def test_application_does_not_depend_on_core_or_runtime(self):
        forbidden = {"core", "runtime"}
        for path in sorted((ROOT / "application").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), path.as_posix())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {alias.name.split(".", 1)[0] for alias in node.names}
                    self.assertFalse(roots & forbidden, path.name)
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn(
                        (node.module or "").split(".", 1)[0], forbidden,
                        path.name)
        for component in capture()["circular_import_components"]:
            self.assertFalse(
                any(module.startswith("application.") for module in component),
                component)

    def test_old_core_action_implementations_are_absent(self):
        found: dict[str, list[str]] = {name: [] for name in REMOVED_CORE_ACTIONS}
        for path in sorted((ROOT / "core").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), path.as_posix())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                        node.name in found:
                    found[node.name].append(path.name)
        self.assertEqual({}, {
            name: paths for name, paths in found.items() if paths})

    def test_handlers_receive_commands_and_return_named_result_types(self):
        from application.bootstrap import BootstrapHandler, BootstrapResult
        from application.commit_group import CommitGroupHandler, CommitGroupResult
        from application.compile_candidate import (
            CompileCandidateHandler, CompileCandidateResult)
        from application.generation import (
            GenerateStage1Handler, GenerateStage2Handler,
            GenerateStage3Handler, GenerationResult)
        from application.human_gate import (
            CreateHumanGateHandler, HumanGateResult)
        from application.recompute_impact import (
            RecomputeImpactHandler, RecomputeImpactResult)
        from application.repair import (
            CreateRepairPlanHandler, RepairPlanResult,
            ScopedReplacementHandler, ScopedReplacementResult,
            ValidateRepairPlanHandler)
        from application.review import (
            FinalReviewHandler, InitialReviewHandler, ReviewResult)

        pairs = (
            (BootstrapHandler, BootstrapResult),
            (GenerateStage1Handler, GenerationResult),
            (GenerateStage2Handler, GenerationResult),
            (GenerateStage3Handler, GenerationResult),
            (InitialReviewHandler, ReviewResult),
            (FinalReviewHandler, ReviewResult),
            (CreateRepairPlanHandler, RepairPlanResult),
            (ValidateRepairPlanHandler, RepairPlanResult),
            (ScopedReplacementHandler, ScopedReplacementResult),
            (CompileCandidateHandler, CompileCandidateResult),
            (CommitGroupHandler, CommitGroupResult),
            (RecomputeImpactHandler, RecomputeImpactResult),
            (CreateHumanGateHandler, HumanGateResult),
        )
        for handler, result in pairs:
            signature = inspect.signature(handler.handle)
            self.assertIn("command", signature.parameters, handler.__name__)
            annotation = signature.return_annotation
            self.assertIn(result.__name__, str(annotation), handler.__name__)

    def test_handlers_do_not_discover_latest_revision_or_job(self):
        forbidden_names = {"latest", "latest_checkpoint", "latest_revision"}
        for path in sorted((ROOT / "application").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), path.as_posix())
            names = {
                node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            attributes = {
                node.attr for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)}
            self.assertFalse((names | attributes) & forbidden_names, path.name)


if __name__ == "__main__":
    unittest.main()
