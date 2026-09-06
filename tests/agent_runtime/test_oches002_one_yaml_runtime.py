#!/usr/bin/env python3
"""OCHES002 one-YAML runtime wiring and checkpoint recovery tests."""
from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from adapters.llm import ProviderContractError
from contracts.validator import load_document
from domain.agent_binding import binding_lineage
from runtime.errors import ProjectJobError
from runtime.job_runtime import (
    PROVIDER_RETRY_RUNTIME_PROTOCOL, ProjectJobRuntimeIntegration,
    repair_checkpoint_id,
)
from runtime.repair_runtime import ProjectRepairRuntime
from domain.artifacts import artifact_fingerprint
from agents.project_tools import ProjectReadModel, ProjectToolError
from infrastructure.persistence.transcript_store import (
    TranscriptStore, transcript_session_dir,
)
from tests.agent_runtime.test_oches002_repair_runtime import (
    Oches002RepairRuntimeTests,
    ScriptedBoundProvider,
)


class Oches002OneYamlRuntimeTests(Oches002RepairRuntimeTests):
    """Reuse the exact waiting-Job fixture but load only local tests."""

    def setUp(self):
        super().setUp()
        self.planning_id, self.stage_id = \
            ProjectJobRuntimeIntegration._session_ids(
                self.value["job_id"],
                self.checkpoint["checkpoint_fingerprint"])
        self.testcase_id = next(
            unit["unit_id"] for unit in self.runtime.model.units.values()
            if unit["unit_kind"] == "LOGICAL_TESTCASE")
        self.providers = []

    def integration(self, factory=None):
        return ProjectJobRuntimeIntegration(
            workspace_root=self.fixture.root,
            result_root=self.fixture.root / "result",
            provider_factory=factory or self.provider_factory)

    def provider_factory(self, _job_root, _manifest, profile_role):
        if profile_role == "repair.orchestrator":
            provider = self.orchestrator_provider(
                {"kind": "TESTCASE", "id": self.testcase_id},
                "STAGE_2", self.planning_id,
                "REPAIRPLAN.ONEYAML.001")
        elif profile_role == "repair.stage2":
            dispatch = load_document(
                self.job /
                "audit/oches002_awaiting_scoped_replacement.json")
            provider = self.stage_provider(
                load_document(self.job / dispatch["dispatch_path"]),
                self.stage_id)
        else:
            raise AssertionError("unexpected role {}".format(profile_role))
        self.providers.append((profile_role, provider))
        return provider

    def assert_dispatch_rejected_before_provider(self, mutate, code):
        planned = self.runtime.run_orchestrator(
            self.orchestrator_provider(
                {"kind": "TESTCASE", "id": self.testcase_id},
                "STAGE_2", self.planning_id,
                "REPAIRPLAN.PREPROVIDER.001"),
            self.planning_id)
        waiting_path = (
            self.job / "audit/oches002_awaiting_scoped_replacement.json")
        waiting = load_document(waiting_path)
        dispatch_path = self.job / waiting["dispatch_path"]
        dispatch = load_document(dispatch_path)
        mutate(dispatch)
        dispatch["dispatch_fingerprint"] = artifact_fingerprint(
            dispatch, "dispatch_fingerprint")
        dispatch_path.write_text(
            json.dumps(dispatch, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        waiting["dispatch_fingerprint"] = dispatch["dispatch_fingerprint"]
        waiting["checkpoint_fingerprint"] = artifact_fingerprint(
            waiting, "checkpoint_fingerprint")
        waiting_path.write_text(
            json.dumps(waiting, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        calls = []
        result = self.integration(
            lambda *_: calls.append("provider") or None).advance(
                self.value["job_id"])
        self.assertEqual("PAUSED_RECOVERY_REQUIRED", result["state"])
        self.assertEqual(code, result["diagnostic"]["code"])
        self.assertEqual([], calls)

    def persist_legacy_rejected_evidence(
            self, diagnostic="STALE_EVIDENCE"):
        integration = self.integration()
        legacy_session = integration._legacy_planning_session_id(
            self.value["job_id"],
            self.checkpoint["checkpoint_fingerprint"])
        plan = self.fixture._plan(
            self.submission, self.report, self.request,
            {"kind": "TESTCASE", "id": self.testcase_id}, "STAGE_2")
        plan["planning_session_id"] = legacy_session
        plan["plan_fingerprint"] = "f" * 64
        receipt = {
            "schema_version": "1.0",
            "receipt_id": "ROUTERRECEIPT.LEGACY.REJECTED",
            "job_id": self.value["job_id"],
            "plan_id": plan["plan_id"],
            "plan_fingerprint": plan["plan_fingerprint"],
            "status": "REJECTED",
            "diagnostic": {
                "code": diagnostic,
                "message": "legacy formal plan was rejected",
            },
            "formal_dispatch_id": "NONE",
            "receipt_fingerprint": "0" * 64,
        }
        receipt["receipt_fingerprint"] = artifact_fingerprint(
            receipt, "receipt_fingerprint")
        token = "legacy_rejected"
        plan_relative = self.runtime._persist(
            "staging/orchestrator/repair_plan.{}.json".format(token), plan)
        receipt_relative = self.runtime._persist(
            "audit/router_receipt.{}.json".format(token), receipt)
        transcript = TranscriptStore(
            transcript_session_dir(self.job, "ORCHESTRATOR", legacy_session),
            job_id=self.value["job_id"], role="ORCHESTRATOR",
            session_id=legacy_session, lineage={"legacy_protocol": "OCHES002"})
        transcript.record("TOOL_CALL", {
            "call_id": "CALL.LEGACY.REJECTED",
            "name": "submit_repair_plan",
            "arguments": plan,
        })
        transcript.record("TOOL_RESULT", {
            "status": "REJECTED", "receipt": receipt, "dispatch": None,
        })
        transcript.finalize("COMPLETED", "COMPLETED", 2)
        return plan_relative, receipt_relative

    def test_one_yaml_error_path_reaches_validated_without_commit(self):
        before = copy.deepcopy(self.runtime.model.artifact_roots)
        result = self.integration(self.provider_factory).advance(
            self.value["job_id"])

        self.assertEqual("SCOPED_REPLACEMENT_VALIDATED", result["state"])
        self.assertEqual(before, self.runtime.model.artifact_roots)
        replacement = load_document(self.job / result["replacement_path"])
        self.assertEqual("STAGE_2", replacement["stage"])
        self.assertFalse((self.job / "approved").exists())
        self.assertTrue((self.job / "staging/generated/uvm").is_dir())
        self.assertEqual(
            ["repair.orchestrator", "repair.stage2"],
            [role for role, _ in self.providers])
        self.assertEqual([2, 2], [
            len(provider.requests) for _, provider in self.providers])

        calls = []
        replay = self.integration(
            lambda *_: calls.append("unexpected") or None).advance(
                self.value["job_id"])
        self.assertEqual(result, replay)
        self.assertEqual([], calls)

    def test_scope_expanded_dispatch_fails_before_stage_provider_creation(self):
        self.assert_dispatch_rejected_before_provider(
            lambda dispatch: dispatch["targets"].append({
                "kind": "TESTCASE", "id": "TC.SCOPE.EXPANDED"}),
            "SCOPE_EXPANSION")

    def test_cross_job_dispatch_fails_before_stage_provider_creation(self):
        self.assert_dispatch_rejected_before_provider(
            lambda dispatch: dispatch.__setitem__(
                "job_id", "JOB.PROJECT.OTHER.001"),
            "STALE_EVIDENCE")

    def test_stale_root_dispatch_fails_before_stage_provider_creation(self):
        self.assert_dispatch_rejected_before_provider(
            lambda dispatch: dispatch["unit_roots"].__setitem__(
                "stage2", "f" * 64),
            "STALE_DISPATCH")

    def test_restart_from_orchestrator_mid_session_skips_completed_tool(self):
        first = self.orchestrator_provider(
            {"kind": "TESTCASE", "id": self.testcase_id},
            "STAGE_2", self.planning_id, "REPAIRPLAN.RESUME.ORCH")

        def crash(_request):
            raise KeyboardInterrupt("simulated process loss")

        submission_turn = first.turns[1]
        first.turns[1] = crash
        with patch.object(TranscriptStore, "finalize", return_value={}):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.run_orchestrator(first, self.planning_id)
        self.assertEqual(2, len(first.requests))
        self.assertFalse((
            self.job /
            "audit/oches002_awaiting_scoped_replacement.json").exists())

        resumed_orchestrator = ScriptedBoundProvider(
            first.provider_id, first.model_id, [submission_turn])
        created = []

        def factory(_job_root, _manifest, role):
            created.append(role)
            if role == "repair.orchestrator":
                return resumed_orchestrator
            checkpoint = load_document(
                self.job /
                "audit/oches002_awaiting_scoped_replacement.json")
            dispatch = load_document(self.job / checkpoint["dispatch_path"])
            return self.stage_provider(dispatch, self.stage_id)

        result = self.integration(factory).advance(self.value["job_id"])
        self.assertEqual("SCOPED_REPLACEMENT_VALIDATED", result["state"])
        self.assertEqual(1, len(resumed_orchestrator.requests))
        self.assertEqual(
            ["repair.orchestrator", "repair.stage2"], created)

    def test_restart_from_stage_mid_session_skips_orchestrator_and_tool(self):
        planned = self.runtime.run_orchestrator(
            self.orchestrator_provider(
                {"kind": "TESTCASE", "id": self.testcase_id},
                "STAGE_2", self.planning_id, "REPAIRPLAN.RESUME.STAGE"),
            self.planning_id)
        dispatch = planned["dispatch"]
        first = self.stage_provider(dispatch, self.stage_id)

        def crash(_request):
            raise KeyboardInterrupt("simulated process loss")

        submission_turn = first.turns[1]
        first.turns[1] = crash
        with patch.object(TranscriptStore, "finalize", return_value={}):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.run_stage(
                    first, dispatch, self.stage_id)
        self.assertEqual(2, len(first.requests))

        stage_binding = binding_lineage(
            self.value, "repair", "stage2")
        resumed_stage = ScriptedBoundProvider(
            stage_binding["provider_id"], stage_binding["model_id"],
            [submission_turn])
        roles = []

        def factory(_job_root, _manifest, role):
            roles.append(role)
            if role != "repair.stage2":
                raise AssertionError("Orchestrator must not replay")
            return resumed_stage

        result = self.integration(factory).advance(self.value["job_id"])
        self.assertEqual("SCOPED_REPLACEMENT_VALIDATED", result["state"])
        self.assertEqual(["repair.stage2"], roles)
        self.assertEqual(1, len(resumed_stage.requests))

    def test_restart_after_validation_only_closes_fifo(self):
        result = self.runtime.run(
            orchestrator_provider=self.orchestrator_provider(
                {"kind": "TESTCASE", "id": self.testcase_id},
                "STAGE_2", self.planning_id,
                "REPAIRPLAN.RESUME.VALIDATED"),
            stage_provider_factory=lambda dispatch: self.stage_provider(
                dispatch, self.stage_id),
            planning_session_id=self.planning_id,
            stage_session_id=self.stage_id)
        validated = load_document(self.job / result["checkpoint_path"])
        replacement_bytes = (
            self.job / validated["replacement_path"]).read_bytes()
        checkpoint_bytes = (
            self.job / result["checkpoint_path"]).read_bytes()
        created = []

        resumed = self.integration(
            lambda *_: created.append("unexpected") or None).advance(
                self.value["job_id"])
        self.assertEqual(validated, resumed)
        self.assertEqual([], created)
        self.assertEqual(
            replacement_bytes,
            (self.job / validated["replacement_path"]).read_bytes())
        self.assertEqual(
            checkpoint_bytes,
            (self.job / result["checkpoint_path"]).read_bytes())

    def test_validated_replay_rechecks_exact_transcript_response(self):
        result = self.integration(self.provider_factory).advance(
            self.value["job_id"])
        replacement = load_document(self.job / result["replacement_path"])
        stage_dir = replacement["stage"].lower().replace("_", "")
        response_path = (
            self.job / "transcripts" / stage_dir /
            replacement["session_id"] / "0006.response.json")
        response_path.write_bytes(response_path.read_bytes() + b" ")
        calls = []
        with self.assertRaises(ProjectJobError) as caught:
            self.integration(
                lambda *_: calls.append("provider") or None).advance(
                    self.value["job_id"])
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        self.assertEqual([], calls)

    def test_validated_replay_rejects_individually_fingerprinted_cross_link(self):
        result = self.integration(self.provider_factory).advance(
            self.value["job_id"])
        checkpoint_path = (
            self.job / "audit/oches002_scoped_replacement_validated.json")
        checkpoint = load_document(checkpoint_path)
        unrelated = load_document(self.job / result["replacement_path"])
        unrelated["job_id"] = "JOB.PROJECT.OTHER.001"
        unrelated["replacement_fingerprint"] = artifact_fingerprint(
            unrelated, "replacement_fingerprint")
        unrelated_path = self.runtime._persist(
            "staging/scoped_replacements/{}.json".format(
                unrelated["replacement_fingerprint"][:24]), unrelated)
        checkpoint["replacement_path"] = unrelated_path
        checkpoint["replacement_fingerprint"] = \
            unrelated["replacement_fingerprint"]
        checkpoint["checkpoint_fingerprint"] = artifact_fingerprint(
            checkpoint, "checkpoint_fingerprint")
        checkpoint_path.write_text(
            json.dumps(checkpoint, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        calls = []
        with self.assertRaises(ProjectJobError) as caught:
            self.integration(
                lambda *_: calls.append("provider") or None).advance(
                    self.value["job_id"])
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        self.assertEqual([], calls)

    def test_provider_identity_mismatch_fails_before_response(self):
        expected = binding_lineage(
            self.value, "repair", "orchestrator")
        wrong = ScriptedBoundProvider(
            expected["provider_id"], "wrong-model", [])
        result = self.integration(
            lambda *_: wrong).advance(self.value["job_id"])
        self.assertEqual("PAUSED_RECOVERY_REQUIRED", result["state"])
        self.assertEqual(
            "INVALID_AGENT_BINDING", result["diagnostic"]["code"])
        self.assertEqual([], wrong.requests)

    def test_active_cancel_closes_fifo_without_starting_provider(self):
        created = []
        integration = self.integration(
            lambda *_: created.append("unexpected") or None)
        checkpoint_id = repair_checkpoint_id(self.checkpoint)
        integration.scheduler.enqueue(
            self.value["job_id"], checkpoint_id,
            self.checkpoint["checkpoint_fingerprint"])
        with integration.scheduler._lock():
            events = integration.scheduler._events()
            integration.scheduler._append(
                events, event="STARTED", job_id=self.value["job_id"],
                checkpoint_id=checkpoint_id,
                checkpoint_fingerprint=
                    self.checkpoint["checkpoint_fingerprint"],
                actor={"kind": "FRAMEWORK", "identity": "OCHES002_FIFO"})
        integration.scheduler.request_cancel(
            job_id=self.value["job_id"], checkpoint_id=checkpoint_id,
            checkpoint_fingerprint=self.checkpoint["checkpoint_fingerprint"],
            requester_kind="OPERATOR", requester_identity="OPS.TEST")

        result = integration.advance(self.value["job_id"])
        self.assertEqual("PAUSED_BY_HUMAN", result["state"])
        self.assertEqual([], created)
        self.assertEqual(
            "CANCELLED",
            integration.scheduler.state()[self.value["job_id"]]["state"])

    def test_legacy_rejected_plan_is_requeued_append_only_once(self):
        integration = self.integration()
        checkpoint_id = repair_checkpoint_id(self.checkpoint)
        self.persist_legacy_rejected_evidence()
        integration.scheduler.enqueue(
            self.value["job_id"], checkpoint_id,
            self.checkpoint["checkpoint_fingerprint"])

        def reject(*_args):
            raise ProjectJobError(
                "REPAIR_PLAN_REJECTED", "legacy formal plan was rejected")

        failed = integration.scheduler.run_next(reject)
        self.assertEqual("FAILED", failed["event"])
        before_events = len(list((
            self.fixture.root / "result/scheduler/events").glob("*.json")))

        result = integration.advance(self.value["job_id"])
        self.assertEqual("SCOPED_REPLACEMENT_VALIDATED", result["state"])
        state = integration.scheduler.state()[self.value["job_id"]]
        self.assertEqual("COMPLETED", state["state"])
        self.assertEqual(2, state["attempt"])
        events = sorted((
            self.fixture.root / "result/scheduler/events").glob("*.json"))
        appended = [load_document(path)["event"]
                    for path in events[before_events:]]
        self.assertEqual(["REQUEUED", "STARTED", "COMPLETED"], appended)

        provider_count = len(self.providers)
        replay = integration.advance(self.value["job_id"])
        self.assertEqual("SCOPED_REPLACEMENT_VALIDATED", replay["state"])
        self.assertEqual(provider_count, len(self.providers))

    def test_attempt_two_stale_history_recovers_to_attempt_three_only(self):
        integration = self.integration()
        checkpoint_id = repair_checkpoint_id(self.checkpoint)
        self.persist_legacy_rejected_evidence()
        integration.scheduler.enqueue(
            self.value["job_id"], checkpoint_id,
            self.checkpoint["checkpoint_fingerprint"])
        integration.scheduler.run_next(lambda *_: (_ for _ in ()).throw(
            ProjectJobError("REPAIR_PLAN_REJECTED", "legacy rejection")))
        integration.scheduler.retry_failed(
            job_id=self.value["job_id"], checkpoint_id=checkpoint_id,
            checkpoint_fingerprint=self.checkpoint["checkpoint_fingerprint"],
            diagnostic_code="REPAIR_PLAN_REJECTED")
        integration.scheduler.run_next(lambda *_: (_ for _ in ()).throw(
            ProjectJobError("STALE_EVIDENCE", "old read-model bug")))
        before = len(integration.scheduler.events_for_job(
            self.value["job_id"]))

        recovered = integration.recover_rejected_plan_failure(
            self.value["job_id"])

        self.assertEqual("QUEUED", recovered["state"])
        self.assertEqual(3, recovered["attempt"])
        self.assertEqual([], self.providers)
        appended = integration.scheduler.events_for_job(
            self.value["job_id"])[before:]
        self.assertEqual(["REQUEUED"], [item["event"] for item in appended])
        self.assertEqual("STALE_EVIDENCE", appended[0]["diagnostic_code"])

        result = integration.advance(self.value["job_id"])
        self.assertEqual("SCOPED_REPLACEMENT_VALIDATED", result["state"])
        self.assertEqual(3, integration.scheduler.state()[
            self.value["job_id"]]["attempt"])

    def test_request_only_provider_failure_retries_with_fresh_session(self):
        binding = binding_lineage(
            self.value, "repair", "orchestrator")

        def provider_failure(_request):
            raise ProviderContractError("simulated remote failure")

        failed_provider = ScriptedBoundProvider(
            binding["provider_id"], binding["model_id"],
            [provider_failure])
        retry_planning, retry_stage = \
            ProjectJobRuntimeIntegration._session_ids(
                self.value["job_id"],
                self.checkpoint["checkpoint_fingerprint"],
                PROVIDER_RETRY_RUNTIME_PROTOCOL)
        orchestrator_calls = 0

        def factory(_job_root, _manifest, role):
            nonlocal orchestrator_calls
            if role == "repair.orchestrator":
                orchestrator_calls += 1
                if orchestrator_calls == 1:
                    return failed_provider
                return self.orchestrator_provider(
                    {"kind": "TESTCASE", "id": self.testcase_id},
                    "STAGE_2", retry_planning,
                    "REPAIRPLAN.PROVIDER.RETRY")
            if role == "repair.stage2":
                checkpoint = load_document(
                    self.job /
                    "audit/oches002_awaiting_scoped_replacement.json")
                dispatch = load_document(
                    self.job / checkpoint["dispatch_path"])
                return self.stage_provider(dispatch, retry_stage)
            raise AssertionError("unexpected role {}".format(role))

        integration = self.integration(factory)
        first = integration.advance(self.value["job_id"])
        self.assertEqual("PAUSED_RETRYABLE", first["state"])
        result = integration.advance(self.value["job_id"])
        self.assertEqual("SCOPED_REPLACEMENT_VALIDATED", result["state"])
        state = integration.scheduler.state()[self.value["job_id"]]
        self.assertEqual(("COMPLETED", 2), (state["state"], state["attempt"]))
        events = integration.scheduler.events_for_job(self.value["job_id"])
        self.assertEqual(
            ["ENQUEUED", "STARTED", "FAILED", "REQUEUED",
             "STARTED", "COMPLETED"],
            [item["event"] for item in events])
        self.assertEqual("PROVIDER_UNAVAILABLE", events[3]["diagnostic_code"])
        self.assertTrue((
            self.job / "transcripts/orchestrator" /
            self.planning_id / "manifest.json").is_file())
        self.assertTrue((
            self.job / "transcripts/orchestrator" /
            retry_planning / "manifest.json").is_file())

    def test_provider_failure_recovery_has_no_job_lifetime_limit(self):
        binding = binding_lineage(
            self.value, "repair", "orchestrator")
        providers = []

        def factory(_job_root, _manifest, role):
            self.assertEqual("repair.orchestrator", role)

            def provider_failure(_request):
                raise ProviderContractError("simulated repeated failure")

            provider = ScriptedBoundProvider(
                binding["provider_id"], binding["model_id"],
                [provider_failure])
            providers.append(provider)
            return provider

        integration = self.integration(factory)
        first = integration.advance(self.value["job_id"])
        self.assertEqual("PAUSED_RETRYABLE", first["state"])
        self.assertEqual("PROVIDER_UNAVAILABLE", first["diagnostic"]["code"])
        state = integration.scheduler.state()[self.value["job_id"]]
        self.assertEqual(("FAILED", 1), (state["state"], state["attempt"]))
        before = len(integration.scheduler.events_for_job(
            self.value["job_id"]))
        provider_count = len(providers)

        replay = integration.advance(self.value["job_id"])
        self.assertEqual("PAUSED_RETRYABLE", replay["state"])
        self.assertEqual("PROVIDER_UNAVAILABLE", replay["diagnostic"]["code"])
        self.assertEqual(provider_count + 1, len(providers))
        self.assertEqual(before + 3, len(integration.scheduler.events_for_job(
            self.value["job_id"])))
        self.assertEqual(2, integration.scheduler.state()[
            self.value["job_id"]]["attempt"])

    def test_provider_failure_recovery_rejects_tampered_request(self):
        binding = binding_lineage(
            self.value, "repair", "orchestrator")

        def provider_failure(_request):
            raise ProviderContractError("simulated remote failure")

        provider = ScriptedBoundProvider(
            binding["provider_id"], binding["model_id"],
            [provider_failure])
        integration = self.integration(lambda *_: provider)
        checkpoint_id = repair_checkpoint_id(self.checkpoint)
        integration.scheduler.enqueue(
            self.value["job_id"], checkpoint_id,
            self.checkpoint["checkpoint_fingerprint"])
        integration.scheduler.run_next(integration._execute)
        request_path = (
            self.job / "transcripts/orchestrator" /
            self.planning_id / "0001.request.json")
        request_path.write_bytes(request_path.read_bytes() + b" ")
        before = len(integration.scheduler.events_for_job(
            self.value["job_id"]))

        with self.assertRaises(ProjectJobError) as caught:
            integration.recover_provider_failure(self.value["job_id"])
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        self.assertEqual(before, len(integration.scheduler.events_for_job(
            self.value["job_id"])))

    def test_rejected_plan_history_requires_exact_plan_receipt_transcript(self):
        plan_relative, _ = self.persist_legacy_rejected_evidence()
        model = ProjectReadModel.from_checkpoint(self.job, self.checkpoint)
        rejected = [item for item in model.history
                    if item["record_type"] == "REJECTED_REPAIR_PLAN"]
        self.assertEqual(1, len(rejected))
        self.assertEqual(
            "REJECTED_UNTRUSTED_PLAN", rejected[0]["authority_status"])

        path = self.job / plan_relative
        value = load_document(path)
        value["retrieval_rounds"] = 2
        path.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8")
        with self.assertRaises(ProjectToolError) as caught:
            ProjectReadModel.from_checkpoint(self.job, self.checkpoint)
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)

    def test_rejected_plan_history_rejects_unpaired_or_unexpected_evidence(self):
        _, receipt_relative = self.persist_legacy_rejected_evidence(
            diagnostic="NOT_A_ROUTER_DIAGNOSTIC")
        with self.assertRaises(ProjectToolError) as diagnostic:
            ProjectReadModel.from_checkpoint(self.job, self.checkpoint)
        self.assertEqual("STALE_EVIDENCE", diagnostic.exception.code)

        (self.job / receipt_relative).unlink()
        with self.assertRaises(ProjectToolError) as unpaired:
            ProjectReadModel.from_checkpoint(self.job, self.checkpoint)
        self.assertEqual("STALE_EVIDENCE", unpaired.exception.code)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(Oches002OneYamlRuntimeTests):
        if name in Oches002OneYamlRuntimeTests.__dict__:
            suite.addTest(Oches002OneYamlRuntimeTests(name))
    return suite


if __name__ == "__main__":
    unittest.main()
