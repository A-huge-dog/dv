"""Stage 3 separates executable coverage from explicit upstream blockers."""
from __future__ import annotations

import copy
import unittest

from domain.stage3 import enrich_stage3
from domain.uvm_testcase import build_manifest, testcase_class_name
from runtime.errors import ProjectJobError


class Stage3MappingTests(unittest.TestCase):
    def setUp(self):
        self.testcases = [
            {"testcase_id": "TC.RUN", "status": "CHECKABLE"},
            {"testcase_id": "TC.SKIP", "status": "CHECKABLE"},
            {"testcase_id": "TC.BLOCKED", "status": "BLOCKED_CONTRACT"},
            {"testcase_id": "TC.OBSERVE", "status": "OBSERVATION_ONLY"},
        ]
        self.project = {
            "job_id": "JOB.PROJECT.MAPPING", "input_fingerprint": "1" * 64,
            "testcase": {"top": "tiny"},
        }
        self.response = {
            "model_id": "test-model", "request_id": "test-request",
            "provider_metadata": {"provider_id": "test-provider",
                                  "response_id": "test-response"},
        }

    @staticmethod
    def _skip(testcase_id):
        return {
            "testcase_id": testcase_id, "reason_kind": "BLOCKED_CONTRACT",
            "reason": "The current UVM context has no public observation point.",
            "routing_required": True,
        }

    def _raw(self, implemented=("TC.RUN",), skipped=("TC.SKIP",)):
        units = [{
            "role": "TESTCASE", "testcase_ids": [tc_id],
            "content": "class {} extends public_base_test; endclass\n".format(
                testcase_class_name(tc_id)),
        } for tc_id in implemented]
        if not units:
            units = [{"role": "SHARED", "testcase_ids": [],
                      "content": "// All checkable testcases require review.\n"}]
        return {
            "code_units": units, "assembly": list(range(len(units))),
            "implemented_testcase_ids": list(implemented),
            "skipped_testcases": [self._skip(tc_id) for tc_id in skipped],
        }

    def _enrich(self, raw):
        return enrich_stage3(
            raw, self.project, {"artifact_fingerprint": "2" * 64},
            {"artifact_fingerprint": "3" * 64}, self.testcases, "4" * 64,
            1, self.response, "5" * 64, "6" * 64, ProjectJobError)

    def _assert_mapping_failure(self, raw, message, ids):
        with self.assertRaises(ProjectJobError) as caught:
            self._enrich(raw)
        diagnostics = caught.exception.failure_context["diagnostics"]
        matching = [item for item in diagnostics
                    if item["code"] == "TESTCASE_MAPPING_OVERREACH"
                    and item["message"] == message]
        self.assertEqual(1, len(matching), diagnostics)
        self.assertEqual(",".join(sorted(ids)), matching[0]["offending_content"])
        self.assertEqual(len(ids), matching[0]["match_count"])
        self.assertTrue(matching[0]["required_correction"])

    def test_explicit_upstream_skips_survive_into_execution_manifest(self):
        raw = self._raw(skipped=("TC.SKIP", "TC.BLOCKED", "TC.OBSERVE"))
        original = copy.deepcopy(raw)
        candidate = self._enrich(raw)
        manifest = build_manifest(
            self.testcases,
            implemented_testcase_ids=candidate["implemented_testcase_ids"],
            skipped_testcases=candidate["skipped_testcases"])
        self.assertEqual(["TC.RUN"], [tc["testcase_id"] for tc in manifest["testcases"]])
        self.assertEqual(
            ["TC.BLOCKED", "TC.OBSERVE", "TC.SKIP"],
            [tc["testcase_id"] for tc in manifest["skipped_testcases"]])
        self.assertEqual(original, raw)

    def test_upstream_noncheckable_testcases_need_not_be_redeclared(self):
        candidate = self._enrich(self._raw())
        self.assertEqual(["TC.RUN"], candidate["implemented_testcase_ids"])
        self.assertEqual([self._skip("TC.SKIP")], candidate["skipped_testcases"])

    def test_all_skipped_can_include_upstream_blockers(self):
        candidate = self._enrich(self._raw(
            implemented=(), skipped=tuple(tc["testcase_id"] for tc in self.testcases)))
        self.assertEqual([], candidate["implemented_testcase_ids"])
        self.assertEqual(4, len(candidate["skipped_testcases"]))

    def test_unknown_ids_cannot_be_implemented_or_skipped(self):
        for raw in (
            self._raw(implemented=("TC.RUN", "TC.UNKNOWN")),
            self._raw(skipped=("TC.SKIP", "TC.UNKNOWN")),
        ):
            with self.subTest(raw=raw):
                self._assert_mapping_failure(raw, "unknown testcase IDs", {"TC.UNKNOWN"})

    def test_noncheckable_testcases_cannot_be_implemented(self):
        for tc_id in ("TC.BLOCKED", "TC.OBSERVE"):
            with self.subTest(testcase_id=tc_id):
                self._assert_mapping_failure(
                    self._raw(implemented=("TC.RUN", tc_id)),
                    "non-CHECKABLE testcases declared implemented", {tc_id})

    def test_missing_checkable_testcase_names_the_missing_id(self):
        self._assert_mapping_failure(
            self._raw(skipped=()),
            "CHECKABLE testcases neither implemented nor skipped", {"TC.SKIP"})

    def test_implemented_and_skipped_are_disjoint(self):
        self._assert_mapping_failure(
            self._raw(skipped=("TC.SKIP", "TC.RUN")),
            "testcases both implemented and skipped", {"TC.RUN"})

    def test_duplicate_skip_ids_with_different_reasons_are_rejected(self):
        raw = self._raw()
        duplicate = self._skip("TC.SKIP")
        duplicate["reason"] = "A second explanation must not create a second disposition."
        raw["skipped_testcases"].append(duplicate)
        self._assert_mapping_failure(
            raw, "duplicate skipped testcase IDs", {"TC.SKIP"})


if __name__ == "__main__":
    unittest.main()
