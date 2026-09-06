"""EDA-002 boundary checks: UVM platform stays outside Job binding."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from application.uvm_execution import BINDING_PATH, build_uvm_testcase_binding
from domain.uvm_context import RuntimeCapability
from domain.uvm_testcase import build_manifest, validate_generated_tests


class UvmTestcaseBoundaryTests(unittest.TestCase):
    def test_manifest_is_deterministic_and_source_cannot_control_platform_marker(self):
        logical = [{"testcase_id": "TC.SMOKE.1", "status": "CHECKABLE"}]
        manifest = build_manifest(logical)
        testcase = manifest["testcases"][0]
        source = (
            "class {} extends coral_npu_base_test; "
            "function void run(); api.start(); endfunction endclass".format(
                testcase["uvm_class"]))
        validate_generated_tests(source, manifest, ValueError)
        with self.assertRaises(ValueError):
            validate_generated_tests(
                source + ' initial $display("{}");'.format(
                    testcase["pass_marker"]), manifest, ValueError)

    def test_binding_has_no_uvm_platform_or_environment_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_id = "JOB.PROJECT.UVM.BOUNDARY"
            job_root = root / "result/jobs" / job_id
            generated = job_root / "approved/generated/uvm"
            generated.mkdir(parents=True)
            manifest = build_manifest([
                {"testcase_id": "TC.SMOKE.1", "status": "CHECKABLE"}])
            testcase = manifest["testcases"][0]
            (generated / "generated_tests_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8")
            (generated / "generated_tests.sv").write_text(
                "class {} extends coral_npu_base_test; "
                "function void run(); api.start(); endfunction endclass".format(
                    testcase["uvm_class"]), encoding="utf-8")
            (job_root / "audit").mkdir()
            (job_root / "audit/approval.json").write_text("{}", encoding="utf-8")
            rtl = job_root / "input_baseline/rtl"
            rtl.mkdir(parents=True)
            (rtl / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
            project_input = {
                "job_id": job_id,
                "rtl": {
                    "sources": [{"baseline_path": (
                        "result/jobs/{}/input_baseline/rtl/dut.sv".format(job_id))}],
                    "top": "dut", "parameters": {},
                },
            }
            binding = build_uvm_testcase_binding(
                workspace_root=root, job_root=job_root,
                manifest=project_input, approval_path="audit/approval.json",
                capability=RuntimeCapability(
                    capability_id="UVM.TEST.CAPABILITY",
                    base_class="coral_npu_base_test",
                    extension_point="run",
                    api_type="api_type",
                    api_object="api",
                    methods={"start": "task start();"},
                    operations=frozenset({"start"}),
                    aggregate_fingerprint="a" * 64))
            self.assertTrue((job_root / BINDING_PATH).is_file())
            encoded = json.dumps(binding).casefold()
            self.assertNotIn("platform", encoded)
            self.assertNotIn("include_dir", encoded)
            self.assertNotIn("environment", encoded)
            self.assertNotIn("testbench", encoded)


if __name__ == "__main__":
    unittest.main()
