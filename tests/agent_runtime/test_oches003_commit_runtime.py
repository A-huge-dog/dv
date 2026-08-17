#!/usr/bin/env python3
"""Stage-local commit, deferred Stage 3 build, and final-review tests."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest

from contracts.validator import (
    eda_probe_evidence_fingerprint,
    eda_probe_request_fingerprint,
    load_document,
)
from core.project_commit_runtime import (
    COMMIT_PATH,
    FINAL_PATH,
    ProjectCommitRuntime,
)
from core.project_job import ProjectJobError
from tests.agent_runtime.test_oches002_repair_runtime import (
    Oches002RepairRuntimeTests,
)
from tests.agent_runtime.test_project_job_workflow import FakeReviewerProvider


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


class FakeCompileRunner:
    def __init__(self, workspace_root, project_input, status="PASS"):
        self.workspace_root = workspace_root
        self.project_input = project_input
        self.status = status
        self.calls = 0

    def build_only(self, source_paths, _top, approval_ref, authority):
        self.calls += 1
        source_fingerprints = [{
            "path": path,
            "fingerprint": hashlib.sha256(
                (self.workspace_root / path).read_bytes()).hexdigest(),
        } for path in sorted(source_paths)]
        request = {
            "schema_version": "1.0",
            "request_id": "EDAPROBE.PROJECT.TINY.PRECOMMIT.{}".format(
                authority[:16].upper()),
            "job_id": self.project_input["job_id"],
            "probe_kind": "PROJECT_BUILD",
            "executable_ref": "EDAEXEC.VERILATOR",
            "argv_template": [
                {"kind": "SOURCE", "value": path}
                for path in sorted(source_paths)],
            "environment_fingerprint":
                self.project_input["eda"]["environment_fingerprint"],
            "timeout_seconds": 60,
            "resource_limits": {
                "cpu_seconds": 60, "memory_mb": 512, "output_files": 16,
            },
            "output_subdir": "runs/oches003/fake/build",
            "expected_artifacts": [],
            "source_fingerprints": source_fingerprints,
            "approval_ref": approval_ref,
            "request_fingerprint": "0" * 64,
        }
        request["request_fingerprint"] = eda_probe_request_fingerprint(request)
        empty = hashlib.sha256(b"").hexdigest()
        failed = self.status != "PASS"
        evidence = {
            "schema_version": "1.0",
            "evidence_id": "EVIDENCE.EDA.FAKE{}".format(self.status),
            "request_id": request["request_id"],
            "request_fingerprint": request["request_fingerprint"],
            "job_id": self.project_input["job_id"],
            "probe_kind": "PROJECT_BUILD",
            "executable_ref": "EDAEXEC.VERILATOR",
            "environment_fingerprint": request["environment_fingerprint"],
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": "2026-01-01T00:00:01Z",
            "exit_code": 1 if failed else 0,
            "execution_status": self.status,
            "timed_out": False,
            "logs": [{
                "kind": kind,
                "relative_path":
                    "runs/oches003/fake/{}.log".format(kind.casefold()),
                "fingerprint": empty,
                "size_bytes": 0,
            } for kind in ("STDERR", "STDOUT")],
            "artifacts": [],
            "diagnostic_codes": ["PROCESS_EXIT_NONZERO"] if failed else [],
            "evidence_class": "REAL_EDA_QUALIFICATION",
            "qualification_scope": "PROJECT_VERILATOR_EXECUTION",
            "evidence_fingerprint": "0" * 64,
        }
        evidence["evidence_fingerprint"] = \
            eda_probe_evidence_fingerprint(evidence)
        return {"request": request, "evidence": evidence}


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
        self.compile_runner = None

    def tearDown(self):
        self.fixture.tearDown()

    def runtime(self, status="PASS", checkpoint_hook=None):
        def compile_factory(workspace_root, _result_root, project_input):
            self.compile_runner = FakeCompileRunner(
                workspace_root, project_input, status)
            return self.compile_runner

        return ProjectCommitRuntime(
            workspace_root=self.fixture.fixture.root,
            result_root=self.fixture.fixture.root / "result",
            provider_factory=lambda *_: self.reviewer,
            compile_runner_factory=compile_factory,
            checkpoint_hook=checkpoint_hook)

    def test_compile_commit_impact_final_review_and_replay(self):
        runtime = self.runtime()
        result = runtime.advance(self.value["job_id"])

        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        self.assertFalse((self.job / COMMIT_PATH).exists())
        self.assertTrue((self.job / FINAL_PATH).is_file())
        self.assertTrue((self.job /
                         "audit/oches001_human_review_checkpoint.json").is_file())
        candidate = load_document(self.job / result["candidate_metadata_path"])
        request = load_document(self.job / result["review_request_path"])
        self.assertNotIn("implemented_ac_evidence", candidate)
        self.assertEqual("FINAL_REVIEW_REQUIRED", candidate["traceability_status"])
        self.assertEqual([
            "ORCHESTRATOR_PLAN", "ROUTER_RECEIPT", "FORMAL_DISPATCH",
            "SCOPED_REPLACEMENT", "VALIDATION_RESULT", "GROUP_COMMIT",
            "IMPACT_RESULT", "REPAIR_EPISODE", "VALIDATION_RESULT",
            "GROUP_COMMIT", "IMPACT_RESULT", "REPAIR_EPISODE",
        ], [item["record_type"] for item in request["repair_lineage"]])
        self.assertEqual(1, self.compile_runner.calls)
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
        self.assertEqual(1, self.compile_runner.calls)
        self.assertEqual(1, self.reviewer.calls)

    def test_compile_failure_preserves_stage2_authority_and_skips_reviewer(self):
        before = copy.deepcopy(self.fixture.runtime.model.artifact_roots)
        runtime = self.runtime("FAIL")
        result = runtime.advance(self.value["job_id"])

        self.assertEqual("PAUSED_COMPILE_REPAIR_REQUIRED", result["state"])
        self.assertFalse((self.job / COMMIT_PATH).exists())
        self.assertFalse((self.job / FINAL_PATH).exists())
        self.assertNotEqual(before["ac_testcase_map"], result[
            "artifact_roots"]["ac_testcase_map"])
        self.assertEqual(before["testcase"], result[
            "artifact_roots"]["testcase"])
        self.assertEqual(0, self.reviewer.calls)
        replay = runtime.advance(self.value["job_id"])
        self.assertEqual(result, replay)
        self.assertEqual(1, self.compile_runner.calls)
        self.assertEqual(0, self.reviewer.calls)

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
        self.assertIsNone(self.compile_runner)
        self.assertEqual(0, self.reviewer.calls)
        after_commit = sorted(path.read_bytes() for path in
                              stage2_transcripts.rglob("*.json"))
        self.assertEqual(before, after_commit)

        result = self.runtime().advance(self.value["job_id"])
        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        after_restart = sorted(path.read_bytes() for path in
                               stage2_transcripts.rglob("*.json"))
        self.assertEqual(after_commit, after_restart)
        self.assertEqual(1, self.compile_runner.calls)
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
        self.assertIsNone(self.compile_runner)
        self.assertFalse((self.job / COMMIT_PATH).exists())


if __name__ == "__main__":
    unittest.main()


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(Oches003CommitRuntimeTests)
