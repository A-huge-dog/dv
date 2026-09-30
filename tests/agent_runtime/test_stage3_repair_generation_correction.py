"""Repair-generation correction uses the repair model and resumes immutably."""
from __future__ import annotations

import json
import unittest
from dataclasses import replace

import yaml

from application.generation import GenerateStage3Input
from contracts.validator import load_document
from domain.uvm_testcase import UVM_TEST_SELECTION_CONTRACT
from runtime.errors import ProjectJobError
from runtime.project_job import ProjectJobWorkflow
from runtime.staged_workflow import MAX_CORRECTION_ATTEMPTS, StagedProjectWorkflow
from tests.agent_runtime import test_project_job_workflow as fixtures


class RepairProvider(fixtures.FakeProvider):
    provider_id = "fake-repair-stage3-provider"
    model_id = "fake-repair-stage3-model"

    def __init__(self):
        super().__init__()
        self.reject_corrections = False
        self.omit_testcase = False

    def select_tools(self, request):
        response = super().select_tools(request)
        candidate = response["tool_calls"][0]["arguments"]
        if (request["metadata"].get("candidate_correction_attempt") is None
                or self.reject_corrections):
            if self.omit_testcase:
                candidate["code_units"] = candidate["code_units"][:1]
                candidate["assembly"] = [0]
                candidate["implemented_testcase_ids"] = []
            else:
                candidate["code_units"][1]["testcase_ids"] = []
        else:
            # A complete correction may regenerate several code units while
            # retaining the same upstream testcase mapping.
            for unit in candidate["code_units"]:
                unit["content"] += "// Regenerated within the same testcase scope.\n"
        return response


