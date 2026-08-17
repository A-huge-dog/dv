#!/usr/bin/env python3
"""PJ-002.9-HF1 incremental artifact, assembly, and impact tests."""
from __future__ import annotations

import copy
import json
import unittest

from contracts.validator import accepted, load_document, validate
from core.project_incremental import (
    IncrementalArtifactStore, build_review_bundle, build_stage2_bundle,
    build_stage3_bundle, evaluate_impact, validate_index,
)
from core.project_job import ProjectJobError, ProjectJobWorkflow
from core.project_staged import artifact_fingerprint
from tests.agent_runtime import test_project_job_workflow as workflow_tests
from tests.agent_runtime.test_project_job_workflow import (
    FakeProvider, FakeReviewerProvider, Stage3ValidatedRetryProvider,
)


class IncrementalArtifactTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workflow_tests.ProjectJobWorkflowTests(methodName="runTest")
        self.fixture.setUp()
        self.root = self.fixture.root

    def tearDown(self):
        self.fixture.tearDown()

    def _clean_bundle(self, duplicate_testcases: bool = False):
        generator = FakeProvider(duplicate_testcases=duplicate_testcases)
        reviewer = FakeReviewerProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        submission = self.fixture.project_input()
        checkpoint = self.fixture.start_checked(workflow, submission)
        self.assertEqual("AWAITING_HUMAN_REVIEW", checkpoint["state"])
        job = self.root / "result/jobs" / submission["job_id"]
        map1 = load_document(job / checkpoint["scenario_ac_map_path"])
        map2 = load_document(job / checkpoint["ac_testcase_map_path"])
        candidate = load_document(job / checkpoint["candidate_metadata_path"])
        report = load_document(job / checkpoint["review_report_path"])
        store = IncrementalArtifactStore(job, 1024 * 1024, ProjectJobError)
        stage1 = store.load("STAGE1", "CURRENT", map1["revision"], submission["job_id"])
        stage2 = store.load("STAGE2", "CURRENT", map2["revision"], submission["job_id"])
        stage3_index, stage3_units, assembly = store.load_stage3(
            candidate["revision"], submission["job_id"])
        review = store.load(
            "REVIEW", "CURRENT", report["review_round"] - 1,
            submission["job_id"])
        return {
            "workflow": workflow, "submission": submission,
            "checkpoint": checkpoint, "job": job, "map1": map1,
            "map2": map2, "candidate": candidate, "report": report,
            "stage1": stage1, "stage2": stage2,
            "stage3": (stage3_index, stage3_units),
            "assembly": assembly, "review": review,
            "store": store,
            "generator": generator, "reviewer": reviewer,
        }

    def test_linear_clean_flow_persists_exact_unit_roots_and_replays(self):
        bundle = self._clean_bundle(duplicate_testcases=True)
        checkpoint = bundle["checkpoint"]
        expected_roots = {
            "stage1_units": bundle["stage1"][0]["root_fingerprint"],
            "stage2_units": bundle["stage2"][0]["root_fingerprint"],
            "stage3_units": bundle["stage3"][0]["root_fingerprint"],
            "stage3_assembly": bundle["assembly"]["assembly_fingerprint"],
            "review_units": bundle["review"][0]["root_fingerprint"],
        }
        for key, fingerprint in expected_roots.items():
            self.assertEqual(fingerprint, checkpoint["bundle_fingerprints"][key])
        self.assertEqual(
            bundle["candidate"]["content"], bundle["assembly"]["content"])
        self.assertEqual(
            bundle["candidate"]["content_fingerprint"],
            bundle["assembly"]["content_fingerprint"])
        testcase_code = next(
            unit for unit in bundle["stage3"][1].values()
            if unit["unit_kind"] == "CODE_TESTCASE")
        local = testcase_code["local_evidence"][0]
        self.assertEqual(testcase_code["unit_id"], local["code_unit_id"])
        self.assertEqual(
            testcase_code["content_fingerprint"],
            local["code_unit_fingerprint"])
        self.assertNotIn("selections", local)
        kinds = {
            unit["unit_kind"]
            for _, units in (
                bundle["stage1"], bundle["stage2"], bundle["stage3"],
                bundle["review"])
            for unit in units.values()
        }
        self.assertTrue({
            "SCENARIO", "ACCEPTANCE_CRITERION", "LOGICAL_TESTCASE",
            "AC_COVERAGE", "CODE_SHARED", "CODE_TESTCASE", "REVIEW_AC",
            "REVIEW_TESTCASE", "REVIEW_SHARED",
        }.issubset(kinds))
        calls = (bundle["generator"].calls, bundle["reviewer"].calls)
        self.assertEqual(
            checkpoint,
            bundle["workflow"].start(bundle["submission"]))
        self.assertEqual(calls, (
            bundle["generator"].calls, bundle["reviewer"].calls))
        paths = checkpoint["artifact_unit_index_paths"]
        impact = bundle["store"].compare_and_persist(paths, paths, 0)
        self.assertFalse(impact["manifest"]["dirty_units"])
        self.assertTrue(impact["manifest"]["reused_units"])
        self.assertTrue((bundle["job"] / impact["path"]).is_file())

    def test_stage2_coverage_is_framework_derived_and_locally_fingerprinted(self):
        bundle = self._clean_bundle(duplicate_testcases=True)
        index, units = bundle["stage2"]
        testcase_units = {
            unit_id: unit for unit_id, unit in units.items()
            if unit["unit_kind"] == "LOGICAL_TESTCASE"}
        coverage = next(
            unit for unit in units.values()
            if unit["unit_kind"] == "AC_COVERAGE")
        self.assertEqual("FRAMEWORK", coverage["producer"]["kind"])
        self.assertEqual(
            sorted(testcase_units),
            coverage["semantic_body"]["testcase_ids"])
        dependency_ids = {
            item["identity"] for item in coverage["dependency_fingerprints"]
            if item["kind"] == "LOGICAL_TESTCASE"}
        self.assertEqual(set(testcase_units), dependency_ids)
        raw = FakeProvider(duplicate_testcases=True).stage2()
        raw["ac_coverage"] = []
        self.assertFalse(accepted(validate("ac_testcase_candidate", raw)))
        self.assertEqual(
            len(units), index["completeness"]["unit_count"])

    def test_one_testcase_change_reuses_unrelated_units(self):
        bundle = self._clean_bundle(duplicate_testcases=True)
        old_stage1 = bundle["stage1"]
        old_stage2 = bundle["stage2"]
        old_stage3 = bundle["stage3"]
        old_review = bundle["review"]
        owner_scope = old_stage1[0]["owner_scope_fingerprint"]

        new_map2 = copy.deepcopy(bundle["map2"])
        new_map2["revision"] = 1
        testcases = copy.deepcopy(new_map2["logical_testcases"])
        changed_id = testcases[0]["testcase_id"]
        sibling_id = testcases[1]["testcase_id"]
        testcases[0]["objective"] += " Changed locally."
        testcases[0]["testcase_fingerprint"] = artifact_fingerprint(
            testcases[0], "testcase_fingerprint")
        new_map2["logical_testcases"] = testcases
        new_map2["artifact_fingerprint"] = artifact_fingerprint(
            new_map2, "artifact_fingerprint")
        new_stage2_units, new_stage2_index = build_stage2_bundle(
            new_map2, testcases, old_stage1[1], owner_scope,
            ProjectJobError)
        new_stage2 = (
            new_stage2_index,
            {item["unit_id"]: item for item in new_stage2_units})

        new_candidate = copy.deepcopy(bundle["candidate"])
        new_candidate["revision"] = 1
        new_stage3_units, new_stage3_index, _ = build_stage3_bundle(
            new_candidate, old_stage1[1], new_stage2[1], owner_scope,
            ProjectJobError)
        new_stage3 = (
            new_stage3_index,
            {item["unit_id"]: item for item in new_stage3_units})
        new_review_units, new_review_index = build_review_bundle(
            bundle["report"], old_stage1[1], new_stage2[1], new_stage3[1],
            owner_scope, new_map2["policy_fingerprint"],
            new_map2["spec_fingerprint"], ProjectJobError)
        new_review = (
            new_review_index,
            {item["unit_id"]: item for item in new_review_units})
        impact = evaluate_impact(
            {"stage1": old_stage1, "stage2": old_stage2,
             "stage3": old_stage3, "review": old_review},
            {"stage1": old_stage1, "stage2": new_stage2,
             "stage3": new_stage3, "review": new_review},
            ProjectJobError)
        dirty_ids = {item["unit_id"] for item in impact["dirty_units"]}
        reused_ids = {item["unit_id"] for item in impact["reused_units"]}
        self.assertIn(changed_id, dirty_ids)
        self.assertIn("COVERAGE.AC.0001", dirty_ids)
        self.assertIn(sibling_id, reused_ids)
        self.assertIn("SCENARIO.0001", reused_ids)
        self.assertIn("AC.0001", reused_ids)
        self.assertTrue(any(item.startswith("CODE.SHARED.")
                            for item in reused_ids))
        self.assertFalse(impact["removed_units"])

    def test_shared_code_change_expands_declared_dirty_closure(self):
        bundle = self._clean_bundle(duplicate_testcases=True)
        owner_scope = bundle["stage1"][0]["owner_scope_fingerprint"]
        changed = copy.deepcopy(bundle["candidate"])
        changed["revision"] = 1
        shared = next(
            item for item in changed["code_units"]
            if item["role"] == "SHARED")
        shared["content"] = shared["content"].replace(
            "module ", "module /* shared change */ ", 1)
        shared["content_fingerprint"] = __import__("hashlib").sha256(
            shared["content"].encode()).hexdigest()
        by_id = {item["code_unit_id"]: item for item in changed["code_units"]}
        changed["content"] = "".join(
            by_id[unit_id]["content"]
            for unit_id in changed["assembly_manifest"])
        changed["content_fingerprint"] = __import__("hashlib").sha256(
            changed["content"].encode()).hexdigest()
        units, index, _ = build_stage3_bundle(
            changed, bundle["stage1"][1], bundle["stage2"][1],
            owner_scope, ProjectJobError)
        changed_stage3 = (index, {item["unit_id"]: item for item in units})
        impact = evaluate_impact(
            {"stage1": bundle["stage1"], "stage2": bundle["stage2"],
             "stage3": bundle["stage3"]},
            {"stage1": bundle["stage1"], "stage2": bundle["stage2"],
             "stage3": changed_stage3}, ProjectJobError)
        dirty_kinds = {item["unit_kind"] for item in impact["dirty_units"]}
        self.assertIn("CODE_SHARED", dirty_kinds)
        self.assertIn("CODE_TESTCASE", dirty_kinds)

    def test_tamper_path_substitution_and_cross_job_fail_closed(self):
        bundle = self._clean_bundle()
        index = copy.deepcopy(bundle["stage2"][0])
        index["children"][0]["path"] = "../escape.json"
        index["root_fingerprint"] = artifact_fingerprint(
            index, "root_fingerprint")
        with self.assertRaises(ProjectJobError) as caught:
            validate_index(index, bundle["job"], ProjectJobError)
        self.assertIn(caught.exception.code, {"INVALID_SCHEMA", "PATH_ESCAPE"})

        other = copy.deepcopy(bundle["stage2"][0])
        other["job_id"] = "JOB.PROJECT.OTHER.001"
        other["root_fingerprint"] = artifact_fingerprint(
            other, "root_fingerprint")
        with self.assertRaises(ProjectJobError) as cross:
            evaluate_impact(
                {"stage2": bundle["stage2"]},
                {"stage2": (other, bundle["stage2"][1])},
                ProjectJobError)
        self.assertEqual("CROSS_JOB_ARTIFACT", cross.exception.code)

    def test_reviewer_does_not_repair_and_initial_candidate_corrects_once(self):
        reviewer = FakeReviewerProvider(revision_stage="AC_TESTCASE_MAP")
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", FakeProvider(), reviewer)
        checkpoint = self.fixture.start_checked(
            workflow, self.fixture.project_input())
        self.assertEqual("AWAITING_REPAIR_PLAN", checkpoint["state"])
        self.assertEqual(1, reviewer.calls)
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        self.assertFalse((job / "staging/mappings/ac_testcase_map.r001.json").exists())
        self.assertEqual(
            checkpoint, workflow.start(self.fixture.project_input()))
        self.assertEqual(1, reviewer.calls)

        self.fixture.tearDown()
        self.fixture = workflow_tests.ProjectJobWorkflowTests(methodName="runTest")
        self.fixture.setUp()
        self.root = self.fixture.root
        invalid = Stage3ValidatedRetryProvider(["code"])
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", invalid,
            FakeReviewerProvider())
        result = self.fixture.start_checked(
            workflow, self.fixture.project_input())
        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        self.assertEqual(2, invalid.stage3_calls)
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        self.assertTrue((
            job / "staging/requests/stage3.r000.correction001.json").is_file())
        self.assertFalse((
            job / "staging/requests/stage3.r000.retry001.json").exists())


if __name__ == "__main__":
    unittest.main()
