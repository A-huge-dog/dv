"""Stage 2 candidate rejection, bounded correction, and immutable authority."""
from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from agents.errors import AgentLoopError
from contracts.validator import load_document
from runtime.agent_loop import AgentLoopPolicy
from runtime.commit_runtime import ProjectCommitRuntime
from runtime.errors import ProjectJobError
from infrastructure.persistence.transcript_store import load_terminal_transcript_events
from tests.agent_runtime import test_oches002_repair_runtime as fixtures


class Stage2RepairCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Oches002RepairRuntimeTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.runtime = self.fixture.runtime
        testcase_id = next(
            unit["unit_id"] for unit in self.runtime.model.units.values()
            if unit["unit_kind"] == "LOGICAL_TESTCASE")
        self.dispatch = self.runtime.run_orchestrator(
            self.fixture.orchestrator_provider({
                "kind": "TESTCASE", "id": testcase_id}),
            "PLANNING.CORRECTION.001")["dispatch"]
        self.commit_runtime = ProjectCommitRuntime(
            workspace_root=self.fixture.fixture.root,
            result_root=self.fixture.fixture.root / "result",
            provider_factory=lambda *_args: None)
        self.session_id = self.commit_runtime._stage_session_id(
            self.dispatch, self.runtime.model.artifact_roots, 1)

    def candidate(self):
        replacements = []
        for target in self.dispatch["target_units"]:
            body = copy.deepcopy(self.runtime.model.units[target["unit_id"]][
                "semantic_body"])
            body["stimulus"] += " Corrected stimulus."
            replacements.append({
                "unit_id": target["unit_id"], "semantic_body": body})
        return {"replacements": replacements}

    def provider(self, turns):
        binding = self.dispatch["stage_agent"]
        return fixtures.ScriptedBoundProvider(
            binding["provider_id"], binding["model_id"], turns)

    def assert_no_replacement(self):
        self.assertFalse((self.fixture.job /
                          self.runtime.validated_checkpoint_path).exists())
        self.assertEqual([], list((self.fixture.job /
                                   "staging/scoped_replacements").glob("*.json")))
        self.assertFalse(any(record["record_type"] == "SCOPED_REPLACEMENT"
                             for _, record in self.runtime._records().records()))

    @staticmethod
    def feedback(request):
        history = request["messages"][-1]["content"]
        return json.loads(history.split("\n", 1)[1])["result"]

    def test_rejected_candidate_is_corrected_before_any_replacement_is_saved(self):
        invalid = self.candidate()
        invalid["replacements"][0]["semantic_body"]["checker"] = ""
        valid = self.candidate()
        roots = copy.deepcopy(self.runtime.model.artifact_roots)

        def correct(request):
            self.assert_no_replacement()
            feedback = self.feedback(request)
            self.assertEqual("REJECTED", feedback["status"])
            self.assertEqual("MISSING_STIMULUS_OR_ORACLE",
                             feedback["diagnostic"]["code"])
            self.assertTrue(feedback["diagnostics"][0]["message"])
            self.assertIn("same dispatch", feedback["required_action"])
            return fixtures.call(2, "submit_stage2_replacement", valid)

        provider = self.provider([
            fixtures.call(1, "submit_stage2_replacement", invalid), correct])
        result = self.runtime.run_stage(provider, self.dispatch, self.session_id)

        self.assertEqual("VALIDATED", result["status"])
        self.assertEqual(2, len(provider.requests))
        self.assertEqual(roots, self.runtime.model.artifact_roots)
        self.assertEqual(self.session_id + ".TURN.002",
                         result["replacement"]["stage_agent"]["request_id"])
        self.assertEqual(valid["replacements"][0]["semantic_body"],
                         result["replacement"]["replacements"][0]["semantic_body"])
        replay_provider = self.provider([])
        self.assertEqual(result, self.runtime.run_stage(
            replay_provider, self.dispatch, self.session_id))
        self.assertEqual([], replay_provider.requests)

    def test_mapping_diagnostics_reach_the_next_submission(self):
        invalid = self.candidate()
        body = invalid["replacements"][0]["semantic_body"]
        body["status"] = "SPEC_AMBIGUITY"
        body["reason"] = "Spec clarification is needed."

        def correct(request):
            feedback = self.feedback(request)
            self.assertEqual("UNAUTHORIZED_ORACLE", feedback["diagnostic"]["code"])
            self.assertTrue(feedback["diagnostics"])
            for diagnostic in feedback["diagnostics"]:
                self.assertEqual("UNAUTHORIZED_ORACLE", diagnostic["code"])
                self.assertIn("logical_testcases", diagnostic["path"])
                self.assertIn("checker", diagnostic["message"])
            self.assert_no_replacement()
            return fixtures.call(2, "submit_stage2_replacement", self.candidate())

        result = self.runtime.run_stage(self.provider([
            fixtures.call(1, "submit_stage2_replacement", invalid), correct]),
            self.dispatch, self.session_id)
        self.assertEqual("VALIDATED", result["status"])

    def test_identity_mutation_is_terminal_instead_of_correctable(self):
        invalid = self.candidate()
        invalid["replacements"][0]["unit_id"] = "TC.UNAUTHORIZED"
        provider = self.provider([
            fixtures.call(1, "submit_stage2_replacement", invalid),
            fixtures.call(2, "submit_stage2_replacement", self.candidate())])
        with self.assertRaises(ProjectJobError) as caught:
            self.runtime.run_stage(provider, self.dispatch, self.session_id)
        self.assertEqual("IDENTITY_MUTATION", caught.exception.code)
        self.assertEqual(1, len(provider.requests))
        self.assert_no_replacement()
        with self.assertRaises(ProjectJobError) as retry_error:
            self.commit_runtime._next_stage_attempt(
                self.fixture.job, self.runtime, self.dispatch)
        self.assertEqual("INVALID_RETRY_STATE", retry_error.exception.code)

    def test_schema_limit_diagnostics_are_correctable(self):
        invalid = self.candidate()
        invalid["replacements"][0]["semantic_body"]["timeout_cycles"] = 1000001

        def correct(request):
            feedback = self.feedback(request)
            self.assertEqual("INVALID_REPLACEMENT_CONTENT",
                             feedback["diagnostic"]["code"])
            self.assertTrue(any("timeout_cycles" in item["path"]
                                for item in feedback["diagnostics"]))
            self.assert_no_replacement()
            return fixtures.call(2, "submit_stage2_replacement", self.candidate())

        result = self.runtime.run_stage(self.provider([
            fixtures.call(1, "submit_stage2_replacement", invalid), correct]),
            self.dispatch, self.session_id)
        self.assertEqual("VALIDATED", result["status"])

    def test_repeated_rejection_obeys_existing_turn_budget(self):
        invalid = self.candidate()
        invalid["replacements"][0]["semantic_body"]["checker"] = ""
        provider = self.provider([
            fixtures.call(index, "submit_stage2_replacement", invalid)
            for index in (1, 2)])

        def bounded_policy(**kwargs):
            return AgentLoopPolicy(**kwargs, max_turns=2)

        with patch("runtime.repair_runtime.AgentLoopPolicy", bounded_policy):
            with self.assertRaises(AgentLoopError) as caught:
                self.runtime.run_stage(provider, self.dispatch, self.session_id)
        self.assertEqual("PAUSED_BUDGET", caught.exception.code)
        self.assertEqual(2, len(provider.requests))
        self.assert_no_replacement()
        manifest = load_document(self.fixture.job / "transcripts/stage2" /
                                 self.session_id / "manifest.json")
        self.assertEqual({"status": "FAILED", "code": "PAUSED_BUDGET",
                          "result_sequence": None}, manifest["terminal"])
        next_session, feedback = self.commit_runtime._next_stage_attempt(
            self.fixture.job, self.runtime, self.dispatch)
        self.assertNotEqual(self.session_id, next_session)
        self.assertEqual(invalid, feedback["candidate"])
        self.assertEqual(self.session_id, feedback["source_session_id"])
        self.assertEqual("REJECTED", feedback["validation"]["status"])

        def corrected(request):
            incoming = json.loads(request["messages"][-1]["content"])
            self.assertEqual(feedback,
                             incoming["stage2_candidate_correction_feedback"])
            self.assertIn("not authority", incoming["required_action"])
            self.assert_no_replacement()
            return fixtures.call(1, "submit_stage2_replacement", self.candidate())

        resumed = self.runtime.run_stage(
            self.provider([corrected]), self.dispatch, next_session,
            correction_feedback=feedback)
        self.assertEqual("VALIDATED", resumed["status"])
        self.assertEqual(next_session + ".TURN.001",
                         resumed["replacement"]["stage_agent"]["request_id"])

    def test_budget_recovery_rejects_incomplete_or_accepted_history(self):
        invalid = self.candidate()
        invalid["replacements"][0]["semantic_body"]["checker"] = ""
        with patch("runtime.repair_runtime.AgentLoopPolicy", side_effect=(
                lambda **kwargs: AgentLoopPolicy(**kwargs, max_turns=1))):
            with self.assertRaises(AgentLoopError):
                self.runtime.run_stage(self.provider([
                    fixtures.call(1, "submit_stage2_replacement", invalid)]),
                    self.dispatch, self.session_id)
        transcript = load_terminal_transcript_events(
            job_root=self.fixture.job, job_id=self.runtime.model.job_id,
            role="STAGE_2", session_id=self.session_id,
            lineage=self.commit_runtime._stage_lineage(self.runtime, self.dispatch))
        incomplete = copy.deepcopy(transcript)
        incomplete["events"].pop()
        accepted = copy.deepcopy(transcript)
        accepted["events"][-1]["value"]["status"] = "VALIDATED"
        for history in (incomplete, accepted):
            with self.subTest(history=history["events"][-1]["kind"]):
                with patch("runtime.commit_runtime.load_terminal_transcript_events",
                           return_value=history):
                    with self.assertRaises(ProjectJobError) as caught:
                        self.commit_runtime._next_stage_attempt(
                            self.fixture.job, self.runtime, self.dispatch)
                self.assertEqual("INVALID_RETRY_STATE", caught.exception.code)
        checkpoint = self.fixture.job / self.runtime.validated_checkpoint_path
        checkpoint.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ProjectJobError) as caught:
            self.commit_runtime._next_stage_attempt(
                self.fixture.job, self.runtime, self.dispatch)
        self.assertEqual("INVALID_RETRY_STATE", caught.exception.code)

    def test_token_budget_after_response_resumes_from_last_rejected_candidate(self):
        invalid = self.candidate()
        invalid["replacements"][0]["semantic_body"]["checker"] = ""
        unexecuted = self.candidate()
        provider = self.provider([
            fixtures.call(1, "submit_stage2_replacement", invalid),
            fixtures.call(2, "submit_stage2_replacement", unexecuted)])
        with patch("runtime.repair_runtime.AgentLoopPolicy", side_effect=(
                lambda **kwargs: AgentLoopPolicy(**kwargs, max_tokens=30))):
            with self.assertRaises(AgentLoopError) as caught:
                self.runtime.run_stage(provider, self.dispatch, self.session_id)
        self.assertEqual("PAUSED_BUDGET", caught.exception.code)
        self.assertEqual(2, len(provider.requests))
        self.assert_no_replacement()
        transcript = load_terminal_transcript_events(
            job_root=self.fixture.job, job_id=self.runtime.model.job_id,
            role="STAGE_2", session_id=self.session_id,
            lineage=self.commit_runtime._stage_lineage(self.runtime, self.dispatch))
        self.assertEqual([
            "REQUEST", "RESPONSE", "TOOL_CALL", "TOOL_RESULT",
            "REQUEST", "RESPONSE"], [item["kind"] for item in transcript["events"]])

        wrong_identity = copy.deepcopy(transcript)
        wrong_identity["events"][-1]["value"]["model_id"] = "another-model"
        unsafe_tool = copy.deepcopy(transcript)
        unsafe_tool["events"][-1]["value"]["tool_calls"][0]["name"] = "write_file"
        multiple_calls = copy.deepcopy(transcript)
        multiple_calls["events"][-1]["value"]["tool_calls"].append(
            copy.deepcopy(multiple_calls["events"][-1]["value"]["tool_calls"][0]))
        invalid_arguments = copy.deepcopy(transcript)
        invalid_arguments["events"][-1]["value"]["tool_calls"][0]["arguments"] = []
        for history in (wrong_identity, unsafe_tool, multiple_calls, invalid_arguments):
            with self.subTest(response=history["events"][-1]["value"]):
                with patch("runtime.commit_runtime.load_terminal_transcript_events",
                           return_value=history):
                    with self.assertRaises(ProjectJobError):
                        self.commit_runtime._next_stage_attempt(
                            self.fixture.job, self.runtime, self.dispatch)

        next_session, feedback = self.commit_runtime._next_stage_attempt(
            self.fixture.job, self.runtime, self.dispatch)
        self.assertNotEqual(self.session_id, next_session)
        self.assertEqual(invalid, feedback["candidate"])
        self.assertNotEqual(unexecuted, feedback["candidate"])
        self.assertEqual("REJECTED", feedback["validation"]["status"])
        self.assert_no_replacement()
        resumed = self.runtime.run_stage(self.provider([
            fixtures.call(1, "submit_stage2_replacement", self.candidate())]),
            self.dispatch, next_session, correction_feedback=feedback)
        self.assertEqual("VALIDATED", resumed["status"])
        self.assertEqual(next_session + ".TURN.001",
                         resumed["replacement"]["stage_agent"]["request_id"])


if __name__ == "__main__":
    unittest.main()
