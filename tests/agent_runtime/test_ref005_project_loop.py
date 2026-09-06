#!/usr/bin/env python3
"""REF-005 Project-loop checkpoint and pause qualification."""
from __future__ import annotations

import unittest
from importlib import import_module

from contracts.validator import load_document
from runtime.project_job import ProjectJobWorkflow
from runtime.project_loop import (
    CheckpointRepository, ProjectLoop, ProjectLoopRequest, ResumePolicy,
    WorkflowState,
)

try:
    _fixtures = import_module("test_project_job_workflow")
except ModuleNotFoundError:
    _fixtures = import_module("tests.agent_runtime.test_project_job_workflow")


class Ref005ProjectLoopTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _fixtures.ProjectJobWorkflowTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.generator = _fixtures.FakeProvider()
        self.reviewer = _fixtures.FakeReviewerProvider()
        self.workflow = ProjectJobWorkflow(self.root, self.root / "result")

        def provider_factory(_job_root, _manifest, role):
            return self.reviewer if role.startswith("review.") else self.generator

        self.loop = ProjectLoop(
            self.workflow, provider_factory=provider_factory)

    def project_input(self):
        return self.fixture.project_input()

    def completed_owner_review(self, *args):
        return self.fixture.completed_owner_review(*args)

    def test_auto_path_runs_to_first_human_pause_then_replays_exactly(self):
        submission = self.project_input()
        first = self.loop.run_until_pause(ProjectLoopRequest(submission))
        self.assertEqual("AWAITING_SCENARIO_ROUTING", first["state"])
        self.assertEqual(ResumePolicy.HUMAN.value, first["resume_policy"])
        self.assertEqual(1, self.generator.calls)

        job_root = self.root / "result/jobs" / submission["job_id"]
        form = load_document(job_root / first["owner_review_path"])
        routed = self.completed_owner_review(
            form, "AC_TESTCASE_MAP_AND_TESTCASE", "")
        second = self.loop.run_until_pause(ProjectLoopRequest(
            submission, scenario_routing=routed))
        self.assertEqual("AWAITING_HUMAN_REVIEW", second["state"])
        self.assertEqual(ResumePolicy.HUMAN.value, second["resume_policy"])
        self.assertEqual(3, self.generator.calls)
        self.assertEqual(1, self.reviewer.calls)

        replay = self.loop.run_until_pause(ProjectLoopRequest(submission))
        self.assertEqual(second, replay)
        self.assertEqual(3, self.generator.calls)
        self.assertEqual(1, self.reviewer.calls)

    def test_checkpoint_repository_uses_verified_persisted_authority(self):
        submission = self.project_input()
        self.loop.run_until_pause(ProjectLoopRequest(submission))
        checkpoint = CheckpointRepository(self.workflow).read(
            ProjectLoopRequest(submission))
        self.assertEqual(WorkflowState.AWAITING_SCENARIO_ROUTING, checkpoint.state)
        self.assertEqual(ResumePolicy.HUMAN, checkpoint.policy)
        self.assertEqual(
            "staging/validations/scenario_owner_review.json",
            checkpoint.source_path)

    def test_policy_matrix_has_no_implicit_human_or_retry_progress(self):
        expected = {
            WorkflowState.BOOTSTRAP: ResumePolicy.AUTO,
            WorkflowState.INITIAL_GENERATION: ResumePolicy.AUTO,
            WorkflowState.AWAITING_REPAIR_PLAN: ResumePolicy.AUTO,
            WorkflowState.AWAITING_SCOPED_REPLACEMENT: ResumePolicy.AUTO,
            WorkflowState.SCOPED_REPLACEMENT_VALIDATED: ResumePolicy.AUTO,
            WorkflowState.AWAITING_SCENARIO_ROUTING: ResumePolicy.HUMAN,
            WorkflowState.AWAITING_HUMAN_REVIEW: ResumePolicy.HUMAN,
            WorkflowState.AWAITING_EXECUTION_AUTHORIZATION: ResumePolicy.HUMAN,
            WorkflowState.READY_FOR_BINDING: ResumePolicy.AUTO,
            WorkflowState.READY_FOR_EXECUTION: ResumePolicy.AUTO,
            WorkflowState.EXECUTION_PASS: ResumePolicy.TERMINAL,
            WorkflowState.EXECUTION_FAIL: ResumePolicy.TERMINAL,
            WorkflowState.EXECUTION_BLOCKED: ResumePolicy.TERMINAL,
            WorkflowState.PAUSED_RETRYABLE: ResumePolicy.RETRY,
            WorkflowState.PAUSED_COMPILE_REPAIR_REQUIRED: ResumePolicy.RETRY,
        }
        self.assertEqual(
            expected,
            {state: CheckpointRepository.policy_for(state)
             for state in expected},
        )


if __name__ == "__main__":
    unittest.main()
