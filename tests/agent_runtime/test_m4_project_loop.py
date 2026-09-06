#!/usr/bin/env python3
"""M4 ProjectLoop integration for the continuous UVM Worker."""
from __future__ import annotations

import copy
import json
import unittest
from importlib import import_module
from unittest.mock import patch

from contracts.validator import load_document
from domain.uvm_testcase import testcase_marker
from infrastructure.persistence.worker_state_store import WorkerStateStore
from runtime.errors import ProjectJobError
from runtime.project_job import ProjectJobWorkflow
from runtime.project_loop import (
    CheckpointRepository, ProjectLoop, ProjectLoopRequest, ResumePolicy,
    WorkflowState,
)
from scripts.dvlib import canonical_hash

try:
    _project_fixtures = import_module("test_project_job_workflow")
except ModuleNotFoundError:
    _project_fixtures = import_module(
        "tests.agent_runtime.test_project_job_workflow")

class ScriptedContinuousUvmProvider:
    provider_id = _project_fixtures.FakeUvmProvider.provider_id
    model_id = _project_fixtures.FakeUvmProvider.model_id

    def __init__(self, tools):
        self.tools = list(tools)
        self.calls = 0
        self.requests = []
        self.candidate_version = 0

    def probe(self):
        return {
            "schema_version": "1.0", "provider_id": self.provider_id,
            "model_id": self.model_id, "status": "PASS",
            "tool_call_capable": True, "provider_version": "m4-test",
            "diagnostics": [],
        }

    def restore_probe(self, _probe):
        return None

    @staticmethod
    def _replacements(request, version):
        payload = json.loads(request["messages"][1]["content"])
        project_token = canonical_hash({
            "job_id": payload["job_identity"]["job_id"],
            "project_id": "PROJECT.TINY",
            "artifact_kind": "PORTABLE_SV_TESTBENCH",
        })[:16].upper()
        markers = "// DV_PROJECT_{}_PASS\n{}".format(
            project_token, "\n".join(
                "// {}".format(testcase_marker(item["testcase_id"]))
                for item in payload["immutable_generation_context"][
                    "logical_testcases"]))
        return [{
            "logical_path": path,
            "content": "// M4 candidate {}\n{}\n".format(version, markers),
        } for path in payload["immutable_generation_context"][
            "generated_file_slots"]]

    def select_tools(self, request):
        self.calls += 1
        self.requests.append(copy.deepcopy(request))
        if not self.tools:
            raise AssertionError("M4 UVM Provider script is exhausted")
        name = self.tools.pop(0)
        arguments = {}
        if name == "write_uvm_replacements":
            self.candidate_version += 1
            arguments = {"replacements": self._replacements(
                request, self.candidate_version)}
        elif name == "pause_task":
            arguments = {"reason": "persisted pause for ProjectLoop resume"}
        return {
            "schema_version": "1.0",
            "request_id": request["request_id"],
            "operation": "SELECT_TOOLS", "finish_reason": "TOOL_CALLS",
            "content": "", "tool_calls": [{
                "call_id": "CALL.M4.{:03d}".format(self.calls),
                "name": name, "arguments": arguments,
            }],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "model_id": self.model_id,
            "provider_metadata": {
                "provider_id": self.provider_id,
                "response_id": "RESPONSE.M4.{:03d}".format(self.calls),
                "provider_status": "completed",
            },
            "diagnostics": [],
        }


class ScriptedXcelium:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.requests = []

    def __call__(self, _value, _job_root, request):
        self.requests.append(copy.deepcopy(request))
        if not self.statuses:
            raise AssertionError("M4 Xcelium script is exhausted")
        status = self.statuses.pop(0)
        return {
            "execution_status": status,
            "exit_code": 0 if status == "PASS" else 1,
            "stdout": "compile {}".format(status),
            "stderr": "first compile failed" if status == "FAIL" else "",
            "diagnostic_codes": (
                ["COMPILE_FAILED"] if status == "FAIL" else []),
            "logs": [],
        }


