#!/usr/bin/env python3
"""OCHES003 canonical records, prompts, stops, indexes, and serial execution."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from core.project_oches003 import (
    RepairRecordStore, SerialRepairExecutor, build_prompt_contract,
    canonical_repair_groups, map_provider_stop,
)


FP = "a" * 64


class Oches003RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = RepairRecordStore(
            self.root, job_id="JOB.PROJECT.OCHES003.TEST",
            input_fingerprint="1" * 64, spec_fingerprint="2" * 64,
            policy_fingerprint="3" * 64)

    def tearDown(self):
        self.temp.cleanup()

    def test_grouping_is_permutation_invariant_and_merges_dependencies(self):
        units = {
            "TC.A": {"dependency_fingerprints": [{
                "kind": "ACCEPTANCE_CRITERION", "identity": "AC.SHARED",
                "fingerprint": FP}]},
            "TC.B": {"dependency_fingerprints": [{
                "kind": "ACCEPTANCE_CRITERION", "identity": "AC.SHARED",
                "fingerprint": FP}]},
            "CODE.C": {"dependency_fingerprints": []},
        }
        repairs = [{
            "stage": "STAGE_2", "issue_ids": ["ISSUE.B"],
            "targets": [{"kind": "TESTCASE", "id": "TC.B"}],
        }, {
            "stage": "STAGE_2", "issue_ids": ["ISSUE.A"],
            "targets": [{"kind": "TESTCASE", "id": "TC.A"}],
        }, {
            "stage": "STAGE_3", "issue_ids": ["ISSUE.C"],
            "targets": [{"kind": "CODE_UNIT", "id": "CODE.C"}],
        }]
        first = canonical_repair_groups(
            repairs, policy_fingerprint="3" * 64, units=units)
        second = canonical_repair_groups(
            list(reversed(repairs)), policy_fingerprint="3" * 64,
            units=units)
        self.assertEqual(first, second)
        self.assertEqual(2, len(first))
        self.assertEqual(["TC.A", "TC.B"], first[0]["target_ids"])
        self.assertEqual("STAGE_3", first[1]["stage"])

    def test_records_rebuild_indexes_and_tamper_fails_closed(self):
        path, record = self.store.append("REPLAY_RECEIPT", {
            "request_fingerprint": "4" * 64,
            "authoritative_path": "audit/source.json",
            "authoritative_fingerprint": "5" * 64,
            "status": "NO_SIDE_EFFECT",
        })
        first = self.store.rebuild_indexes(procedural=[{
            "key": "prompt:REVIEWER:1.0", "path": "prompt.json",
            "fingerprint": "6" * 64,
        }])
        before = {key: (self.root / value).read_bytes()
                  for key, value in first.items()}
        second = self.store.rebuild_indexes(procedural=[{
            "key": "prompt:REVIEWER:1.0", "path": "prompt.json",
            "fingerprint": "6" * 64,
        }])
        self.assertEqual(before, {key: (self.root / value).read_bytes()
                                  for key, value in second.items()})
        value = json.loads((self.root / path).read_text())
        value["payload"]["status"] = "SIDE_EFFECT"
        (self.root / path).write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            self.store.records()
        self.assertEqual("REPLAY_RECEIPT", record["record_type"])

    def test_five_prompts_and_all_stop_mappings(self):
        for role in ("REVIEWER", "ORCHESTRATOR", "STAGE_1", "STAGE_2", "STAGE_3"):
            prompt = build_prompt_contract(
                role=role, job_id="JOB.PROJECT.OCHES003.TEST",
                input_fingerprint="1" * 64, spec_fingerprint="2" * 64,
                policy_fingerprint="3" * 64,
                artifact_roots={"testcase": "4" * 64},
                unit_roots={"stage3": "5" * 64}, dependencies=[],
                provider_id="provider", model_id="model",
                role_fingerprint="6" * 64,
                tool_allow_list=[] if role == "REVIEWER" else ["read"],
                final_output="candidate", formal_scope={"ids": []})
            self.assertEqual(0 if role == "REVIEWER" else 3,
                             prompt["retrieval_call_limit"])
            self.assertIn("Never invent", prompt["instructions"])
        base = {"finish_reason": "TOOL_CALLS",
                "tool_calls": [{"name": "read", "arguments": {}}],
                "legal_tools": ["read"], "submission_tools": {"submit"}}
        self.assertEqual("TOOL_RESULT_REQUIRED", map_provider_stop(**base))
        self.assertEqual("COMPLETED", map_provider_stop(
            **{**base, "tool_calls": [{"name": "submit", "arguments": {}}]}))
        self.assertEqual("TOOL_PROTOCOL_VIOLATION", map_provider_stop(
            **base, retrieval_count=3))
        self.assertEqual("MALFORMED_MODEL_OUTPUT", map_provider_stop(
            finish_reason="STOP"))
        for condition, expected in (
                ("CONTENT_FILTER", "CONTENT_FILTERED"),
                ("REFUSAL", "MODEL_REFUSAL"),
                ("LENGTH", "OUTPUT_LIMIT_EXCEEDED")):
            self.assertEqual(expected, map_provider_stop(finish_reason=condition))
        self.assertEqual("PROVIDER_UNAVAILABLE", map_provider_stop(
            exception_code="TIMEOUT"))
        self.assertEqual("CANCELLED", map_provider_stop(cancel_requested=True))

    def test_partial_success_one_final_review_human_and_replay(self):
        units = {
            "SCENARIO.A": {"revision": 0, "dependency_fingerprints": []},
            "CODE.B": {"revision": 0, "dependency_fingerprints": []},
        }
        repairs = [{
            "stage": "STAGE_3", "issue_ids": ["ISSUE.B"],
            "targets": [{"kind": "CODE_UNIT", "id": "CODE.B"}],
        }, {
            "stage": "STAGE_1", "issue_ids": ["ISSUE.A"],
            "targets": [{"kind": "SCENARIO", "id": "SCENARIO.A"}],
        }]
        calls = {"review": 0, "human": 0}

        def dispatch(group, state):
            return {
                "plan_id": "REPAIRPLAN.TEST", "group_id": group["group_id"],
                "artifact_roots": state["artifact_roots"],
                "unit_roots": state["unit_roots"],
                "owner_scope_fingerprint": state["owner_scope_fingerprint"],
                "policy_fingerprint": "3" * 64,
                "dependencies": {
                    identity: state["units"][identity]["dependency_fingerprints"]
                    for identity in group["target_ids"]},
                "spec_identities": [], "tool_allow_list": ["read"],
            }

        def replacement(dispatch_value, _state):
            return {"replacement_fingerprint": (
                "7" if "SCENARIO.A" in dispatch_value["dependencies"] else "8") * 64}

        def validate(_replacement, dispatch_value, _state):
            return {"status": (
                "PASS" if "SCENARIO.A" in dispatch_value["dependencies"] else "FAIL"),
                "diagnostics": [], "unexecuted_checks": []}

        def commit(_replacement, state):
            new = copy.deepcopy(state)
            new["artifact_roots"] = {"bundle": "9" * 64}
            new["unit_roots"] = {"all": "a" * 64}
            new["units"]["SCENARIO.A"]["revision"] = 1
            return {"state": new, "target_revisions": [{
                "unit_id": "SCENARIO.A", "old_revision": 0,
                "new_revision": 1}]}

        def review(state, episodes):
            calls["review"] += 1
            self.assertEqual({"bundle": "9" * 64}, state["artifact_roots"])
            self.assertEqual(2, len(episodes))
            return {"initial_report_path": "initial.json",
                    "final_request_path": "request.json",
                    "final_report_path": "report.json"}

        def human(_state, _review):
            calls["human"] += 1
            return {"state": "AWAITING_HUMAN_REVIEW"}

        executor = SerialRepairExecutor(self.store)
        result = executor.execute(
            repairs=repairs, state={
                "artifact_roots": {"bundle": "4" * 64},
                "unit_roots": {"all": "5" * 64}, "units": units,
                "owner_scope_fingerprint": "6" * 64,
            }, dispatch_group=dispatch, produce_replacement=replacement,
            validate_group=validate, commit_group=commit,
            evaluate_group_impact=lambda *_: {
                "dirty_units": ["SCENARIO.A"], "reused_units": [],
                "direct_dependency_closure": ["SCENARIO.A"]},
            final_review=review, human_transition=human)
        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        self.assertEqual({"review": 1, "human": 1}, calls)
        replay = executor.execute(
            repairs=repairs, state={}, dispatch_group=None,
            produce_replacement=None, validate_group=None, commit_group=None,
            evaluate_group_impact=None, final_review=None,
            human_transition=None)
        self.assertEqual(result, replay)
        self.assertEqual({"review": 1, "human": 1}, calls)
        self.assertEqual("REPLAY_RECEIPT", self.store.records()[-1][1][
            "record_type"])


if __name__ == "__main__":
    unittest.main()
