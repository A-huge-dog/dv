#!/usr/bin/env python3
"""PJ-002 staged Spec-only Project workflow tests."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from contracts.validator import accepted, load_document, validate
from core.project_job import ProjectJobError, ProjectJobWorkflow
from core.project_staged import (
    StagedProjectWorkflow, _enrich_code_evidence, _enrich_evidence,
    _validate_code_evidence, inspect_no_rtl_request)
from scripts.dvlib import canonical_hash


SPEC = """# Tiny
The portable interface is module `tiny` with input `clk` and output `y`.
The testbench shall drive `clk` low and then high.
Acceptance: output `y` shall equal sampled `clk`.
Every check shall complete within 20 clock cycles.
"""


def _evidence(content: str, needle: str) -> dict:
    for line, text in enumerate(content.splitlines(), start=1):
        if needle in text:
            return {"line_start": line, "line_end": line, "snippet": text}
    raise AssertionError("missing evidence: {}".format(needle))


def _spec_range(content: str, needle: str) -> dict:
    evidence = _evidence(content, needle)
    return {
        "path": "spec.md",
        "line_start": evidence["line_start"],
        "line_end": evidence["line_end"],
    }


class FakeProvider:
    provider_id = "fake-project-provider"
    model_id = "fake-project-model"

    def __init__(self, ambiguity_stage=None, duplicate_testcases=False):
        self.calls = 0
        self.probe_calls = 0
        self.restore_calls = 0
        self.requests = []
        self.ambiguity_stage = ambiguity_stage
        self.duplicate_testcases = duplicate_testcases

    def probe(self):
        self.probe_calls += 1
        return {
            "schema_version": "1.0",
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "status": "PASS",
            "tool_call_capable": True,
            "provider_version": "test",
            "diagnostics": [],
        }

    def restore_probe(self, probe):
        self.restore_calls += 1

    @staticmethod
    def stage1():
        spec = _spec_range(SPEC, "Acceptance:")
        return {
            "scenarios": [{
                "objective": "Verify y follows sampled clk.",
                "verification_level": "UNIT",
                "status": "CHECKABLE",
                "reason": "",
                "spec_evidence": [spec],
            }],
            "acceptance_criteria": [{
                "scenario_indexes": [0],
                "behavior": "Output y equals sampled clk.",
                "verification_level": "UNIT",
                "status": "CHECKABLE",
                "reason": "",
                "spec_evidence": [spec],
            }],
            "completeness": {
                "declared_complete": True,
                "omitted_behaviors": [],
            },
        }

    def stage2(self):
        evidence = _spec_range(SPEC, "Acceptance:")
        testcase = {
            "objective": "Drive clk and check y.",
            "scenario_ids": ["SCENARIO.0001"],
            "ac_ids": ["AC.0001"],
            "preconditions": "Start with clk low.",
            "stimulus": "Drive clk low and then high.",
            "transaction_sequence": "Low sample followed by high sample.",
            "timing_intent": "Sample after each bounded clock transition.",
            "checker": "Compare y with clk and fatal on mismatch.",
            "expected_result": "y equals clk.",
            "failure_condition": "y differs from clk.",
            "timeout_cycles": 20,
            "status": "CHECKABLE",
            "reason": "Exact Spec defines stimulus and expected result.",
            "spec_evidence": [evidence],
        }
        testcases = [testcase]
        if self.duplicate_testcases:
            second = copy.deepcopy(testcase)
            second["objective"] = "Repeat the bounded y/clk check."
            testcases.append(second)
        return {
            "logical_testcases": testcases,
            "completeness": {
                "declared_complete": True,
                "omissions": [],
            },
        }

    @staticmethod
    def stage3(payload):
        top = payload["job_identity"]["testbench_top"]
        marker = payload["job_identity"]["pass_marker"]
        testcase_ids = [
            item["testcase_id"] for item in
            payload["ac_testcase_map"]["index"].get(
                "logical_testcases", [])]
        if not testcase_ids:
            testcase_ids = [
                item["testcase_id"]
                for shard in payload["ac_testcase_map"]["shards"]
                for item in shard["logical_testcases"]]
        shared = """module {top};
  logic clk;
  logic y;
  tiny dut (.clk(clk), .y(y));
  always #5 clk = ~clk;
""".format(top=top)
        testcase = """  initial begin
    clk = 1'b0;
    $dumpfile("project.vcd");
    $dumpvars(0, {top});
    #1;
    if (y !== clk) $fatal(1, "AC_TINY_LOW_FAIL");
    clk = 1'b1;
    #1;
    if (y !== clk) $fatal(1, "AC_TINY_HIGH_FAIL");
    $display("{marker}");
    $finish;
  end
