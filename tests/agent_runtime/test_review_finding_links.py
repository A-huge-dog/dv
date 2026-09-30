"""Finding links honor shared code roles without weakening testcase mappings."""
from __future__ import annotations

import copy
import unittest

from contracts.validator import load_document
from domain.review import build_review_report, validate_review_report
from runtime.errors import ProjectJobError
from runtime.project_job import ProjectJobWorkflow

try:
    from test_project_job_workflow import (
        FakeProvider, ProjectJobWorkflowTests,
        SharedCheckerProvider, SharedCheckerReviewer,
    )
except ModuleNotFoundError:
    from tests.agent_runtime.test_project_job_workflow import (
        FakeProvider, ProjectJobWorkflowTests,
        SharedCheckerProvider, SharedCheckerReviewer,
    )


class SeparateTestcaseProvider(SharedCheckerProvider):
    """Generate two independently mapped testcases plus an unbound shared unit."""

    def stage2(self):
        result = FakeProvider.stage2(self)
        second = copy.deepcopy(result["logical_testcases"][0])
        second["ac_ids"] = ["AC.0002"]
        second["objective"] = "Check the independently mapped second AC."
        result["logical_testcases"].append(second)
        return result


class SeparateTestcaseReviewer(SharedCheckerReviewer):
    def _report(self, review):
        report = super()._report(review)
        coverage = {
            item["ac_id"]: item
            for item in review["ac_testcase_map"]["index"]["ac_coverage"]
        }
        units = review["testcase_candidate"]["code_units"]
        for row in report["ac_reviews"]:
            testcase_ids = set(coverage[row["ac_id"]]["testcase_ids"])
            unit = next(item for item in units
                        if set(item["testcase_ids"]) & testcase_ids)
            for field, needle in (
                    ("stimulus_evidence", "clk = 1'b0;"),
                    ("checker_evidence", "AC_TINY_LOW_FAIL")):
                row[field] = [{"content": next(
                    line for line in unit["content"].splitlines()
                    if needle in line)}]
        return report


def finding_for(ac, testcase_id, unit):
    return {
        "severity": "ERROR",
        "suspected_origin_stage": "STAGE_3",
        "affected": {
            "scenario_ids": copy.deepcopy(ac["scenario_ids"]),
            "ac_ids": [ac["ac_id"]],
            "testcase_ids": [testcase_id],
            "code_unit_ids": [unit["code_unit_id"]],
        },
        "spec_evidence": [{
            key: ac["spec_evidence"][0][key]
            for key in ("path", "line_start", "line_end")
        }],
        "testcase_evidence": [{
            "content": "\n".join(unit["content"].splitlines())}],
        "problem_and_required_change": (
            "Correct the referenced code for the affected testcase."),
    }


class WrongTestcaseFindingReviewer(SeparateTestcaseReviewer):
    def _report(self, review):
        report = super()._report(review)
        ac = review["scenario_ac_map"]["acceptance_criteria"][0]
        coverage = next(item for item in review["ac_testcase_map"][
            "index"]["ac_coverage"] if item["ac_id"] == ac["ac_id"])
        testcase_id = coverage["testcase_ids"][0]
        wrong_unit = next(item for item in review["testcase_candidate"][
            "code_units"] if item["role"] == "TESTCASE"
            and testcase_id not in item["testcase_ids"])
        self.wrong_code_unit_id = wrong_unit["code_unit_id"]
        report["verdict"] = "FINDINGS_REPORTED"
        report["findings"] = [finding_for(ac, testcase_id, wrong_unit)]
        return report


