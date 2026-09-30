"""Stage 2 submissions use the same complete validation as commit."""
from __future__ import annotations

import copy
import unittest

from application.repair import (
    ScopedReplacementDependencies, ScopedReplacementHandler, ScopedReplacementInput,
)
from contracts.validator import load_document
from runtime.errors import ProjectJobError
from tests.agent_runtime.test_oches002_repair_runtime import (
    Oches002RepairRuntimeTests, ScriptedBoundProvider, call,
)


class Stage2ReplacementValidationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Oches002RepairRuntimeTests(methodName="runTest")
        self.fixture.setUp()
        self.runtime = self.fixture.runtime
        self.job = self.fixture.job
        self.testcase_id = next(
            item["unit_id"] for item in self.runtime.model.units.values()
            if item["unit_kind"] == "LOGICAL_TESTCASE")
        self.dispatch = self.runtime.run_orchestrator(
            self.fixture.orchestrator_provider({
                "kind": "TESTCASE", "id": self.testcase_id}),
            "PLANNING.STAGE2.VALIDATION.001")["dispatch"]

    def tearDown(self):
        self.fixture.tearDown()

    def submit(self, changes, session="STAGESESSION.VALIDATION.001", *,
               direct=False):
        body = copy.deepcopy(
            self.runtime.model.units[self.testcase_id]["semantic_body"])
        body.update(changes)
        binding = self.dispatch["stage_agent"]
        candidate = {"replacements": [{
            "unit_id": self.testcase_id, "semantic_body": body,
        }]}
        provider = ScriptedBoundProvider(
            binding["provider_id"], binding["model_id"], [call(
                1, "submit_stage2_replacement", candidate)])
        before = copy.deepcopy(self.runtime.model.artifact_roots)
        try:
            if direct:
                request = {
                    "request_id": session + ".TURN.001",
                    "metadata": {
                        "session_id": session, "job_id": self.runtime.model.job_id,
                        "role": "STAGE_2",
                    },
                }
                response = provider.select_tools(request)
                handler = ScopedReplacementHandler(ScopedReplacementDependencies(
                    error=ProjectJobError,
                    validate_authority=self.runtime.validate_stage_authority,
                    persist=self.runtime._persist, records=self.runtime._records))
                return handler.handle(ScopedReplacementInput(
                    candidate=candidate,
                    agent_context={"request": request, "response": response},
                    dispatch=self.dispatch, read_model=self.runtime.model,
                    session_id=session, job_root=self.job,
                    authority_checkpoint_path=self.runtime.authority_checkpoint_path,
                    validated_checkpoint_path=self.runtime.validated_checkpoint_path,
                )).tool_result
            return self.runtime.run_stage(provider, self.dispatch, session)
        finally:
            self.assertEqual(before, self.runtime.model.artifact_roots)

    def assert_not_accepted(self):
        self.assertFalse(list(self.job.glob("staging/scoped_replacements/*.json")))
        self.assertFalse((self.job /
            "audit/oches002_scoped_replacement_validated.json").exists())

    def test_incomplete_checkable_oracle_is_rejected_before_persistence(self):
        for field in ("checker", "expected_result"):
            with self.subTest(field=field), self.assertRaises(ProjectJobError) as caught:
                self.submit({"status": "CHECKABLE", field: ""},
                            "STAGESESSION.VALIDATION." + field.upper(), direct=True)
            self.assertEqual("MISSING_STIMULUS_OR_ORACLE", caught.exception.code)
            self.assert_not_accepted()

    def test_complete_mapping_schema_is_checked_before_persistence(self):
        # The semantic submission schema has no upper timeout bound, while
        # the committed AC/testcase map does. Both entry points must agree.
        with self.assertRaises(ProjectJobError) as caught:
            self.submit({"timeout_cycles": 1000001}, direct=True)
        self.assertEqual("INVALID_REPLACEMENT_CONTENT", caught.exception.code)
        diagnostics = caught.exception.failure_context["correction_diagnostics"]
        self.assertEqual(1, len(diagnostics))
        self.assertEqual("INVALID_REPLACEMENT_CONTENT", diagnostics[0]["code"])
        self.assertEqual(self.testcase_id, diagnostics[0]["testcase_id"])
        self.assertTrue(diagnostics[0]["path"].endswith(".timeout_cycles"))
        self.assertIn("above maximum", diagnostics[0]["message"])
        self.assert_not_accepted()

    def assert_blocked_accepted(self, oracle):
        changes = {"status": "BLOCKED_CONTRACT", "reason": "Harness unavailable."}
        changes.update(oracle)
        result = self.submit(changes)
        self.assertEqual("VALIDATED", result["status"])
        body = result["replacement"]["replacements"][0]["semantic_body"]
        self.assertEqual("BLOCKED_CONTRACT", body["status"])
        for field, expected in oracle.items():
            self.assertEqual(expected, body[field])
        checkpoint = load_document(self.job / result["checkpoint_path"])
        self.assertEqual("SCOPED_REPLACEMENT_VALIDATED", checkpoint["state"])
        self.assertFalse(result["current_state_modified"])
        self.assertFalse((self.job / "staging/mappings/ac_testcase_map.r001.json").exists())

    def test_blocked_with_oracle_is_accepted_and_retains_its_status(self):
        self.assert_blocked_accepted({
            "checker": "Check the current run log when the harness is available.",
            "expected_result": "The configured named test completed successfully.",
        })

    def test_blocked_without_oracle_is_accepted_and_retains_its_status(self):
        self.assert_blocked_accepted({
            "checker": "", "expected_result": "", "stimulus": "",
            "transaction_sequence": "", "failure_condition": "",
        })


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(Stage2ReplacementValidationTests)


if __name__ == "__main__":
    unittest.main()
