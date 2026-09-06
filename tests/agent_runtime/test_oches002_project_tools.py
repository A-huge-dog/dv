#!/usr/bin/env python3
"""OCHES002 current-Job read model and nine-tool qualification."""
from __future__ import annotations

import copy
import unittest

from contracts.validator import accepted, load_document, validate
from agents.project_tools import (
    ProjectReadModel, ProjectToolError, READ_TOOL_NAMES, STAGE_READ_TOOLS,
    read_tool_definitions,
)
from tests.agent_runtime.test_oches001_repair_control import (
    Oches001RepairControlTests,
)


class Oches002ProjectToolTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Oches001RepairControlTests(methodName="runTest")
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def awaiting(self):
        (workflow, submission, job, checkpoint, report, request,
         generator, reviewer) = self.fixture._awaiting()
        model = ProjectReadModel.from_checkpoint(
            job, checkpoint, budget_status={
                "calls_used": 4, "calls_remaining": 8,
                "elapsed_seconds": 12.5,
                "sessions_used": 1, "sessions_remaining": 0,
                "tokens": 999999,
            })
        return {
            "workflow": workflow, "submission": submission, "job": job,
            "checkpoint": checkpoint, "report": report, "request": request,
            "generator": generator, "reviewer": reviewer, "model": model,
        }

    def assert_result(self, value, tool_name):
        self.assertEqual(tool_name, value["tool_name"])
        self.assertTrue(accepted(validate("project_read_tool_result", value)))

    def test_expected_roots_bind_the_effective_uvm(self):
        roots = ProjectReadModel._expected_artifact_roots(
            {"artifact_fingerprint": "1" * 64},
            {"artifact_fingerprint": "2" * 64},
            {
                "effective_uvm_root": "3" * 64,
                "candidate_fingerprint": "4" * 64,
            })
        self.assertEqual({
            "scenario_ac_map": "1" * 64,
            "ac_testcase_map": "2" * 64,
            "effective_uvm": "3" * 64,
            "testcase": "4" * 64,
        }, roots)

    def test_all_nine_tools_use_one_exact_current_snapshot(self):
        bundle = self.awaiting()
        model = bundle["model"]
        report = bundle["report"]
        issue_id = report["findings"][0]["issue_id"]

        issue = model.call("get_issue", {
            "issue_ids": ["ISSUE.UNKNOWN", issue_id],
        })
        self.assert_result(issue, "get_issue")
        self.assertEqual([issue_id], [item["issue_id"] for item in issue["items"]])
        self.assertEqual("NOT_FOUND", issue["diagnostics"][0]["code"])
        self.assertEqual(
            report["report_fingerprint"],
            issue["items"][0]["report_fingerprint"])

        unit_ids = sorted(model.units)
        requested = [unit_ids[-1], "UNIT.UNKNOWN", unit_ids[0]]
        units = model.call("get_unit", {"unit_ids": requested})
        self.assert_result(units, "get_unit")
        self.assertEqual(
            sorted([unit_ids[0], unit_ids[-1]]),
            [item["unit_id"] for item in units["items"]])
        self.assertEqual("UNIT.UNKNOWN", units["diagnostics"][0]["item_key"])

        dependent_unit = next(
            item for item in model.units.values()
            if any(
                (dependency["kind"], dependency["identity"])
                in model._units_by_lineage
                for dependency in item["dependency_fingerprints"]))
        dependencies = model.call("get_direct_dependencies", {
            "unit_ids": [dependent_unit["unit_id"]],
        })
        self.assert_result(dependencies, "get_direct_dependencies")
        self.assertTrue(dependencies["items"][0]["dependencies"])
        self.assertEqual(
            sorted(dependencies["items"][0]["dependencies"], key=lambda item: (
                item["kind"], item["identity"])),
            dependencies["items"][0]["dependencies"])

        upstream_id = next(
            dependency["identity"]
            for dependency in dependent_unit["dependency_fingerprints"]
            if (dependency["kind"], dependency["identity"])
            in model._units_by_lineage)
        dependents = model.call("get_dependents", {"unit_ids": [upstream_id]})
        self.assert_result(dependents, "get_dependents")
        self.assertIn(
            dependent_unit["unit_id"],
            [item["unit_id"]
             for item in dependents["items"][0]["dependents"]])

        evidence = next(
            item for unit in model.units.values()
            for item in unit["spec_evidence"])
        evidence_ref = {key: evidence[key] for key in (
            "path", "line_start", "line_end", "snippet_fingerprint")}
        spec = model.call("get_spec_evidence", {
            "evidence_refs": [evidence_ref, {
                **evidence_ref, "snippet_fingerprint": "f" * 64,
            }],
        })
        self.assert_result(spec, "get_spec_evidence")
        self.assertEqual(evidence["snippet"], spec["items"][0]["snippet"])
        self.assertEqual("NOT_FOUND", spec["diagnostics"][0]["code"])
        source = next(
            item for item in bundle["request"]["spec_evidence"]
            if item["path"] == evidence["path"])
        self.assertEqual(
            source["fingerprint"], spec["items"][0]["source_fingerprint"])

        history = model.call("get_repair_history", {
            "identities": [model.job_id, "REPAIRPLAN.UNKNOWN"],
        })
        self.assert_result(history, "get_repair_history")
        self.assertTrue(history["items"])
        self.assertEqual(
            list(range(1, len(history["items"]) + 1)),
            [item["sequence"] for item in history["items"]])
        self.assertEqual("NOT_FOUND", history["diagnostics"][0]["code"])

        comparison = model.call("compare_unit_revisions", {
            "comparisons": [{
                "unit_id": unit_ids[0], "from_revision": 0,
                "to_revision": 999,
            }],
        })
        self.assert_result(comparison, "compare_unit_revisions")
        self.assertFalse(comparison["items"])
        self.assertEqual("NOT_FOUND", comparison["diagnostics"][0]["code"])

        impact = model.call("estimate_repair_impact", {
            "target_ids": [upstream_id, "UNIT.UNKNOWN"],
        })
        self.assert_result(impact, "estimate_repair_impact")
        dirty_ids = {item["unit_id"] for item in impact["items"][0]["dirty_units"]}
        self.assertIn(upstream_id, dirty_ids)
        self.assertIn(dependent_unit["unit_id"], dirty_ids)
        self.assertEqual("NOT_FOUND", impact["diagnostics"][0]["code"])

        budget = model.call("get_budget_status", {})
        self.assert_result(budget, "get_budget_status")
        self.assertEqual(4, budget["items"][0]["calls_used"])
        self.assertNotIn("tokens", budget["items"][0])
        self.assertEqual(
            "NOT_MODELED_IN_OCHES002",
            budget["items"][0]["token_capacity_mode"])

        roots = {
            value["artifact_root"] for value in (
                issue, units, dependencies, dependents, spec, history,
                comparison, impact, budget)}
        self.assertEqual({model.artifact_root}, roots)

    def test_accepted_dispatch_is_history_but_does_not_create_revision(self):
        bundle = self.awaiting()
        current = ProjectReadModel.from_checkpoint(
            bundle["job"], bundle["checkpoint"])
        target = {
            "kind": "TESTCASE",
            "id": next(item["unit_id"] for item in current.units.values()
                       if item["unit_kind"] == "LOGICAL_TESTCASE"),
        }
        plan = self.fixture._plan(
            bundle["submission"], bundle["report"], bundle["request"],
            target, "STAGE_2")
        final = bundle["workflow"].submit_repair_plan(
            bundle["submission"], plan)
        self.assertEqual("AWAITING_SCOPED_REPLACEMENT", final["state"])
        model = ProjectReadModel.from_checkpoint(bundle["job"], final)
        result = model.call("compare_unit_revisions", {
            "comparisons": [{
                "unit_id": target["id"],
                "from_revision": 0, "to_revision": 0,
            }],
        })
        self.assert_result(result, "compare_unit_revisions")
        self.assertEqual(1, len(result["items"]))
        item = result["items"][0]
        self.assertEqual(0, item["from_unit"]["revision"])
        self.assertEqual(0, item["to_unit"]["revision"])
        self.assertEqual([], item["changes"])
        self.assertEqual(target["id"], item["from_unit"]["unit_id"])
        self.assertEqual(target["id"], item["to_unit"]["unit_id"])

        history = model.call("get_repair_history", {
            "identities": [plan["plan_id"]],
        })
        self.assert_result(history, "get_repair_history")
        plan_records = [
            item for item in history["items"]
            if item["record_type"] == "REPAIR_PLAN"]
        self.assertEqual(1, len(plan_records))
        self.assertEqual(plan, plan_records[0]["record"])

    def test_tool_contracts_stage_allowlist_and_scope_inputs(self):
        definitions = read_tool_definitions()
        self.assertEqual(sorted(READ_TOOL_NAMES), [
            item["name"] for item in definitions])
        stage = read_tool_definitions(STAGE_READ_TOOLS)
        self.assertEqual(sorted(STAGE_READ_TOOLS), [
            item["name"] for item in stage])
        self.assertNotIn("get_dependents", [item["name"] for item in stage])
        with self.assertRaises(ProjectToolError) as caught:
            read_tool_definitions({"get_issue", "run_eda"})
        self.assertEqual("TOOL_PERMISSION_DENIED", caught.exception.code)

        model = self.awaiting()["model"]
        for name, arguments in (
            ("get_issue", {"issue_ids": []}),
            ("get_unit", {"unit_ids": ["A", "A"]}),
            ("get_budget_status", {"tokens": True}),
            ("get_spec_evidence", {"evidence_refs": [{
                "path": "../rtl.sv", "line_start": 1, "line_end": 1,
                "snippet_fingerprint": "x",
            }]}),
        ):
            with self.assertRaises(ProjectToolError) as invalid:
                model.call(name, arguments)
            self.assertEqual("INVALID_TOOL_CALL", invalid.exception.code)

    def test_spec_report_and_cross_job_tamper_fail_closed(self):
        bundle = self.awaiting()
        request_path = bundle["job"] / bundle["checkpoint"]["review_request_path"]
        request = load_document(request_path)
        request["spec_evidence"][0]["content"] += "\ntampered"
        request_path.write_text(
            __import__("json").dumps(request, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        with self.assertRaises(ProjectToolError) as stale:
            ProjectReadModel.from_checkpoint(
                bundle["job"], bundle["checkpoint"])
        self.assertEqual("STALE_EVIDENCE", stale.exception.code)

        self.fixture.tearDown()
        self.fixture = Oches001RepairControlTests(methodName="runTest")
        self.fixture.setUp()
        fresh = self.awaiting()
        cross = copy.deepcopy(fresh["checkpoint"])
        cross["job_id"] = "JOB.PROJECT.OTHER.001"
        with self.assertRaises(ProjectToolError) as rejected:
            ProjectReadModel.from_checkpoint(fresh["job"], cross)
        self.assertEqual("CROSS_JOB_ARTIFACT", rejected.exception.code)


if __name__ == "__main__":
    unittest.main()


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(Oches002ProjectToolTests)
