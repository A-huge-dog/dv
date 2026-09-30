"""Explicit blocked mappings pass Stage 2 without becoming executable coverage."""
from __future__ import annotations

import copy
import itertools
import unittest

from domain.artifacts import artifact_fingerprint
from domain.stage1 import enrich_stage1
from domain.stage2 import enrich_stage2, validate_ac_testcase_map
from domain.uvm_testcase import build_manifest
from runtime.errors import ProjectJobError
from scripts.dvlib import canonical_hash

try:
    from test_project_job_workflow import SPEC, SharedCheckerProvider
except ModuleNotFoundError:
    from tests.agent_runtime.test_project_job_workflow import (
        SPEC, SharedCheckerProvider,
    )


class Stage2BlockedMappingTests(unittest.TestCase):
    def setUp(self):
        self.project_input = {
            "job_id": "JOB.PROJECT.TINY.BLOCKED",
            "input_fingerprint": canonical_hash({"fixture": "blocked"}),
        }
        self.sources = {"spec.md": SPEC}
        self.spec_fp = canonical_hash(self.sources)
        self.policy_fp = canonical_hash({"policy": "stage2-test"})
        self.response = {
            "request_id": "REQUEST.STAGE2.BLOCKED",
            "model_id": "fake-project-model",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "provider_metadata": {
                "provider_id": "fake-project-provider",
                "response_id": "RESPONSE.STAGE2.BLOCKED",
            },
        }
        self.map1 = enrich_stage1(
            SharedCheckerProvider.stage1(), self.project_input,
            self.sources, self.spec_fp, 0, self.response,
            max_items=16, policy_fingerprint=self.policy_fp,
            error=ProjectJobError)

    def _raw(self, status="BLOCKED_CONTRACT"):
        raw = SharedCheckerProvider().stage2()
        row = raw["logical_testcases"][0]
        row["status"] = status
        row["reason"] = "Portable execution is blocked by a missing binding."
        return raw

    def _enrich(self, raw):
        return enrich_stage2(
            raw, self.project_input, self.map1, self.sources,
            self.spec_fp, 0, self.response, max_items=16,
            max_per_shard=16, max_file_bytes=262144,
            policy_fingerprint=self.policy_fp, error=ProjectJobError)

    def _validate(self, value):
        return validate_ac_testcase_map(
            value, self.map1, self.project_input, self.sources,
            self.spec_fp, self.policy_fp, value["logical_testcases"],
            ProjectJobError)

    def _expect_error(self, code, call):
        with self.assertRaises(ProjectJobError) as caught:
            call()
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    @staticmethod
    def _rehash(value, *, testcases=False, coverage=False):
        if testcases:
            for row in value["logical_testcases"]:
                row["testcase_fingerprint"] = artifact_fingerprint(
                    row, "testcase_fingerprint")
        if coverage:
            for row in value["ac_coverage"]:
                row["coverage_fingerprint"] = artifact_fingerprint(
                    row, "coverage_fingerprint")
        value["artifact_fingerprint"] = artifact_fingerprint(
            value, "artifact_fingerprint")

    def test_blocked_oracle_fields_are_optional_and_do_not_enable_execution(self):
        for has_checker, has_expected in itertools.product((False, True), repeat=2):
            with self.subTest(checker=has_checker, expected=has_expected):
                raw = self._raw()
                row = raw["logical_testcases"][0]
                if not has_checker:
                    row["checker"] = ""
                if not has_expected:
                    row["expected_result"] = ""
                row["stimulus"] = ""
                row["transaction_sequence"] = ""
                row["failure_condition"] = ""

                formal, testcases, shards = self._enrich(raw)

                self.assertEqual(formal, self._validate(formal))
                self.assertEqual([], shards)
                self.assertEqual("BLOCKED_CONTRACT", testcases[0]["status"])
                self.assertEqual(row["checker"], testcases[0]["checker"])
                self.assertEqual(row["expected_result"],
                                 testcases[0]["expected_result"])
                self.assertEqual([], formal["completeness"]["omitted_ac_ids"])
                self.assertEqual(["AC.0001", "AC.0002"],
                                 testcases[0]["ac_ids"])
                for coverage in formal["ac_coverage"]:
                    self.assertEqual([testcases[0]["testcase_id"]],
                                     coverage["testcase_ids"])
                self.assertEqual([], build_manifest(testcases)["testcases"])
                self.assertTrue(all(ac["status"] == "CHECKABLE"
                                    for ac in self.map1["acceptance_criteria"]))

    def test_mixed_mapping_executes_only_the_checkable_testcase(self):
        raw = self._raw("CHECKABLE")
        checkable = raw["logical_testcases"][0]
        checkable["ac_ids"] = ["AC.0001"]
        blocked = copy.deepcopy(checkable)
        blocked.update({
            "objective": "Keep the second AC explicitly blocked.",
            "status": "BLOCKED_CONTRACT",
            "ac_ids": ["AC.0002"],
            "checker": "", "expected_result": "",
        })
        raw["logical_testcases"].append(blocked)

        formal, testcases, _ = self._enrich(raw)

        self.assertEqual(formal, self._validate(formal))
        self.assertEqual(["CHECKABLE", "BLOCKED_CONTRACT"],
                         [row["status"] for row in testcases])
        self.assertEqual([testcases[0]["testcase_id"]], [
            row["testcase_id"] for row in build_manifest(testcases)["testcases"]])

    def test_spec_ambiguity_still_rejects_oracles_and_checkable_ac_coverage(self):
        for has_checker, has_expected in itertools.product((False, True), repeat=2):
            with self.subTest(checker=has_checker, expected=has_expected):
                raw = self._raw("SPEC_AMBIGUITY")
                row = raw["logical_testcases"][0]
                if not has_checker:
                    row["checker"] = ""
                if not has_expected:
                    row["expected_result"] = ""
                expected_code = (
                    "UNAUTHORIZED_ORACLE" if has_checker or has_expected
                    else "MISSING_TESTCASE_COVERAGE")
                self._expect_error(expected_code, lambda: self._enrich(raw))

    def test_checkable_testcase_still_requires_stimulus_and_oracle(self):
        for field in ("stimulus", "transaction_sequence", "checker",
                      "expected_result", "failure_condition"):
            for empty in ("", "   "):
                with self.subTest(field=field, empty=repr(empty)):
                    raw = self._raw("CHECKABLE")
                    raw["logical_testcases"][0][field] = empty
                    self._expect_error(
                        "MISSING_STIMULUS_OR_ORACLE", lambda: self._enrich(raw))

    def test_absent_mapping_requires_explicit_omissions(self):
        raw = self._raw()
        raw["logical_testcases"] = []
        self._expect_error("MISSING_TESTCASE_COVERAGE", lambda: self._enrich(raw))

        raw["completeness"] = {
            "declared_complete": False,
            "omissions": [{"ac_id": ac_id, "reason": "No testcase is available."}
                          for ac_id in ("AC.0001", "AC.0002")],
        }
        formal, testcases, _ = self._enrich(raw)
        self.assertEqual([], testcases)
        self.assertEqual(["AC.0001", "AC.0002"],
                         formal["completeness"]["omitted_ac_ids"])

    def test_blocked_mapping_still_requires_reason_known_objects_and_spec(self):
        for field, bad_value, expected_code in (
                ("reason", "", "INVALID_MAPPING"),
                ("ac_ids", ["AC.UNKNOWN"], "ORPHAN_MAPPING"),
                ("scenario_ids", ["SCENARIO.UNKNOWN"], "ORPHAN_MAPPING"),
                ("spec_evidence", [], "MISSING_SPEC_EVIDENCE")):
            with self.subTest(field=field):
                raw = self._raw()
                raw["logical_testcases"][0][field] = bad_value
                self._expect_error(expected_code, lambda: self._enrich(raw))

    def test_blocked_formal_mapping_rejects_tampered_evidence_and_fingerprints(self):
        formal, _, _ = self._enrich(self._raw())
        stale_map = copy.deepcopy(formal)
        stale_map["revision"] += 1
        self._expect_error("STALE_EVIDENCE", lambda: self._validate(stale_map))

        wrong_job = copy.deepcopy(formal)
        wrong_job["job_id"] = "JOB.PROJECT.OTHER.001"
        self._rehash(wrong_job)
        self._expect_error("STALE_EVIDENCE", lambda: self._validate(wrong_job))

        stale_tc = copy.deepcopy(formal)
        stale_tc["logical_testcases"][0]["objective"] = "Changed without rehashing."
        self._rehash(stale_tc)
        self._expect_error("STALE_EVIDENCE", lambda: self._validate(stale_tc))

        wrong_spec = copy.deepcopy(formal)
        wrong_spec["logical_testcases"][0]["spec_evidence"][0]["snippet"] = \
            "Invented spec content."
        self._rehash(wrong_spec, testcases=True)
        self._expect_error(
            "SPEC_EVIDENCE_MISMATCH", lambda: self._validate(wrong_spec))

    def test_blocked_formal_mapping_still_checks_coverage_and_unique_testcases(self):
        formal, _, _ = self._enrich(self._raw())
        duplicate = copy.deepcopy(formal)
        duplicate["logical_testcases"].append(copy.deepcopy(
            duplicate["logical_testcases"][0]))
        self._rehash(duplicate)
        self._expect_error("DUPLICATE_MAPPING_ID", lambda: self._validate(duplicate))

        for testcase_ids, expected_code in (
                ([], "MAPPING_MISMATCH"),
                (["TC.UNKNOWN"], "UNKNOWN_TESTCASE_REFERENCE")):
            with self.subTest(testcase_ids=testcase_ids):
                bad = copy.deepcopy(formal)
                bad["ac_coverage"][0]["testcase_ids"] = testcase_ids
                self._rehash(bad, coverage=True)
                self._expect_error(expected_code, lambda: self._validate(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
