#!/usr/bin/env python3
"""OCHES001 Reviewer, one-regeneration, and Router qualification."""
from __future__ import annotations

import copy
import unittest

from contracts.validator import accepted, load_document, validate
from runtime.errors import ProjectJobError
from runtime.project_job import ProjectJobWorkflow
from domain.agent_binding import binding_lineage
from domain.repair import validate_repair_plan
from agents.project_tools import ProjectReadModel
from domain.artifacts import artifact_fingerprint
from scripts.dvlib import canonical_hash
try:
    from test_project_job_workflow import (
        FakeProvider, FakeReviewerProvider, ProjectJobWorkflowTests)
except ModuleNotFoundError:
    from tests.agent_runtime.test_project_job_workflow import (
        FakeProvider, FakeReviewerProvider, ProjectJobWorkflowTests)


class Oches001RepairControlTests(unittest.TestCase):
    setUp = ProjectJobWorkflowTests.setUp
    tearDown = ProjectJobWorkflowTests.tearDown
    project_input = ProjectJobWorkflowTests.project_input
    start_checked = ProjectJobWorkflowTests.start_checked
    completed_owner_review = staticmethod(
        ProjectJobWorkflowTests.completed_owner_review)
    def _awaiting(self, stage="TESTCASE"):
        generator = FakeProvider()
        reviewer = FakeReviewerProvider(revision_stage=stage)
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        submission = self.project_input()
        checkpoint = self.start_checked(workflow, submission)
        self.assertEqual("AWAITING_REPAIR_PLAN", checkpoint["state"])
        job = self.root / "result/jobs" / submission["job_id"]
        report = load_document(job / checkpoint["review_report_path"])
        request = load_document(job / checkpoint["review_request_path"])
        return (workflow, submission, job, checkpoint, report, request,
                generator, reviewer)

    def _plan(self, submission, report, request, target, stage="STAGE_3"):
        job_value = ProjectJobWorkflow(
            self.root, self.root / "result").bootstrap_handler.handle(
                submission, create=False)
        value = {
            "schema_version": "1.0",
            "plan_id": "REPAIRPLAN.TINY.001",
            "planning_session_id": "PLANNING.TINY.001",
            "job_id": submission["job_id"],
            "input_fingerprint": report["input_fingerprint"],
            "source_report_id": report["report_id"],
            "source_report_fingerprint": report["report_fingerprint"],
            "artifact_roots": copy.deepcopy(report["artifact_roots"]),
            "scope_fingerprint": request["coverage_scope"][
                "scope_fingerprint"],
            "retrieval_rounds": 1,
            "status": "READY",
            "orchestrator": {
                "runtime_role": "ORCHESTRATOR",
                "model_class": "PROFILED",
                **binding_lineage(job_value, "repair", "orchestrator"),
                "request_id": "REQUEST.ORCHESTRATOR.001",
                "response_id": "RESPONSE.ORCHESTRATOR.001",
            },
            "repairs": [{
                "issue_ids": [report["findings"][0]["issue_id"]],
                "stage": stage,
                "targets": [target],
            }],
            "plan_fingerprint": "0" * 64,
        }
        value["plan_fingerprint"] = artifact_fingerprint(
            value, "plan_fingerprint")
        return value

    def test_accepted_plan_stops_before_scoped_replacement_commit(self):
        (workflow, submission, job, checkpoint, report, request,
         generator, reviewer) = self._awaiting()
        model = ProjectReadModel.from_checkpoint(job, checkpoint)
        target = {
            "kind": "TESTCASE",
            "id": next(item["unit_id"] for item in model.units.values()
                       if item["unit_kind"] == "LOGICAL_TESTCASE"),
        }
        plan = self._plan(
            submission, report, request, target, "STAGE_2")
        self.assertTrue(accepted(validate("project_repair_plan", plan)))
        result = workflow.submit_repair_plan(submission, plan)
        self.assertEqual("AWAITING_SCOPED_REPLACEMENT", result["state"])
        self.assertEqual(3, generator.calls)
        self.assertEqual(1, reviewer.calls)
        states = [
            load_document(path) for path in sorted(job.glob(
                "audit/job_regeneration_state.*.json"))]
        self.assertEqual(
            ["INITIAL_GENERATION_DONE"],
            [item["event"] for item in states])
        self.assertFalse(states[-1]["regeneration_used"])
        dispatch = load_document(job / result["dispatch_path"])
        self.assertEqual(
            dispatch["dispatch_fingerprint"],
            result["dispatch_fingerprint"])
        self.assertFalse(list(job.glob("staging/scoped_replacements/*.json")))
        reviewer_manifests = sorted(job.glob(
            "transcripts/reviewer/*/manifest.json"))
        self.assertEqual(1, len(reviewer_manifests))
        self.assertTrue(all(
            load_document(path)["terminal"]["status"] == "COMPLETED"
            for path in reviewer_manifests))
        self.assertEqual(result, workflow.submit_repair_plan(submission, plan))
        self.assertEqual(3, generator.calls)
        self.assertEqual(1, reviewer.calls)

    def test_router_rejects_wrong_stage_unknown_target_and_scope_expansion(self):
        workflow, submission, job, checkpoint, report, request, generator, reviewer = \
            self._awaiting()
        value = workflow.bootstrap_handler.handle(submission, create=False)
        model = ProjectReadModel.from_checkpoint(job, checkpoint)
        kind_map = {
            "SCENARIO": {"SCENARIO"},
            "ACCEPTANCE_CRITERION": {"ACCEPTANCE_CRITERION"},
            "TESTCASE": {"LOGICAL_TESTCASE"},
            "CODE_UNIT": {"CODE_SHARED", "CODE_TESTCASE"},
        }
        inventory = {
            target_kind: {
                unit["unit_id"] for unit in model.units.values()
                if unit["unit_kind"] in unit_kinds
            }
            for target_kind, unit_kinds in kind_map.items()
        }
        orchestrator = {
            "runtime_role": "ORCHESTRATOR", "model_class": "PROFILED",
            **binding_lineage(value, "repair", "orchestrator"),
        }
        stages = {
            stage: {
                "runtime_role": "STAGE_AGENT", "model_class": "PROFILED",
                **binding_lineage(
                    value, "repair", stage.replace("STAGE_", "stage")),
            }
            for stage in ("STAGE_1", "STAGE_2", "STAGE_3")
        }

        def route(plan):
            return validate_repair_plan(
                plan, value, report, model.artifact_roots,
                request["coverage_scope"]["scope_fingerprint"], inventory,
                model, orchestrator, stages, ProjectJobError)[0]

        wrong = self._plan(
            submission, report, request,
            {"kind": "CODE_UNIT", "id": "CODE.TESTCASE.MISSING"},
            "STAGE_1")
        receipt = route(wrong)
        self.assertEqual("REJECTED", receipt["status"])
        self.assertEqual("WRONG_STAGE_TARGET_KIND", receipt["diagnostic"]["code"])
        self.assertEqual(3, generator.calls)
        self.assertEqual(1, reviewer.calls)

        expanded = self._plan(
            submission, report, request,
            {"kind": "TESTCASE", "id": "TC.TINY.Y_FOLLOWS_CLK"},
            "STAGE_2")
        expanded["planning_session_id"] = "PLANNING.TINY.002"
        expanded["plan_id"] = "REPAIRPLAN.TINY.002"
        expanded["scope_fingerprint"] = "2" * 64
        expanded["plan_fingerprint"] = artifact_fingerprint(
            expanded, "plan_fingerprint")
        receipt = route(expanded)
        self.assertEqual("SCOPE_EXPANSION", receipt["diagnostic"]["code"])

        unknown = self._plan(
            submission, report, request,
            {"kind": "TESTCASE", "id": "TC.UNKNOWN"}, "STAGE_2")
        unknown["planning_session_id"] = "PLANNING.TINY.003"
        unknown["plan_id"] = "REPAIRPLAN.TINY.003"
        unknown["plan_fingerprint"] = artifact_fingerprint(
            unknown, "plan_fingerprint")
        receipt = route(unknown)
        self.assertEqual("UNKNOWN_TARGET", receipt["diagnostic"]["code"])

    def test_warning_does_not_consume_regeneration_and_old_fields_are_rejected(self):
        class WarningReviewer(FakeReviewerProvider):
            def _report(self, review):
                report = super()._report(review)
                ac = review["scenario_ac_map"]["acceptance_criteria"][0]
                evidence = {
                    key: ac["spec_evidence"][0][key]
                    for key in ("path", "line_start", "line_end")}
                report["verdict"] = "FINDINGS_REPORTED"
                report["findings"] = [{
                    "severity": "WARNING",
                    "suspected_origin_stage": "STAGE_3",
                    "affected": {
                        "scenario_ids": copy.deepcopy(ac["scenario_ids"]),
                        "ac_ids": [ac["ac_id"]],
                        "testcase_ids": [],
                        "code_unit_ids": [],
                    },
                    "spec_evidence": [evidence],
                    "testcase_evidence": [],
                    "problem_and_required_change": (
                        "Readability is weak; simplify the affected code in a "
                        "future Human-authorized change."),
                }]
                return report

        generator = FakeProvider()
        reviewer = WarningReviewer()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        checkpoint = self.start_checked(workflow)
        self.assertEqual("AWAITING_HUMAN_REVIEW", checkpoint["state"])
        self.assertEqual(0, checkpoint["error_count"])
        self.assertEqual(1, checkpoint["warning_count"])
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        states = [load_document(path) for path in sorted(job.glob(
            "audit/job_regeneration_state.*.json"))]
        self.assertFalse(states[-1]["regeneration_used"])
        raw = load_document(
            job / "audit/pj002_provider_response.review.r001.json")[
                "tool_calls"][0]["arguments"]
        split = copy.deepcopy(raw)
        split["findings"][0]["description"] = "obsolete split field"
        split["findings"][0]["required_correction"] = "obsolete split field"
        self.assertFalse(accepted(validate(
            "project_testcase_review_candidate", split)))
        info = copy.deepcopy(raw)
        info["findings"][0]["severity"] = "INFO"
        self.assertFalse(accepted(validate(
            "project_testcase_review_candidate", info)))


if __name__ == "__main__":
    unittest.main()


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(Oches001RepairControlTests)
