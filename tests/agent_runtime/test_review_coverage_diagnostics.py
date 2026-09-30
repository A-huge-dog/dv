"""Reviewer coverage failures identify every affected AC and field."""
from __future__ import annotations

import copy
import json
import unittest

from contracts.validator import load_document
from domain.artifacts import artifact_fingerprint
from domain.review import build_review_report, validate_review_report
from runtime.errors import ProjectJobError
from runtime.project_job import ProjectJobWorkflow

try:
    from test_project_job_reviewer import ProjectJobReviewerTests
    from test_project_job_workflow import (
        FakeReviewerProvider, SharedCheckerProvider, SharedCheckerReviewer,
    )
except ModuleNotFoundError:
    from tests.agent_runtime.test_project_job_reviewer import (
        ProjectJobReviewerTests,
    )
    from tests.agent_runtime.test_project_job_workflow import (
        FakeReviewerProvider, SharedCheckerProvider, SharedCheckerReviewer,
    )


class CoverageRetryReviewer(SharedCheckerReviewer):
    def _report(self, review):
        report = super()._report(review)
        if self.calls == 0:
            report["ac_reviews"][0]["stimulus_evidence"] = []
            report["ac_reviews"][0]["checker_evidence"] = []
            report["ac_reviews"][1]["checker_evidence"] = []
        return report


