#!/usr/bin/env python3
"""Standalone Stage 3 test Job and retry-budget tests."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from contracts.validator import load_document
from core.project_job import ProjectJobError, ProjectJobWorkflow
from core.project_stage3 import StandaloneStage3Workflow
from tests.agent_runtime.test_project_job_workflow import (
    FakeProvider, FakeReviewerProvider, SPEC,
    Stage3ValidatedRetryProvider,
)


class PreEdgeProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.stage3_calls = 0

    def select_tools(self, request):
        response = super().select_tools(request)
        if request["metadata"]["stage"] != "TESTCASE":
            return response
        self.stage3_calls += 1
        if self.stage3_calls == 1:
            candidate = response["tool_calls"][0]["arguments"]
            testcase = next(
                item for item in candidate["code_units"]
                if item["role"] == "TESTCASE")
            testcase["content"] = testcase["content"].replace(
                "    $display(",
                "    while (!s_axi_bvalid) begin #1; end\n"
                "    $display(", 1)
        return response


class AlternateModelProvider(FakeProvider):
    model_id = "fake-stage3-alternate-model"


class StandaloneStage3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "spec.md").write_text(SPEC, encoding="utf-8")
        (self.root / "tiny.sv").write_text(
            "module tiny(input logic clk, output logic y);\n"
            "  assign y = clk;\nendmodule\n", encoding="utf-8")
        (self.root / "config").mkdir()
        generator = {
            "schema_version": "1.0",
            "provider_kind": "DASHSCOPE",
            "provider_id": "fake-project-provider",
            "model_id": "fake-project-model",
            "endpoint":
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "auth_env": "DASHSCOPE_API_KEY",
            "api_version": "chat-completions-v1",
            "tool_calls_required": True,
            "store": False,
            "timeout_seconds": 60,
            "max_retries": 0,
            "max_output_tokens": 8192,
            "enable_thinking": False,
        }
        reviewer = copy.deepcopy(generator)
        reviewer["provider_id"] = "fake-project-reviewer"
        reviewer["model_id"] = "fake-reviewer-model"
        (self.root / "config/generator.yaml").write_text(
            yaml.safe_dump(generator, sort_keys=False), encoding="utf-8")
        (self.root / "config/reviewer.yaml").write_text(
            yaml.safe_dump(reviewer, sort_keys=False), encoding="utf-8")
        profile = {
            "schema_version": "1.0",
            "profile_id": "PROJECT_AGENT_PROFILE.STAGE3_TEST",
            "initial": {key: "config/generator.yaml" for key in (
                "stage1", "stage2", "stage3")},
            "repair": {
                "orchestrator": "config/generator.yaml",
                **{key: "config/generator.yaml" for key in (
                    "stage1", "stage2", "stage3")},
            },
            "review": {key: "config/reviewer.yaml" for key in (
                "initial", "final")},
        }
        (self.root / "config/agents.yaml").write_text(
            yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _completed_owner_review(form):
        result = copy.deepcopy(form)
        result["actor"]["identity"] = "human.dv.owner"
        for item in result["scenarios"]:
            item["comment"] = ""
            item["routing"]["destination"] = \
                "AC_TESTCASE_MAP_AND_TESTCASE"
        return result

    def _submission(self, job_id="JOB.PROJECT.TINY.SOURCE"):
        return {
            "schema_version": "2.0",
            "job_id": job_id,
            "project_id": "PROJECT.TINY",
            "spec": {"sources": ["spec.md"]},
            "rtl": {
                "sources": ["tiny.sv"],
                "top": "tiny",
                "parameters": {},
            },
            "agent_profile": "config/agents.yaml",
            "eda": {
                "profile_id": "EDAPROFILE.VERILATOR.PROJECT.V1",
                "timeout_seconds": 60,
            },
            "input_authority": {
                "actor_type": "HUMAN",
                "identity": "human.project.owner",
                "roles": ["SPEC_OWNER", "DESIGN_OWNER"],
                "decision": "APPROVE",
            },
        }

    def _source_job(self, job_id="JOB.PROJECT.TINY.SOURCE"):
        provider = Stage3ValidatedRetryProvider([])
        reviewer = FakeReviewerProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", provider, reviewer)
        submission = self._submission(job_id)
        checkpoint = workflow.start(submission)
        job = self.root / "result/jobs" / job_id
        form = load_document(job / checkpoint["owner_review_path"])
        result = workflow.route_scenarios(
            submission, self._completed_owner_review(form))
        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        self.assertTrue((job /
            "staging/generated/portable_sv/testcase.r000.json").is_file())
        self.assertEqual(1, provider.stage3_calls)
        self.assertFalse((job /
            "staging/requests/stage3.r000.correction001.json").exists())
        return job

    @staticmethod
    def _stage3_input(
            job_id="stage3_R",
            source_job_id="JOB.PROJECT.TINY.SOURCE",
            generator_config="config/generator.yaml"):
        return {
            "schema_version": "1.0",
            "job_id": job_id,
            "source": {
                "project_job_id": source_job_id,
                "scenario_ac_map":
                    "staging/mappings/scenario_ac_map.checked.r000.json",
                "ac_testcase_map":
                    "staging/mappings/ac_testcase_map.r000.json",
            },
            "generator_config": generator_config,
            "input_authority": {
                "actor_type": "HUMAN",
                "identity": "human.stage3.owner",
                "role": "STAGE3_TEST_OWNER",
                "decision": "APPROVE",
            },
        }

    @staticmethod
    def _snapshot(root: Path):
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()
        }

    def test_yaml_selects_alternate_stage3_model_without_new_source_job(self):
        source = self._source_job()
        before = self._snapshot(source)
        alternate = yaml.safe_load(
            (self.root / "config/generator.yaml").read_text())
        alternate["model_id"] = "fake-stage3-alternate-model"
        (self.root / "config/stage3_alternate.yaml").write_text(
            yaml.safe_dump(alternate, sort_keys=False), encoding="utf-8")
        provider = AlternateModelProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", provider,
            FakeReviewerProvider())
        submission = self._stage3_input(
            job_id="stage3_R_alt",
            generator_config="config/stage3_alternate.yaml")

        runner = StandaloneStage3Workflow(workflow)
        result = runner.run(submission)

        self.assertEqual("PASS", result["status"])
        manifest = load_document(self.root /
            "result/jobs/stage3_R_alt/input_baseline/"
            "stage3_job_manifest.json")
        self.assertEqual(
            "fake-stage3-alternate-model",
            manifest["generator"]["model_id"])
        response = load_document(self.root /
            "result/jobs/stage3_R_alt/audit/"
            "pj002_provider_response.stage3.r000.json")
        self.assertEqual(
            "fake-stage3-alternate-model", response["model_id"])
        self.assertEqual(1, provider.probe_calls)
        self.assertEqual(0, provider.restore_calls)
        self.assertEqual(before, self._snapshot(source))
        conflicting = copy.deepcopy(submission)
        conflicting["generator_config"] = "config/generator.yaml"
        with self.assertRaises(ProjectJobError) as caught:
            runner.run(conflicting)
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        self.assertEqual(1, provider.calls)

    def test_pre_edge_handshake_is_deferred_to_semantic_review(self):
        self._source_job()
        provider = PreEdgeProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", provider,
            FakeReviewerProvider())

        result = StandaloneStage3Workflow(workflow).run(
            self._stage3_input())

        self.assertEqual("PASS", result["status"])
        self.assertEqual(1, provider.stage3_calls)
        self.assertFalse((self.root /
            "result/jobs/stage3_R/staging/requests/"
            "stage3.r000.retry001.json").exists())

    def test_unsafe_conflicting_and_tampered_source_fail_before_call(self):
        source = self._source_job()
        provider = FakeProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", provider,
            FakeReviewerProvider())
        runner = StandaloneStage3Workflow(workflow)
        with self.assertRaises(ProjectJobError) as unsafe:
            runner.run(self._stage3_input(job_id="../stage3_R"))
        self.assertEqual("INVALID_SCHEMA", unsafe.exception.code)
        self.assertEqual(0, provider.calls)

        map2 = source / "staging/mappings/ac_testcase_map.r000.json"
        document = load_document(map2)
        document["job_id"] = "JOB.PROJECT.OTHER"
        map2.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ProjectJobError):
            runner.run(self._stage3_input())
        self.assertEqual(0, provider.calls)
        self.assertEqual(0, provider.probe_calls)


if __name__ == "__main__":
    unittest.main()
