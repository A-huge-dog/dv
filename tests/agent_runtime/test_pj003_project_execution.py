#!/usr/bin/env python3
"""PJ-003 approval, binding, execution, and replay qualification."""
from __future__ import annotations

import copy
import json
import os
import unittest
from importlib import import_module

from adapters.eda import XceliumAdapter
from contracts.validator import load_document
from domain.artifacts import artifact_fingerprint
from runtime.errors import ProjectJobError
from runtime.project_job import ProjectJobWorkflow
from runtime.project_loop import ProjectLoop, ProjectLoopRequest

try:
    _fixtures = import_module("test_project_job_workflow")
    _xcelium = import_module("test_xcelium_adapter")
except ModuleNotFoundError:
    _fixtures = import_module("tests.agent_runtime.test_project_job_workflow")
    _xcelium = import_module("tests.agent_runtime.test_xcelium_adapter")


class MarkerProvider(_fixtures.FakeProvider):
    marker = ""

    @classmethod
    def stage3(cls, payload):
        result = super().stage3(payload)
        result["code_units"][0]["content"] += "// {}\n".format(cls.marker)
        return result


class CompileFailureProvider(MarkerProvider):
    marker = "FAKE_COMPILE_FAILURE"


class LicenseBlockedProvider(MarkerProvider):
    marker = "FAKE_LICENSE_FAILURE"


