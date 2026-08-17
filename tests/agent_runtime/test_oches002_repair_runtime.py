#!/usr/bin/env python3
"""OCHES002 real-tool Orchestrator and scoped Stage session tests."""
from __future__ import annotations

import copy
import unittest

from contracts.validator import accepted, load_document, validate
from core.project_agent_profile import binding_lineage
from core.project_job import ProjectJobError
from core.project_repair_runtime import ProjectRepairRuntime
from core.project_scoped_repair import artifact_fingerprint
from core.project_tools import ProjectToolError
from core.tool_session import ToolSessionError
from core.project_staged import (
    build_review_request, build_reviewer_repair_lineage,
    provider_review_request,
)
from scripts.dvlib import canonical_hash
from tests.agent_runtime.test_oches001_repair_control import (
    Oches001RepairControlTests,
)


class ScriptedBoundProvider:
    def __init__(self, provider_id, model_id, turns):
        self.provider_id = provider_id
        self.model_id = model_id
        self.turns = list(turns)
        self.requests = []
        self.probe_calls = 0
        self.restore_calls = 0

    def probe(self):
        self.probe_calls += 1
        return {
            "schema_version": "1.0",
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "status": "PASS",
            "tool_call_capable": True,
            "provider_version": "scripted",
            "diagnostics": [],
        }

    def restore_probe(self, _probe):
        self.restore_calls += 1

    def select_tools(self, request):
        self.requests.append(copy.deepcopy(request))
        if not self.turns:
            raise AssertionError("provider must not be called again")
        selected = self.turns.pop(0)
        calls = selected(request) if callable(selected) else selected
        return {
            "schema_version": "1.0", "request_id": request["request_id"],
            "operation": "SELECT_TOOLS", "finish_reason": "TOOL_CALLS",
            "content": "scripted raw response", "tool_calls": calls,
            "usage": {"input_tokens": 10, "output_tokens": 10},
            "model_id": self.model_id,
            "provider_metadata": {
                "provider_id": self.provider_id,
                "response_id": "RESPONSE.{}".format(request["request_id"]),
            },
            "diagnostics": [],
        }


def call(number, name, arguments):
    return [{
        "call_id": "CALL.{:03d}".format(number),
        "name": name, "arguments": copy.deepcopy(arguments),
    }]


