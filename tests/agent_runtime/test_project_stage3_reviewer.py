#!/usr/bin/env python3
"""PJ-002-HF5 standalone, source-bound Reviewer test Job tests."""
from __future__ import annotations

import copy
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from contracts.validator import load_document
from core.project_job import ProjectJobError, ProjectJobWorkflow
from core.project_stage3 import StandaloneStage3Workflow
from core.project_stage3_reviewer import StandaloneStage3ReviewerWorkflow
from tests.agent_runtime import test_project_job_reviewer as reviewer_tests
from tests.agent_runtime import test_project_job_workflow as workflow_tests
from tests.agent_runtime import test_project_stage3 as stage3_tests


class AlternateReviewerProvider(workflow_tests.FakeReviewerProvider):
    model_id = "fake-reviewer-alternate-model"


class ReviewerJobTests(unittest.TestCase):
    setUp = stage3_tests.StandaloneStage3Tests.setUp
    tearDown = stage3_tests.StandaloneStage3Tests.tearDown
    _submission = stage3_tests.StandaloneStage3Tests._submission
    _completed_owner_review = staticmethod(
        stage3_tests.StandaloneStage3Tests._completed_owner_review)
    _stage3_input = staticmethod(stage3_tests.StandaloneStage3Tests._stage3_input)
    _snapshot = staticmethod(stage3_tests.StandaloneStage3Tests._snapshot)

    def _completed_source(self):
        submission = self._submission()
        workflow = ProjectJobWorkflow(self.root, self.root / "result", workflow_tests.FakeProvider(), workflow_tests.FakeReviewerProvider())
        checkpoint = workflow.start(submission)
        form = load_document(self.root / "result/jobs" / submission["job_id"] /
                             checkpoint["owner_review_path"])
        return submission, workflow.route_scenarios(
            submission, self._completed_owner_review(form))

    @staticmethod
    def _review_input(job_id="JOB.PROJECT.TINY.REVIEWER"):
        return {
            "schema_version": "1.0", "job_id": job_id,
            "source": {
                "project_job_id": "JOB.PROJECT.TINY.SOURCE",
                "scenario_ac_map": "staging/mappings/scenario_ac_map.checked.r000.json",
                "ac_testcase_map": "staging/mappings/ac_testcase_map.r000.json",
            },
            "testcase_candidate": "testcase_inputs/testcase.r000.json",
            "reviewer_config": "config/reviewer.yaml",
            "input_authority": {"actor_type": "HUMAN", "identity": "human.stage3.owner",
                                "role": "STAGE3_TEST_OWNER", "decision": "APPROVE"},
        }

    def _independent_candidate(self) -> Path:
        source = self.root / "result/jobs/JOB.PROJECT.TINY.SOURCE/staging/generated/portable_sv/testcase.r000.json"
        candidate = self.root / "testcase_inputs/testcase.r000.json"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, candidate)
        return candidate

    def test_clean_result_is_source_read_only_and_replays(self):
        self._completed_source()
        self._independent_candidate()
        source = self.root / "result/jobs/JOB.PROJECT.TINY.SOURCE"
        before = self._snapshot(source)
        reviewer = workflow_tests.FakeReviewerProvider()
        runner = StandaloneStage3ReviewerWorkflow(ProjectJobWorkflow(
            self.root, self.root / "result", reviewer_provider=reviewer))
        value = self._review_input()
        raw = yaml.safe_dump(value, sort_keys=False).encode("utf-8")
        result = runner.run(value, raw)
        self.assertEqual("PASS", result["status"])
        self.assertEqual("CLEAN", result["reviewer_verdict"])
        self.assertEqual("TEST_ONLY_NO_PROMOTION_OR_EDA", result["qualification_scope"])
        test_root = self.root / "result/jobs/JOB.PROJECT.TINY.REVIEWER"
        self.assertFalse((test_root / "approved").exists())
        self.assertFalse((test_root / "runs").exists())
        self.assertFalse((test_root / "reports").exists())
        text = (test_root / result["review_request_path"]).read_text().casefold()
        self.assertNotIn("tiny.sv", text)
        self.assertNotIn("assign y", text)
        self.assertEqual(before, self._snapshot(source))
        self.assertEqual(result, runner.run(value, raw))
        self.assertEqual(1, reviewer.calls)
        self.assertEqual(before, self._snapshot(source))

    def test_config_and_source_candidate_drift_fail_before_review_call(self):
        self._completed_source()
        candidate = self._independent_candidate()
        reviewer = workflow_tests.FakeReviewerProvider()
        runner = StandaloneStage3ReviewerWorkflow(ProjectJobWorkflow(
            self.root, self.root / "result", reviewer_provider=reviewer))
        value = self._review_input()
        runner.run(value)
        changed = copy.deepcopy(value)
        changed["reviewer_config"] = "config/generator.yaml"
        with self.assertRaises(ProjectJobError) as stale:
            runner.run(changed)
        self.assertEqual("STALE_EVIDENCE", stale.exception.code)
        raw = candidate.read_text()
        candidate.write_text(raw.replace("testcase", "testcase_tampered", 1))
        with self.assertRaises(ProjectJobError) as tampered:
            runner.run(value)
        self.assertIn(tampered.exception.code, {"STALE_EVIDENCE", "INVALID_TESTCASE", "INVALID_SCHEMA"})
        self.assertEqual(1, reviewer.calls)

    def test_independent_candidate_is_required_and_path_safe(self):
        self._completed_source()
        self._independent_candidate()
        reviewer = workflow_tests.FakeReviewerProvider()
        runner = StandaloneStage3ReviewerWorkflow(ProjectJobWorkflow(
            self.root, self.root / "result", reviewer_provider=reviewer))
        nested = self._review_input()
        nested["source"]["testcase_candidate"] = nested.pop(
            "testcase_candidate")
        with self.assertRaises(ProjectJobError) as legacy:
            runner.run(nested)
        self.assertEqual("INVALID_SCHEMA", legacy.exception.code)
        bad = self._review_input()
        bad["testcase_candidate"] = "../tiny.sv"
        with self.assertRaises(ProjectJobError) as caught:
            runner.run(bad)
        self.assertEqual("INVALID_SCHEMA", caught.exception.code)
        self.assertEqual(0, reviewer.calls)

    def test_different_reviewer_model_probes_instead_of_reusing_source(self):
        self._completed_source()
        self._independent_candidate()
        config = yaml.safe_load(
            (self.root / "config/reviewer.yaml").read_text(encoding="utf-8"))
        config["model_id"] = AlternateReviewerProvider.model_id
        (self.root / "config/reviewer_alternate.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        value = self._review_input("JOB.PROJECT.TINY.ALTERNATE_REVIEWER")
        value["reviewer_config"] = "config/reviewer_alternate.yaml"
        reviewer = AlternateReviewerProvider()

        result = StandaloneStage3ReviewerWorkflow(ProjectJobWorkflow(
            self.root, self.root / "result", reviewer_provider=reviewer)).run(
                value)

        self.assertEqual("PASS", result["status"])
        self.assertEqual(1, reviewer.probe_calls)
        self.assertEqual(1, reviewer.restore_calls)
        self.assertEqual(1, reviewer.calls)
        probe = load_document(self.root / "result/jobs/"
                              "JOB.PROJECT.TINY.ALTERNATE_REVIEWER/audit/"
                              "reviewer_provider_probe.json")
        self.assertEqual(AlternateReviewerProvider.model_id, probe["model_id"])

    def test_invalid_report_and_valid_ambiguity_stay_test_only(self):
        self._completed_source()
        self._independent_candidate()
        source = self.root / "result/jobs/JOB.PROJECT.TINY.SOURCE"
        before = self._snapshot(source)
        retried = reviewer_tests.ValidatedRetryReviewer(["schema"])
        runner = StandaloneStage3ReviewerWorkflow(ProjectJobWorkflow(
            self.root, self.root / "result", reviewer_provider=retried))
        with self.assertRaises(ProjectJobError):
            runner.run(self._review_input("JOB.PROJECT.TINY.RETRY"))
        self.assertEqual(1, retried.calls)
        self.assertEqual(1, len(list((self.root / "result/jobs/JOB.PROJECT.TINY.RETRY/audit").glob(
            "pj002_rejected_review_response.*.json"))))
        ambiguous = reviewer_tests.AmbiguityReviewer()
        ambiguity = StandaloneStage3ReviewerWorkflow(ProjectJobWorkflow(
            self.root, self.root / "result", reviewer_provider=ambiguous)).run(
                self._review_input("JOB.PROJECT.TINY.AMBIGUITY"))
        self.assertEqual("FINDINGS_REPORTED", ambiguity["reviewer_verdict"])
        self.assertEqual(before, self._snapshot(source))


if __name__ == "__main__":
    unittest.main()
