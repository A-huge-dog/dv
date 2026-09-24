"""Automatic Project execution, exact replay, and coverage qualification."""
from __future__ import annotations

import copy
import json
import os
import unittest
from importlib import import_module
from unittest.mock import patch

from adapters.eda import XceliumAdapter
from contracts.validator import load_document
from domain.artifacts import artifact_fingerprint
from runtime.errors import ProjectJobError
from runtime.project_job import ProjectJobWorkflow
from runtime.project_loop import ProjectLoop, ProjectLoopRequest, CheckpointRepository
from application.project_execution import (
    EXECUTION_INPUT_PATH, EXECUTION_BUNDLE_PATH, EXECUTION_EVIDENCE_PATH,
    EXECUTION_RESULT_PATH, REVIEW_COMPLETE_PATH, LEGACY_REVIEW_PATH,
    _human_checkpoint_fingerprint,
)

try:
    fixtures = import_module("test_project_job_workflow")
    xcelium = import_module("test_xcelium_adapter")
except ModuleNotFoundError:
    fixtures = import_module("tests.agent_runtime.test_project_job_workflow")
    xcelium = import_module("tests.agent_runtime.test_xcelium_adapter")


def install_xcelium(root):
    tools = root / "tools"
    tools.mkdir(exist_ok=True)
    executable = tools / "xrun"
    source = xcelium.FAKE_XRUN.replace(
        'match = re.search(r"DV_[A-Z0-9_]+_PASS", content)',
        'selected = next((a.split("=",1)[1] for a in args if a.startswith("+UVM_TESTNAME=")), "")\n'
        '    body = content.split("class " + selected + " extends", 1)[-1] if selected else content\n'
        '    match = re.search(r"DV_[A-Z0-9_]+_PASS", body)')
    executable.write_text(source)
    executable.chmod(0o755)
    # Test fixtures have only authoring context; supply an explicit testbench top.
    pkg = root / "uvm/pkg.sv"
    pkg.write_text(pkg.read_text() + '\nmodule tb_tiny; initial run_test(); endmodule\n')
    def factory(job_root, manifest, execution):
        return XceliumAdapter(root, root / "result", manifest["job_id"], executable,
                              execution["environment_identity"], {"PATH": "/usr/bin", "LC_ALL": "C"},
                              timeout_seconds=execution["constraints"]["timeout_seconds"])
    return factory


