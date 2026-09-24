#!/usr/bin/env python3
"""Stage-local UVM commit and final-review tests."""
from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from contracts.validator import (
    load_document,
)
from runtime.commit_runtime import (
    COMMIT_PATH,
    FINAL_PATH,
    ProjectCommitRuntime,
)
from runtime.errors import ProjectJobError
from tests.agent_runtime.test_oches002_repair_runtime import (
    Oches002RepairRuntimeTests,
)
from tests.agent_runtime.test_project_job_workflow import (
    FakeReviewerProvider, FakeUvmProvider,
)


class FinalSemanticReviewer(FakeReviewerProvider):
    """Select final evidence from committed SV, never from generator fields."""

    def _report(self, review):
        map1 = review["scenario_ac_map"]
        coverage = {
            item["ac_id"]: item
            for item in review["ac_testcase_map"]["index"]["ac_coverage"]
        }
        lines = review["testcase_candidate"]["content"].splitlines()
        stimulus = next(line for line in lines if "clk = 1'b1;" in line)
        checker = next(line for line in lines if "AC_TINY_HIGH_FAIL" in line)
        return {
            "verdict": "CLEAN",
            "findings": [],
            "ac_reviews": [{
                "ac_id": ac["ac_id"],
                "status": "COVERED",
                "spec_evidence": [{
                    key: ac["spec_evidence"][0][key]
                    for key in ("path", "line_start", "line_end")
                }],
                "stimulus_evidence": [{"content": stimulus}],
                "checker_evidence": [{"content": checker}],
                "omission": "",
            } for ac in map1["acceptance_criteria"]
              if ac["ac_id"] in coverage],
            "diagnostics": [],
        }


