"""Targeted EDA-002 Project-YAML context and testcase-boundary checks."""
from __future__ import annotations

import hashlib
import unittest

from contracts.validator import accepted, validate
from domain.stage3 import _blocked_contract_diagnostics
from domain.uvm_context import project_uvm_context
from domain.uvm_testcase import build_manifest, validate_generated_tests
from runtime.errors import ProjectJobError


def _project_context():
    text = "class public_base_test extends uvm_test; endclass\n"
    return {
        "uvm_testcase_context": {"files": [{
            "logical_path": "uvm/tests/public_base_test.svh",
            "baseline_path": "result/jobs/JOB.PROJECT.TEST/input_baseline/uvm_testcase_context/public_base_test.svh",
            "content": text,
            "fingerprint": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }]},
    }


def _project_context_with_public_api():
    text = """
interface public_control_if;
  logic halted;
endinterface
class public_platform_api extends uvm_object;
  task apply_reset(); endtask
  task wait_for_halted(); endtask
endclass
"""
    return {
        "uvm_testcase_context": {"files": [{
            "logical_path": "uvm/public_platform_api.svh",
            "baseline_path": "result/jobs/JOB.PROJECT.TEST/input_baseline/uvm_testcase_context/public_platform_api.svh",
            "content": text,
            "fingerprint": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }]},
    }


class Eda002UvmContextTests(unittest.TestCase):
    def test_project_yaml_context_exposes_only_logical_paths_and_full_text(self):
        request = project_uvm_context(_project_context()).request_value()
        file = request["uvm_testcase_context"]["files"][0]
        self.assertEqual(
            {"logical_path", "fingerprint", "content"}, set(file))
        self.assertEqual("uvm/tests/public_base_test.svh", file["logical_path"])
        self.assertIn("class public_base_test", file["content"])
        self.assertNotIn("baseline_path", file)

    def test_manifest_preserves_explicitly_routed_skipped_testcases(self):
        manifest = build_manifest([{"testcase_id": "TC.SMOKE.1", "status": "CHECKABLE"}])
        name = manifest["testcases"][0]["uvm_class"]
        source = """
class {name} extends public_base_test;
  task execute_testcase();
    api.reset();
    api.observe(value);
  endtask
endclass
""".format(name=name)
        validate_generated_tests(source, manifest, ValueError)
        partial = build_manifest(
            [{"testcase_id": "TC.SMOKE.1", "status": "CHECKABLE"}],
            implemented_testcase_ids=[],
            skipped_testcases=[{
                "testcase_id": "TC.SMOKE.1",
                "reason_kind": "BLOCKED_CONTRACT",
                "reason": "The frozen UVM context has no transaction driver.",
                "routing_required": True,
            }])
        self.assertEqual([], partial["testcases"])
        self.assertEqual("TC.SMOKE.1", partial["skipped_testcases"][0]["testcase_id"])
        validate_generated_tests("// All logical testcases require routing.\n", partial, ValueError)
        self.assertTrue(accepted(validate("generated_uvm_tests_manifest", partial)))

    def test_stage3_candidate_requires_a_typed_skipped_testcase(self):
        candidate = {
            "code_units": [{
                "role": "SHARED",
                "testcase_ids": [],
                "content": "// All testcase generation is routed.\n",
            }],
            "assembly": [0],
            "implemented_testcase_ids": [],
            "skipped_testcases": [{
                "testcase_id": "TC.SMOKE.1",
                "reason_kind": "RTL_CONTRACT_MISMATCH",
                "reason": "The UVM monitor cannot observe the specified RTL event.",
                "routing_required": True,
            }],
        }
        self.assertTrue(accepted(validate("uvm_testcase_candidate", candidate)))
        del candidate["skipped_testcases"]
        self.assertFalse(accepted(validate("uvm_testcase_candidate", candidate)))

    def test_blocked_contract_cannot_deny_frozen_public_uvm_declarations(self):
        testcases = [{
            "testcase_id": "TC.SMOKE.1",
            "status": "CHECKABLE",
            "objective": "Apply reset and wait for halted.",
            "stimulus": "Apply reset.",
            "transaction_sequence": "Call reset, then wait for halted.",
            "checker": "Observe halted.",
            "expected_result": "halted is asserted.",
        }]
        skipped = [{
            "testcase_id": "TC.SMOKE.1",
            "reason_kind": "BLOCKED_CONTRACT",
            "reason": "The frozen context has no apply_reset task and cannot observe halted.",
            "routing_required": True,
        }]

        diagnostics = _blocked_contract_diagnostics(
            skipped, testcases, _project_context_with_public_api(),
            ProjectJobError)

        self.assertEqual(
            {"UVM_CONTEXT_SKIP_CONTRADICTION", "ALL_TESTCASES_SKIPPED"},
            {item["code"] for item in diagnostics})
        contradiction = next(
            item for item in diagnostics
            if item["code"] == "UVM_CONTEXT_SKIP_CONTRADICTION")
        self.assertIn("apply_reset", contradiction["offending_content"])
        self.assertIn("halted", contradiction["offending_content"])

    def test_genuinely_missing_public_uvm_capability_can_still_be_skipped(self):
        testcases = [{
            "testcase_id": "TC.SMOKE.1",
            "status": "CHECKABLE",
            "objective": "Exercise JTAG edge timing.",
            "stimulus": "Toggle TMS and TDI around TCK.",
            "transaction_sequence": "Pulse TRST and sample TDO.",
            "checker": "Check JTAG edge behavior.",
            "expected_result": "TDO changes on the specified edge.",
        }]
        skipped = [{
            "testcase_id": "TC.SMOKE.1",
            "reason_kind": "BLOCKED_CONTRACT",
            "reason": "The frozen UVM context defines no JTAG interface or TCK signal.",
            "routing_required": True,
        }]

        self.assertEqual([], _blocked_contract_diagnostics(
            skipped, testcases, _project_context(), ProjectJobError))


if __name__ == "__main__":
    unittest.main()
