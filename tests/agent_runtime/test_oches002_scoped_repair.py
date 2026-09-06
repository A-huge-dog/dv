#!/usr/bin/env python3
"""OCHES002 scope-rich dispatch and full replacement qualification."""
from __future__ import annotations

import copy
import hashlib
import unittest

from contracts.validator import accepted, validate
from domain.agent_binding import binding_lineage
from runtime.errors import ProjectJobError
from domain.artifacts import artifact_fingerprint
from domain.repair import (
    formalize_scoped_replacement, validate_repair_plan,
    validate_scoped_replacement,
)
from agents.project_tools import ProjectReadModel, STAGE_READ_TOOLS
from tests.agent_runtime.test_oches001_repair_control import (
    Oches001RepairControlTests,
)


class Oches002ScopedRepairTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Oches001RepairControlTests(methodName="runTest")
        self.fixture.setUp()
        (self.workflow, self.submission, self.job, self.checkpoint,
         self.report, self.request, _, _) = self.fixture._awaiting()
        self.model = ProjectReadModel.from_checkpoint(
            self.job, self.checkpoint)
        self.job_value = self.workflow.bootstrap_handler.handle(
            self.submission, create=False)
        self.turns = {}

    def tearDown(self):
        self.fixture.tearDown()

    @property
    def inventory(self):
        by_kind = {
            "SCENARIO": "SCENARIO",
            "ACCEPTANCE_CRITERION": "ACCEPTANCE_CRITERION",
            "TESTCASE": "LOGICAL_TESTCASE",
        }
        result = {
            kind: {unit["unit_id"] for unit in self.model.units.values()
                   if unit["unit_kind"] == unit_kind}
            for kind, unit_kind in by_kind.items()
        }
        result["CODE_UNIT"] = {
            unit["unit_id"] for unit in self.model.units.values()
            if unit["unit_kind"] in {"CODE_SHARED", "CODE_TESTCASE"}}
        return result

    def dispatch(self, stage, target):
        plan = self.fixture._plan(
            self.submission, self.report, self.request, target, stage)
        receipt, dispatch = validate_repair_plan(
            plan, self.job_value, self.report,
            self.model.artifact_roots,
            self.request["coverage_scope"]["scope_fingerprint"],
            self.inventory, self.model,
            {
                "runtime_role": "ORCHESTRATOR", "model_class": "PROFILED",
                **binding_lineage(
                    self.job_value, "repair", "orchestrator"),
            },
            {
                name: {
                    "runtime_role": "STAGE_AGENT",
                    "model_class": "PROFILED",
                    **binding_lineage(
                        self.job_value, "repair",
                        name.replace("STAGE_", "stage")),
                }
                for name in ("STAGE_1", "STAGE_2", "STAGE_3")
            },
            ProjectJobError)
        self.assertEqual("ACCEPTED", receipt["status"])
        self.assertIsNotNone(dispatch)
        return dispatch

    def replacement(self, dispatch):
        items = []
        for target in dispatch["target_units"]:
            unit = self.model.units[target["unit_id"]]
            body = copy.deepcopy(unit["semantic_body"])
            if dispatch["stage"] == "STAGE_1":
                key = "objective" if unit["unit_kind"] == "SCENARIO" \
                    else "behavior"
                body[key] += " Refined."
            elif dispatch["stage"] == "STAGE_2":
                body["stimulus"] += " Refined."
            else:
                body["segments"][0] += "\n// refined by scoped repair"
            items.append({
                "unit_id": unit["unit_id"],
                "semantic_body": body,
            })
        candidate = {"replacements": items}
        session_id = "STAGESESSION.TEST.001"
        request = {
            "request_id": session_id + ".TURN.001",
            "metadata": {
                "session_id": session_id, "job_id": dispatch["job_id"],
                "role": dispatch["stage"],
            },
        }
        response = {
            "request_id": request["request_id"],
            "model_id": dispatch["stage_agent"]["model_id"],
            "provider_metadata": {
                "provider_id": dispatch["stage_agent"]["provider_id"],
                "response_id": "RESPONSE." + request["request_id"],
            },
            "tool_calls": [{
                "name": "submit_stage{}_replacement".format(
                    dispatch["stage"][-1]),
                "arguments": copy.deepcopy(candidate),
            }],
        }
        value = formalize_scoped_replacement(
            candidate, dispatch, self.model, session_id,
            request, response, ProjectJobError)
        self.turns[dispatch["dispatch_fingerprint"]] = (request, response)
        return value

    def assert_rejected(self, replacement, dispatch, code):
        replacement["replacement_fingerprint"] = artifact_fingerprint(
            replacement, "replacement_fingerprint")
        with self.assertRaises(Exception) as caught:
            request, response = self.turns[dispatch["dispatch_fingerprint"]]
            validate_scoped_replacement(
                replacement, dispatch, self.model, ProjectJobError,
                session_id="STAGESESSION.TEST.001",
                request=request, response=response)
        self.assertEqual(code, caught.exception.code)

    def test_testcase_dispatch_routes_to_stage2_and_validates_without_commit(self):
        testcase_id = next(
            unit["unit_id"] for unit in self.model.units.values()
            if unit["unit_kind"] == "LOGICAL_TESTCASE")
        dispatch = self.dispatch(
            "STAGE_2", {"kind": "TESTCASE", "id": testcase_id})
        self.assertTrue(accepted(validate("project_formal_dispatch", dispatch)))
        self.assertEqual("2.0", dispatch["schema_version"])
        self.assertEqual(sorted(STAGE_READ_TOOLS), dispatch["tool_allow_list"])
        self.assertEqual(3, dispatch["retrieval_call_limit"])
        self.assertEqual(
            self.job_value["agent_profile"]["bindings"]["repair"][
                "stage2"]["model_id"],
            dispatch["stage_agent"]["model_id"])
        self.assertTrue(dispatch["target_units"])
        self.assertTrue(all(
            item["unit_kind"] == "LOGICAL_TESTCASE"
            for item in dispatch["target_units"]))
        self.assertTrue(dispatch["direct_dependencies"])

        before = {
            unit_id: unit["artifact_fingerprint"]
            for unit_id, unit in self.model.units.items()}
        replacement = self.replacement(dispatch)
        request, response = self.turns[dispatch["dispatch_fingerprint"]]
        validated = validate_scoped_replacement(
            replacement, dispatch, self.model, ProjectJobError,
            session_id="STAGESESSION.TEST.001",
            request=request, response=response)
        self.assertEqual(replacement, validated)
        self.assertEqual(before, {
            unit_id: unit["artifact_fingerprint"]
            for unit_id, unit in self.model.units.items()})

    def test_identity_count_evidence_and_noop_fail_closed(self):
        testcase_id = next(
            unit["unit_id"] for unit in self.model.units.values()
            if unit["unit_kind"] == "LOGICAL_TESTCASE")
        dispatch = self.dispatch(
            "STAGE_2", {"kind": "TESTCASE", "id": testcase_id})

        renamed = self.replacement(dispatch)
        renamed["replacements"][0]["unit_id"] = "TC.RENAMED"
        self.assert_rejected(renamed, dispatch, "IDENTITY_MUTATION")

        added = self.replacement(dispatch)
        extra = copy.deepcopy(added["replacements"][0])
        extra["unit_id"] = "TC.EXTRA"
        added["replacements"].append(extra)
        self.assert_rejected(added, dispatch, "IDENTITY_MUTATION")

        noop = self.replacement(dispatch)
        noop["replacements"][0]["semantic_body"] = copy.deepcopy(
            self.model.units[testcase_id]["semantic_body"])
        self.assert_rejected(noop, dispatch, "NO_SEMANTIC_CHANGE")

    def test_stage2_changes_content_but_not_relationships(self):
        testcase_id = next(
            unit["unit_id"] for unit in self.model.units.values()
            if unit["unit_kind"] == "LOGICAL_TESTCASE")
        stage2 = self.dispatch(
            "STAGE_2", {"kind": "TESTCASE", "id": testcase_id})
        valid2 = self.replacement(stage2)
        request2, response2 = self.turns[stage2["dispatch_fingerprint"]]
        validate_scoped_replacement(
            valid2, stage2, self.model, ProjectJobError,
            session_id="STAGESESSION.TEST.001",
            request=request2, response=response2)
        changed_relation = copy.deepcopy(valid2)
        changed_relation["replacements"][0]["semantic_body"][
            "scenario_ids"] = ["SCENARIO.RENAMED"]
        self.assert_rejected(changed_relation, stage2, "IDENTITY_MUTATION")

    def test_spec_evidence_list_rejects_duplicate_reorder_remove_and_rewrite(self):
        unit = next(
            item for item in self.model.units.values()
            if item["unit_kind"] == "LOGICAL_TESTCASE" and
            item["spec_evidence"])
        original = unit["spec_evidence"][0]
        document = self.model.spec_documents[original["path"]]["content"]
        lines = document.splitlines()
        line_number = 1 if original["line_start"] != 1 else 2
        snippet = lines[line_number - 1]
        extra = {
            "path": original["path"],
            "line_start": line_number, "line_end": line_number,
            "snippet": snippet,
            "snippet_fingerprint": hashlib.sha256(
                snippet.encode("utf-8")).hexdigest(),
        }
        self.model.evidence[self.model._evidence_key(extra)] = \
            self.model._validate_spec_evidence(extra)
        unit["spec_evidence"].append(copy.deepcopy(extra))
        dispatch = self.dispatch(
            "STAGE_2", {"kind": "TESTCASE", "id": unit["unit_id"]})

        duplicate = self.replacement(dispatch)
        duplicate["replacements"][0]["spec_evidence"].append(
            copy.deepcopy(duplicate["replacements"][0]["spec_evidence"][0]))
        self.assert_rejected(duplicate, dispatch, "SCOPE_EXPANSION")

        reordered = self.replacement(dispatch)
        reordered["replacements"][0]["spec_evidence"].reverse()
        self.assert_rejected(reordered, dispatch, "SCOPE_EXPANSION")

        removed = self.replacement(dispatch)
        removed["replacements"][0]["spec_evidence"].pop()
        self.assert_rejected(removed, dispatch, "SCOPE_EXPANSION")

        rewritten = self.replacement(dispatch)
        rewritten["replacements"][0]["spec_evidence"][0]["snippet"] += " changed"
        self.assert_rejected(rewritten, dispatch, "SCOPE_EXPANSION")

    def test_submission_turn_fields_and_response_fingerprint_are_reverified(self):
        testcase_id = next(
            unit["unit_id"] for unit in self.model.units.values()
            if unit["unit_kind"] == "LOGICAL_TESTCASE")
        dispatch = self.dispatch(
            "STAGE_2", {"kind": "TESTCASE", "id": testcase_id})
        wrong_response = self.replacement(dispatch)
        wrong_response["response_fingerprint"] = "f" * 64
        self.assert_rejected(
            wrong_response, dispatch, "INVALID_AGENT_BINDING")

        wrong_request = self.replacement(dispatch)
        wrong_request["stage_agent"]["request_id"] = "REQUEST.FORGED.001"
        self.assert_rejected(
            wrong_request, dispatch, "INVALID_AGENT_BINDING")


if __name__ == "__main__":
    unittest.main()


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(Oches002ScopedRepairTests)