class Oches003CommitRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Oches002RepairRuntimeTests(methodName="runTest")
        self.fixture.setUp()
        testcase_id = next(
            unit["unit_id"] for unit in self.fixture.runtime.model.units.values()
            if unit["unit_kind"] == "LOGICAL_TESTCASE")
        self.repair = self.fixture.runtime.run(
            orchestrator_provider=self.fixture.orchestrator_provider({
                "kind": "TESTCASE", "id": testcase_id}, stage="STAGE_2"),
            stage_provider_factory=lambda dispatch:
                self.fixture.stage_provider(dispatch),
            planning_session_id="PLANNING.OCHES003.001",
            stage_session_id="STAGESESSION.OCHES003.001")
        self.job = self.fixture.job
        self.value = self.fixture.value
        self.reviewer = FinalSemanticReviewer()
        self.uvm = FakeUvmProvider()
        self.verilator_patch = patch(
            "adapters.eda.ProjectVerilatorRunner",
            side_effect=AssertionError("UVM repair must not invoke Verilator"))
        self.verilator = self.verilator_patch.start()
        self.addCleanup(self.verilator_patch.stop)

    def tearDown(self):
        self.fixture.tearDown()

    def runtime(self, checkpoint_hook=None):
        return ProjectCommitRuntime(
            workspace_root=self.fixture.fixture.root,
            result_root=self.fixture.fixture.root / "result",
            provider_factory=lambda _root, _value, role: (
                self.uvm if role.endswith(".uvm") else
                self.reviewer if role.startswith("review.") else
                self.fixture.generator),
            checkpoint_hook=checkpoint_hook)

    def test_uvm_commit_impact_final_review_and_replay_without_verilator(self):
        runtime = self.runtime()
        result = runtime.advance(self.value["job_id"])

        self.assertEqual("READY_FOR_EXECUTION_PREPARATION", result["state"])
        self.assertFalse((self.job / COMMIT_PATH).exists())
        self.assertTrue((self.job / FINAL_PATH).is_file())
        self.assertTrue((self.job /
                         "audit/project_review_complete.json").is_file())
        candidate = load_document(self.job / result["candidate_metadata_path"])
        request = load_document(self.job / result["review_request_path"])
        self.assertNotIn("implemented_ac_evidence", candidate)
        self.assertEqual("PASS", candidate["validation"]["status"])
        self.assertEqual([
            "ORCHESTRATOR_PLAN", "ROUTER_RECEIPT", "FORMAL_DISPATCH",
            "SCOPED_REPLACEMENT", "VALIDATION_RESULT", "GROUP_COMMIT",
            "IMPACT_RESULT", "REPAIR_EPISODE", "VALIDATION_RESULT",
            "GROUP_COMMIT", "IMPACT_RESULT", "REPAIR_EPISODE",
        ], [item["record_type"] for item in request["repair_lineage"]])
        self.verilator.assert_not_called()
        self.assertEqual(1, self.reviewer.calls)
        records = [load_document(path) for path in sorted(
            (self.job / "audit/repair_records").glob("*.json"))]
        commits = [item for item in records
                   if item["record_type"] == "GROUP_COMMIT"]
        self.assertEqual(["COMMITTED", "COMMITTED"], [
            item["payload"]["status"] for item in commits])
        self.assertNotEqual(
            commits[0]["payload"]["before_roots"]["ac_testcase_map"],
            commits[0]["payload"]["current_roots"]["ac_testcase_map"])
        self.assertEqual(
            commits[0]["payload"]["current_roots"]["ac_testcase_map"],
            commits[1]["payload"]["current_roots"]["ac_testcase_map"])
        self.assertNotEqual(
            commits[1]["payload"]["before_roots"]["testcase"],
            commits[1]["payload"]["current_roots"]["testcase"])
        states = [
            load_document(path) for path in sorted(self.job.glob(
                "audit/job_regeneration_state.*.json"))]
        self.assertEqual(
            ["INITIAL_GENERATION_DONE", "REGENERATION_STARTED",
             "FINAL_REVIEW_DONE"],
            [item["event"] for item in states])
        self.assertEqual(
            states[-1]["state_fingerprint"],
            result["regeneration_state_fingerprint"])

        replay = runtime.advance(self.value["job_id"])
        self.assertEqual(result, replay)
        self.verilator.assert_not_called()
        self.assertEqual(1, self.reviewer.calls)

    def test_restart_after_stage2_commit_does_not_repeat_stage2_provider(self):
        stage2_transcripts = self.job / "transcripts/stage2"
        before = sorted(path.read_bytes() for path in stage2_transcripts.rglob(
            "*.json"))

        def interrupt(stage, _checkpoint):
            if stage == "STAGE_2_COMMITTED":
                raise RuntimeError("injected stage2 checkpoint interruption")

        with self.assertRaisesRegex(RuntimeError, "injected stage2"):
            self.runtime(checkpoint_hook=interrupt).advance(
                self.value["job_id"])
        self.verilator.assert_not_called()
        self.assertEqual(0, self.reviewer.calls)
        after_commit = sorted(path.read_bytes() for path in
                              stage2_transcripts.rglob("*.json"))
        self.assertEqual(before, after_commit)

        result = self.runtime().advance(self.value["job_id"])
        self.assertEqual("READY_FOR_EXECUTION_PREPARATION", result["state"])
        after_restart = sorted(path.read_bytes() for path in
                               stage2_transcripts.rglob("*.json"))
        self.assertEqual(after_commit, after_restart)
        self.verilator.assert_not_called()
        self.assertEqual(1, self.reviewer.calls)

    def test_tampered_replacement_fails_before_compile(self):
        replacement_path = self.job / self.repair["replacement_path"]
        replacement = load_document(replacement_path)
        replacement["replacements"][0]["semantic_body"] = {
            "segments": ["tampered"]}
        replacement_path.write_text(
            json.dumps(replacement, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")

        with self.assertRaises(ProjectJobError) as caught:
            self.runtime().advance(self.value["job_id"])
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        self.verilator.assert_not_called()
        self.assertFalse((self.job / COMMIT_PATH).exists())


if __name__ == "__main__":
    unittest.main()


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(Oches003CommitRuntimeTests)
