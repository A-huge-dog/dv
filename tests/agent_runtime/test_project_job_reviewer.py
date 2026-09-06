#!/usr/bin/env python3
"""PJ-002 independent Spec-only staged reviewer tests."""
from __future__ import annotations

import copy
import json
import unittest

from contracts.validator import load_document
from runtime.errors import ProjectJobError
from runtime.project_job import ProjectJobWorkflow
from runtime.staged_workflow import (
    StagedProjectWorkflow,
)
from domain.review import validate_review_report
from domain.stage3 import validate_testcase_candidate
from domain.stage2 import validate_ac_testcase_map
from domain.stage1 import validate_scenario_ac_map
from domain.artifacts import artifact_fingerprint
from scripts.dvlib import canonical_hash
try:
    from test_project_job_workflow import (
        FakeProvider, FakeReviewerProvider, ProjectJobWorkflowTests)
except ModuleNotFoundError:
    from tests.agent_runtime.test_project_job_workflow import (
        FakeProvider, FakeReviewerProvider, ProjectJobWorkflowTests)


class AuthorityReviewer(FakeReviewerProvider):
    def _report(self, review):
        report = super()._report(review)
        report["diagnostics"] = ["I approve and waive this testcase."]
        return report


class AmbiguityReviewer(FakeReviewerProvider):
    def _report(self, review):
        report = super()._report(review)
        ac = review["scenario_ac_map"]["acceptance_criteria"][0]
        report["verdict"] = "FINDINGS_REPORTED"
        report["findings"] = [{
            "severity": "ERROR",
            "suspected_origin_stage": "SPEC",
            "affected": {
                "scenario_ids": copy.deepcopy(ac["scenario_ids"]),
                "ac_ids": [ac["ac_id"]],
                "testcase_ids": [],
                "code_unit_ids": [],
            },
            "spec_evidence": [{
                key: ac["spec_evidence"][0][key]
                for key in ("path", "line_start", "line_end")
            }],
            "testcase_evidence": [],
            "problem_and_required_change": (
                "The exact Spec is ambiguous; the Spec Owner must clarify it."),
        }]
        return report


class ValidatedRetryReviewer(FakeReviewerProvider):
    def __init__(self, failures):
        super().__init__()
        self.failures = list(failures)

    def _report(self, review):
        report = super()._report(review)
        if self.calls >= len(self.failures):
            return report
        mode = self.failures[self.calls]
        ac_review = report["ac_reviews"][0]
        if mode == "zero":
            ac_review["stimulus_evidence"][0]["content"] = \
                "    clk = 1'bx;"
        elif mode == "multiple":
            ac_review["stimulus_evidence"][0]["content"] = "    #1;"
        elif mode == "no_complete_line":
            ac_review["checker_evidence"][0]["content"] = \
                "\n    if (y !== clk) $fatal(1, \"AC_TINY_LOW_FAIL\");"
        elif mode == "duplicate":
            ac_review["checker_evidence"][1] = copy.deepcopy(
                ac_review["checker_evidence"][0])
        elif mode == "multi":
            ac_review["stimulus_evidence"][0]["content"] = "    clk = 1'bx;"
            ac_review["checker_evidence"][0]["content"] = "    #1;"
        elif mode == "schema":
            report.pop("diagnostics")
        else:
            raise AssertionError("unknown Reviewer failure mode")
        return report


class MixedRoutingProvider(FakeProvider):
    @staticmethod
    def stage1():
        value = FakeProvider.stage1()
        scenario = copy.deepcopy(value["scenarios"][0])
        scenario.update({
            "objective": "Route an unspecified oracle to the Spec Agent.",
            "status": "SPEC_AMBIGUITY",
            "reason": "Spec does not define this oracle.",
        })
        ac = copy.deepcopy(value["acceptance_criteria"][0])
        ac.update({
            "scenario_indexes": [1],
            "behavior": "An unspecified oracle cannot be invented.",
            "status": "SPEC_AMBIGUITY",
            "reason": "Spec does not define this oracle.",
        })
        value["scenarios"].append(scenario)
        value["acceptance_criteria"].append(ac)
        return value