class Pj003ProjectExecutionTests(unittest.TestCase):
    provider_type = _fixtures.FakeProvider

    def setUp(self):
        self.fixture = _fixtures.ProjectJobWorkflowTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.result_root = self.root / "result"
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.xrun = self.tools / "xrun"
        self.xrun.write_text(_xcelium.FAKE_XRUN, encoding="utf-8")
        self.xrun.chmod(0o755)
        self.generator = self.provider_type()
        self.reviewer = _fixtures.FakeReviewerProvider()
        self.workflow = ProjectJobWorkflow(self.root, self.result_root)
        self.created_adapters = []

        def provider_factory(_job_root, _manifest, role):
            return self.reviewer if role.startswith("review.") else self.generator

        def adapter_factory(_job_root, manifest, authorization):
            adapter = self._adapter(
                manifest["job_id"], authorization["environment_identity"])
            self.created_adapters.append(adapter)
            return adapter

        self.loop = ProjectLoop(
            self.workflow, provider_factory=provider_factory,
            eda_adapter_factory=adapter_factory)

    def _adapter(self, job_id, identity="XCELIUMENV.PJ003.TEST.24_09"):
        return XceliumAdapter(
            self.root, self.result_root, job_id, self.xrun, identity, {
                "PATH": "{}{}{}".format(
                    self.tools, os.pathsep, "/usr/bin"),
                "LM_LICENSE_FILE": "27000@private-license-host",
                "LC_ALL": "C",
            }, timeout_seconds=10)

    def invocation_count(self):
        path = self.tools / "invocations.txt"
        return int(path.read_text()) if path.exists() else 0

    def to_human_gate(self):
        submission = self.fixture.project_input()
        first = self.loop.run_until_pause(ProjectLoopRequest(submission))
        job_root = self.result_root / "jobs" / submission["job_id"]
        form = load_document(job_root / first["owner_review_path"])
        routing = self.fixture.completed_owner_review(
            form, "AC_TESTCASE_MAP_AND_TESTCASE", "")
        checkpoint = self.loop.run_until_pause(ProjectLoopRequest(
            submission, scenario_routing=routing))
        self.assertEqual("AWAITING_HUMAN_REVIEW", checkpoint["state"])
        return submission, job_root, checkpoint

    @staticmethod
    def decision(submission, checkpoint, approval, kind="APPROVE"):
        return {
            "schema_version": "1.0",
            "decision_id": "DECISION.PJ003.{}".format(kind),
            "approval_request_id": approval["approval_request_id"],
            "job_id": submission["job_id"],
            "thread_id": approval["thread_id"],
            "decision": kind,
            "approver_identity": "human.dv.owner",
            "approver_role": "DV_OWNER",
            "reason": "scripted PJ-003 qualification decision",
            "evidence_ids": copy.deepcopy(approval["validation_artifact_ids"]),
            "candidate_fingerprint": approval["candidate_fingerprint"],
            "checkpoint_id": checkpoint["checkpoint_id"],
            "decided_at": "2026-08-18T12:00:00Z",
        }

    def approve(self):
        submission, job_root, checkpoint = self.to_human_gate()
        approval = load_document(job_root / checkpoint["approval_request_path"])
        result = self.loop.run_until_pause(ProjectLoopRequest(
            submission,
            human_decision=self.decision(submission, checkpoint, approval)))
        self.assertEqual("AWAITING_EXECUTION_AUTHORIZATION", result["state"])
        return submission, job_root, result

    def authorization(self, submission, job_root):
        approved = load_document(
            job_root / "approved/generated/manifests/project_testcase.json")
        adapter = self._adapter(submission["job_id"])
        value = {
            "schema_version": "1.0",
            "artifact_kind": "PROJECT_EXECUTION_AUTHORIZATION",
            "authorization_id": "AUTHORIZATION.PROJECT.PJ003.TEST",
            "job_id": submission["job_id"],
            "purpose": "EXECUTE_APPROVED_TESTCASE",
            "authorizer_identity": "human.dv.owner",
            "authorizer_role": "DV_OWNER",
            "testcase_approval_fingerprint": approved["authority_fingerprint"],
            "profile_id": "EDAPROFILE.XCELIUM.PROJECT.V1",
            "executable_ref": "EDAEXEC.XCELIUM",
            "environment_identity": adapter.environment_identity,
            "environment_fingerprint": adapter.environment_fingerprint,
            "constraints": {
                "timeout_seconds": 10,
                "seed": 17,
                "uvm": False,
                "coverage": False,
                "waves": False,
            },
            "authorized_at": "2026-08-18T00:00:00Z",
            "expires_at": "2099-08-18T00:00:00Z",
            "authorization_fingerprint": "0" * 64,
        }
        value["authorization_fingerprint"] = artifact_fingerprint(
            value, "authorization_fingerprint")
        return value

    def execute(self):
        submission, job_root, _ = self.approve()
        authorization = self.authorization(submission, job_root)
        result = self.loop.run_until_pause(ProjectLoopRequest(
            submission, execution_authorization=authorization))
        return submission, job_root, authorization, result

    def test_approve_authorize_bind_execute_pass_and_exact_restart(self):
        submission, job_root, _, result = self.execute()
        self.assertEqual("EXECUTION_PASS", result["state"])
        self.assertEqual(2, self.invocation_count())
        evidence = load_document(job_root / "audit/pj003_execution_evidence.json")
        self.assertEqual("PASS", evidence["execution_status"])
        self.assertEqual("PASS", evidence["build"]["status"])
        self.assertEqual("PASS", evidence["run"]["status"])
        self.assertNotIn("private-license-host", json.dumps(evidence))
        provider_calls = (self.generator.calls, self.reviewer.calls)
        replay = self.loop.run_until_pause(ProjectLoopRequest(submission))
        self.assertEqual(result, replay)
        self.assertEqual(2, self.invocation_count())
        self.assertEqual(provider_calls,
                         (self.generator.calls, self.reviewer.calls))
        self.assertFalse((job_root / "audit/project_completed.json").exists())

    def test_reject_is_a_terminal_human_pause_without_approval(self):
        submission, job_root, checkpoint = self.to_human_gate()
        approval = load_document(job_root / checkpoint["approval_request_path"])
        result = self.loop.run_until_pause(ProjectLoopRequest(
            submission, human_decision=self.decision(
                submission, checkpoint, approval, "REJECT")))
        self.assertEqual("PAUSED_BY_HUMAN", result["state"])
        self.assertFalse((job_root /
            "approved/generated/manifests/project_testcase.json").exists())
        self.assertEqual(0, self.invocation_count())
        self.assertEqual(result, self.loop.run_until_pause(
            ProjectLoopRequest(submission)))

    def test_revision_request_is_a_terminal_human_pause(self):
        submission, job_root, checkpoint = self.to_human_gate()
        approval = load_document(job_root / checkpoint["approval_request_path"])
        result = self.loop.run_until_pause(ProjectLoopRequest(
            submission, human_decision=self.decision(
                submission, checkpoint, approval, "REQUEST_REVISION")))
        self.assertEqual("PAUSED_BY_HUMAN", result["state"])
        self.assertEqual("REQUEST_REVISION", result["decision"])
        self.assertEqual(0, self.invocation_count())

    def test_testcase_approval_cannot_substitute_for_execution_authority(self):
        submission, job_root, _ = self.approve()
        decision = load_document(job_root / "audit/pj003_testcase_decision.json")
        with self.assertRaises(ProjectJobError) as caught:
            self.loop.run_until_pause(ProjectLoopRequest(
                submission, execution_authorization=decision))
        self.assertEqual("INVALID_SCHEMA", caught.exception.code)
        self.assertEqual(0, self.invocation_count())

    def test_cross_job_authorization_fails_before_eda(self):
        submission, job_root, _ = self.approve()
        authorization = self.authorization(submission, job_root)
        authorization["job_id"] = "JOB.PROJECT.CROSS.JOB"
        authorization["authorization_fingerprint"] = artifact_fingerprint(
            authorization, "authorization_fingerprint")
        with self.assertRaises(ProjectJobError) as caught:
            self.loop.run_until_pause(ProjectLoopRequest(
                submission, execution_authorization=authorization))
        self.assertEqual("INVALID_APPROVAL_PROVENANCE", caught.exception.code)
        self.assertEqual(0, self.invocation_count())

    def test_tampered_approved_testcase_fails_before_eda(self):
        submission, job_root, _ = self.approve()
        approved = load_document(
            job_root / "approved/generated/manifests/project_testcase.json")
        (job_root / approved["approved_testcase_path"]).write_text(
            "tampered", encoding="utf-8")
        with self.assertRaises(ProjectJobError) as caught:
            self.loop.run_until_pause(ProjectLoopRequest(
                submission,
                execution_authorization=self.authorization(
                    submission, job_root)))
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        self.assertEqual(0, self.invocation_count())

    def test_partial_project_execution_pair_fails_closed(self):
        submission, job_root, _, result = self.execute()
        self.assertEqual("EXECUTION_PASS", result["state"])
        (job_root / "audit/pj003_execution_result.json").unlink()
        with self.assertRaises(ProjectJobError) as caught:
            self.loop.run_until_pause(ProjectLoopRequest(submission))
        self.assertEqual("PARTIAL_ARTIFACT", caught.exception.code)
        self.assertEqual(2, self.invocation_count())

    def test_unregistered_output_tamper_is_rejected_on_restart(self):
        submission, job_root, _, result = self.execute()
        self.assertEqual("EXECUTION_PASS", result["state"])
        evidence = load_document(job_root / "audit/pj003_execution_evidence.json")
        output = job_root / evidence["run"]["output_subdir"]
        (output / "unregistered.bin").write_bytes(b"tamper")
        with self.assertRaises(ProjectJobError) as caught:
            self.loop.run_until_pause(ProjectLoopRequest(submission))
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        self.assertEqual(2, self.invocation_count())

    def test_compile_failure_stops_before_run(self):
        self.generator = CompileFailureProvider()
        _, _, _, result = self.execute()
        self.assertEqual("EXECUTION_FAIL", result["state"])
        self.assertEqual(1, self.invocation_count())

    def test_license_failure_is_typed_blocked_and_stops_before_run(self):
        self.generator = LicenseBlockedProvider()
        _, _, _, result = self.execute()
        self.assertEqual("EXECUTION_BLOCKED", result["state"])
        self.assertEqual(1, self.invocation_count())

    def test_unavailable_trusted_adapter_publishes_blocked_evidence(self):
        submission, job_root, _ = self.approve()

        def unavailable(_job_root, _manifest, _authorization):
            raise ProjectJobError("BLOCKED_TOOL", "fake tool unavailable")

        self.loop.eda_adapter_factory = unavailable
        result = self.loop.run_until_pause(ProjectLoopRequest(
            submission,
            execution_authorization=self.authorization(submission, job_root)))
        self.assertEqual("EXECUTION_BLOCKED", result["state"])
        evidence = load_document(job_root / "audit/pj003_execution_evidence.json")
        self.assertIsNone(evidence["build"])
        self.assertIn("BLOCKED_TOOL", evidence["diagnostic_codes"])
        self.assertEqual(0, self.invocation_count())


if __name__ == "__main__":
    unittest.main()