endmodule
""".format(top=top, marker=marker)
        return {
            "code_units": [{
                "role": "SHARED",
                "testcase_ids": [],
                "content": shared,
            }, {
                "role": "TESTCASE",
                "testcase_ids": sorted(testcase_ids),
                "content": testcase,
            }],
            "assembly": [0, 1],
            "implemented_testcase_ids": sorted(testcase_ids),
        }

    def select_tools(self, request):
        self.requests.append(copy.deepcopy(request))
        self.calls += 1
        stage = request["metadata"]["stage"]
        if self.ambiguity_stage == stage:
            artifact = {
                "outcome": "SPEC_AMBIGUITY",
                "reason": "Spec lacks a concrete portable binding.",
            }
            tool_name = "submit_project_stage_blocked"
        elif stage == "SCENARIO_AC_MAP":
            artifact = self.stage1()
        elif stage == "AC_TESTCASE_MAP":
            artifact = self.stage2()
        else:
            payload = json.loads(request["messages"][1]["content"])
            artifact = self.stage3(payload)
        if self.ambiguity_stage != stage:
            tool_name = next(
                name for name in request["legal_tool_names"]
                if name != "submit_project_stage_blocked")
        return {
            "schema_version": "1.0",
            "request_id": request["request_id"],
            "operation": "SELECT_TOOLS",
            "finish_reason": "TOOL_CALLS",
            "content": "",
            "tool_calls": [{
                "call_id": "CALL.GEN.{:03d}".format(self.calls),
                "name": tool_name,
                "arguments": artifact,
            }],
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "model_id": self.model_id,
            "provider_metadata": {
                "provider_id": self.provider_id,
                "response_id": "RESP.GEN.{:03d}".format(self.calls),
                "provider_status": "completed",
            },
            "diagnostics": [],
        }


class FakeReviewerProvider:
    provider_id = "fake-project-reviewer"
    model_id = "fake-reviewer-model"

    def __init__(self, revision_stage=None):
        self.calls = 0
        self.probe_calls = 0
        self.restore_calls = 0
        self.requests = []
        self.revision_stage = revision_stage

    def probe(self):
        self.probe_calls += 1
        return {
            "schema_version": "1.0",
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "status": "PASS",
            "tool_call_capable": True,
            "provider_version": "test",
            "diagnostics": [],
        }

    def restore_probe(self, probe):
        self.restore_calls += 1

    def _report(self, review):
        map1 = review["scenario_ac_map"]
        map2 = review["ac_testcase_map"]["index"]
        candidate = review["testcase_candidate"]
        ac = map1["acceptance_criteria"][0]
        coverage = map2["ac_coverage"][0]
        raw_spec = {
            key: ac["spec_evidence"][0][key]
            for key in ("path", "line_start", "line_end")}
        select_line = lambda needle: {
            "content": _evidence(candidate["content"], needle)["snippet"]}
        blocking = self.revision_stage is not None and self.calls == 0
        findings = []
        if blocking:
            stage = {
                "SCENARIO_AC_MAP": "STAGE_1",
                "AC_TESTCASE_MAP": "STAGE_2",
                "TESTCASE": "STAGE_3",
            }[self.revision_stage]
            findings.append({
                "severity": "ERROR",
                "suspected_origin_stage": stage,
                "affected": {
                    "scenario_ids": copy.deepcopy(ac["scenario_ids"]),
                    "ac_ids": [ac["ac_id"]],
                    "testcase_ids": copy.deepcopy(coverage["testcase_ids"]),
                    "code_unit_ids": [],
                },
                "spec_evidence": [raw_spec],
                "testcase_evidence": [],
                "problem_and_required_change": (
                    "The staged mapping is incorrect; regenerate the affected "
                    "objects from the appropriate Stage."),
            })
        return {
            "verdict": "FINDINGS_REPORTED" if blocking else "CLEAN",
            "findings": findings,
            "ac_reviews": [{
                "ac_id": ac["ac_id"],
                "status": "COVERED",
                "spec_evidence": [raw_spec],
                "stimulus_evidence": [
                    select_line("clk = 1'b0;"),
                    select_line("clk = 1'b1;")],
                "checker_evidence": [
                    select_line("AC_TINY_LOW_FAIL"),
                    select_line("AC_TINY_HIGH_FAIL")],
                "omission": "",
            }],
            "diagnostics": [],
        }

    def select_tools(self, request):
        self.requests.append(copy.deepcopy(request))
        review = json.loads(request["messages"][-1]["content"])
        report = self._report(review)
        self.calls += 1
        return {
            "schema_version": "1.0",
            "request_id": request["request_id"],
            "operation": "SELECT_TOOLS",
            "finish_reason": "TOOL_CALLS",
            "content": "",
            "tool_calls": [{
                "call_id": "CALL.REVIEW.{:03d}".format(self.calls),
                "name": "submit_staged_project_review",
                "arguments": report,
            }],
            "usage": {"input_tokens": 30, "output_tokens": 10},
            "model_id": self.model_id,
            "provider_metadata": {
                "provider_id": self.provider_id,
                "response_id": "RESP.REVIEW.{:03d}".format(self.calls),
                "provider_status": "completed",
            },
            "diagnostics": [],
        }


class SlowProvider(FakeProvider):
    def select_tools(self, request):
        time.sleep(1.05)
        return super().select_tools(request)


class Stage3ValidatedRetryProvider(FakeProvider):
    def __init__(self, failures):
        super().__init__()
        self.failures = list(failures)
        self.stage3_calls = 0

    def select_tools(self, request):
        response = super().select_tools(request)
        if request["metadata"]["stage"] != "TESTCASE":
            return response
        index = self.stage3_calls
        self.stage3_calls += 1
        if index >= len(self.failures):
            return response
        mode = self.failures[index]
        candidate = response["tool_calls"][0]["arguments"]
        if mode == "code":
            testcase = next(
                item for item in candidate["code_units"]
                if item["role"] == "TESTCASE")
            testcase["content"] = testcase["content"].replace(
                "    $finish;\n", "")
        elif mode == "compile":
            testcase = next(
                item for item in candidate["code_units"]
                if item["role"] == "TESTCASE")
            testcase["content"] = testcase["content"].replace(
                "    clk = 1'b0;",
                "    clk = 1'b0\n", 1)
        elif mode == "mapping":
            candidate["code_units"][1]["testcase_ids"] = []
        elif mode == "assembly":
            candidate["assembly"] = [0, 0]
        else:
            raise AssertionError("unknown Stage 3 failure mode")
        return response


class SharedCheckerProvider(FakeProvider):
    """Generate one code candidate for two ACs mapped to the same testcase."""

    @staticmethod
    def stage1():
        result = FakeProvider.stage1()
        second = copy.deepcopy(result["acceptance_criteria"][0])
        second.update({
            "behavior": "Output y matches the driven clock value.",
        })
        result["acceptance_criteria"].append(second)
        return result

    def stage2(self):
        result = FakeProvider.stage2(self)
        result["logical_testcases"][0]["ac_ids"].append(
            "AC.0002")
        return result

class SingleCodeUnitProvider(FakeProvider):
    """Submit one complete TESTCASE unit with no artificial SHARED split."""

    @staticmethod
    def stage3(payload):
        result = FakeProvider.stage3(payload)
        complete = "".join(item["content"] for item in result["code_units"])
        testcase_ids = copy.deepcopy(result["implemented_testcase_ids"])
        result["code_units"] = [{
            "role": "TESTCASE",
            "testcase_ids": testcase_ids,
            "content": complete,
        }]
        result["assembly"] = [0]
        return result


class CounterTimeoutProvider(FakeProvider):
    """Use a symbolic clock delay and an explicit clock-sampled counter."""

    @staticmethod
    def stage3(payload):
        result = FakeProvider.stage3(payload)
        shared = result["code_units"][0]
        shared["content"] = shared["content"].replace(
            "  always #5 clk = ~clk;\n",
            "  localparam time CLK_HALF = 5;\n"
            "  always #CLK_HALF clk = ~clk;\n")
        testcase = result["code_units"][1]
        testcase["content"] = testcase["content"].replace(
            "    clk = 1'b0;\n",
            "    integer timeout_count;\n"
            "    clk = 1'b0;\n"
            "    timeout_count = 0;\n"
            "    while (timeout_count < 20) begin\n"
            "      @(posedge clk);\n"
            "      timeout_count = timeout_count + 1;\n"
            "    end\n")
        return result


class EmptyExpectedResultProvider(FakeProvider):
    """Prove the upstream Stage 2 validator still owns expected_result."""

    def stage2(self):
        result = super().stage2()
        result["logical_testcases"][0]["expected_result"] = ""
        return result


class TestcaseMappingOverreachProvider(FakeProvider):
    @staticmethod
    def stage3(payload):
        result = FakeProvider.stage3(payload)
        result["code_units"][1]["testcase_ids"] = []
        return result


class SharedCheckerReviewer(FakeReviewerProvider):
    def _report(self, review):
        map1 = review["scenario_ac_map"]
        candidate = review["testcase_candidate"]
        ac_reviews = []
        for ac in map1["acceptance_criteria"]:
            raw_spec = {
                key: ac["spec_evidence"][0][key]
                for key in ("path", "line_start", "line_end")
            }
            ac_reviews.append({
                "ac_id": ac["ac_id"],
                "status": "COVERED",
                "spec_evidence": [raw_spec],
                "stimulus_evidence": [{
                    "content": _evidence(
                        candidate["content"], "clk = 1'b0;")["snippet"]}],
                "checker_evidence": [{
                    "content": _evidence(
                        candidate["content"],
                        "AC_TINY_LOW_FAIL")["snippet"]}],
                "omission": "",
            })
        return {
            "verdict": "CLEAN",
            "findings": [],
            "ac_reviews": ac_reviews,
            "diagnostics": [],
        }


class InterruptedStage3RetryProvider(Stage3ValidatedRetryProvider):
    def __init__(self):
        super().__init__(["code"])

    def select_tools(self, request):
        if (request["metadata"]["stage"] == "TESTCASE" and
                request["metadata"].get("retry_attempt") == 1):
            self.requests.append(copy.deepcopy(request))
            raise RuntimeError("simulated retry interruption")
        return super().select_tools(request)


class MalformedGenerationProvider(FakeProvider):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode

    def select_tools(self, request):
        response = super().select_tools(request)
        call = response["tool_calls"][0]
        if self.mode == "text_only":
            response["finish_reason"] = "STOP"
            response["content"] = json.dumps(call["arguments"])
            response["tool_calls"] = []
        elif self.mode == "duplicate_call":
            duplicate = copy.deepcopy(call)
            duplicate["call_id"] += ".SECOND"
            response["tool_calls"].append(duplicate)
        elif self.mode == "wrong_name":
            call["name"] = "submit_unregistered_candidate"
        elif self.mode == "extra_field":
            call["arguments"]["runtime_fingerprint"] = "0" * 64
        elif self.mode == "missing_field":
            call["arguments"].pop("completeness")
        elif self.mode == "non_object_arguments":
            call["arguments"] = []
        elif self.mode == "model_snippet":
            call["arguments"]["scenarios"][0]["spec_evidence"][0][
                "snippet"] = "model-authored"
        elif self.mode == "model_fingerprint":
            call["arguments"]["scenarios"][0]["spec_evidence"][0][
                "snippet_fingerprint"] = "0" * 64
        elif self.mode == "unknown_path":
            call["arguments"]["scenarios"][0]["spec_evidence"][0][
                "path"] = "unknown.md"
        elif self.mode == "zero_line":
            call["arguments"]["scenarios"][0]["spec_evidence"][0][
                "line_start"] = 0
        elif self.mode == "negative_line":
            call["arguments"]["scenarios"][0]["spec_evidence"][0][
                "line_start"] = -1
        elif self.mode == "reversed_line":
            evidence = call["arguments"]["scenarios"][0]["spec_evidence"][0]
            evidence["line_start"], evidence["line_end"] = 5, 4
        elif self.mode == "out_of_range":
            evidence = call["arguments"]["scenarios"][0]["spec_evidence"][0]
            evidence["line_start"] = evidence["line_end"] = 999
        elif self.mode == "missing_evidence":
            call["arguments"]["scenarios"][0]["spec_evidence"] = []
        elif self.mode == "legacy_candidate":
            call["arguments"]["schema_version"] = "2.0"
            call["arguments"]["artifact_kind"] = "SCENARIO_AC_MAP"
        elif self.mode == "duplicate_local_ref":
            call["arguments"]["scenarios"][0]["framework_id"] = "FORBIDDEN"
        elif self.mode == "unknown_local_ref":
            call["arguments"]["acceptance_criteria"][0][
                "scenario_indexes"] = [999]
        elif self.mode == "false_completeness":
            call["arguments"]["completeness"]["declared_complete"] = False
        else:
            raise AssertionError("unknown malformed mode")
        return response


class ProjectJobWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "spec.md").write_text(SPEC, encoding="utf-8")
        (self.root / "tiny.sv").write_text(
            "module tiny(input logic clk, output logic y);\n"
            "  assign y = clk;\nendmodule\n", encoding="utf-8")
        (self.root / "config").mkdir()
        base = {
            "schema_version": "1.0",
            "provider_kind": "DASHSCOPE",
            "provider_id": "fake-project-provider",
            "model_id": "fake-project-model",
            "endpoint":
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "auth_env": "DASHSCOPE_API_KEY",
            "api_version": "chat-completions-v1",
            "tool_calls_required": True,
            "store": False,
            "timeout_seconds": 60,
            "max_retries": 0,
            "max_output_tokens": 8192,
            "enable_thinking": False,
        }
        reviewer = copy.deepcopy(base)
        reviewer["provider_id"] = "fake-project-reviewer"
        reviewer["model_id"] = "fake-reviewer-model"
        (self.root / "config/generator.yaml").write_text(
            yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
        (self.root / "config/reviewer.yaml").write_text(
            yaml.safe_dump(reviewer, sort_keys=False), encoding="utf-8")
        profile = {
            "schema_version": "1.0",
            "profile_id": "PROJECT_AGENT_PROFILE.TEST",
            "initial": {
                "stage1": "config/generator.yaml",
                "stage2": "config/generator.yaml",
                "stage3": "config/generator.yaml",
            },
            "repair": {
                "orchestrator": "config/generator.yaml",
                "stage1": "config/generator.yaml",
                "stage2": "config/generator.yaml",
                "stage3": "config/generator.yaml",
            },
            "review": {
                "initial": "config/reviewer.yaml",
                "final": "config/reviewer.yaml",
            },
        }
        (self.root / "config/agents.yaml").write_text(
            yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def project_input(self):
        return {
            "schema_version": "2.0",
            "job_id": "JOB.PROJECT.TINY.001",
            "project_id": "PROJECT.TINY",
            "spec": {"sources": ["spec.md"]},
            "rtl": {
                "sources": ["tiny.sv"],
                "top": "tiny",
                "parameters": {},
            },
            "agent_profile": "config/agents.yaml",
            "eda": {
                "profile_id": "EDAPROFILE.VERILATOR.PROJECT.V1",
                "timeout_seconds": 60,
            },
            "input_authority": {
                "actor_type": "HUMAN",
                "identity": "human.project.owner",
                "roles": ["SPEC_OWNER", "DESIGN_OWNER"],
                "decision": "APPROVE",
            },
        }

    def start_checked(self, workflow, submission=None):
        submission = submission or self.project_input()
        checkpoint = workflow.start(submission)
        if checkpoint.get("state") != "AWAITING_SCENARIO_ROUTING":
            return checkpoint
        job = workflow.result_root / "jobs" / submission["job_id"]
        form = load_document(job / checkpoint["owner_review_path"])
        completed = self.completed_owner_review(
            form, "AC_TESTCASE_MAP_AND_TESTCASE", "")
        return workflow.route_scenarios(submission, completed)

    @staticmethod
    def completed_owner_review(form, destination, comment):
        result = copy.deepcopy(form)
        result["actor"]["identity"] = "human.dv.owner"
        for item in result["scenarios"]:
            item["comment"] = comment
            item["routing"]["destination"] = destination
        return result

    def test_owner_review_context_and_typed_routing(self):
        generator = FakeProvider()
        reviewer = FakeReviewerProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        submission = self.project_input()
        checkpoint = workflow.start(submission)
        self.assertEqual("AWAITING_SCENARIO_ROUTING", checkpoint["state"])
        self.assertEqual(1, generator.calls)
        self.assertEqual(1, generator.probe_calls)

        self.assertEqual(0, reviewer.probe_calls)
        self.assertEqual(0, reviewer.calls)
        job = self.root / "result/jobs" / submission["job_id"]
        form_path = job / checkpoint["owner_review_path"]
        form = load_document(form_path)
        context = form["scenarios"][0]
        self.assertEqual("SCENARIO.0001", context["scenario_id"])
        self.assertEqual(["AC.0001"], context["ac_ids"])
        self.assertEqual("CHECKABLE", context["status"])
        self.assertTrue(context["spec_evidence"][0]["snippet_fingerprint"])
        self.assertEqual("", form["actor"]["identity"])
        self.assertIsNone(context["routing"]["destination"])
        self.assertEqual("", context["comment"])

        missing_identity = copy.deepcopy(form)
        missing_identity["scenarios"][0]["routing"]["destination"] = \
            "AC_TESTCASE_MAP_AND_TESTCASE"
        with self.assertRaises(ProjectJobError) as caught:
            workflow.route_scenarios(submission, missing_identity)
        self.assertEqual("INVALID_OWNER_AUTHORITY", caught.exception.code)

        missing_destination = copy.deepcopy(form)
        missing_destination["actor"]["identity"] = "human.dv.owner"
        with self.assertRaises(ProjectJobError) as caught:
            workflow.route_scenarios(submission, missing_destination)
        self.assertEqual("INCOMPLETE_OWNER_ROUTING", caught.exception.code)

        tampered = self.completed_owner_review(
            form, "AC_TESTCASE_MAP_AND_TESTCASE", "")
        tampered["scenarios"][0]["ac_ids"] = ["AC.TINY.TAMPERED"]
        with self.assertRaises(ProjectJobError) as caught:
            workflow.route_scenarios(submission, tampered)
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)

        invalid = self.completed_owner_review(
            form, "SCENARIO_AC_MAPPER", "   ")
        with self.assertRaises(ProjectJobError) as caught:
            workflow.route_scenarios(submission, invalid)
        self.assertEqual("INVALID_OWNER_ROUTING", caught.exception.code)
        completed = self.completed_owner_review(
            form, "AC_TESTCASE_MAP_AND_TESTCASE", "")
        result = workflow.route_scenarios(submission, completed)
        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        snapshot = load_document(
            job / "audit/scenario_owner_review_submission.json")
        self.assertEqual(completed, snapshot["submitted_form"])
        malformed_snapshot = copy.deepcopy(snapshot)
        malformed_snapshot["submitted_form"] = {}
        self.assertFalse(accepted(validate(
            "scenario_owner_review_submission", malformed_snapshot)))
        self.assertEqual(
            snapshot["submission_fingerprint"],
            result["owner_review_submission_fingerprint"])
        self.assertEqual(result, workflow.route_scenarios(
            submission, completed))
        conflicting = copy.deepcopy(completed)
        conflicting["scenarios"][0]["comment"] = "different submission"
        with self.assertRaises(ProjectJobError) as caught:
            workflow.route_scenarios(submission, conflicting)
        self.assertEqual("CONFLICTING_REPLAY", caught.exception.code)

    def test_human_rejection_pauses_and_later_approval_resumes_same_job(self):
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result",
            FakeProvider(), FakeReviewerProvider())
        submission = self.project_input()
        checkpoint = self.start_checked(workflow, submission)
        self.assertEqual("AWAITING_HUMAN_REVIEW", checkpoint["state"])
        job = self.root / "result/jobs" / submission["job_id"]
        approval = load_document(
            job / checkpoint["approval_request_path"])

        def decision(kind, identity):
            return {
                "schema_version": "1.0",
                "decision_id": "DECISION.{}.{}".format(kind, identity),
                "approval_request_id": approval["approval_request_id"],
                "job_id": submission["job_id"],
                "thread_id": approval["thread_id"],
                "decision": kind,
                "approver_identity": identity,
                "approver_role": "DV_REVIEWER",
                "reason": "explicit Human review decision",
                "evidence_ids": approval["validation_artifact_ids"],
                "candidate_fingerprint": approval["candidate_fingerprint"],
                "checkpoint_id": checkpoint["checkpoint_id"],
                "decided_at": "2026-08-14T00:00:00Z",
            }

        paused = workflow.resume(
            submission, decision("REJECT", "human.dv.reviewer.one"))
        self.assertEqual("PAUSED_BY_HUMAN", paused["state"])
        completed = workflow.resume(
            submission, decision("APPROVE", "human.dv.reviewer.two"))
        self.assertEqual("COMPLETE", completed["state"])
        self.assertTrue((job / "audit/project_completed.json").is_file())

    def test_reviewer_bad_attempt_resumes_with_a_fresh_attempt(self):
        class RetryReviewer(FakeReviewerProvider):
            def select_tools(self, request):
                active = copy.deepcopy(request)
                if request["metadata"].get("retry_attempt"):
                    active["messages"] = active["messages"][:-1]
                response = super().select_tools(active)
                if self.calls == 1:
                    response["tool_calls"][0]["arguments"][
                        "unexpected_field"] = True
                return response

        reviewer = RetryReviewer()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", FakeProvider(), reviewer)
        submission = self.project_input()
        checkpoint = workflow.start(submission)
        job = self.root / "result/jobs" / submission["job_id"]
        form = load_document(job / checkpoint["owner_review_path"])
        routing = self.completed_owner_review(
            form, "AC_TESTCASE_MAP_AND_TESTCASE", "")
        with self.assertRaises(ProjectJobError) as paused:
            workflow.route_scenarios(submission, routing)
        self.assertEqual("ATTEMPT_PAUSED", paused.exception.code)
        result = workflow.route_scenarios(submission, routing)
        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        self.assertEqual(2, reviewer.calls)
        self.assertTrue((job /
            "audit/pj002_provider_response.review.r001.retry001.json"
        ).is_file())

    def test_owner_mapper_single_r001_and_exact_replay(self):
        generator = FakeProvider()
        reviewer = FakeReviewerProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        submission = self.project_input()
        checkpoint = workflow.start(submission)
        job = self.root / "result/jobs" / submission["job_id"]
        form = load_document(job / checkpoint["owner_review_path"])
        routing = self.completed_owner_review(
            form, "SCENARIO_AC_MAPPER",
            "Re-evaluate the complete Scenario mapping as one atomic unit.")
        result = workflow.route_scenarios(submission, routing)
        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        self.assertTrue((
            job / "staging/mappings/scenario_ac_map.r001.json").is_file())
        self.assertTrue((
            job / "staging/mappings/scenario_ac_map.r001.lineage.json").is_file())
        self.assertFalse((
            job / "staging/mappings/scenario_ac_map.r002.json").exists())
        self.assertEqual(4, generator.calls)
        initial_prompt = generator.requests[0]["messages"][0]["content"]
        correction_request = next(
            request for request in generator.requests
            if request["request_id"].endswith(".R001"))
        correction_prompt = correction_request["messages"][0]["content"]
        self.assertNotIn(
            "exact authorized replacement scope", initial_prompt)
        self.assertIn(
            "routing.destination is SCENARIO_AC_MAPPER", correction_prompt)
        self.assertIn(
            "prior_stage_artifact.scenario_ids", correction_prompt)
        self.assertIn(
            "every and only those Scenario IDs", correction_prompt)
        self.assertIn(
            "Do not emit, modify, regenerate, or carry any Scenario routed "
            "to SPEC_AGENT or AC_TESTCASE_MAP_AND_TESTCASE",
            correction_prompt)
        self.assertEqual(result, workflow.route_scenarios(submission, routing))
        self.assertEqual(4, generator.calls)

    def test_owner_mapper_scope_failure_retries_in_same_job(self):
        class ScopeRetryProvider(FakeProvider):
            @staticmethod
            def stage1_two():
                value = FakeProvider.stage1()
                scenario = copy.deepcopy(value["scenarios"][0])
                scenario["objective"] = "Verify a second mapped scenario."
                ac = copy.deepcopy(value["acceptance_criteria"][0])
                ac["scenario_indexes"] = [1]
                ac["behavior"] = "Output y remains defined after sampling."
                value["scenarios"].append(scenario)
                value["acceptance_criteria"].append(ac)
                return value

            def select_tools(self, request):
                response = super().select_tools(request)
                if request["metadata"]["stage"] == "SCENARIO_AC_MAP":
                    candidate = self.stage1_two()
                    if ".RETRY" in request["request_id"]:
                        candidate["scenarios"] = candidate["scenarios"][:1]
                        candidate["acceptance_criteria"] = \
                            candidate["acceptance_criteria"][:1]
                    response["tool_calls"][0]["arguments"] = candidate
                return response

        generator = ScopeRetryProvider(
            ambiguity_stage="AC_TESTCASE_MAP")
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator,
            FakeReviewerProvider())
        submission = self.project_input()
        checkpoint = workflow.start(submission)
        job = self.root / "result/jobs" / submission["job_id"]
        form = load_document(job / checkpoint["owner_review_path"])
        routing = self.completed_owner_review(
            form, "AC_TESTCASE_MAP_AND_TESTCASE", "")
        target = next(
            item for item in routing["scenarios"]
            if item["scenario_id"] == "SCENARIO.0001")
        target["routing"]["destination"] = "SCENARIO_AC_MAPPER"
        target["comment"] = "Regenerate only this Scenario."

        with self.assertRaises(ProjectJobError) as caught:
            workflow.route_scenarios(submission, routing)

        self.assertEqual("ATTEMPT_PAUSED", caught.exception.code)
        self.assertEqual(2, generator.calls)
        self.assertTrue((
            job / "staging/requests/stage1.owner.r001.json"
        ).is_file())
        self.assertFalse((
            job / "staging/mappings/scenario_ac_map.r001.json").is_file())
        rejection = list(job.glob(
            "audit/pj002_rejected_stage_response.*.json"))
        self.assertEqual(1, len(rejection))
        self.assertEqual(
            "MAPPING_SCOPE_VIOLATION",
            load_document(rejection[0])["diagnostic"]["code"])
        with self.assertRaises(ProjectJobError) as resumed:
            workflow.route_scenarios(submission, routing)
        self.assertEqual(
            "SPEC_AMBIGUITY", resumed.exception.code,
            str(resumed.exception))
        self.assertEqual(4, generator.calls)
        self.assertTrue((
            job / "staging/requests/stage1.owner.r001.retry001.json"
        ).is_file())
        self.assertTrue((
            job / "staging/mappings/scenario_ac_map.r001.json").is_file())

    def test_reordered_semantic_arrays_formalize_identically(self):
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", FakeProvider(),
            FakeReviewerProvider())
        manifest = workflow.bootstrap(self.project_input())
        staged = StagedProjectWorkflow(workflow)
        _, sources, spec_fp = staged._spec(manifest)
        evidence = [_spec_range(SPEC, "Acceptance:")]
        raw1 = FakeProvider.stage1()
        raw1["scenarios"].append({
            "objective": "Verify the auxiliary semantic scenario.",
            "verification_level": "UNIT",
            "status": "CHECKABLE",
            "reason": "",
            "spec_evidence": copy.deepcopy(evidence),
        })
        raw1["acceptance_criteria"].append({
            "scenario_indexes": [1, 0],
            "behavior": "Exercise the same exact behavior relationship.",
            "verification_level": "UNIT",
            "status": "CHECKABLE",
            "reason": "",
            "spec_evidence": copy.deepcopy(evidence),
        })
        response = {
            "request_id": "REQ.CANONICAL",
            "model_id": "fake-project-model",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "provider_metadata": {
                "provider_id": "fake-project-provider",
                "response_id": "RESP.CANONICAL"},
        }
        raw1_reordered = copy.deepcopy(raw1)
        raw1_reordered["scenarios"].reverse()
        raw1_reordered["acceptance_criteria"].reverse()
        for ac in raw1_reordered["acceptance_criteria"]:
            ac["scenario_indexes"] = [1 - item for item in ac["scenario_indexes"]]
        map_a = staged._enrich_stage1(
            raw1, manifest, sources, spec_fp, 0, response)
        map_b = staged._enrich_stage1(
            raw1_reordered, manifest, sources, spec_fp, 0, response)
        self.assertNotEqual(map_a, map_b)
        tc_base = FakeProvider().stage2()["logical_testcases"][0]
        tc_base["scenario_ids"] = [
            "SCENARIO.0001", "SCENARIO.0002"]
        tc_base["ac_ids"] = [
            "AC.0001", "AC.0002"]
        tc_second = copy.deepcopy(tc_base)
        tc_second["objective"] = "Second logical semantic mapping."
        tc_second["scenario_ids"] = ["SCENARIO.0001"]
        tc_second["ac_ids"] = ["AC.0001"]
        stage2_a = {
            "logical_testcases": [tc_base, tc_second],
            "completeness": {"declared_complete": True, "omissions": []},
        }
        stage2_b = copy.deepcopy(stage2_a)
        stage2_b["logical_testcases"].reverse()
        stage2_b["logical_testcases"][1]["scenario_ids"].reverse()
        stage2_b["logical_testcases"][1]["ac_ids"].reverse()
        job = workflow._job_root(manifest)
        formal_a, testcases_a, _ = staged._enrich_stage2(
            stage2_a, manifest, map_a, sources, spec_fp, 0, response, job)
        formal_b, testcases_b, _ = staged._enrich_stage2(
            stage2_b, manifest, map_a, sources, spec_fp, 0, response, job)
        self.assertNotEqual(testcases_a, testcases_b)
        self.assertNotEqual(formal_a, formal_b)

    def test_noncheckable_scenario_routes_to_spec_issue_without_testcase(self):
        class ObservationProvider(FakeProvider):
            @staticmethod
            def stage1():
                value = FakeProvider.stage1()
                for item in [*value["scenarios"],
                             *value["acceptance_criteria"]]:
                    item["status"] = "SPEC_AMBIGUITY"
                    item["reason"] = "Spec does not define the oracle."
                return value

        generator = ObservationProvider()
        reviewer = FakeReviewerProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        submission = self.project_input()
        checkpoint = workflow.start(submission)
        job = self.root / "result/jobs" / submission["job_id"]
        form = load_document(job / checkpoint["owner_review_path"])
        invalid = self.completed_owner_review(
            form, "AC_TESTCASE_MAP_AND_TESTCASE", "")
        with self.assertRaises(ProjectJobError) as caught:
            workflow.route_scenarios(submission, invalid)
        self.assertEqual("INVALID_OWNER_ROUTING", caught.exception.code)
        result = workflow.route_scenarios(
            submission, self.completed_owner_review(
                form, "SPEC_AGENT",
                "Spec Owner must define an authoritative oracle."))
        self.assertEqual("SPEC_ISSUES_RECORDED", result["state"])
        self.assertFalse(result["full_spec_coverage_complete"])
        self.assertEqual(1, generator.calls)
        self.assertEqual(0, reviewer.calls)
        self.assertTrue((
            job / "staging/mappings/scenario_spec_issues.r000.json").is_file())
        self.assertFalse((
            job / "staging/mappings/ac_testcase_map.r000.json").exists())

    def test_checked_scenario_continues_while_spec_issue_is_preserved(self):
        class MixedProvider(FakeProvider):
            @staticmethod
            def stage1():
                value = FakeProvider.stage1()
                evidence = [_spec_range(SPEC, "Acceptance:")]
                value["scenarios"].append({
                    "objective": "Route the missing oracle contract.",
                    "verification_level": "UNIT",
                    "status": "SPEC_AMBIGUITY",
                    "reason": "Spec does not define this oracle.",
                    "spec_evidence": copy.deepcopy(evidence),
                })
                value["acceptance_criteria"].append({
                    "scenario_indexes": [1],
                    "behavior": "An unspecified behavior cannot be invented.",
                    "verification_level": "UNIT",
                    "status": "SPEC_AMBIGUITY",
                    "reason": "Spec does not define this oracle.",
                    "spec_evidence": copy.deepcopy(evidence),
                })
                return value

        generator = MixedProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator,
            FakeReviewerProvider())
        submission = self.project_input()
        checkpoint = workflow.start(submission)
        job = self.root / "result/jobs" / submission["job_id"]
        form = load_document(job / checkpoint["owner_review_path"])
        routing = self.completed_owner_review(
            form, "SPEC_AGENT", "Spec needs clarification.")
        for route in routing["scenarios"]:
            if route["status"] == "CHECKABLE":
                route["comment"] = ""
                route["routing"]["destination"] = \
                    "AC_TESTCASE_MAP_AND_TESTCASE"
        result = workflow.route_scenarios(submission, routing)
        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        self.assertTrue(result["checked_testcases_complete"])
        self.assertFalse(result["full_spec_coverage_complete"])
        spec_issues = load_document(
            job / result["scenario_partition_paths"]["spec_issues"])
        self.assertEqual(
            ["SCENARIO.0002"],
            spec_issues["scenario_ids"])
        map2 = load_document(job / result["ac_testcase_map_path"])
        self.assertEqual(
            ["AC.0001"], map2["completeness"]["ac_ids"])
        self.assertEqual(3, generator.calls)

    def test_three_spec_only_calls_review_gate_and_exact_replay(self):
        generator = FakeProvider()
        reviewer = FakeReviewerProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        checkpoint = self.start_checked(workflow)
        self.assertEqual("AWAITING_HUMAN_REVIEW", checkpoint["state"])
        self.assertEqual(3, generator.calls)
        self.assertEqual(1, reviewer.calls)
        self.assertEqual(checkpoint, workflow.start(self.project_input()))
        self.assertEqual(3, generator.calls)
        self.assertEqual(1, reviewer.calls)
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        manifests = sorted(job.glob("transcripts/*/*/manifest.json"))
        self.assertEqual(4, len(manifests))
        self.assertEqual(
            {"STAGE_1", "STAGE_2", "STAGE_3", "REVIEWER"},
            {load_document(path)["role"] for path in manifests})
        before = {
            path.relative_to(job).as_posix():
                (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted((job / "transcripts").rglob("*.json"))}
        self.assertEqual(checkpoint, workflow.start(self.project_input()))
        after = {
            path.relative_to(job).as_posix():
                (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted((job / "transcripts").rglob("*.json"))}
        self.assertEqual(before, after)
        self.assertEqual(3, generator.calls)
        self.assertEqual(1, reviewer.calls)
        expected_tools = {
            "SCENARIO_AC_MAP": "submit_scenario_ac_candidate",
            "AC_TESTCASE_MAP": "submit_ac_testcase_candidate",
            "TESTCASE": "submit_portable_sv_testcase_candidate",
        }
        for request in generator.requests:
            stage = request["metadata"]["stage"]
            self.assertEqual("SELECT_TOOLS", request["operation"])
            self.assertEqual("REQUIRED", request["tool_choice_policy"])
            self.assertEqual(
                {
                    expected_tools[stage],
                    "submit_project_stage_blocked",
                },
                set(request["legal_tool_names"]))
            self.assertEqual(2, len(request["tools"]))
            candidate_tool = next(
                tool for tool in request["tools"]
                if tool["name"] == expected_tools[stage])

            self.assertFalse(
                {"job_id", "provider", "artifact_fingerprint"} &
                set(candidate_tool["input_schema"]["properties"]))
            payload = json.loads(request["messages"][1]["content"])
            if stage in {"SCENARIO_AC_MAP", "AC_TESTCASE_MAP"}:
                for source in payload["spec_evidence"]:
                    self.assertEqual({"path", "lines"}, set(source))
                    self.assertEqual(
                        [
                            {"line_number": line_number, "text": text}
                            for line_number, text in enumerate(
                                SPEC.splitlines(), start=1)
                        ],
                        source["lines"])
                self.assertNotIn(
                    "schema_version",
                    candidate_tool["input_schema"]["properties"])
                self.assertTrue(
                    candidate_tool["input_schema"]["$id"].endswith("4.0"))
                evidence = (
                    candidate_tool["input_schema"]["$defs"]["evidence"])
                self.assertEqual(
                    {"path", "line_start", "line_end"},
                    set(evidence["properties"]))
            else:
                self.assertEqual(
                    "urn:dv:project:portable-sv-testcase-candidate:9.0",
                    candidate_tool["input_schema"]["$id"])
                self.assertEqual(
                    {"code_units", "assembly", "implemented_testcase_ids"},
                    set(candidate_tool["input_schema"]["properties"]))
                self.assertNotIn("spec_evidence", payload)
                self.assertIn("scenario_ac_map", payload)
                self.assertIn("ac_testcase_map", payload)
        review_schema = reviewer.requests[0]["tools"][0]["input_schema"]
        self.assertEqual(
            "urn:dv:project:testcase-review-candidate:6.0",
            review_schema["$id"])
        self.assertEqual(
            {"content"},
            set(review_schema["$defs"]["code_evidence"]["properties"]))
        requests = generator.requests + reviewer.requests
        serialized = json.dumps(requests, sort_keys=True)
        self.assertNotIn("tiny.sv", serialized)
        self.assertNotIn("assign y = clk", serialized)
        self.assertNotIn('"rtl"', serialized.casefold())
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        map1 = load_document(
            job / "staging/mappings/scenario_ac_map.r000.json")
        map2 = load_document(
            job / "staging/mappings/ac_testcase_map.r000.json")
        candidate = load_document(
            job /
            "staging/generated/portable_sv/testcase.r000.json")
        self.assertTrue(accepted(validate("scenario_ac_map", map1)))
        self.assertTrue(accepted(validate("ac_testcase_map", map2)))
        self.assertTrue(accepted(validate(
            "project_testcase_candidate", candidate)))
        stage3_raw = load_document(
            job / "audit/pj002_provider_response.stage3.r000.json")[
                "tool_calls"][0]["arguments"]
        self.assertTrue(accepted(validate(
            "portable_sv_testcase_candidate", stage3_raw)))
        self.assertNotIn("implemented_ac_evidence", stage3_raw)
        self.assertNotIn("implemented_ac_evidence", candidate)
        legacy_stage3 = copy.deepcopy(stage3_raw)
        legacy_stage3["implemented_ac_evidence"] = []
        self.assertFalse(accepted(validate(
            "portable_sv_testcase_candidate", legacy_stage3)))
        review_raw = load_document(
            job / "audit/pj002_provider_response.review.r001.json")[
                "tool_calls"][0]["arguments"]
        self.assertTrue(accepted(validate(
            "project_testcase_review_candidate", review_raw)))
        legacy_review = copy.deepcopy(review_raw)
        legacy_review["ac_reviews"][0]["checker_evidence"][0] = {
            "line_start": 1, "line_end": 1}
        self.assertFalse(accepted(validate(
            "project_testcase_review_candidate", legacy_review)))
        self.assertEqual(
            candidate["candidate_fingerprint"],
            checkpoint["bundle_fingerprints"]["testcase"])
        self.assertNotIn("eda_eligibility", checkpoint)
        self.assertFalse((job / "approved").exists())
        self.assertTrue((job / "runs/stage3").is_dir())
        self.assertEqual(1, len(list(job.glob(
            "audit/stage3_eda_evidence.*.json"))))
        self.assertFalse(any(
            "FIRST_RUN.R1" in str(path)
            for path in (self.root / "result").rglob("*")))

    def test_initial_stage2_candidate_correction_is_append_only_and_replayed(self):
        class CorrectingProvider(FakeProvider):
            def select_tools(self, request):
                response = super().select_tools(request)
                if (request["metadata"]["stage"] == "AC_TESTCASE_MAP" and
                        request["metadata"].get(
                            "candidate_correction_attempt") is None):
                    response["tool_calls"][0]["arguments"]["maxItems"] = 2048
                return response

        generator = CorrectingProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator,
            FakeReviewerProvider())
        submission = self.project_input()
        first = workflow.start(submission)
        job = self.root / "result/jobs" / submission["job_id"]
        original_path = job / "audit/pj002_provider_response.stage2.r000.json"
        self.assertEqual("AWAITING_SCENARIO_ROUTING", first["state"])
        result = self.start_checked(workflow, submission)
        original_bytes = original_path.read_bytes()
        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        self.assertEqual(4, generator.calls)
        self.assertEqual(original_bytes, original_path.read_bytes())
        original = load_document(original_path)
        self.assertEqual(
            2048, original["tool_calls"][0]["arguments"]["maxItems"])
        correction_request = load_document(
            job / "staging/requests/stage2.r000.correction001.json")
        correction_response = load_document(
            job / "audit/pj002_provider_response.stage2.r000.correction001.json")
        self.assertNotIn(
            "maxItems", correction_response["tool_calls"][0]["arguments"])
        feedback = list(job.glob(
            "audit/pj002_candidate_correction_feedback.*.json"))
        rejection = list(job.glob(
            "audit/pj002_candidate_correction_rejection.*.json"))
        self.assertEqual(1, len(feedback))
        self.assertEqual(1, len(rejection))
        feedback_value = load_document(feedback[0])
        self.assertIn("maxItems is an array schema constraint",
                      feedback_value["required_action"])
        self.assertEqual(
            canonical_hash(load_document(
                job / "staging/requests/stage2.r000.json")),
            correction_request["metadata"]["original_request_fingerprint"])
        replay = workflow.start(submission)
        self.assertEqual(result, replay)
        self.assertEqual(4, generator.calls)

    def test_candidate_correction_rejects_unrelated_semantic_drift(self):
        class DriftingProvider(FakeProvider):
            def select_tools(self, request):
                response = super().select_tools(request)
                if request["metadata"]["stage"] == "AC_TESTCASE_MAP":
                    candidate = response["tool_calls"][0]["arguments"]
                    if request["metadata"].get(
                            "candidate_correction_attempt") is None:
                        candidate["maxItems"] = 2048
                    elif request["metadata"].get(
                            "candidate_correction_attempt") == 1:
                        candidate["logical_testcases"][0]["objective"] = \
                            "Unrelated rewritten objective."
                return response

        generator = DriftingProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator,
            FakeReviewerProvider())
        submission = self.project_input()
        checkpoint = workflow.start(submission)
        job = self.root / "result/jobs" / submission["job_id"]
        form = load_document(job / checkpoint["owner_review_path"])
        with self.assertRaises(ProjectJobError) as caught:
            workflow.route_scenarios(
                submission, self.completed_owner_review(
                    form, "AC_TESTCASE_MAP_AND_TESTCASE", ""))
        self.assertEqual(
            "ATTEMPT_PAUSED", caught.exception.code)
        self.assertEqual(3, generator.calls)
        self.assertEqual(1, len(list(job.glob(
            "audit/pj002_candidate_attempt_paused.*.json"))))
        result = workflow.route_scenarios(
            submission, self.completed_owner_review(
                form, "AC_TESTCASE_MAP_AND_TESTCASE", ""))
        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        self.assertTrue((job /
            "audit/pj002_provider_response.stage2.r000.correction002.json"
        ).is_file())
        self.assertEqual(5, generator.calls)

    def test_contract_correction_accepts_slash_separator_expansion(self):
        self.assertTrue(StagedProjectWorkflow._separator_equivalent(
            {"failure_condition": (
                "a burst is generated/required; hierarchical/internal state")},
            {"failure_condition": (
                "a burst is generated or required; "
                "hierarchical or internal state")}))
        self.assertFalse(StagedProjectWorkflow._separator_equivalent(
            {"objective": "verify original behavior"},
            {"objective": "verify unrelated rewritten behavior"}))

    def test_initial_stage1_and_stage3_each_correct_only_once(self):
        class CorrectingStageProvider(FakeProvider):
            def __init__(self, bad_stage):
                super().__init__()
                self.bad_stage = bad_stage

            def select_tools(self, request):
                response = super().select_tools(request)
                if (request["metadata"]["stage"] == self.bad_stage and
                        request["metadata"].get(
                            "candidate_correction_attempt") is None):
                    response["tool_calls"][0]["arguments"]["maxItems"] = 1
                return response

        for stage, suffix in (
                ("SCENARIO_AC_MAP", "STAGE1"),
                ("TESTCASE", "STAGE3")):
            with self.subTest(stage=stage):
                generator = CorrectingStageProvider(stage)
                workflow = ProjectJobWorkflow(
                    self.root, self.root / "result", generator,
                    FakeReviewerProvider())
                submission = self.project_input()
                submission["job_id"] = \
                    "JOB.PROJECT.TINY.CORRECTION.{}".format(suffix)
                result = self.start_checked(workflow, submission)
                self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
                self.assertEqual(4, generator.calls)
                job = self.root / "result/jobs" / submission["job_id"]
                requests = list(job.glob(
                    "staging/requests/*.correction001.json"))
                self.assertEqual(1, len(requests))
                self.assertFalse(list(job.glob(
                    "staging/requests/*.correction002.json")))

    def test_stage3_verilator_failure_regenerates_in_same_run_with_feedback(self):
        generator = Stage3ValidatedRetryProvider(["compile"])
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator,
            FakeReviewerProvider())
        submission = self.project_input()
        submission["job_id"] = "JOB.PROJECT.TINY.STAGE3.COMPILE.RETRY"

        result = self.start_checked(workflow, submission)

        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        self.assertEqual(2, generator.stage3_calls)
        job = self.root / "result/jobs" / submission["job_id"]
        correction = load_document(
            job / "staging/requests/stage3.r000.correction001.json")
        feedback = json.loads(correction["messages"][-1]["content"])[
            "candidate_correction_feedback"]
        self.assertIn("Verilator", feedback["required_action"])
        self.assertEqual(
            "VERILATOR_BUILD_FAILED",
            feedback["validation_diagnostics"][0]["code"])
        self.assertIn(
            "<stage3_testcase>",
            feedback["validation_diagnostics"][0]["message"])
        self.assertEqual(2, len(list(job.glob(
            "audit/stage3_eda_evidence.*.json"))))
        self.assertFalse(list(job.glob(
            "audit/pj002_stage3_evidence_attempt_paused.*.json")))

    def test_each_initial_stage_can_restore_one_missing_contract_field(self):
        class MissingFieldProvider(FakeProvider):
            def __init__(self, bad_stage, missing_field):
                super().__init__()
                self.bad_stage = bad_stage
                self.missing_field = missing_field

            def select_tools(self, request):
                response = super().select_tools(request)
                if (request["metadata"]["stage"] == self.bad_stage and
                        request["metadata"].get(
                            "candidate_correction_attempt") is None):
                    response["tool_calls"][0]["arguments"].pop(
                        self.missing_field)
                return response

        cases = (
            ("SCENARIO_AC_MAP", "completeness", "STAGE1"),
            ("AC_TESTCASE_MAP", "completeness", "STAGE2"),
            ("TESTCASE", "assembly", "STAGE3"),
        )
        for stage, field, suffix in cases:
            with self.subTest(stage=stage, field=field):
                generator = MissingFieldProvider(stage, field)
                workflow = ProjectJobWorkflow(
                    self.root, self.root / "result", generator,
                    FakeReviewerProvider())
                submission = self.project_input()
                submission["job_id"] = \
                    "JOB.PROJECT.TINY.MISSING.{}".format(suffix)
                result = self.start_checked(workflow, submission)
                self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
                self.assertEqual(4, generator.calls)

    def test_each_initial_stage_semantic_error_receives_one_minimal_correction(self):
        class SemanticCorrectionProvider(FakeProvider):
            def __init__(self, bad_stage):
                super().__init__()
                self.bad_stage = bad_stage

            def select_tools(self, request):
                response = super().select_tools(request)
                if (request["metadata"]["stage"] == self.bad_stage and
                        request["metadata"].get(
                            "candidate_correction_attempt") is None):
                    candidate = response["tool_calls"][0]["arguments"]
                    if self.bad_stage == "SCENARIO_AC_MAP":
                        candidate["completeness"]["declared_complete"] = False
                    else:
                        candidate["logical_testcases"][0][
                            "expected_result"] = ""
                return response

        for stage, suffix in (
                ("SCENARIO_AC_MAP", "STAGE1"),
                ("AC_TESTCASE_MAP", "STAGE2")):
            with self.subTest(stage=stage):
                generator = SemanticCorrectionProvider(stage)
                workflow = ProjectJobWorkflow(
                    self.root, self.root / "result", generator,
                    FakeReviewerProvider())
                submission = self.project_input()
                submission["job_id"] = \
                    "JOB.PROJECT.TINY.SEMANTIC.{}".format(suffix)
                result = self.start_checked(workflow, submission)
                self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
                self.assertEqual(4, generator.calls)

        class Stage3SemanticCorrectionProvider(FakeProvider):
            def select_tools(self, request):
                response = super().select_tools(request)
                if (request["metadata"]["stage"] == "TESTCASE" and
                        request["metadata"].get(
                            "candidate_correction_attempt") is None):
                    response["tool_calls"][0]["arguments"][
                        "code_units"][1]["testcase_ids"] = []
                return response

        stage3 = Stage3SemanticCorrectionProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", stage3,
            FakeReviewerProvider())
        submission = self.project_input()
        submission["job_id"] = "JOB.PROJECT.TINY.SEMANTIC.STAGE3"
        result = self.start_checked(workflow, submission)
        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        self.assertEqual(4, stage3.calls)

    def test_candidate_correction_budget_and_tamper_fail_closed(self):
        class InvalidStage1Provider(FakeProvider):
            def select_tools(self, request):
                response = super().select_tools(request)
                if request["metadata"]["stage"] == "SCENARIO_AC_MAP":
                    response["tool_calls"][0]["arguments"]["maxItems"] = 1
                return response

        budgeted = InvalidStage1Provider()
        budget_workflow = ProjectJobWorkflow(
            self.root, self.root / "result", budgeted,
            FakeReviewerProvider())
        budget_workflow.max_total_provider_calls = 1
        budget_submission = self.project_input()
        budget_submission["job_id"] = "JOB.PROJECT.TINY.CORRECTION.BUDGET"
        with self.assertRaises(ProjectJobError) as budget_error:
            budget_workflow.start(budget_submission)
        self.assertEqual("TOOL_LIMIT_EXCEEDED", budget_error.exception.code)
        self.assertEqual(1, budgeted.calls)
        budget_job = self.root / "result/jobs" / budget_submission["job_id"]
        self.assertFalse((budget_job /
            "staging/requests/stage1.r000.correction001.json").exists())

        provider = InvalidStage1Provider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", provider,
            FakeReviewerProvider())
        submission = self.project_input()
        submission["job_id"] = "JOB.PROJECT.TINY.CORRECTION.TAMPER"
        with self.assertRaises(ProjectJobError) as exhausted:
            workflow.start(submission)
        self.assertEqual(
            "ATTEMPT_PAUSED", exhausted.exception.code)
        self.assertEqual(2, provider.calls)
        job = self.root / "result/jobs" / submission["job_id"]
        response_path = job / (
            "audit/pj002_provider_response.stage1.r000.correction001.json")
        tampered = load_document(response_path)
        tampered["request_id"] = "REQUEST.CROSS.JOB"
        response_path.write_text(
            json.dumps(tampered, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        with self.assertRaises(ProjectJobError) as stale:
            workflow.start(submission)
        self.assertEqual("STALE_EVIDENCE", stale.exception.code)
        self.assertEqual(2, provider.calls)

    def test_testcase_content_selection_unique_match_and_fail_closed(self):
        content = (
            "module tb;\n"
            "  logic clk;\n"
            "  clk = 1'b0;\n"
            "  if (clk !== 1'b0) $fatal(1, \"低电平\");\n"
            "endmodule\n")
        selected = _enrich_code_evidence([{
            "content": (
                "  clk = 1'b0;\n"
                "  if (clk !== 1'b0) $fatal(1, \"低电平\");"),
        }], content, ProjectJobError)
        self.assertEqual(3, selected[0]["line_start"])
        self.assertEqual(4, selected[0]["line_end"])
        self.assertTrue(selected[0]["snippet_fingerprint"])
        _validate_code_evidence(
            selected[0], content, "checker", ProjectJobError)

        failures = (
            ("  clk =", "TESTCASE_EVIDENCE_MISMATCH"),
            ("\n  clk = 1'b0;", "TESTCASE_EVIDENCE_MISMATCH"),
            ("  clk = 1'b0;\n", "TESTCASE_EVIDENCE_MISMATCH"),
            ("   ", "TESTCASE_EVIDENCE_MISMATCH"),
        )
        for selection, code in failures:
            with self.subTest(selection=repr(selection)):
                with self.assertRaises(ProjectJobError) as caught:
                    _enrich_code_evidence(
                        [{"content": selection}], content, ProjectJobError,
                        "AC.0001", "STIMULUS")
                self.assertEqual(code, caught.exception.code)
                self.assertEqual(
                    "AC.0001",
                    caught.exception.failure_context["ac_id"])
                self.assertEqual(
                    "STIMULUS",
                    caught.exception.failure_context["evidence_kind"])
                self.assertEqual(
                    0, caught.exception.failure_context["match_count"])
                self.assertTrue(caught.exception.failure_context[
                    "required_correction"])

        duplicate = "  clk = 1'b0;\n  clk = 1'b0;\n"
        with self.assertRaises(ProjectJobError) as caught:
            _enrich_code_evidence(
                [{"content": "  clk = 1'b0;"}],
                duplicate, ProjectJobError)
        self.assertEqual(
            "AMBIGUOUS_TESTCASE_EVIDENCE", caught.exception.code)
        self.assertEqual(2, caught.exception.failure_context["match_count"])
        self.assertEqual(
            "  clk = 1'b0;",
            caught.exception.failure_context["offending_content"])

        comment = _enrich_code_evidence(
            [{"content": "// clk = 1'b0;"}],
            "// clk = 1'b0;\n", ProjectJobError)[0]
        with self.assertRaises(ProjectJobError) as caught:
            _validate_code_evidence(
                comment, "// clk = 1'b0;\n", "stimulus", ProjectJobError)
        self.assertEqual("METADATA_ONLY_COVERAGE", caught.exception.code)

        stimulus = _enrich_code_evidence(
            [{"content": "  logic clk;"}], content, ProjectJobError)[0]
        _validate_code_evidence(
            stimulus, content, "stimulus", ProjectJobError)

        no_checker = _enrich_code_evidence(
            [{"content": "  clk = 1'b0;"}], content, ProjectJobError)[0]
        _validate_code_evidence(
            no_checker, content, "checker", ProjectJobError)

        canonical = _enrich_code_evidence([
            {"content": "  clk = 1'b0;"},
            {"content": "  clk = 1'b0;"},
        ], content, ProjectJobError)
        self.assertEqual(1, len(canonical))
        self.assertEqual(3, canonical[0]["line_start"])

    def test_stage3_candidate_does_not_own_ac_evidence(self):
        generator = SharedCheckerProvider()
        reviewer = SharedCheckerReviewer()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        submission = self.project_input()

        result = self.start_checked(workflow, submission)

        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        job = self.root / "result/jobs" / submission["job_id"]
        candidate = load_document(
            job / "staging/generated/portable_sv/testcase.r000.json")
        self.assertNotIn("implemented_ac_evidence", candidate)
        report = load_document(job / result["review_report_path"])
        self.assertEqual(2, len(report["ac_reviews"]))
        self.assertEqual(3, generator.calls)
        self.assertEqual(1, reviewer.calls)
        self.assertFalse((job / "approved").exists())
        self.assertTrue((job / "runs/stage3").is_dir())

    def test_stage3_allows_one_testcase_code_unit(self):
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", SingleCodeUnitProvider(),
            FakeReviewerProvider())

        result = self.start_checked(workflow)

        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        candidate = load_document(
            job / "staging/generated/portable_sv/testcase.r000.json")
        self.assertEqual(1, len(candidate["code_units"]))
        self.assertEqual("TESTCASE", candidate["code_units"][0]["role"])
        stage3_index = load_document(
            job / "staging/units/stage3/index.current.r000.json")
        self.assertEqual(1, stage3_index["completeness"]["unit_count"])

    def test_stage3_does_not_guess_timeout_from_numeric_delays(self):
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", CounterTimeoutProvider(),
            FakeReviewerProvider())

        result = self.start_checked(workflow)

        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        candidate = load_document(
            self.root / "result/jobs/JOB.PROJECT.TINY.001/staging/"
            "generated/portable_sv/testcase.r000.json")
        self.assertNotIn("BOUNDED_TIMEOUT", candidate["validation"]["checks"])
        self.assertIn("while (timeout_count < 20)", candidate["content"])
        self.assertIn("always #CLK_HALF", candidate["content"])

    def test_stage2_still_rejects_empty_expected_result_before_stage3(self):
        generator = EmptyExpectedResultProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator,
            FakeReviewerProvider())

        with self.assertRaises(ProjectJobError) as caught:
            self.start_checked(workflow)

        self.assertEqual(
            "ATTEMPT_PAUSED", caught.exception.code)
        self.assertEqual(3, generator.calls)

    def test_stage3_testcase_mapping_overreach_still_fails_closed(self):
        generator = TestcaseMappingOverreachProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator,
            FakeReviewerProvider())

        with patch("core.project_staged.ProjectVerilatorRunner") as runner:
            with self.assertRaises(ProjectJobError) as caught:
                self.start_checked(workflow)

        self.assertEqual(
            "ATTEMPT_PAUSED", caught.exception.code)
        self.assertEqual(4, generator.calls)
        runner.assert_not_called()

    def test_stage3_clock_semantics_are_not_a_framework_regex_gate(self):
        class NoClockProvider(FakeProvider):
            @staticmethod
            def stage3(payload):
                result = FakeProvider.stage3(payload)
                shared = result["code_units"][0]
                shared["content"] = shared["content"].replace(
                    "  always #5 clk = ~clk;\n", "")
                return result

        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", NoClockProvider(),
            FakeReviewerProvider())

        result = self.start_checked(workflow)

        self.assertEqual("AWAITING_HUMAN_REVIEW", result["state"])
        candidate = load_document(
            self.root / "result/jobs/JOB.PROJECT.TINY.001/staging/"
            "generated/portable_sv/testcase.r000.json")
        self.assertNotIn("CLOCK", candidate["validation"]["checks"])
        self.assertNotIn("TESTBENCH_TOP", candidate["validation"]["checks"])

    def test_post_response_failure_resumes_same_job_without_provider_replay(
            self):
        generator = FakeProvider()
        reviewer = FakeReviewerProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        original = __import__(
            "core.project_staged", fromlist=["_raw_generation"]
        )._raw_generation

        def fail_after_response(response, stage, error):
            if stage == "SCENARIO_AC_MAP":
                raise ProjectJobError(
                    "DEVELOPER_LOGIC_ERROR",
                    "simulated failure after immutable provider response")
            return original(response, stage, error)

        with patch("core.project_staged._raw_generation", fail_after_response):
            with self.assertRaises(ProjectJobError) as caught:
                workflow.start(self.project_input())
        self.assertEqual("DEVELOPER_LOGIC_ERROR", caught.exception.code)
        self.assertEqual(1, generator.calls)

        original_request = StagedProjectWorkflow._request

        def changed_prompt(staged, *args, **kwargs):
            replay = original_request(staged, *args, **kwargs)
            replay["messages"][0]["content"] += (
                " Framework prompt corrected after the failed run.")
            return replay

        with patch.object(
                StagedProjectWorkflow, "_request", changed_prompt):
            checkpoint = workflow.start(self.project_input())

        self.assertEqual("AWAITING_SCENARIO_ROUTING", checkpoint["state"])
        self.assertEqual(1, generator.calls)
        self.assertEqual(1, generator.probe_calls)
        self.assertEqual(0, reviewer.probe_calls)
        self.assertEqual(1, generator.restore_calls)
        self.assertEqual(0, reviewer.restore_calls)
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        self.assertTrue((
            job / "staging/mappings/scenario_ac_map.r000.json").is_file())

    def test_post_response_recovery_rejects_cached_identity_tamper(self):
        generator = FakeProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator,
            FakeReviewerProvider())

        with patch(
                "core.project_staged._raw_generation",
                side_effect=ProjectJobError(
                    "DEVELOPER_LOGIC_ERROR",
                    "simulated failure after immutable provider response")):
            with self.assertRaises(ProjectJobError):
                workflow.start(self.project_input())

        response_path = self.root / (
            "result/jobs/JOB.PROJECT.TINY.001/audit/"
            "pj002_provider_response.stage1.r000.json")
        response = load_document(response_path)
        response["request_id"] = "REQUEST.CROSS.JOB"
        response_path.write_text(
            json.dumps(response, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        with self.assertRaises(ProjectJobError) as caught:
            workflow.start(self.project_input())

        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        self.assertEqual(1, generator.calls)

    def test_post_response_recovery_rejects_transcript_event_tamper(self):
        generator = FakeProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator,
            FakeReviewerProvider())

        with patch(
                "core.project_staged._raw_generation",
                side_effect=ProjectJobError(
                    "DEVELOPER_LOGIC_ERROR",
                    "simulated failure after immutable provider response")):
            with self.assertRaises(ProjectJobError):
                workflow.start(self.project_input())

        response_path = self.root / (
            "result/jobs/JOB.PROJECT.TINY.001/transcripts/stage1/"
            "INITIAL.STAGE1.R000/0002.response.json")
        response_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ProjectJobError) as caught:
            workflow.start(self.project_input())

        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        self.assertEqual(1, generator.calls)

    def test_generator_and_reviewer_may_share_configured_identity(self):
        submission = self.project_input()
        submission["job_id"] = "JOB.PROJECT.TINY.SHARED.MODEL"
        profile_path = self.root / submission["agent_profile"]
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        profile["review"]["initial"] = profile["initial"]["stage1"]
        profile["review"]["final"] = profile["initial"]["stage1"]
        profile_path.write_text(
            yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        generator = FakeProvider()
        reviewer = FakeReviewerProvider()
        reviewer.provider_id = generator.provider_id
        reviewer.model_id = generator.model_id
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)

        checkpoint = self.start_checked(workflow, submission)

        self.assertEqual("AWAITING_HUMAN_REVIEW", checkpoint["state"])
        self.assertEqual(generator.provider_id, reviewer.provider_id)
        self.assertEqual(generator.model_id, reviewer.model_id)

    def test_generator_and_reviewer_may_share_provider_instance(self):
        provider = FakeProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", provider, provider)
        self.assertIs(workflow.provider, workflow.reviewer_provider)

    def test_generation_tool_response_shape_and_candidate_schema_fail_closed(
            self):
        expected = {
            "text_only": "ATTEMPT_PAUSED",
            "duplicate_call": "ATTEMPT_PAUSED",
            "wrong_name": "ATTEMPT_PAUSED",
            "extra_field": "ATTEMPT_PAUSED",
            "missing_field": "ATTEMPT_PAUSED",
            "non_object_arguments": "ATTEMPT_PAUSED",
            "model_snippet": "ATTEMPT_PAUSED",
            "model_fingerprint": "ATTEMPT_PAUSED",
            "unknown_path": "ATTEMPT_PAUSED",
            "zero_line": "ATTEMPT_PAUSED",
            "negative_line": "ATTEMPT_PAUSED",
            "reversed_line": "ATTEMPT_PAUSED",
            "out_of_range": "ATTEMPT_PAUSED",
            "missing_evidence": "ATTEMPT_PAUSED",
            "legacy_candidate": "ATTEMPT_PAUSED",
            "duplicate_local_ref": "ATTEMPT_PAUSED",
            "unknown_local_ref": "ATTEMPT_PAUSED",
            "false_completeness": "ATTEMPT_PAUSED",
        }
        for mode, code in expected.items():
            with self.subTest(mode=mode):
                provider = MalformedGenerationProvider(mode)
                reviewer = FakeReviewerProvider()
                workflow = ProjectJobWorkflow(
                    self.root, self.root / "result", provider, reviewer)
                submission = self.project_input()
                submission["job_id"] = \
                    "JOB.PROJECT.TINY.MALFORMED.{}".format(mode.upper())
                with self.assertRaises(ProjectJobError) as caught:
                    workflow.start(submission)
                self.assertEqual(code, caught.exception.code)
                self.assertEqual(2, provider.calls)
                self.assertEqual(0, reviewer.calls)
                job = self.root / "result/jobs" / submission["job_id"]
                self.assertFalse((
                    job /
                    "staging/mappings/scenario_ac_map.r000.json").exists())

    def test_line_addressed_multiline_markdown_unicode_and_duplicate_text(self):
        source_text = (
            "# Tiny – 行为\n"
            "\n"
            "**Repeated evidence**\n"
            "相同文本\n"
            "相同文本\n"
            "Acceptance: output `y` shall equal sampled `clk`.\n"
        )
        (self.root / "spec.md").write_text(source_text, encoding="utf-8")

        class LineRangeProvider(FakeProvider):
            @staticmethod
            def stage1():
                value = FakeProvider.stage1()
                evidence = {
                    "path": "spec.md", "line_start": 1, "line_end": 5}
                value["scenarios"][0]["spec_evidence"] = [evidence]
                value["acceptance_criteria"][0]["spec_evidence"] = [
                    copy.deepcopy(evidence)]
                return value

            def stage2(self):
                value = super().stage2()
                value["logical_testcases"][0]["spec_evidence"] = [{
                    "path": "spec.md", "line_start": 4, "line_end": 5}]
                return value

        generator = LineRangeProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator,
            FakeReviewerProvider())
        checkpoint = self.start_checked(workflow)
        self.assertEqual("AWAITING_HUMAN_REVIEW", checkpoint["state"])

        expected_lines = [
            {"line_number": index, "text": text}
            for index, text in enumerate(source_text.splitlines(), start=1)]
        for request in generator.requests:
            payload = json.loads(request["messages"][1]["content"])
            if request["metadata"]["stage"] == "TESTCASE":
                self.assertNotIn("spec_evidence", payload)
                self.assertTrue(payload["scenario_ac_map"]["scenarios"][0][
                    "spec_evidence"])
            else:
                self.assertEqual(
                    [{"path": "spec.md", "lines": expected_lines}],
                    payload["spec_evidence"])

        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        map1 = load_document(
            job / "staging/mappings/scenario_ac_map.r000.json")
        selected = "\n".join(source_text.splitlines()[0:5])
        evidence = map1["scenarios"][0]["spec_evidence"][0]
        self.assertEqual(selected, evidence["snippet"])
        self.assertEqual(
            hashlib.sha256(selected.encode("utf-8")).hexdigest(),
            evidence["snippet_fingerprint"])
        map2 = load_document(
            job / "staging/mappings/ac_testcase_map.r000.json")
        duplicate = map2["logical_testcases"][0]["spec_evidence"][0]
        self.assertEqual("相同文本\n相同文本", duplicate["snippet"])
        self.assertEqual(4, duplicate["line_start"])
        self.assertEqual(5, duplicate["line_end"])

    def test_range_enrichment_rejects_invalid_empty_and_oversized_evidence(self):
        sources = {
            "spec.md": "first\n\nlast",
            "large.md": "x" * 4097,
        }
        cases = (
            ([{"path": "missing.md", "line_start": 1, "line_end": 1}],
             "UNKNOWN_SPEC_REFERENCE"),
            ([{"path": "spec.md", "line_start": 0, "line_end": 1}],
             "SPEC_EVIDENCE_MISMATCH"),
            ([{"path": "spec.md", "line_start": -1, "line_end": 1}],
             "SPEC_EVIDENCE_MISMATCH"),
            ([{"path": "spec.md", "line_start": 3, "line_end": 2}],
             "SPEC_EVIDENCE_MISMATCH"),
            ([{"path": "spec.md", "line_start": 4, "line_end": 4}],
             "SPEC_EVIDENCE_MISMATCH"),
            ([{"path": "spec.md", "line_start": 2, "line_end": 2}],
             "SPEC_EVIDENCE_MISMATCH"),
            ([{"path": "large.md", "line_start": 1, "line_end": 1}],
             "FILE_LIMIT_EXCEEDED"),
            ([], "MISSING_SPEC_EVIDENCE"),
            ([{
                "path": "spec.md", "line_start": 1, "line_end": 1,
                "snippet": "model-authored",
            }], "INVALID_MAPPING"),
            ([{
                "path": "spec.md", "line_start": 1, "line_end": 1,
                "snippet_fingerprint": "0" * 64,
            }], "INVALID_MAPPING"),
        )
        for evidence, expected in cases:
            with self.subTest(expected=expected, evidence=evidence):
                with self.assertRaises(ProjectJobError) as caught:
                    _enrich_evidence(evidence, sources, ProjectJobError)
                self.assertEqual(expected, caught.exception.code)

    def test_stage2_review_defect_invalidates_only_stage2_and_downstream(self):
        generator = FakeProvider()
        reviewer = FakeReviewerProvider("AC_TESTCASE_MAP")
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        checkpoint = self.start_checked(workflow)
        self.assertEqual("AWAITING_REPAIR_PLAN", checkpoint["state"])
        stages = [item["metadata"]["stage"] for item in generator.requests]
        self.assertEqual(1, stages.count("SCENARIO_AC_MAP"))
        self.assertEqual(1, stages.count("AC_TESTCASE_MAP"))
        self.assertEqual(1, stages.count("TESTCASE"))
        self.assertEqual(1, reviewer.calls)

    def test_spec_ambiguity_fails_closed_but_job_remains_retryable(self):
        generator = FakeProvider(ambiguity_stage="TESTCASE")
        reviewer = FakeReviewerProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        with self.assertRaises(ProjectJobError) as caught:
            self.start_checked(workflow)
        self.assertEqual("SPEC_AMBIGUITY", caught.exception.code)
        self.assertEqual(0, reviewer.calls)
        with self.assertRaises(ProjectJobError) as replay:
            workflow.start(self.project_input())
        self.assertEqual("SPEC_AMBIGUITY", replay.exception.code)
        self.assertEqual(3, generator.calls)
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        failure_paths = list(job.glob("audit/pj002_paused.*.json"))
        self.assertEqual(1, len(failure_paths))
        failure = load_document(failure_paths[0])
        self.assertEqual("PAUSED_RETRYABLE", failure["state"])
        self.assertEqual("SPEC_AMBIGUITY", failure["diagnostic"]["code"])
        self.assertFalse((
            job / "audit/pj002_terminal_checkpoint.json").exists())
        self.assertFalse((job / "approved").exists())
        self.assertFalse((job / "runs").exists())

    def test_no_rtl_gate_rejects_injected_field_before_call(self):
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result")
        manifest = workflow.bootstrap(self.project_input())
        request = {
            "messages": [{"role": "USER", "content": "Spec only"}],
            "metadata": {"rtl_path": "tiny.sv"},
        }
        with self.assertRaises(ProjectJobError) as caught:
            inspect_no_rtl_request(
                request, manifest, self.root, ProjectJobError)
        self.assertEqual("RTL_EVIDENCE_FORBIDDEN", caught.exception.code)
        exact = {
            "messages": [{
                "role": "USER",
                "content": (self.root / "tiny.sv").read_text(
                    encoding="utf-8"),
            }],
            "metadata": {"kind": "untrusted_data"},
        }
        with self.assertRaises(ProjectJobError) as caught:
            inspect_no_rtl_request(
                exact, manifest, self.root, ProjectJobError)
        self.assertEqual("RTL_EVIDENCE_FORBIDDEN", caught.exception.code)

    def test_deterministic_sharding_and_missing_shard_fail_closed(self):
        generator = FakeProvider(duplicate_testcases=True)
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator,
            FakeReviewerProvider(), max_mapping_items_per_shard=1)
        self.start_checked(workflow)
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        index = load_document(
            job / "staging/mappings/ac_testcase_map.r000.json")
        self.assertEqual("SHARDED", index["storage"])
        self.assertEqual(2, len(index["shards"]))
        self.assertEqual([], index["logical_testcases"])
        (job / index["shards"][0]["path"]).unlink()
        (job / "audit/oches001_human_review_checkpoint.json").unlink()
        with self.assertRaises(ProjectJobError) as caught:
            workflow.start(self.project_input())
        self.assertEqual("PARTIAL_ARTIFACT", caught.exception.code)

    def test_provider_budget_resets_for_a_new_process_run(self):
        limited = ProjectJobWorkflow(
            self.root, self.root / "result",
            FakeProvider(), FakeReviewerProvider(),
            max_total_provider_calls=2)
        with self.assertRaises(ProjectJobError) as caught:
            self.start_checked(limited)
        self.assertEqual("TOOL_LIMIT_EXCEEDED", caught.exception.code)
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        self.assertTrue((
            job / "staging/mappings/ac_testcase_map.r000.json").is_file())
        self.assertTrue((
            job / "staging/generated/portable_sv/testcase.r000.json").exists())

    def test_local_provider_request_error_keeps_its_real_code(self):
        class InvalidRequestProvider(FakeProvider):
            def select_tools(self, _request):
                error = RuntimeError("local adapter contract rejected request")
                error.safe_failure_code = "INVALID_PROVIDER_REQUEST"
                raise error

        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", InvalidRequestProvider(),
            FakeReviewerProvider())
        with self.assertRaises(ProjectJobError) as caught:
            workflow._complete(
                "initial.stage3", {"operation": "SELECT_TOOLS"},
                workflow._budget())
        self.assertEqual("INVALID_PROVIDER_REQUEST", caught.exception.code)

    def test_terminal_bundle_tamper_fails_closed_without_provider_replay(self):
        generator = FakeProvider()
        reviewer = FakeReviewerProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", generator, reviewer)
        checkpoint = self.start_checked(workflow)
        job = self.root / "result/jobs/JOB.PROJECT.TINY.001"
        path = job / checkpoint["scenario_ac_map_path"]
        value = load_document(path)
        value["acceptance_criteria"][0]["behavior"] = "tampered"
        path.write_text(
            json.dumps(value, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        with self.assertRaises(ProjectJobError) as caught:
            workflow.start(self.project_input())
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        self.assertEqual(3, generator.calls)
        self.assertEqual(1, reviewer.calls)

    def test_token_file_time_and_repair_budgets_fail_closed(self):
        token_limited = ProjectJobWorkflow(
            self.root, self.root / "result",
            FakeProvider(), FakeReviewerProvider(),
            max_total_tokens=20)
        with self.assertRaises(ProjectJobError) as caught:
            token_limited.start(self.project_input())
        self.assertEqual("TOOL_LIMIT_EXCEEDED", caught.exception.code)

        # Each budget case uses a distinct immutable Job/workspace.
        for suffix, provider, reviewer, kwargs, expected in (
            ("FILE", FakeProvider(), FakeReviewerProvider(),
             {"max_staged_file_bytes": 1024}, "FILE_LIMIT_EXCEEDED"),
            ("TIME", SlowProvider(), FakeReviewerProvider(),
             {"max_elapsed_seconds": 1}, "TOOL_LIMIT_EXCEEDED"),
        ):
            root = self.root / suffix.casefold()
            root.mkdir()
            (root / "spec.md").write_text(SPEC, encoding="utf-8")
            (root / "tiny.sv").write_text(
                (self.root / "tiny.sv").read_text(encoding="utf-8"),
                encoding="utf-8")
            __import__("shutil").copytree(
                self.root / "config", root / "config")
            submission = self.project_input()
            submission["job_id"] = "JOB.PROJECT.TINY.{}".format(suffix)
            workflow = ProjectJobWorkflow(
                root, root / "result", provider, reviewer, **kwargs)
            with self.subTest(budget=suffix):
                with self.assertRaises(ProjectJobError) as caught:
                    self.start_checked(workflow, submission)
                self.assertEqual(expected, caught.exception.code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