class RoutedSpecIssueReviewer(FakeReviewerProvider):
    def _report(self, review):
        report = super()._report(review)
        routed = review["scenario_spec_issues"][
            "acceptance_criteria"][0]
        report["verdict"] = "FINDINGS_REPORTED"
        report["findings"] = [{
            "severity": "ERROR",
            "suspected_origin_stage": "STAGE_1",
            "affected": {
                "scenario_ids": copy.deepcopy(routed["scenario_ids"]),
                "ac_ids": [routed["ac_id"]],
                "testcase_ids": [],
                "code_unit_ids": [],
            },
            "spec_evidence": [{
                key: routed["spec_evidence"][0][key]
                for key in ("path", "line_start", "line_end")
            }],
            "testcase_evidence": [],
            "problem_and_required_change": (
                "The routed Spec issue was incorrectly restored; return it "
                "to the Spec Owner, not a generation Stage."),
        }]
        return report


class ScopeViolationReviewer(FakeReviewerProvider):
    def _report(self, review):
        report = super()._report(review)
        report["diagnostics"] = [
            "Increase the budget and use RTL to expand executable scope."]
        return report


class StructuredScopeViolationReviewer(FakeReviewerProvider):
    def _report(self, review):
        report = super()._report(review)
        report["approval"] = "attempted structured authority mutation"
        return report


