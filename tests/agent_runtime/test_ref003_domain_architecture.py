#!/usr/bin/env python3
"""REF-003 domain ownership and dependency qualification."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from scripts.capture_refactor_baseline import capture


ROOT = Path(__file__).resolve().parents[2]
DOMAIN_MODULES = {
    "agent_binding", "artifacts", "evidence", "repair", "review",
    "stage1", "stage2", "stage3",
}
MOVED_DEFINITIONS = {
    "artifact_fingerprint", "build_review_report", "build_review_request",
    "build_stage1_bundle", "build_stage2_bundle", "build_stage3_bundle",
    "canonical_repair_groups", "dispatch_canonical_group", "enrich_stage1",
    "enrich_stage2", "enrich_stage3", "evaluate_impact",
    "formalize_repair_plan", "formalize_scoped_replacement",
    "provider_review_request", "validate_ac_testcase_map",
    "validate_repair_plan", "validate_review_report",
    "validate_scenario_ac_map", "validate_scoped_replacement",
    "validate_testcase_candidate", "validate_unit", "validate_assembly",
}


class Ref003DomainArchitectureTests(unittest.TestCase):
    def test_domain_modules_are_present_and_legacy_facades_are_absent(self):
        present = {
            path.stem for path in (ROOT / "domain").glob("*.py")
            if path.name != "__init__.py" and not path.name.startswith("_")
        }
        self.assertTrue(DOMAIN_MODULES.issubset(present))
        for relative in (
            "core/project_incremental.py",
            "core/project_repair.py",
            "core/project_reviewer.py",
        ):
            self.assertFalse((ROOT / relative).exists())

    def test_domain_has_no_runtime_or_authority_dependency(self):
        forbidden_roots = {"adapters", "core", "infrastructure", "runtime"}
        forbidden_calls = {
            "open", "read_bytes", "read_text", "write_bytes", "write_text",
            "select_tools", "probe", "restore_probe", "build_only", "run",
        }
        for path in sorted((ROOT / "domain").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), path.as_posix())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {alias.name.split(".", 1)[0] for alias in node.names}
                    self.assertFalse(roots & forbidden_roots, path.name)
                elif isinstance(node, ast.ImportFrom):
                    root = (node.module or "").split(".", 1)[0]
                    self.assertNotIn(root, forbidden_roots, path.name)
                elif isinstance(node, ast.Call):
                    name = (node.func.attr if isinstance(node.func, ast.Attribute)
                            else node.func.id if isinstance(node.func, ast.Name)
                            else "")
                    self.assertNotIn(name, forbidden_calls, path.name)

    def test_moved_definitions_have_one_domain_owner(self):
        owners: dict[str, list[str]] = {name: [] for name in MOVED_DEFINITIONS}
        for path in sorted(ROOT.rglob("*.py")):
            if any(part in {".git", "__pycache__", "tests"}
                   for part in path.relative_to(ROOT).parts):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), path.as_posix())
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and \
                        node.name in owners:
                    owners[node.name].append(path.relative_to(ROOT).as_posix())
        for name, paths in owners.items():
            self.assertEqual(1, len(paths), (name, paths))
            self.assertTrue(paths[0].startswith("domain/"), (name, paths))

    def test_domain_introduces_no_import_cycle(self):
        architecture = capture()
        for component in architecture["circular_import_components"]:
            self.assertFalse(
                any(module.startswith("domain.") for module in component),
                component)


if __name__ == "__main__":
    unittest.main()
