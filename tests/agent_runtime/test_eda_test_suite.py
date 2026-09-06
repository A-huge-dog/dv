#!/usr/bin/env python3
"""EDA-001 binding-first suite execution with a controlled fake xrun."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from adapters.eda import TrustedEdaBoundaryError, XceliumAdapter
from application.eda_test_suite import (
    EdaTestSuiteTool, build_eda_test_suite_binding,
)
from application.eda_loop import (
    ExecuteEdaSuiteInLoopHandler, ExecuteEdaSuiteInLoopInput,
)

try:
    from test_xcelium_adapter import FAKE_XRUN
except ModuleNotFoundError:
    from tests.agent_runtime.test_xcelium_adapter import FAKE_XRUN


class EdaTestSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        tools = self.root / "tools"
        tools.mkdir()
        xrun = tools / "xrun"
        xrun.write_text(FAKE_XRUN, encoding="utf-8")
        xrun.chmod(0o755)
        (self.root / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
        (self.root / "tb.sv").write_text(
            'module tb; initial begin $display("DV_SUITE_PASS"); $finish; end endmodule\n',
            encoding="utf-8")
        self.job_id = "JOB.EDA.SUITE.TEST"
        self.result = self.root / "result"
        self.adapter = XceliumAdapter(
            self.root, self.result, self.job_id, xrun,
            "XCELIUMENV.SUITE.TEST.24_09", {
                "PATH": str(tools) + os.pathsep + "/usr/bin",
                "LM_LICENSE_FILE": "27000@private-license-host", "LC_ALL": "C",
            }, timeout_seconds=10)
        self.tool = EdaTestSuiteTool(self.root, self.result, self.job_id, self.adapter)

    def binding(self, *, uvm: bool = False):
        return build_eda_test_suite_binding(
            job_root=self.result / "jobs" / self.job_id,
            workspace_root=self.root, job_id=self.job_id,
            binding_id="BINDING.EDA.SUITE.TEST",
            sources=[{"path": "dut.sv", "role": "RTL"},
                     {"path": "tb.sv", "role": "TESTBENCH"}],
            dut_top="dut", testbench_top="tb",
            testcases=[
                {"id": "SMOKE.1", "source_paths": ["tb.sv"],
                 "selected_test": "smoke", "seed": 1, "timeout_seconds": 10,
                 "pass_marker": "DV_SUITE_PASS", "uvm": uvm},
                {"id": "SMOKE.2", "source_paths": ["tb.sv"],
                 "selected_test": "smoke_two", "seed": 2, "timeout_seconds": 10,
                 "pass_marker": "DV_SUITE_PASS", "uvm": uvm},
            ])

    def test_multiple_testcases_and_exact_replay(self):
        path, binding = self.binding()
        result = self.tool.execute_eda_test_suite(path, binding["binding_fingerprint"])
        self.assertTrue(result.final_result["all_testcases_passed"])
        self.assertEqual(2, result.final_result["testcase_total"])
        self.assertFalse(result.replayed)
        replay = self.tool.execute_eda_test_suite(path, binding["binding_fingerprint"])
        self.assertTrue(replay.replayed)
        self.assertEqual(result.final_result, replay.final_result)

    def test_binding_source_drift_fails_before_eda_replay(self):
        path, binding = self.binding()
        self.tool.execute_eda_test_suite(path, binding["binding_fingerprint"])
        (self.root / "dut.sv").write_text("module dut; wire changed; endmodule\n")
        with self.assertRaises(TrustedEdaBoundaryError) as caught:
            self.tool.execute_eda_test_suite(path, binding["binding_fingerprint"])
        self.assertEqual("STALE_EVIDENCE", caught.exception.diagnostics[0]["code"])

    def test_coverage_unavailable_is_explicit_and_waves_are_not_created(self):
        path, binding = self.binding()
        result = self.tool.execute_eda_test_suite(
            path, binding["binding_fingerprint"], coverage=True)
        coverage = result.final_result["coverage"]
        self.assertEqual("UNAVAILABLE", coverage["status"])
        self.assertEqual([], coverage["native_report_paths"])
        self.assertFalse(any(path.name.endswith((".vcd", ".shm"))
                             for path in (self.result / "jobs" / self.job_id / "runs").rglob("*")))

    def test_uvm_suite_enables_uvm_during_shared_build(self):
        path, binding = self.binding(uvm=True)
        self.tool.execute_eda_test_suite(path, binding["binding_fingerprint"])
        request_path = next((self.result / "jobs" / self.job_id /
                             "audit/xcelium").glob("*.build.request.json"))
        request = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertIn("-uvm", [item["value"]
                               for item in request["argv_template"]])

    def test_binding_requires_both_rtl_and_testbench(self):
        with self.assertRaises(TrustedEdaBoundaryError):
            build_eda_test_suite_binding(
                job_root=self.result / "jobs" / self.job_id,
                workspace_root=self.root, job_id=self.job_id,
                binding_id="BINDING.EDA.BAD",
                sources=[{"path": "dut.sv", "role": "RTL"}], dut_top="dut",
                testbench_top="tb", testcases=[])

    def test_loop_handler_calls_binding_first_eda_tool_without_pj003_authority(self):
        job_root = self.result / "jobs" / self.job_id
        suite = {
            "binding_id": "BINDING.EDA.LOOP.TEST",
            "sources": [{"path": "dut.sv", "role": "RTL"},
                        {"path": "tb.sv", "role": "TESTBENCH"}],
            "dut_top": "dut", "testbench_top": "tb", "coverage": False,
            "testcases": [{
                "id": "SMOKE.LOOP", "source_paths": ["tb.sv"],
                "selected_test": "smoke", "seed": 1, "timeout_seconds": 10,
                "pass_marker": "DV_SUITE_PASS", "uvm": False,
            }],
        }
        checkpoint = ExecuteEdaSuiteInLoopHandler().handle(
            ExecuteEdaSuiteInLoopInput(
                job_root, self.root, {"job_id": self.job_id}, {
                    "state": "AWAITING_HUMAN_REVIEW", "job_id": self.job_id,
                    "error_count": 0, "review_verdict": "CLEAN"}, suite, self.tool))
        self.assertEqual("EDA_EXECUTION_PASS", checkpoint["state"])
        self.assertNotIn("authorization", checkpoint)


if __name__ == "__main__":
    unittest.main()