class M4ProjectLoopTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _project_fixtures.ProjectJobWorkflowTests(
            methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.generator = _project_fixtures.FakeProvider()
        self.reviewer = _project_fixtures.FakeReviewerProvider()

    def _loop(self, uvm_provider, runner, *, calls=12):
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result",
            max_total_provider_calls=calls,
            uvm_build_runner=runner)
        workflow.role_providers["initial.uvm"] = uvm_provider
        workflow.role_providers["repair.uvm"] = uvm_provider

        def provider_factory(_job_root, _manifest, role):
            return self.reviewer if role.startswith("review.") \
                else self.generator

        return ProjectLoop(
            workflow, provider_factory=provider_factory,
            uvm_build_runner=runner), workflow

    def _route_to_generation(self, loop, submission):
        first = loop.run_until_pause(ProjectLoopRequest(submission))
        self.assertEqual("AWAITING_SCENARIO_ROUTING", first["state"])
        job_root = self.root / "result/jobs" / submission["job_id"]
        form = load_document(job_root / first["owner_review_path"])
        routing = self.fixture.completed_owner_review(
            form, "AC_TESTCASE_MAP_AND_TESTCASE", "")
        return loop.run_until_pause(ProjectLoopRequest(
            submission, scenario_routing=routing))

    @staticmethod
    def _stage_names(generator):
        return [request["metadata"]["stage"]
                for request in generator.requests]

    def test_fail_repair_pass_enters_stage3_only_after_worker_success(self):
        provider = ScriptedContinuousUvmProvider([
            "write_uvm_replacements", "run_xcelium_compile",
            "write_uvm_replacements", "run_xcelium_compile", "finish_task",
        ])
        xcelium = ScriptedXcelium(["FAIL", "PASS"])
        loop, _workflow = self._loop(provider, xcelium)
        submission = self.fixture.project_input()

        result = self._route_to_generation(loop, submission)

        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        self.assertEqual(2, len(xcelium.requests))
        self.assertEqual({"DVWORKER.UVM.INITIAL"}, {
            request["metadata"]["session_id"]
            for request in provider.requests})
        self.assertIn(
            "first compile failed",
            json.dumps(provider.requests[2]["messages"]))
        self.assertEqual(
            ["SCENARIO_AC_MAP", "AC_TESTCASE_MAP", "TESTCASE"],
            self._stage_names(self.generator))
        job_root = self.root / "result/jobs" / submission["job_id"]
        worker = load_document(sorted((
            job_root / "audit/workers/DVTASK.UVM.INITIAL").glob(
                "state-*.json"))[-1])
        self.assertEqual("SUCCEEDED", worker["status"])

    def test_retryable_pause_resumes_same_job_without_repeating_xcelium(self):
        provider = ScriptedContinuousUvmProvider([
            "write_uvm_replacements", "run_xcelium_compile", "pause_task",
            "write_uvm_replacements", "run_xcelium_compile", "finish_task",
        ])
        xcelium = ScriptedXcelium(["FAIL", "PASS"])
        loop, _workflow = self._loop(provider, xcelium)
        submission = self.fixture.project_input()

        paused = self._route_to_generation(loop, submission)
        self.assertEqual("PAUSED_RETRYABLE", paused["state"])
        self.assertEqual(ResumePolicy.RETRY.value, paused["resume_policy"])
        self.assertEqual(1, len(xcelium.requests))
        self.assertNotIn("TESTCASE", self._stage_names(self.generator))

        resumed = loop.run_until_pause(ProjectLoopRequest(submission))
        self.assertEqual("AWAITING_HUMAN_REVIEW", resumed["state"])
        self.assertEqual(2, len(xcelium.requests))
        self.assertEqual(6, provider.calls)
        self.assertEqual({"DVWORKER.UVM.INITIAL"}, {
            request["metadata"]["session_id"]
            for request in provider.requests})

    def test_success_replay_after_stage3_crash_has_no_worker_side_effects(self):
        provider = ScriptedContinuousUvmProvider([
            "write_uvm_replacements", "run_xcelium_compile", "finish_task",
        ])
        xcelium = ScriptedXcelium(["PASS"])
        loop, _workflow = self._loop(provider, xcelium)
        submission = self.fixture.project_input()

        with patch(
                "runtime.staged_workflow.GenerateStage3Handler.handle",
                side_effect=ProjectJobError(
                    "FAILED_INTERNAL", "simulated crash before Stage 3")):
            with self.assertRaises(ProjectJobError) as caught:
                self._route_to_generation(loop, submission)
        self.assertEqual("FAILED_INTERNAL", caught.exception.code)
        self.assertEqual(3, provider.calls)
        self.assertEqual(1, len(xcelium.requests))
        self.assertNotIn("TESTCASE", self._stage_names(self.generator))

        resumed = loop.run_until_pause(ProjectLoopRequest(submission))
        self.assertEqual("AWAITING_HUMAN_REVIEW", resumed["state"])
        self.assertEqual(3, provider.calls)
        self.assertEqual(1, len(xcelium.requests))
        self.assertEqual(1, self._stage_names(self.generator).count("TESTCASE"))

    def test_budget_and_recovery_pauses_never_enter_stage3(self):
        provider = ScriptedContinuousUvmProvider([
            "write_uvm_replacements",
        ])
        xcelium = ScriptedXcelium([])
        loop, _workflow = self._loop(provider, xcelium, calls=1)
        submission = self.fixture.project_input()

        paused = self._route_to_generation(loop, submission)
        self.assertEqual("PAUSED_BUDGET", paused["state"])
        self.assertEqual(ResumePolicy.RETRY.value, paused["resume_policy"])
        self.assertEqual([], xcelium.requests)
        self.assertNotIn("TESTCASE", self._stage_names(self.generator))

        job_root = self.root / "result/jobs" / submission["job_id"]
        current = load_document(sorted((
            job_root / "audit/workers/DVTASK.UVM.INITIAL").glob(
                "state-*.json"))[-1])
        store = WorkerStateStore(
            job_root=job_root,
            task_path="audit/workers/DVTASK.UVM.INITIAL",
            task_id="DVTASK.UVM.INITIAL",
            worker_session_id="DVWORKER.UVM.INITIAL",
            job_id=submission["job_id"],
            authority_fingerprint=current["authority_fingerprint"])
        store.mark_status(
            "PAUSED_RECOVERY_REQUIRED",
            transcript_cursor=store.current["transcript_cursor"],
            current_phase="ACT",
            error={"code": "PAUSED_RECOVERY_REQUIRED",
                   "message": "operator must resolve uncertain action"})

        recovery = loop.run_until_pause(ProjectLoopRequest(submission))
        self.assertEqual("PAUSED_RECOVERY_REQUIRED", recovery["state"])
        self.assertEqual(ResumePolicy.OPERATOR.value,
                         recovery["resume_policy"])
        self.assertEqual(1, provider.calls)
        self.assertEqual([], xcelium.requests)
        self.assertNotIn("TESTCASE", self._stage_names(self.generator))

    def test_tampered_worker_state_fails_closed_before_stage3(self):
        provider = ScriptedContinuousUvmProvider([
            "write_uvm_replacements",
        ])
        xcelium = ScriptedXcelium([])
        loop, _workflow = self._loop(provider, xcelium, calls=1)
        submission = self.fixture.project_input()
        paused = self._route_to_generation(loop, submission)
        self.assertEqual("PAUSED_BUDGET", paused["state"])

        job_root = self.root / "result/jobs" / submission["job_id"]
        latest = sorted((
            job_root / "audit/workers/DVTASK.UVM.INITIAL").glob(
                "state-*.json"))[-1]
        tampered = load_document(latest)
        tampered["current_error"]["message"] = "tampered"
        latest.write_text(
            json.dumps(tampered, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")

        with self.assertRaises(ProjectJobError) as caught:
            loop.run_until_pause(ProjectLoopRequest(submission))
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        self.assertEqual([], xcelium.requests)
        self.assertNotIn("TESTCASE", self._stage_names(self.generator))

    def test_blocked_tool_preserves_project_authority_and_skips_stage3(self):
        provider = ScriptedContinuousUvmProvider([
            "write_uvm_replacements", "run_xcelium_compile",
        ])

        def unavailable_xcelium(*_args):
            raise ProjectJobError("BLOCKED_TOOL", "Xcelium is unavailable")

        loop, _workflow = self._loop(provider, unavailable_xcelium)
        submission = self.fixture.project_input()
        blocked = self._route_to_generation(loop, submission)

        self.assertEqual("BLOCKED_TOOL", blocked["state"])
        self.assertEqual(ResumePolicy.OPERATOR.value,
                         blocked["resume_policy"])
        self.assertFalse(blocked["current_state_modified"])
        self.assertNotIn("TESTCASE", self._stage_names(self.generator))

    def test_worker_policy_failure_is_terminal_and_skips_stage3(self):
        provider = ScriptedContinuousUvmProvider(["unscoped_shell"])
        loop, _workflow = self._loop(provider, ScriptedXcelium([]))
        submission = self.fixture.project_input()

        failed = self._route_to_generation(loop, submission)

        self.assertEqual("FAILED_POLICY", failed["state"])
        self.assertEqual(ResumePolicy.TERMINAL.value,
                         failed["resume_policy"])
        self.assertNotIn("TESTCASE", self._stage_names(self.generator))

    def test_project_loop_policy_covers_every_worker_non_success_terminal(self):
        expected = {
            WorkflowState.PAUSED_BUDGET: ResumePolicy.RETRY,
            WorkflowState.PAUSED_RETRYABLE: ResumePolicy.RETRY,
            WorkflowState.PAUSED_RECOVERY_REQUIRED: ResumePolicy.OPERATOR,
            WorkflowState.BLOCKED_INPUT: ResumePolicy.OPERATOR,
            WorkflowState.BLOCKED_TOOL: ResumePolicy.OPERATOR,
            WorkflowState.CANCELLED: ResumePolicy.TERMINAL,
            WorkflowState.FAILED_POLICY: ResumePolicy.TERMINAL,
            WorkflowState.FAILED_INTERNAL: ResumePolicy.TERMINAL,
        }
        self.assertEqual(
            expected,
            {state: CheckpointRepository.policy_for(state)
             for state in expected})


if __name__ == "__main__":
    unittest.main()