class Oches002RepairRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Oches001RepairControlTests(methodName="runTest")
        self.fixture.setUp()
        (self.workflow, self.submission, self.job, self.checkpoint,
         self.report, self.request, _, _) = self.fixture._awaiting()
        self.value = self.workflow.bootstrap(self.submission, create=False)
        self.runtime = ProjectRepairRuntime(
            job_root=self.job, checkpoint=self.checkpoint,
            project_input=self.value, error=ProjectJobError)

    def tearDown(self):
        self.fixture.tearDown()

    def orchestrator_provider(
            self, target, stage="STAGE_3",
            planning_session="PLANNING.RUNTIME.001",
            plan_id="REPAIRPLAN.RUNTIME.001"):
        binding = binding_lineage(
            self.value, "repair", "orchestrator")

        def submit(_request):
            formal = self.fixture._plan(
                self.submission, self.report, self.request, target, stage)
            candidate = {
                "schema_version": "1.0",
                "status": formal["status"],
                "repairs": copy.deepcopy(formal["repairs"]),
            }
            return call(2, "submit_repair_plan", candidate)

        return ScriptedBoundProvider(
            binding["provider_id"], binding["model_id"], [
                call(1, "get_issue", {
                    "issue_ids": [self.report["findings"][0]["issue_id"]]}),
                submit,
            ])

    def stage_provider(
            self, dispatch, stage_session="STAGESESSION.RUNTIME.001"):
        binding = dispatch["stage_agent"]

        def submit(request):
            history = request["messages"][-1]["content"]
            self.assertTrue(history.startswith("TOOL_RESULT\n"))
            tool_result = __import__("json").loads(
                history.split("\n", 1)[1])["result"]
            replacements = []
            for unit in tool_result["items"]:
                body = copy.deepcopy(unit["semantic_body"])
                if dispatch["stage"] == "STAGE_1":
                    key = "objective" if unit["unit_kind"] == "SCENARIO" \
                        else "behavior"
                    body[key] += " Refined."
                elif dispatch["stage"] == "STAGE_2":
                    body["stimulus"] += " Refined."
                else:
                    body["segments"][0] += "\n// scoped runtime repair"
                replacements.append({
                    "unit_id": unit["unit_id"],
                    "semantic_body": body,
                })
            stage_number = dispatch["stage"][-1]
            return call(2, "submit_stage{}_replacement".format(
                stage_number), {"replacements": replacements})

        return ScriptedBoundProvider(
            binding["provider_id"], binding["model_id"], [
                call(1, "get_unit", {
                    "unit_ids": [item["unit_id"]
                                 for item in dispatch["target_units"]]}),
                submit,
            ])

    def test_sessions_use_real_tools_validate_and_stop_before_commit(self):
        testcase_id = next(
            unit["unit_id"] for unit in self.runtime.model.units.values()
            if unit["unit_kind"] == "LOGICAL_TESTCASE")
        orchestrator = self.orchestrator_provider({
            "kind": "TESTCASE", "id": testcase_id})
        before = copy.deepcopy(self.runtime.model.artifact_roots)
        stages = []

        def stage_factory(dispatch):
            stages.append(self.stage_provider(dispatch))
            return stages[-1]

        result = self.runtime.run(
            orchestrator_provider=orchestrator,
            stage_provider_factory=stage_factory,
            planning_session_id="PLANNING.RUNTIME.001",
            stage_session_id="STAGESESSION.RUNTIME.001")
        self.assertEqual("VALIDATED", result["status"])
        dispatch = result["dispatch"]
        self.assertTrue(accepted(validate("project_formal_dispatch", dispatch)))
        self.assertFalse(result["current_state_modified"])
        self.assertEqual(before, self.runtime.model.artifact_roots)
        self.assertTrue((self.job / result["replacement_path"]).is_file())
        checkpoint = load_document(self.job / result["checkpoint_path"])
        self.assertEqual("SCOPED_REPLACEMENT_VALIDATED", checkpoint["state"])
        self.assertEqual(
            result["checkpoint_fingerprint"],
            checkpoint["checkpoint_fingerprint"])
        self.assertEqual(2, len(orchestrator.requests))
        self.assertEqual(2, len(stages[0].requests))
        stage_schema = stages[0].requests[-1]["tools"][-1]["input_schema"]
        self.assertEqual({"replacements"}, set(stage_schema["properties"]))
        raw_path = (
            self.job / "transcripts/stage2/STAGESESSION.RUNTIME.001" /
            "0007.tool_call.json")
        if dispatch["stage"] == "STAGE_3":
            raw_path = (
                self.job / "transcripts/stage3/STAGESESSION.RUNTIME.001" /
                "0007.tool_call.json")
        raw_candidate = load_document(raw_path)["arguments"]
        self.assertEqual({"replacements"}, set(raw_candidate))
        self.assertTrue(all(
            set(item) == {"unit_id", "semantic_body"}
            for item in raw_candidate["replacements"]))
        replacement = result["replacement"]
        self.assertEqual(
            "STAGESESSION.RUNTIME.001.TURN.002",
            replacement["stage_agent"]["request_id"])
        self.assertEqual(
            "RESPONSE.STAGESESSION.RUNTIME.001.TURN.002",
            replacement["stage_agent"]["response_id"])
        response = load_document(raw_path.with_name("0006.response.json"))
        self.assertEqual(
            canonical_hash(response), replacement["response_fingerprint"])
        for item in replacement["replacements"]:
            self.assertEqual(
                self.runtime.model.units[item["unit_id"]]["spec_evidence"],
                item["spec_evidence"])

        sol = binding_lineage(self.value, "repair", "orchestrator")
        terra = result["dispatch"]["stage_agent"]
        replay_orchestrator = ScriptedBoundProvider(
            sol["provider_id"], sol["model_id"], [])
        replay_stages = []

        def replay_factory(_dispatch):
            replay_stages.append(ScriptedBoundProvider(
                terra["provider_id"], terra["model_id"], []))
            return replay_stages[-1]

        replay = self.runtime.run(
            orchestrator_provider=replay_orchestrator,
            stage_provider_factory=replay_factory,
            planning_session_id="PLANNING.RUNTIME.001",
            stage_session_id="STAGESESSION.RUNTIME.001")
        self.assertEqual(result, replay)
        self.assertEqual([], replay_orchestrator.requests)
        self.assertEqual([], replay_stages[0].requests)

    def test_framework_formalizes_semantic_plan_candidate(self):
        testcase_id = next(
            unit["unit_id"] for unit in self.runtime.model.units.values()
            if unit["unit_kind"] == "LOGICAL_TESTCASE")
        provider = self.orchestrator_provider({
            "kind": "TESTCASE", "id": testcase_id})
        result = self.runtime.run_orchestrator(
            provider, "PLANNING.RUNTIME.001")
        self.assertEqual("ACCEPTED", result["status"])

        system_prompt = provider.requests[0]["messages"][0]["content"]
        self.assertIn(
            "Select every issue_id and target id exactly and unchanged",
            system_prompt)
        self.assertIn(
            "Never invent, normalize, rewrite, or derive an ID",
            system_prompt)
        self.assertIn(
            "The Framework creates all new plan, group, dispatch, and "
            "session identities",
            system_prompt)
        self.assertNotIn("The Framework supplies all IDs", system_prompt)

        submitted = provider.requests[-1]["tools"][-1]["input_schema"]
        self.assertEqual(
            {"schema_version", "status", "repairs"},
            set(submitted["properties"]))
        raw_candidate = load_document(
            self.job /
            "transcripts/orchestrator/PLANNING.RUNTIME.001/"
            "0007.tool_call.json")["arguments"]
        self.assertEqual(
            {"schema_version", "status", "repairs"}, set(raw_candidate))

        plan_path = next(self.job.glob(
            "staging/orchestrator/repair_plan.*.json"))
        plan = load_document(plan_path)
        expected = binding_lineage(
            self.value, "repair", "orchestrator")
        self.assertEqual("PROFILED", plan["orchestrator"]["model_class"])
        for key, value in expected.items():
            self.assertEqual(value, plan["orchestrator"][key])
        self.assertEqual(
            artifact_fingerprint(plan, "plan_fingerprint"),
            plan["plan_fingerprint"])

    def test_candidate_cannot_declare_framework_authority_or_fingerprint(self):
        binding = binding_lineage(
            self.value, "repair", "orchestrator")
        provider = ScriptedBoundProvider(
            binding["provider_id"], binding["model_id"], [call(
                1, "submit_repair_plan", {
                    "schema_version": "1.0",
                    "status": "READY",
                    "repairs": [],
                    "plan_fingerprint": "0" * 64,
                })])
        with self.assertRaises(ToolSessionError) as caught:
            self.runtime.run_orchestrator(
                provider, "PLANNING.CANDIDATE.AUTHORITY.001")
        self.assertEqual("MALFORMED_MODEL_OUTPUT", caught.exception.code)
        self.assertFalse(list(self.job.glob(
            "staging/orchestrator/repair_plan.*.json")))

    def test_stage_candidate_cannot_declare_framework_owned_fields(self):
        testcase_id = next(
            unit["unit_id"] for unit in self.runtime.model.units.values()
            if unit["unit_kind"] == "LOGICAL_TESTCASE")
        dispatch = self.runtime.run_orchestrator(
            self.orchestrator_provider({
                "kind": "TESTCASE", "id": testcase_id}),
            "PLANNING.STAGE.AUTHORITY.001")["dispatch"]
        binding = dispatch["stage_agent"]
        unit = self.runtime.model.units[
            dispatch["target_units"][0]["unit_id"]]
        provider = ScriptedBoundProvider(
            binding["provider_id"], binding["model_id"], [call(
                1, "submit_stage3_replacement", {
                    "replacements": [{
                        "unit_id": unit["unit_id"],
                        "semantic_body": copy.deepcopy(unit["semantic_body"]),
                        "session_id": "STAGESESSION.FORGED.001",
                    }],
                })])
        with self.assertRaises(ToolSessionError) as caught:
            self.runtime.run_stage(
                provider, dispatch, "STAGESESSION.STAGE.AUTHORITY.001")
        self.assertEqual("MALFORMED_MODEL_OUTPUT", caught.exception.code)
        self.assertFalse(list(self.job.glob(
            "staging/scoped_replacements/*.json")))

    def test_reviewer_lineage_ignores_unreferenced_replacement(self):
        testcase_id = next(
            unit["unit_id"] for unit in self.runtime.model.units.values()
            if unit["unit_kind"] == "LOGICAL_TESTCASE")
        result = self.runtime.run(
            orchestrator_provider=self.orchestrator_provider({
                "kind": "TESTCASE", "id": testcase_id}),
            stage_provider_factory=lambda dispatch: self.stage_provider(
                dispatch, "STAGESESSION.LINEAGE.001"),
            planning_session_id="PLANNING.LINEAGE.001",
            stage_session_id="STAGESESSION.LINEAGE.001")
        before = build_reviewer_repair_lineage(
            self.job, self.value["job_id"], ProjectJobError)
        orphan = copy.deepcopy(result["replacement"])
        self.runtime._persist(
            "staging/scoped_replacements/orphan.json", orphan)
        after = build_reviewer_repair_lineage(
            self.job, self.value["job_id"], ProjectJobError)
        self.assertEqual(before, after)
        self.assertEqual(
            1, sum(item["record_type"] == "SCOPED_REPLACEMENT"
                   for item in after))

    def test_final_reviewer_constructor_carries_complete_history_and_no_session(self):
        initial = self.request
        lineage = [{
            "record_type": "INITIAL_REPORT",
            "path": self.checkpoint["review_report_path"],
            "record_fingerprint": canonical_hash(self.report),
            "record": copy.deepcopy(self.report),
        }]
        final = build_review_request(
            self.value, initial["spec_evidence"], initial["spec_fingerprint"],
            initial["scenario_ac_map"], initial["ac_testcase_map"]["index"],
            initial["ac_testcase_map"]["shards"],
            initial["testcase_candidate"], {
                "provider_id": initial["reviewer"]["provider_id"],
                "model_id": initial["reviewer"]["model_id"],
            }, 2, ProjectJobError, {
                "policy_fingerprint": initial["policy_fingerprint"],
                "owner_routing_decision":
                    initial["owner_routing_decision"],
                "scenario_spec_issues": initial["scenario_spec_issues"],
                "coverage_scope": initial["coverage_scope"],
                "routing_fingerprint": initial["routing_fingerprint"],
            }, self.report, lineage)
        self.assertEqual("7.0", final["schema_version"])
        self.assertEqual(self.report, final["previous_report"])
        self.assertEqual(lineage, final["repair_lineage"])
        self.assertEqual(initial["spec_evidence"], final["spec_evidence"])
        self.assertEqual(initial["scenario_ac_map"], final["scenario_ac_map"])
        self.assertEqual(
            initial["testcase_candidate"], final["testcase_candidate"])
        provider = provider_review_request(final)
        self.assertNotIn("previous_response_id", provider)
        self.assertNotIn("session_id", provider["metadata"])

    def test_stage_sibling_read_is_rejected_before_tool_execution(self):
        code_units = [
            unit["unit_id"] for unit in self.runtime.model.units.values()
            if unit["unit_kind"] in {"CODE_SHARED", "CODE_TESTCASE"}]
        dispatch = self.runtime.run_orchestrator(
            self.orchestrator_provider({
                "kind": "CODE_UNIT", "id": code_units[-1]}),
            "PLANNING.RUNTIME.001")["dispatch"]
        sibling = next(
            unit_id for unit_id in self.runtime.model.units
            if unit_id not in {
                item["unit_id"] for item in dispatch["target_units"]})
        binding = dispatch["stage_agent"]
        provider = ScriptedBoundProvider(
            binding["provider_id"], binding["model_id"], [
                call(1, "get_unit", {"unit_ids": [sibling]}),
            ])
        with self.assertRaises(ProjectToolError) as caught:
            self.runtime.run_stage(
                provider, dispatch, "STAGESESSION.SCOPE.001")
        self.assertEqual("SCOPE_EXPANSION", caught.exception.code)

    def _assert_stage_runtime(self, stage, target, planning, stage_session):
        result = self.runtime.run(
            orchestrator_provider=self.orchestrator_provider(
                target, stage, planning,
                "REPAIRPLAN.{}.001".format(stage)),
            stage_provider_factory=lambda dispatch: self.stage_provider(
                dispatch, stage_session),
            planning_session_id=planning,
            stage_session_id=stage_session)
        self.assertEqual("VALIDATED", result["status"])
        self.assertEqual(stage, result["dispatch"]["stage"])
        self.assertFalse(result["current_state_modified"])

    def test_stage1_runtime_changes_only_scenario_content(self):
        scenario_id = next(
            unit["unit_id"] for unit in self.runtime.model.units.values()
            if unit["unit_kind"] == "SCENARIO")
        self._assert_stage_runtime(
            "STAGE_1", {"kind": "SCENARIO", "id": scenario_id},
            "PLANNING.RUNTIME.STAGE1.001",
            "STAGESESSION.RUNTIME.STAGE1.001")

    def test_stage2_runtime_changes_only_testcase_content(self):
        testcase_id = next(
            unit["unit_id"] for unit in self.runtime.model.units.values()
            if unit["unit_kind"] == "LOGICAL_TESTCASE")
        self._assert_stage_runtime(
            "STAGE_2", {"kind": "TESTCASE", "id": testcase_id},
            "PLANNING.RUNTIME.STAGE2.001",
            "STAGESESSION.RUNTIME.STAGE2.001")


if __name__ == "__main__":
    unittest.main()


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(Oches002RepairRuntimeTests)