class Stage3RepairGenerationCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectJobWorkflowTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.provider = RepairProvider()
        config = load_document(self.root / "config/generator.yaml")
        config.update(provider_id=self.provider.provider_id,
                      model_id=self.provider.model_id)
        (self.root / "config/repair-stage3.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        profile = load_document(self.root / "config/agents.yaml")
        profile["repair"]["stage3"] = "config/repair-stage3.yaml"
        (self.root / "config/agents.yaml").write_text(
            yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        self.workflow = ProjectJobWorkflow(
            self.root, self.root / "result", fixtures.FakeProvider(),
            fixtures.FakeReviewerProvider(),
            role_providers={"repair.stage3": self.provider})
        submission = self.fixture.project_input()
        self.fixture.start_checked(self.workflow, submission)
        value = self.workflow.bootstrap_handler.handle(submission, create=False)
        self.job = self.root / "result/jobs" / value["job_id"]
        self.staged = StagedProjectWorkflow(self.workflow)
        spec, sources, fingerprint = self.staged._spec(value)
        map1, map2, testcases, shards, prior = self.staged._load_resume_bundle(
            self.job, value, sources, fingerprint)
        original_request = load_document(
            self.job / "staging/requests/stage3.r000.json")
        uvm = json.loads(original_request["messages"][1]["content"])[
            "uvm_testcase_context"]
        self.command = GenerateStage3Input(
            project_input=value, job_root=self.job, spec_evidence=spec,
            spec_fingerprint=fingerprint, scenario_ac_map=map1,
            ac_testcase_map=map2, testcases=testcases, shards=shards,
            revision=1, budget=self.workflow._budget(), prior=prior,
            issues=[{"required_change": "Regenerate the repaired testcase."}],
            effective_uvm_files=tuple(uvm["files"]),
            effective_uvm_root=uvm["effective_uvm_root"],
            profile_section="repair")
        self.workflow._probe_provider(self.job, "repair.stage3")

    def generate(self):
        return self.staged.generate_stage3_handler.handle(replace(
            self.command, budget=self.workflow._budget()))

    def test_repair_correction_and_replay_use_repair_provider_identity(self):
        result = self.generate()
        self.assertEqual("PASS", result.artifact["validation"]["status"])
        self.assertEqual(1, result.artifact["revision"])
        self.assertEqual(2, self.provider.calls)
        for request in self.provider.requests:
            self.assertIn(UVM_TEST_SELECTION_CONTRACT,
                          request["messages"][0]["content"])
        original = self.job / "audit/pj002_provider_response.stage3.r001.json"
        original_bytes = original.read_bytes()
        correction = load_document(
            self.job / "staging/requests/stage3.r001.correction001.json")
        self.assertEqual("repair", correction["metadata"]["agent_profile_section"])
        self.assertEqual(self.provider.model_id,
                         correction["metadata"]["model_id"])
        feedback = json.loads(correction["messages"][-1]["content"])[
            "candidate_correction_feedback"]
        self.assertEqual("TESTCASE_MAPPING_OVERREACH",
                         feedback["validation_diagnostics"][0]["code"])

        replay = self.generate()

        self.assertEqual(result.artifact, replay.artifact)
        self.assertTrue(replay.replayed)
        self.assertEqual(2, self.provider.calls)
        self.assertEqual(original_bytes, original.read_bytes())

    def test_budget_pause_resumes_with_correction_instead_of_invalid_replay(self):
        self.workflow.max_total_provider_calls = 1
        with self.assertRaises(ProjectJobError) as paused:
            self.generate()
        self.assertEqual("TOOL_LIMIT_EXCEEDED", paused.exception.code)
        self.assertEqual(1, self.provider.calls)
        original = self.job / "audit/pj002_provider_response.stage3.r001.json"
        original_bytes = original.read_bytes()
        self.assertFalse((self.job /
            "staging/generated/portable_sv/testcase.r001.json").exists())

        result = self.generate()

        self.assertEqual("PASS", result.artifact["validation"]["status"])
        self.assertEqual(2, self.provider.calls)
        self.assertEqual(original_bytes, original.read_bytes())

    def test_aggregate_diagnostic_names_missing_testcase_and_allows_new_unit(self):
        self.provider.omit_testcase = True

        result = self.generate()

        missing_id = next(item["testcase_id"] for item in self.command.testcases
                          if item["status"] == "CHECKABLE")
        feedback = json.loads(self.provider.requests[1]["messages"][-1][
            "content"])["candidate_correction_feedback"]
        diagnostic = next(
            json.loads(item["message"])
            for item in feedback["validation_diagnostics"]
            if item["code"] == "TESTCASE_MAPPING_OVERREACH")
        self.assertEqual(missing_id, diagnostic["offending_content"])
        self.assertEqual(1, diagnostic["match_count"])
        self.assertIn("neither implemented nor skipped", diagnostic["message"])
        self.assertIn("missing CHECKABLE testcase", diagnostic["required_correction"])
        original = load_document(
            self.job / "audit/pj002_provider_response.stage3.r001.json")[
                "tool_calls"][0]["arguments"]
        corrected = load_document(self.job /
            "audit/pj002_provider_response.stage3.r001.correction001.json")[
                "tool_calls"][0]["arguments"]
        self.assertEqual(1, len(original["code_units"]))
        self.assertEqual(2, len(corrected["code_units"]))
        self.assertEqual([0], original["assembly"])
        self.assertEqual([0, 1], corrected["assembly"])
        self.assertEqual([missing_id], result.artifact["implemented_testcase_ids"])
        self.assertEqual(result.artifact, self.generate().artifact)
        self.assertEqual(2, self.provider.calls)

    def test_repeated_invalid_correction_is_bounded_and_next_run_can_recover(self):
        self.provider.reject_corrections = True
        with self.assertRaises(ProjectJobError) as paused:
            self.generate()
        self.assertEqual("ATTEMPT_PAUSED", paused.exception.code)
        self.assertEqual(1 + MAX_CORRECTION_ATTEMPTS, self.provider.calls)
        self.assertFalse((self.job /
            "staging/generated/portable_sv/testcase.r001.json").exists())
        persisted = {
            path: path.read_bytes() for path in self.job.glob(
                "audit/pj002_provider_response.stage3.r001*.json")}
        self.provider.reject_corrections = False

        result = self.generate()

        self.assertEqual("PASS", result.artifact["validation"]["status"])
        self.assertEqual(2 + MAX_CORRECTION_ATTEMPTS, self.provider.calls)
        self.assertEqual(persisted, {
            path: path.read_bytes() for path in persisted})


if __name__ == "__main__":
    unittest.main()