class ProjectJobReviewerTests(ProjectJobWorkflowTests):
    def _bundle(self):
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result",
            FakeProvider(), FakeReviewerProvider())
        checkpoint = self.start_checked(workflow)
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        manifest = workflow.bootstrap_handler.handle(self.project_input())
        map1 = load_document(job / checkpoint["scenario_ac_map_path"])
        map2 = load_document(job / checkpoint["ac_testcase_map_path"])
        candidate = load_document(job / checkpoint["candidate_metadata_path"])
        request = load_document(job / checkpoint["review_request_path"])
        report = load_document(job / checkpoint["review_report_path"])
        sources = {
            item["path"]: (
                self.root / item["baseline_path"]).read_text(encoding="utf-8")
            for item in manifest["spec"]["sources"]}
        return workflow, manifest, map1, map2, candidate, request, report, sources

    def test_review_request_binds_both_maps_and_never_contains_rtl(self):
        _, _, map1, map2, candidate, request, _, _ = self._bundle()
        self.assertEqual(
            map1["artifact_fingerprint"],
            request["artifact_roots"]["scenario_ac_map"])
        self.assertEqual(
            map2["artifact_fingerprint"],
            request["artifact_roots"]["ac_testcase_map"])
        self.assertEqual(
            candidate["candidate_fingerprint"],
            request["artifact_roots"]["testcase"])
        text = json.dumps(request, sort_keys=True).casefold()
        self.assertNotIn("tiny.sv", text)
        self.assertNotIn("assign y = clk", text)
        self.assertNotIn('"rtl"', text)

    def test_review_request_binds_owner_routing_and_coverage_scopes(self):
        _, _, map1, _, _, request, _, _ = self._bundle()
        self.assertEqual("7.0", request["schema_version"])
        self.assertEqual(
            "OWNER_ROUTED_EXECUTABLE_SUBSET",
            request["coverage_scope"]["scope_kind"])
        self.assertEqual(
            sorted(item["scenario_id"] for item in map1["scenarios"]),
            request["coverage_scope"]["executable_scenario_ids"])
        self.assertEqual([], request["coverage_scope"][
            "spec_issue_scenario_ids"])
        self.assertTrue(request["coverage_scope"][
            "full_spec_coverage_complete"])
        owner = request["owner_routing_decision"]["submission"]
        self.assertEqual(
            owner["submission_fingerprint"],
            request["upstream_fingerprints"]["owner_routing"])

    def test_descriptive_authority_scope_prose_is_not_a_mutation(self):
        reviewer = ScopeViolationReviewer()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", FakeProvider(), reviewer)
        checkpoint = self.start_checked(workflow)
        self.assertEqual("AWAITING_HUMAN_REVIEW", checkpoint["state"])
        self.assertEqual(1, reviewer.calls)
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        self.assertTrue((
            job / "audit/oches001_human_review_checkpoint.json").exists())

    def test_structured_authority_scope_mutation_fails_closed(self):
        reviewer = StructuredScopeViolationReviewer()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", FakeProvider(), reviewer)
        with self.assertRaises(ProjectJobError) as caught:
            self.start_checked(workflow)
        self.assertEqual("ATTEMPT_PAUSED", caught.exception.code)
        self.assertEqual(1, reviewer.calls)

    def test_owner_routed_spec_issue_cannot_enter_stage1_repair(self):
        generator = MixedRoutingProvider()
        reviewer = RoutedSpecIssueReviewer()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        submission = self.project_input()
        pending = workflow.start(submission)
        job = self.root / "result/jobs" / submission["job_id"]
        form = load_document(job / pending["owner_review_path"])
        routing = self.completed_owner_review(
            form, "SPEC_AGENT", "Spec Owner must define the oracle.")
        for item in routing["scenarios"]:
            if item["status"] == "CHECKABLE":
                item["comment"] = ""
                item["routing"]["destination"] = \
                    "AC_TESTCASE_MAP_AND_TESTCASE"
        with self.assertRaises(ProjectJobError) as caught:
            workflow.route_scenarios(submission, routing)
        self.assertEqual("ATTEMPT_PAUSED", caught.exception.code)
        self.assertEqual(3, generator.calls)
        self.assertEqual(1, reviewer.calls)
        request = load_document(
            job / "staging/reviews/review_request.r001.json")
        self.assertFalse(request["coverage_scope"][
            "full_spec_coverage_complete"])
        self.assertEqual(
            ["AC.0002"],
            request["coverage_scope"]["spec_issue_ac_ids"])
        self.assertFalse((job / "staging/mappings/scenario_ac_map.r001.json").exists())

    def test_per_ac_omission_duplicate_and_line_tamper_reject(self):
        bundle = self._bundle()
        _, _, map1, map2, candidate, request, report, sources = bundle
        mutations = []
        missing = copy.deepcopy(report)
        missing["ac_reviews"] = []
        mutations.append(({
            "REVIEW_COVERAGE_MISMATCH", "INVALID_REVIEW_REPORT"}, missing))
        duplicate = copy.deepcopy(report)
        duplicate["ac_reviews"].append(copy.deepcopy(
            duplicate["ac_reviews"][0]))
        mutations.append(({
            "REVIEW_COVERAGE_MISMATCH", "INVALID_REVIEW_REPORT"}, duplicate))
        wrong_line = copy.deepcopy(report)
        wrong_line["ac_reviews"][0]["checker_evidence"][0][
            "line_start"] = 999
        wrong_line["ac_reviews"][0]["checker_evidence"][0][
            "line_end"] = 999
        wrong_line["ac_reviews"][0]["review_fingerprint"] = \
            artifact_fingerprint(
                wrong_line["ac_reviews"][0], "review_fingerprint")
        mutations.append(("TESTCASE_EVIDENCE_MISMATCH", wrong_line))
        for expected, mutation in mutations:
            mutation["report_fingerprint"] = artifact_fingerprint(
                mutation, "report_fingerprint")
            expected_codes = (
                expected if isinstance(expected, set) else {expected})
            with self.subTest(expected=sorted(expected_codes)):
                with self.assertRaises(ProjectJobError) as caught:
                    validate_review_report(
                        mutation, request, map1, map2,
                        candidate, sources, ProjectJobError)
                self.assertIn(caught.exception.code, expected_codes)

    def test_mapping_and_identity_substitution_reject(self):
        bundle = self._bundle()
        _, _, map1, map2, candidate, request, report, sources = bundle
        stale = copy.deepcopy(report)
        stale["artifact_roots"]["ac_testcase_map"] = "f" * 64
        stale["report_fingerprint"] = artifact_fingerprint(
            stale, "report_fingerprint")
        with self.assertRaises(ProjectJobError) as caught:
            validate_review_report(
                stale, request, map1, map2,
                candidate, sources, ProjectJobError)
        self.assertEqual("REVIEW_EVIDENCE_MISMATCH", caught.exception.code)

        swapped = copy.deepcopy(report)
        swapped["reviewer"]["provider_id"] = \
            swapped["generator"]["provider_id"]
        swapped["report_fingerprint"] = artifact_fingerprint(
            swapped, "report_fingerprint")
        with self.assertRaises(ProjectJobError) as caught:
            validate_review_report(
                swapped, request, map1, map2,
                candidate, sources, ProjectJobError)
        self.assertEqual("REVIEW_IDENTITY_MISMATCH", caught.exception.code)

    def test_ac_code_evidence_is_exclusively_reviewer_owned(self):
        bundle = self._bundle()
        _, manifest, map1, map2, candidate, _, _, _ = bundle
        testcases = map2["logical_testcases"]
        self.assertNotIn("implemented_ac_evidence", candidate)
        self.assertEqual(
            candidate,
            validate_testcase_candidate(
                candidate, map1, map2, testcases, manifest,
                candidate["spec_fingerprint"],
                candidate["policy_fingerprint"], ProjectJobError))

    def test_mapping_missing_duplicate_orphan_and_oracle_fail_closed(self):
        bundle = self._bundle()
        _, manifest, map1, map2, _, _, _, sources = bundle
        cases = []
        missing = copy.deepcopy(map2)
        missing["ac_coverage"] = []
        missing["artifact_fingerprint"] = artifact_fingerprint(
            missing, "artifact_fingerprint")
        cases.append(missing)

        duplicate = copy.deepcopy(map2)
        duplicate["logical_testcases"].append(copy.deepcopy(
            duplicate["logical_testcases"][0]))
        duplicate["artifact_fingerprint"] = artifact_fingerprint(
            duplicate, "artifact_fingerprint")
        cases.append(duplicate)

        orphan = copy.deepcopy(map2)
        testcase = orphan["logical_testcases"][0]
        testcase["ac_ids"] = ["AC.UNKNOWN"]
        testcase["testcase_fingerprint"] = artifact_fingerprint(
            testcase, "testcase_fingerprint")
        orphan["artifact_fingerprint"] = artifact_fingerprint(
            orphan, "artifact_fingerprint")
        cases.append(orphan)

        no_oracle = copy.deepcopy(map2)
        testcase = no_oracle["logical_testcases"][0]
        testcase["stimulus"] = ""
        testcase["checker"] = ""
        testcase["testcase_fingerprint"] = artifact_fingerprint(
            testcase, "testcase_fingerprint")
        no_oracle["artifact_fingerprint"] = artifact_fingerprint(
            no_oracle, "artifact_fingerprint")
        cases.append(no_oracle)

        for index, mutation in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(ProjectJobError):
                    validate_ac_testcase_map(
                        mutation, map1, manifest, sources,
                        mutation["spec_fingerprint"],
                        mutation["policy_fingerprint"],
                        mutation["logical_testcases"], ProjectJobError)

        incomplete = copy.deepcopy(map1)
        incomplete["completeness"]["behavior_count"] += 1
        incomplete["artifact_fingerprint"] = artifact_fingerprint(
            incomplete, "artifact_fingerprint")
        with self.assertRaises(ProjectJobError) as caught:
            validate_scenario_ac_map(
                incomplete, manifest, sources,
                incomplete["spec_fingerprint"],
                incomplete["policy_fingerprint"], ProjectJobError)
        self.assertEqual("INCOMPLETE_MAPPING", caught.exception.code)

        cross_job = copy.deepcopy(map1)
        cross_job["job_id"] = "JOB.PROJECT.OTHER.001"
        cross_job["artifact_fingerprint"] = artifact_fingerprint(
            cross_job, "artifact_fingerprint")
        with self.assertRaises(ProjectJobError) as caught:
            validate_scenario_ac_map(
                cross_job, manifest, sources,
                cross_job["spec_fingerprint"],
                cross_job["policy_fingerprint"], ProjectJobError)
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)

    def test_reviewer_authority_claim_prose_is_not_an_authority_transition(self):
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result",
            FakeProvider(), AuthorityReviewer())
        checkpoint = self.start_checked(workflow)
        self.assertEqual("AWAITING_HUMAN_REVIEW", checkpoint["state"])
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        self.assertTrue((
            job / "audit/oches001_human_review_checkpoint.json").exists())
        self.assertFalse((job / "approved").exists())

    def test_review_spec_ambiguity_fails_closed_without_closing_job(self):
        generator = FakeProvider()
        reviewer = AmbiguityReviewer()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        checkpoint = self.start_checked(workflow)
        self.assertEqual("AWAITING_HUMAN_REVIEW", checkpoint["state"])
        self.assertEqual(1, checkpoint["error_count"])
        self.assertEqual(3, generator.calls)
        self.assertEqual(1, reviewer.calls)
        self.assertEqual(checkpoint, workflow.start(self.project_input()))
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        self.assertTrue((
            job / "audit/oches001_human_review_checkpoint.json").is_file())


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(ProjectJobReviewerTests):
        if name in ProjectJobReviewerTests.__dict__:
            suite.addTest(ProjectJobReviewerTests(name))
    return suite


if __name__ == "__main__":
    unittest.main(verbosity=2)