class ReviewCoverageDiagnosticsTests(ProjectJobReviewerTests):
    def _coverage_bundle(self):
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result",
            SharedCheckerProvider(), SharedCheckerReviewer())
        checkpoint = self.start_checked(workflow)
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        manifest = workflow.bootstrap_handler.handle(self.project_input())
        sources = {
            item["path"]: (self.root / item["baseline_path"]).read_text(
                encoding="utf-8")
            for item in manifest["spec"]["sources"]
        }
        return {
            "map1": load_document(job / checkpoint["scenario_ac_map_path"]),
            "map2": load_document(job / checkpoint["ac_testcase_map_path"]),
            "candidate": load_document(
                job / checkpoint["candidate_metadata_path"]),
            "request": load_document(job / checkpoint["review_request_path"]),
            "report": load_document(job / checkpoint["review_report_path"]),
            "response": load_document(
                job / "audit/pj002_provider_response.review.r001.json"),
            "sources": sources,
        }

    def _build(self, bundle, response):
        return build_review_report(
            bundle["request"], bundle["candidate"], response,
            bundle["sources"], ProjectJobError)

    def _validate(self, bundle, report):
        return validate_review_report(
            report, bundle["request"], bundle["map1"], bundle["map2"],
            bundle["candidate"], bundle["sources"], ProjectJobError)

    def _assert_diagnostics(self, error, expected):
        self.assertEqual("REVIEW_COVERAGE_MISMATCH", error.code)
        context = error.failure_context
        self.assertFalse(context["diagnostics_truncated"])
        diagnostics = context["diagnostics"]
        self.assertEqual(
            set(expected),
            {(item["ac_id"], item["evidence_kind"])
             for item in diagnostics})
        self.assertEqual(len(expected), len(diagnostics))
        for item in diagnostics:
            self.assertEqual("REVIEW_COVERAGE_MISMATCH", item["code"])
            field = expected[(item["ac_id"], item["evidence_kind"])]
            self.assertIn(field, item["required_correction"])
            self.assertIn(field, item["offending_content"])
        return diagnostics

    def _assert_both_boundaries(self, bundle, mutate, expected):
        response = copy.deepcopy(bundle["response"])
        mutate(response["tool_calls"][0]["arguments"]["ac_reviews"])
        with self.assertRaises(ProjectJobError) as caught:
            self._build(bundle, response)
        build_diagnostics = self._assert_diagnostics(
            caught.exception, expected)

        report = copy.deepcopy(bundle["report"])
        mutate(report["ac_reviews"])
        for item in report["ac_reviews"]:
            item["review_fingerprint"] = artifact_fingerprint(
                item, "review_fingerprint")
        report["report_fingerprint"] = artifact_fingerprint(
            report, "report_fingerprint")
        with self.assertRaises(ProjectJobError) as caught:
            self._validate(bundle, report)
        validation_diagnostics = self._assert_diagnostics(
            caught.exception, expected)
        self.assertEqual(build_diagnostics, validation_diagnostics)

    def test_missing_evidence_is_reported_for_every_ac_at_both_boundaries(self):
        bundle = self._coverage_bundle()

        def remove_evidence(rows):
            rows[0]["stimulus_evidence"] = []
            rows[0]["checker_evidence"] = []
            rows[1]["checker_evidence"] = []

        self._assert_both_boundaries(bundle, remove_evidence, {
            ("AC.0001", "STIMULUS"): "stimulus_evidence",
            ("AC.0001", "CHECKER"): "checker_evidence",
            ("AC.0002", "CHECKER"): "checker_evidence",
        })

    def test_status_omission_conflicts_are_collected_at_both_boundaries(self):
        bundle = self._coverage_bundle()

        def contradict_status(rows):
            rows[0]["omission"] = "The claimed coverage still has a gap."
            rows[1]["status"] = "BLOCKED"
            rows[1]["omission"] = ""

        self._assert_both_boundaries(bundle, contradict_status, {
            ("AC.0001", "REPORT"): "omission",
            ("AC.0002", "REPORT"): "omission",
        })

    def test_bad_snippet_and_missing_fields_are_returned_in_one_batch(self):
        bundle = self._coverage_bundle()
        response = copy.deepcopy(bundle["response"])
        rows = response["tool_calls"][0]["arguments"]["ac_reviews"]
        rows[0]["stimulus_evidence"] = [{"content": "    clk = 1'bx;"}]
        rows[0]["checker_evidence"] = []
        rows[1]["stimulus_evidence"] = []
        with self.assertRaises(ProjectJobError) as caught:
            self._build(bundle, response)
        context = caught.exception.failure_context
        self.assertFalse(context["diagnostics_truncated"])
        diagnostics = context["diagnostics"]
        self.assertEqual(3, len(diagnostics))
        self.assertEqual({
            ("TESTCASE_EVIDENCE_MISMATCH", "AC.0001", "STIMULUS"),
            ("REVIEW_COVERAGE_MISMATCH", "AC.0001", "CHECKER"),
            ("REVIEW_COVERAGE_MISMATCH", "AC.0002", "STIMULUS"),
        }, {(item["code"], item["ac_id"], item["evidence_kind"])
            for item in diagnostics})

    def test_noncovered_checkable_ac_can_explain_missing_evidence(self):
        bundle = self._coverage_bundle()
        finding = FakeReviewerProvider("TESTCASE")._report(
            bundle["request"])["findings"]
        for status in ("BLOCKED", "OMITTED", "OBSERVATION_ONLY"):
            with self.subTest(status=status):
                response = copy.deepcopy(bundle["response"])
                raw = response["tool_calls"][0]["arguments"]
                raw["verdict"] = "FINDINGS_REPORTED"
                raw["findings"] = copy.deepcopy(finding)
                row = raw["ac_reviews"][0]
                row.update({
                    "status": status,
                    "stimulus_evidence": [],
                    "checker_evidence": [],
                    "omission": "The testcase does not check this AC yet.",
                })
                report = self._build(bundle, response)
                self.assertEqual(
                    "PASS", self._validate(bundle, report)["status"])

    def test_noncheckable_ac_does_not_require_omission_but_cannot_be_covered(self):
        bundle = self._coverage_bundle()
        for upstream_status in ("OBSERVATION_ONLY", "BLOCKED_CONTRACT",
                                "SPEC_AMBIGUITY"):
            with self.subTest(upstream_status=upstream_status):
                scoped = copy.deepcopy(bundle)
                upstream = scoped["request"]["scenario_ac_map"][
                    "acceptance_criteria"][0]
                upstream["status"] = upstream_status
                response = copy.deepcopy(scoped["response"])
                row = response["tool_calls"][0]["arguments"]["ac_reviews"][0]
                row.update({
                    "status": "OBSERVATION_ONLY", "omission": "",
                    "stimulus_evidence": [], "checker_evidence": [],
                })
                report = self._build(scoped, response)
                self.assertEqual("", report["ac_reviews"][0]["omission"])
                row["status"] = "COVERED"
                with self.assertRaises(ProjectJobError) as caught:
                    self._build(scoped, response)
                self.assertEqual("UNAUTHORIZED_ORACLE", caught.exception.code)
                self.assertEqual(
                    "AC.0001", caught.exception.failure_context["ac_id"])

    def test_workflow_retry_receives_all_field_diagnostics_and_then_passes(self):
        reviewer = CoverageRetryReviewer()
        generator = SharedCheckerProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        checkpoint = self.start_checked(workflow)
        self.assertEqual("READY_FOR_EXECUTION_PREPARATION", checkpoint["state"])
        self.assertEqual(2, reviewer.calls)
        self.assertEqual(3, generator.calls)

        feedbacks = []
        for message in reviewer.requests[1]["messages"]:
            try:
                value = json.loads(message["content"])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and "review_correction_feedback" in value:
                feedbacks.append(value["review_correction_feedback"])
        self.assertEqual(1, len(feedbacks))
        feedback = feedbacks[0]
        self.assertFalse(feedback["diagnostics_truncated"])
        expected = {
            ("AC.0001", "STIMULUS"): "stimulus_evidence",
            ("AC.0001", "CHECKER"): "checker_evidence",
            ("AC.0002", "CHECKER"): "checker_evidence",
        }
        self.assertEqual(set(expected), {
            (item["ac_id"], item["evidence_kind"])
            for item in feedback["diagnostics"]})
        for item in feedback["diagnostics"]:
            self.assertIn(
                expected[(item["ac_id"], item["evidence_kind"])],
                item["required_correction"])
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        persisted = list(job.glob("audit/pj002_rejected_review_response.*.json"))
        self.assertEqual(1, len(persisted))
        self.assertEqual(feedback, load_document(persisted[0]))


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(ReviewCoverageDiagnosticsTests):
        if name in ReviewCoverageDiagnosticsTests.__dict__:
            suite.addTest(ReviewCoverageDiagnosticsTests(name))
    return suite


if __name__ == "__main__":
    unittest.main(verbosity=2)