class ReviewFindingLinksTests(ProjectJobWorkflowTests):
    def _finding_bundle(self):
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result",
            SeparateTestcaseProvider(), SeparateTestcaseReviewer())
        checkpoint = self.start_checked(workflow)
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        manifest = workflow.bootstrap_handler.handle(self.project_input())
        bundle = {
            "map1": load_document(job / checkpoint["scenario_ac_map_path"]),
            "map2": load_document(job / checkpoint["ac_testcase_map_path"]),
            "candidate": load_document(
                job / checkpoint["candidate_metadata_path"]),
            "request": load_document(job / checkpoint["review_request_path"]),
            "response": load_document(
                job / "audit/pj002_provider_response.review.r001.json"),
            "sources": {
                item["path"]: (self.root / item["baseline_path"]).read_text(
                    encoding="utf-8")
                for item in manifest["spec"]["sources"]
            },
        }
        units = bundle["candidate"]["code_units"]
        bundle["shared"] = next(
            item for item in units if item["role"] == "SHARED")
        self.assertEqual([], bundle["shared"]["testcase_ids"])
        bundle["testcase_ids"] = {
            item["ac_id"]: item["testcase_ids"][0]
            for item in bundle["map2"]["ac_coverage"]
        }
        self.assertEqual(2, len(set(bundle["testcase_ids"].values())))
        bundle["testcase_units"] = {
            ac_id: next(item for item in units
                        if testcase_id in item["testcase_ids"])
            for ac_id, testcase_id in bundle["testcase_ids"].items()
        }
        return bundle

    def _finding(self, bundle, ac_id, testcase_id, unit):
        ac = next(item for item in bundle["map1"]["acceptance_criteria"]
                  if item["ac_id"] == ac_id)
        return finding_for(ac, testcase_id, unit)

    def _report(self, bundle, findings):
        response = copy.deepcopy(bundle["response"])
        raw = response["tool_calls"][0]["arguments"]
        raw["verdict"] = "FINDINGS_REPORTED"
        raw["findings"] = findings
        return build_review_report(
            bundle["request"], bundle["candidate"], response,
            bundle["sources"], ProjectJobError)

    def _validate(self, bundle, report):
        return validate_review_report(
            report, bundle["request"], bundle["map1"], bundle["map2"],
            bundle["candidate"], bundle["sources"], ProjectJobError)

    def _rejected_diagnostics(self, bundle, report):
        with self.assertRaises(ProjectJobError) as caught:
            self._validate(bundle, report)
        self.assertEqual("REVIEW_EVIDENCE_MISMATCH", caught.exception.code)
        context = caught.exception.failure_context
        self.assertFalse(context["diagnostics_truncated"])
        diagnostics = context["diagnostics"]
        for item in diagnostics:
            self.assertEqual("REVIEW_EVIDENCE_MISMATCH", item["code"])
            self.assertEqual("REPORT", item["evidence_kind"])
        self.assertEqual(
            {item["issue_id"] for item in report["findings"]},
            {item["ac_id"] for item in diagnostics})
        return diagnostics

    def test_shared_unit_with_empty_bindings_can_affect_each_mapped_testcase(self):
        bundle = self._finding_bundle()
        findings = [self._finding(
            bundle, ac_id, testcase_id, bundle["shared"])
            for ac_id, testcase_id in bundle["testcase_ids"].items()]
        report = self._report(bundle, findings)

        self.assertEqual("PASS", self._validate(bundle, report)["status"])
        self.assertEqual(2, len(report["findings"]))
        self.assertEqual([], bundle["shared"]["testcase_ids"])

    def test_testcase_unit_for_another_testcase_is_rejected_with_link_details(self):
        bundle = self._finding_bundle()
        requested_tc = bundle["testcase_ids"]["AC.0001"]
        actual_tc = bundle["testcase_ids"]["AC.0002"]
        unit = bundle["testcase_units"]["AC.0002"]
        report = self._report(bundle, [self._finding(
            bundle, "AC.0001", requested_tc, unit)])

        diagnostics = self._rejected_diagnostics(bundle, report)
        self.assertEqual(1, len(diagnostics))
        diagnostic = diagnostics[0]
        for identity in (unit["code_unit_id"], requested_tc, actual_tc):
            self.assertIn(identity, diagnostic["offending_content"])
        self.assertIn(unit["code_unit_id"], diagnostic["required_correction"])
        self.assertIn(actual_tc, diagnostic["required_correction"])

    def test_testcase_outside_affected_ac_is_rejected_with_link_details(self):
        bundle = self._finding_bundle()
        testcase_id = bundle["testcase_ids"]["AC.0002"]
        report = self._report(bundle, [self._finding(
            bundle, "AC.0001", testcase_id,
            bundle["testcase_units"]["AC.0002"])])

        diagnostics = self._rejected_diagnostics(bundle, report)
        self.assertEqual(1, len(diagnostics))
        diagnostic = diagnostics[0]
        for identity in (testcase_id, "AC.0001", "AC.0002"):
            self.assertIn(identity, diagnostic["offending_content"])
        self.assertIn(testcase_id, diagnostic["required_correction"])
        self.assertIn("AC.0002", diagnostic["required_correction"])

    def test_link_errors_from_multiple_findings_are_returned_in_one_batch(self):
        bundle = self._finding_bundle()
        wrong_unit = self._finding(
            bundle, "AC.0001", bundle["testcase_ids"]["AC.0001"],
            bundle["testcase_units"]["AC.0002"])
        wrong_ac = self._finding(
            bundle, "AC.0002", bundle["testcase_ids"]["AC.0001"],
            bundle["shared"])
        report = self._report(bundle, [wrong_unit, wrong_ac])

        diagnostics = self._rejected_diagnostics(bundle, report)
        self.assertEqual(2, len(diagnostics))
        by_issue = {item["ac_id"]: item for item in diagnostics}
        for finding in report["findings"]:
            diagnostic = by_issue[finding["issue_id"]]
            testcase_id = finding["affected"]["testcase_ids"][0]
            self.assertIn(testcase_id, diagnostic["offending_content"])
            self.assertTrue(diagnostic["required_correction"])

    def test_exhausted_retry_preserves_link_failure_and_audit_location(self):
        reviewer = WrongTestcaseFindingReviewer()
        generator = SeparateTestcaseProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        with self.assertRaises(ProjectJobError) as caught:
            self.start_checked(workflow)
        error = caught.exception
        self.assertEqual("ATTEMPT_PAUSED", error.code)
        self.assertEqual(4, reviewer.calls)
        self.assertEqual(3, generator.calls)
        self.assertIn("REVIEW_EVIDENCE_MISMATCH", str(error))
        self.assertIn(reviewer.wrong_code_unit_id, str(error))

        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        rejected = list(job.glob("audit/pj002_rejected_review_response.*.json"))
        self.assertEqual(4, len(rejected))
        self.assertTrue(any(str(path.relative_to(job)) in str(error)
                            for path in rejected))
        for path in rejected:
            record = load_document(path)
            diagnostics = record["diagnostics"]
            self.assertEqual(1, len(diagnostics))
            self.assertTrue(diagnostics[0]["ac_id"].startswith("ISSUE."))
            self.assertEqual(record["diagnostic"]["issue_id"],
                             diagnostics[0]["ac_id"])
            self.assertIn(reviewer.wrong_code_unit_id,
                          diagnostics[0]["offending_content"])


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(ReviewFindingLinksTests):
        if name in ReviewFindingLinksTests.__dict__:
            suite.addTest(ReviewFindingLinksTests(name))
    return suite


if __name__ == "__main__":
    unittest.main(verbosity=2)