class Pj003ProjectExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectJobWorkflowTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.factory = install_xcelium(self.root)
        self.generator = fixtures.FakeProvider(duplicate_testcases=True)
        self.reviewer = fixtures.FakeReviewerProvider()
        self.workflow = ProjectJobWorkflow(self.root, self.root / "result", self.generator, self.reviewer)
        self.loop = ProjectLoop(self.workflow, provider_factory=lambda *_: self.fail("unexpected provider creation"),
                                eda_adapter_factory=self.factory)
        self.submission = self.fixture.project_input()
        self.job = self.root / "result/jobs" / self.submission["job_id"]

    def prepare(self):
        return self.fixture.start_checked(self.workflow, self.submission)

    def execute(self):
        return self.loop.run_until_pause(ProjectLoopRequest(self.submission))

    def calls(self):
        p = self.root / "tools/invocations.txt"
        return int(p.read_text()) if p.exists() else 0

    def test_no_approval_build_then_each_test_and_exact_terminal_replay(self):
        checkpoint = self.prepare()
        self.assertEqual("READY_FOR_EXECUTION_PREPARATION", checkpoint["state"])
        result = self.execute()
        self.assertEqual("EXECUTION_PASS", result["state"])
        self.assertEqual(2, result["summary"]["passed"])
        self.assertEqual(3, self.calls())
        self.assertTrue(result["summary"]["full_verification_passed"])
        self.assertEqual(result, self.execute())
        self.assertEqual(3, self.calls())
        for path in ("audit/pj003_testcase_decision.json", "audit/pj003_execution_authorization.json",
                     "staging/validations/human_review_request.json"):
            self.assertFalse((self.job / path).exists())

    def test_legacy_checkpoint_resumes_without_provider_or_old_bytes_changes(self):
        checkpoint = self.prepare()
        (self.job / REVIEW_COMPLETE_PATH).unlink()
        checkpoint["state"] = "AWAITING_HUMAN_REVIEW"
        checkpoint["checked_testcases_complete"] = True
        checkpoint["checkpoint_fingerprint"] = _human_checkpoint_fingerprint(checkpoint)
        checkpoint["bundle_fingerprints"]["checkpoint"] = checkpoint["checkpoint_fingerprint"]
        path = self.job / LEGACY_REVIEW_PATH
        path.write_text(json.dumps(checkpoint))
        original = path.read_bytes()
        result = self.execute()
        self.assertEqual("EXECUTION_PASS", result["state"])
        self.assertEqual(original, path.read_bytes())
        self.assertEqual(result, self.execute())

    def test_binding_interruption_resumes_without_generation(self):
        self.prepare()
        with patch.object(self.loop, '_bind', side_effect=KeyboardInterrupt):
            self.loop.transitions = self.loop._transitions()
            with self.assertRaises(KeyboardInterrupt): self.execute()
        self.assertEqual(0, self.calls())
        self.loop.transitions = self.loop._transitions()
        self.assertEqual("EXECUTION_PASS", self.execute()["state"])
        self.assertEqual(3, self.calls())

    def test_build_and_case_interruptions_reuse_complete_adapter_evidence(self):
        self.prepare()
        original = XceliumAdapter.run
        calls = 0
        def interrupt(adapter, execution_id, configuration):
            nonlocal calls
            calls += 1
            if calls == 2: raise KeyboardInterrupt()
            return original(adapter, execution_id, configuration)
        with patch.object(XceliumAdapter, 'run', interrupt):
            with self.assertRaises(KeyboardInterrupt): self.execute()
        self.assertEqual(2, self.calls())
        self.assertEqual("EXECUTION_PASS", self.execute()["state"])
        self.assertEqual(3, self.calls())

    def test_interrupt_after_build_does_not_recompile(self):
        self.prepare()
        with patch.object(XceliumAdapter, 'run', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.execute()
        self.assertEqual(1, self.calls())
        self.assertEqual("EXECUTION_PASS", self.execute()["state"])
        self.assertEqual(3, self.calls())

    def test_compile_failure_never_runs_testcases(self):
        (self.root / 'uvm/pkg.sv').write_text((self.root / 'uvm/pkg.sv').read_text() + '// FAKE_COMPILE_FAILURE\n')
        self.prepare()
        result = self.execute()
        self.assertEqual("EXECUTION_FAIL", result["state"])
        self.assertEqual(0, result["summary"]["executed"])
        self.assertEqual(2, result["summary"]["blocked"])
        self.assertEqual(1, self.calls())

    def test_uvm_error_is_failure_even_with_marker_and_zero_exit(self):
        (self.root / 'uvm/pkg.sv').write_text((self.root / 'uvm/pkg.sv').read_text() + '// FAKE_UVM_FAILURE\n')
        self.prepare()
        result = self.execute()
        self.assertEqual("EXECUTION_FAIL", result["state"])
        self.assertEqual(2, result["summary"]["failed"])

    def test_missing_or_changed_frozen_input_is_rejected(self):
        self.prepare()
        with patch.object(self.loop, '_bind', side_effect=KeyboardInterrupt):
            self.loop.transitions = self.loop._transitions()
            with self.assertRaises(KeyboardInterrupt): self.execute()
        self.loop.transitions = self.loop._transitions()
        snapshot = load_document(self.job / EXECUTION_INPUT_PATH)
        (self.job / snapshot["uvm"][0]["path"]).write_text("drift")
        with self.assertRaises(ProjectJobError) as caught: self.execute()
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        self.assertEqual(0, self.calls())

    def test_terminal_generated_entry_tampering_is_rejected(self):
        self.prepare()
        self.execute()
        (self.job / "execution/inputs/project_tests_pkg.sv").write_text("drift")
        with self.assertRaises(ProjectJobError): self.execute()

    def test_terminal_log_tampering_is_rejected(self):
        self.prepare()
        result = self.execute()
        log = result["testcases"][0]["logs"][0]["relative_path"]
        (self.job / log).write_text('tampered')
        with self.assertRaises(ProjectJobError): self.execute()

    def test_skips_and_findings_do_not_block_but_never_imply_full_verification(self):
        original = self.generator.stage3
        def partial(payload):
            value = original(payload)
            skipped = value["implemented_testcase_ids"].pop()
            value["code_units"] = [u for u in value["code_units"] if skipped not in u['testcase_ids']]
            value['assembly'] = list(range(len(value['code_units'])))
            value["skipped_testcases"] = [{"testcase_id": skipped, "reason_kind": "BLOCKED_CONTRACT",
                                             "reason": "Missing public observation", "routing_required": True}]
            return value
        self.generator.stage3 = partial
        report = self.reviewer._report
        def findings(review):
            value = report(review)
            ac = review["scenario_ac_map"]["acceptance_criteria"][0]
            value["verdict"] = "FINDINGS_REPORTED"
            value["findings"] = [{"severity": "ERROR", "suspected_origin_stage": "SPEC",
                "affected": {"scenario_ids": ac["scenario_ids"], "ac_ids": [ac["ac_id"]],
                             "testcase_ids": [], "code_unit_ids": []},
                "spec_evidence": [{k: ac["spec_evidence"][0][k] for k in ("path", "line_start", "line_end")}],
                "testcase_evidence": [], "problem_and_required_change": "Public observation is unspecified."}]
            return value
        self.reviewer._report = findings
        self.prepare()
        result = self.execute()
        self.assertEqual("EXECUTION_PASS", result["state"])
        self.assertEqual(1, result["summary"]["passed"])
        self.assertEqual(1, result["summary"]["skipped"])
        self.assertFalse(result["summary"]["full_verification_passed"])

    def test_zero_executable_cases_are_blocked_without_xcelium(self):
        def skipped(payload):
            return {"code_units": [{"role": "SHARED", "testcase_ids": [], "content": "// No executable testcase\n"}],
                    "assembly": [0], "implemented_testcase_ids": [],
                    "skipped_testcases": [{"testcase_id": t["testcase_id"], "reason_kind": "BLOCKED_CONTRACT",
                                           "reason": "No public driver", "routing_required": True}
                                          for t in payload["generated_testcase_manifest"]["testcases"]]}
        self.generator.stage3 = skipped
        def omitted(review):
            ac = review["scenario_ac_map"]["acceptance_criteria"][0]
            evidence = [{k: ac["spec_evidence"][0][k] for k in ("path", "line_start", "line_end")}]
            return {"verdict": "FINDINGS_REPORTED", "findings": [{
                        "severity": "WARNING", "suspected_origin_stage": "STAGE_3",
                        "affected": {"scenario_ids": ac["scenario_ids"], "ac_ids": [ac["ac_id"]],
                                     "testcase_ids": [], "code_unit_ids": []},
                        "spec_evidence": evidence, "testcase_evidence": [],
                        "problem_and_required_change": "No public driver: coverage omitted."}], "diagnostics": [],
                    "ac_reviews": [{"ac_id": ac["ac_id"], "status": "OMITTED",
                        "spec_evidence": [{k: ac["spec_evidence"][0][k] for k in ("path", "line_start", "line_end")}],
                        "stimulus_evidence": [], "checker_evidence": [], "omission": "No public driver"}]}
        self.reviewer._report = omitted
        self.prepare()
        result = self.execute()
        self.assertEqual("EXECUTION_BLOCKED", result["state"])
        self.assertEqual(0, result["summary"]["executed"])
        self.assertEqual(2, result["summary"]["skipped"])
        self.assertFalse(result["summary"]["full_verification_passed"])
        self.assertEqual(0, self.calls())

    def test_run_timeout_is_recorded_for_each_case(self):
        self.prepare()
        import subprocess
        original = subprocess.Popen.communicate
        def timeout(process, *args, **kwargs):
            if "-elaborate" not in process.args and not getattr(process, "injected_timeout", False):
                process.injected_timeout = True
                raise subprocess.TimeoutExpired(process.args, kwargs["timeout"])
            return original(process, *args, **kwargs)
        with patch.object(subprocess.Popen, "communicate", timeout):
            result = self.execute()
        self.assertEqual("EXECUTION_BLOCKED", result["state"])
        self.assertEqual(2, result["summary"]["blocked"])
        for row in result["testcases"]:
            self.assertIn("TIMEOUT", " ".join(row["diagnostic_codes"]))



if __name__ == '__main__': unittest.main()
