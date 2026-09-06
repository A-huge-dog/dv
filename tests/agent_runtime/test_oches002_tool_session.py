#!/usr/bin/env python3
"""REF-001 Agent loop and transcript-store qualification."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from contracts.validator import accepted, load_document, validate
from infrastructure.persistence.transcript_store import (
    TranscriptStore, create_transcript_store, transcript_session_dir,
)
from agents.errors import AgentLoopError
from runtime.agent_loop import AgentLoop, AgentLoopPolicy
from scripts.dvlib import canonical_hash


def tool(name):
    return {
        "name": name,
        "description": "Test-only {} tool.".format(name),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ids"],
            "properties": {
                "ids": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string"},
                },
            },
        },
    }


def response(request, calls):
    return {
        "schema_version": "1.0",
        "request_id": request["request_id"],
        "operation": "SELECT_TOOLS",
        "finish_reason": "TOOL_CALLS",
        "content": "完整模型原文 {}".format(request["request_id"]),
        "tool_calls": copy.deepcopy(calls),
        "usage": {"input_tokens": 10, "output_tokens": 3},
        "model_id": "scripted-sol",
        "provider_metadata": {
            "provider_id": "scripted-provider",
            "response_id": "RESPONSE.{}".format(request["request_id"]),
        },
        "diagnostics": [],
    }


class ScriptedProvider:
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def select_tools(self, request):
        if not accepted(validate("provider_request", request)):
            raise AssertionError("session generated an invalid provider request")
        self.requests.append(copy.deepcopy(request))
        if not self.turns:
            raise AssertionError("scripted provider queue exhausted")
        calls = self.turns.pop(0)
        return response(request, calls)


def call(number, name, ids=None):
    return {
        "call_id": "CALL.{:03d}".format(number),
        "name": name,
        "arguments": {"ids": list(ids or ["ID.{:03d}".format(number)])},
    }


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.job_root = Path(self.temp.name) / "JOB.PROJECT.TOOL.001"
        self.job_root.mkdir()
        self.job_id = "JOB.PROJECT.TOOL.001"
        self.initial_messages = [
            {"role": "SYSTEM", "content": "顺序调用一个工具。"},
            {"role": "USER", "content": "仅使用当前 Job 证据。"},
        ]
        self.tool_names = [
            "get_issue", "get_unit", "get_spec_evidence",
            "get_direct_dependencies", "get_budget_status",
            "submit_repair_plan",
        ]
        self.tools = [tool(name) for name in self.tool_names]

    def tearDown(self):
        self.temp.cleanup()

    def session(
            self, session_id, provider, handlers, submitted,
            cancel_requested=None):
        lineage = {"source_report_fingerprint": "a" * 64}
        return AgentLoop(
            provider=provider,
            transcript_store=create_transcript_store(
                job_root=self.job_root, job_id=self.job_id,
                role="ORCHESTRATOR", session_id=session_id,
                lineage=lineage),
            job_id=self.job_id,
            session_id=session_id,
            initial_messages=self.initial_messages,
            tools=self.tools,
            retrieval_handlers=handlers,
            submission_handlers={
                "submit_repair_plan":
                    lambda arguments, _context: submitted(arguments)},
            provider_binding={
                "provider_id": "scripted-provider",
                "model_id": "scripted-sol",
            },
            policy=AgentLoopPolicy(
                role="ORCHESTRATOR",
                retrieval_tools=frozenset(handlers),
                submission_tools=frozenset({"submit_repair_plan"})),
            cancel_requested=cancel_requested,
        )

    def worker_session(
            self, session_id, provider, *, observations=None, actions=None,
            terminals=None, validator=None, clock=None, **budget):
        observations = dict(observations or {})
        actions = dict(actions or {})
        terminals = dict(terminals or {})
        names = list(observations) + list(actions) + list(terminals)
        return AgentLoop(
            provider=provider,
            transcript_store=create_transcript_store(
                job_root=self.job_root, job_id=self.job_id,
                role="UVM_GENERATION", session_id=session_id,
                lineage={"authority_fingerprint": "c" * 64}),
            job_id=self.job_id, session_id=session_id,
            initial_messages=self.initial_messages,
            tools=[tool(name) for name in names],
            observation_handlers=observations,
            action_handlers=actions,
            terminal_handlers=terminals,
            completion_validator=validator,
            provider_binding={
                "provider_id": "scripted-provider",
                "model_id": "scripted-sol",
            },
            policy=AgentLoopPolicy(
                role="UVM_GENERATION",
                observation_tools=frozenset(observations),
                action_tools=frozenset(actions),
                terminal_tools=frozenset(terminals),
                **budget),
            clock=clock,
        )

    def test_three_retrievals_then_one_submission_preserve_raw_history(self):
        provider = ScriptedProvider([
            [call(1, "get_issue", ["ISSUE.B", "ISSUE.A"])],
            [call(2, "get_unit", ["UNIT.A"])],
            [call(3, "get_spec_evidence", ["SPEC.A"])],
            [call(4, "submit_repair_plan", ["PLAN.A"])],
        ])
        executed = []

        def handler(name):
            def run(arguments):
                executed.append((name, copy.deepcopy(arguments)))
                return {
                    "schema_version": "1.0",
                    "tool_name": name,
                    "job_id": self.job_id,
                    "items": [{"identity": arguments["ids"][0]}],
                    "diagnostics": ([{
                        "code": "NOT_FOUND", "identity": arguments["ids"][1],
                    }] if len(arguments["ids"]) > 1 else []),
                    "result_fingerprint": str(len(executed)) * 64,
                }
            return run

        submissions = []
        session = self.session(
            "PLANNING.TOOL.001", provider,
            {name: handler(name) for name in (
                "get_issue", "get_unit", "get_spec_evidence")},
            lambda arguments: submissions.append(copy.deepcopy(arguments)) or {
                "status": "ACCEPTED", "arguments": arguments,
            })
        result = session.run()

        self.assertEqual("ACCEPTED", result["status"])
        self.assertEqual(3, session.retrieval_count)
        self.assertEqual(3, len(executed))
        self.assertEqual(1, len(submissions))
        self.assertEqual(4, len(provider.requests))
        self.assertIn(
            '"code":"NOT_FOUND"',
            provider.requests[1]["messages"][-1]["content"])
        self.assertIn(
            '"tool_name":"get_unit"',
            provider.requests[2]["messages"][-1]["content"])

        directory = self.job_root / "transcripts/orchestrator/PLANNING.TOOL.001"
        manifest = load_document(directory / "manifest.json")
        self.assertTrue(accepted(validate(
            "project_transcript_manifest", manifest)))
        self.assertEqual(list(range(1, 17)), [
            item["sequence"] for item in manifest["entries"]])
        for entry in manifest["entries"]:
            raw = (directory / entry["path"]).read_bytes()
            self.assertEqual(
                hashlib.sha256(raw).hexdigest(),
                entry["content_fingerprint"])
        first_response = load_document(directory / "0002.response.json")
        self.assertEqual(
            "完整模型原文 PLANNING.TOOL.001.TURN.001",
            first_response["content"])
        with self.assertRaises(AgentLoopError) as caught:
            session.run()
        self.assertEqual("INVALID_TOOL_CALL", caught.exception.code)
        self.assertEqual(4, len(provider.requests))

    def test_continuous_worker_repairs_after_fake_eda_failure(self):
        session_id = "UVM.WORKER.CONTINUOUS.001"
        provider = ScriptedProvider([
            [call(1, "read", ["CANDIDATE"])],
            [call(2, "write", ["V1"])],
            [call(3, "fake_eda", ["RUN1"])],
            [call(4, "write", ["V2"])],
            [call(5, "fake_eda", ["RUN2"])],
            [call(6, "finish_task", ["DONE"])],
        ])
        action_contexts = []
        eda_results = iter(("FAIL", "PASS"))

        def write(arguments, context):
            action_contexts.append(copy.deepcopy(context))
            return {"status": "WRITTEN", "version": arguments["ids"][0]}

        def fake_eda(arguments, context):
            action_contexts.append(copy.deepcopy(context))
            return {
                "status": next(eda_results), "run": arguments["ids"][0],
            }

        validations = []

        def validate_completion(arguments, context):
            validations.append((copy.deepcopy(arguments), copy.deepcopy(context)))
            return {"status": "PASS", "completion": "VALIDATED"}

        loop = self.worker_session(
            session_id, provider,
            observations={"read": lambda _arguments: {
                "status": "OBSERVED", "candidate": "EMPTY",
            }},
            actions={"write": write, "fake_eda": fake_eda},
            terminals={"finish_task": lambda _arguments, _context: {
                "status": "REQUESTED",
            }},
            validator=validate_completion,
            max_turns=8, max_tokens=1000, max_actions=5,
            max_time_seconds=30)
        result = loop.run()

        self.assertEqual("PASS", result["status"])
        self.assertEqual(6, loop.turn_count)
        self.assertEqual(4, loop.action_count)
        self.assertEqual(78, loop.tokens_used)
        self.assertEqual(1, len(validations))
        self.assertTrue(all(
            request["metadata"]["session_id"] == session_id
            for request in provider.requests))
        self.assertIn(
            '"candidate":"EMPTY"',
            provider.requests[1]["messages"][-1]["content"])
        self.assertIn(
            '"status":"FAIL"',
            provider.requests[3]["messages"][-1]["content"])
        self.assertEqual([
            "{}.ACTION.{:03d}".format(session_id, number)
            for number in range(1, 5)
        ], [context["action_id"] for context in action_contexts])

        directory = transcript_session_dir(
            self.job_root, "UVM_GENERATION", session_id)
        manifest = load_document(directory / "manifest.json")
        self.assertEqual("COMPLETED", manifest["terminal"]["status"])
        self.assertEqual(24, len(manifest["entries"]))
        action_calls = [
            load_document(directory / entry["path"])
            for entry in manifest["entries"] if entry["kind"] == "TOOL_CALL"
            and load_document(directory / entry["path"])["name"] in {
                "write", "fake_eda"}
        ]
        self.assertEqual(4, len({item["action_id"] for item in action_calls}))
        results = [
            load_document(directory / entry["path"])
            for entry in manifest["entries"] if entry["kind"] == "TOOL_RESULT"
        ]
        self.assertEqual(
            ["OBSERVED", "WRITTEN", "FAIL", "WRITTEN", "PASS", "PASS"],
            [item["status"] for item in results])

    def test_terminal_validator_rejection_is_an_observation(self):
        provider = ScriptedProvider([
            [call(1, "finish_task")],
            [call(2, "read")],
            [call(3, "finish_task")],
        ])
        decisions = iter((
            {"status": "NOT_COMPLETE", "diagnostic": "EDA_NOT_RUN"},
            {"status": "PASS"},
        ))
        validation_count = []

        def validator(_arguments, _context):
            validation_count.append(True)
            return next(decisions)

        result = self.worker_session(
            "UVM.WORKER.VALIDATOR.001", provider,
            observations={"read": lambda _arguments: {"status": "OBSERVED"}},
            terminals={"finish_task": lambda _arguments, _context: {
                "status": "REQUESTED",
            }}, validator=validator, max_turns=4).run()

        self.assertEqual({"status": "PASS"}, result)
        self.assertEqual(2, len(validation_count))
        self.assertIn(
            '"status":"NOT_COMPLETE"',
            provider.requests[1]["messages"][-1]["content"])

    def test_continuous_worker_budget_pause_is_not_completion(self):
        session_id = "UVM.WORKER.BUDGET.001"
        provider = ScriptedProvider([
            [call(1, "read")],
            [call(2, "read")],
        ])
        loop = self.worker_session(
            session_id, provider,
            observations={"read": lambda _arguments: {"status": "OBSERVED"}},
            terminals={"finish_task": lambda _arguments, _context: {
                "status": "PASS",
            }}, max_turns=2)

        with self.assertRaises(AgentLoopError) as caught:
            loop.run()
        self.assertEqual("PAUSED_BUDGET", caught.exception.code)
        directory = transcript_session_dir(
            self.job_root, "UVM_GENERATION", session_id)
        self.assertFalse((directory / "manifest.json").exists())
        self.assertEqual(8, len(list(directory.glob("[0-9][0-9][0-9][0-9].*"))))

    def test_token_action_and_time_budgets_pause_before_extra_work(self):
        cases = (
            ("TOKEN", ScriptedProvider([[call(1, "read")]]),
             {"observations": {
                 "read": lambda _arguments: {"status": "OBSERVED"}},
              "max_tokens": 13}, 1),
            ("ACTION", ScriptedProvider([
                [call(1, "write")], [call(2, "write")],
             ]),
             {"actions": {"write": lambda _arguments, _context: {
                 "status": "WRITTEN"}}, "max_actions": 1}, 2),
        )
        for suffix, provider, options, expected_requests in cases:
            with self.subTest(budget=suffix):
                session_id = "UVM.WORKER.BUDGET.{}.001".format(suffix)
                loop = self.worker_session(
                    session_id, provider,
                    terminals={"finish_task": lambda _arguments, _context: {
                        "status": "PASS",
                    }}, **options)
                with self.assertRaises(AgentLoopError) as caught:
                    loop.run()
                self.assertEqual("PAUSED_BUDGET", caught.exception.code)
                self.assertEqual(expected_requests, len(provider.requests))

        ticks = iter((0.0, 0.0, 2.0))
        time_provider = ScriptedProvider([[call(1, "read")]])
        observed = []
        time_loop = self.worker_session(
            "UVM.WORKER.BUDGET.TIME.001", time_provider,
            observations={"read": lambda arguments: observed.append(arguments)},
            terminals={"finish_task": lambda _arguments, _context: {
                "status": "PASS",
            }}, max_time_seconds=1, clock=lambda: next(ticks))
        with self.assertRaises(AgentLoopError) as caught:
            time_loop.run()
        self.assertEqual("PAUSED_BUDGET", caught.exception.code)
        self.assertEqual([], observed)

    def test_fourth_and_duplicate_retrievals_are_not_executed(self):
        for session_id, turns, expected_code in (
            ("PLANNING.LIMIT.001", [
                [call(1, "get_issue")],
                [call(2, "get_unit")],
                [call(3, "get_spec_evidence")],
                [call(4, "get_direct_dependencies")],
            ], "TOOL_PROTOCOL_VIOLATION"),
            ("PLANNING.DUPLICATE.001", [
                [call(1, "get_issue")],
                [call(2, "get_issue")],
            ], "TOOL_PROTOCOL_VIOLATION"),
        ):
            executed = []
            handlers = {
                name: (lambda arguments, selected=name:
                       executed.append(selected) or {"items": []})
                for name in (
                    "get_issue", "get_unit", "get_spec_evidence",
                    "get_direct_dependencies")
            }
            session = self.session(
                session_id, ScriptedProvider(turns), handlers,
                lambda arguments: self.fail("submission must not run"))
            with self.assertRaises(AgentLoopError) as caught:
                session.run()
            self.assertEqual(expected_code, caught.exception.code)
            self.assertEqual(
                3 if "LIMIT" in session_id else 1,
                len(executed))

    def test_multiple_and_forbidden_calls_fail_before_any_handler(self):
        cases = (
            ("PLANNING.MULTIPLE.001", [[
                call(1, "get_issue"), call(2, "get_unit"),
            ]], "TOOL_PROTOCOL_VIOLATION"),
            ("PLANNING.FORBIDDEN.001", [[
                call(1, "get_budget_status"),
            ]], "TOOL_PROTOCOL_VIOLATION"),
        )
        for session_id, turns, expected_code in cases:
            executed = []
            session = self.session(
                session_id, ScriptedProvider(turns),
                {"get_issue": lambda arguments: executed.append(arguments)},
                lambda arguments: self.fail("submission must not run"))
            with self.assertRaises(AgentLoopError) as caught:
                session.run()
            self.assertEqual(expected_code, caught.exception.code)
            self.assertEqual([], executed)
            manifest = load_document(
                self.job_root / "transcripts/orchestrator" / session_id /
                "manifest.json")
            self.assertTrue(accepted(validate(
                "project_transcript_manifest", manifest)))

    def test_manifest_tamper_and_noncontiguous_sequence_are_rejected(self):
        provider = ScriptedProvider([[
            call(1, "submit_repair_plan", ["PLAN.A"]),
        ]])
        session = self.session(
            "PLANNING.MANIFEST.001", provider, {},
            lambda arguments: {"status": "ACCEPTED"})
        session.run()
        path = (self.job_root /
                "transcripts/orchestrator/PLANNING.MANIFEST.001/manifest.json")
        manifest = load_document(path)
        stale = copy.deepcopy(manifest)
        stale["lineage"]["source_report_fingerprint"] = "b" * 64
        self.assertFalse(accepted(validate(
            "project_transcript_manifest", stale)))
        reordered = copy.deepcopy(manifest)
        reordered["entries"][0]["sequence"] = 2
        projected = copy.deepcopy(reordered)
        projected.pop("manifest_fingerprint")
        reordered["manifest_fingerprint"] = canonical_hash(projected)
        self.assertFalse(accepted(validate(
            "project_transcript_manifest", reordered)))

        response_path = path.parent / "0002.response.json"
        response_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(AgentLoopError) as caught:
            self.session(
                "PLANNING.MANIFEST.001", ScriptedProvider([]), {},
                lambda arguments: self.fail("submission must not run"))
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)

        gap_id = "PLANNING.SEQUENCE.GAP.001"
        gap_dir = transcript_session_dir(
            self.job_root, "ORCHESTRATOR", gap_id)
        gap_dir.mkdir(parents=True)
        (gap_dir / "0002.response.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(AgentLoopError) as caught:
            TranscriptStore(
                gap_dir, job_id=self.job_id, role="ORCHESTRATOR",
                session_id=gap_id,
                lineage={"source_report_fingerprint": "a" * 64})
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)

    def test_restart_reuses_persisted_provider_response(self):
        provider = ScriptedProvider([[
            call(2, "submit_repair_plan", ["PLAN.A"]),
        ]])
        executed = []
        session = self.session(
            "PLANNING.RESUME.001", provider,
            {"get_issue": lambda arguments:
             executed.append(arguments) or {"items": ["persisted"]}},
            lambda arguments: {"status": "ACCEPTED"})
        request = session._request(1, self.initial_messages)
        persisted_response = response(request, [call(1, "get_issue")])
        session.transcript_store.record("REQUEST", request)
        session.transcript_store.record("RESPONSE", persisted_response)
        session.transcript_store.record(
            "TOOL_CALL", persisted_response["tool_calls"][0])
        result = session.run()
        self.assertEqual("ACCEPTED", result["status"])
        self.assertEqual(1, len(executed))
        self.assertEqual(1, len(provider.requests))

    def test_cancel_after_response_prevents_tool_handler(self):
        executed = []
        session = self.session(
            "PLANNING.CANCEL.001",
            ScriptedProvider([[call(1, "get_issue")]]),
            {"get_issue": lambda arguments: executed.append(arguments)},
            lambda arguments: self.fail("submission must not execute"),
            cancel_requested=lambda: True)
        with self.assertRaises(AgentLoopError) as caught:
            session.run()
        self.assertEqual("CANCELLED", caught.exception.code)
        self.assertEqual([], executed)
        manifest = load_document(
            self.job_root /
            "transcripts/orchestrator/PLANNING.CANCEL.001/manifest.json")
        self.assertEqual("CANCELLED", manifest["terminal"]["status"])

    def test_single_turn_role_accepts_one_of_two_terminal_tools(self):
        session_id = "STAGE.MULTI.SUBMISSION.001"
        tools = [tool("submit_candidate"), tool("submit_blocked")]
        request = {
            "schema_version": "1.0",
            "request_id": "REQUEST.STAGE.MULTI.SUBMISSION.001",
            "operation": "SELECT_TOOLS",
            "messages": copy.deepcopy(self.initial_messages),
            "tools": copy.deepcopy(tools),
            "tool_choice_policy": "REQUIRED",
            "legal_tool_names": ["submit_candidate", "submit_blocked"],
            "metadata": {"job_id": self.job_id, "stage": "TESTCASE"},
        }
        selected = []
        provider = ScriptedProvider([[
            call(1, "submit_blocked", ["SPEC_AMBIGUITY"]),
        ]])
        loop = AgentLoop(
            provider=provider,
            transcript_store=create_transcript_store(
                job_root=self.job_root, job_id=self.job_id,
                role="STAGE_3", session_id=session_id,
                lineage={"input_fingerprint": "a" * 64}),
            job_id=self.job_id, session_id=session_id,
            initial_messages=self.initial_messages, tools=tools,
            retrieval_handlers={},
            submission_handlers={
                "submit_candidate": lambda _arguments, _context:
                    self.fail("candidate handler must not run"),
                "submit_blocked": lambda arguments, _context:
                    selected.append(arguments) or {"status": "BLOCKED"},
            },
            provider_binding={
                "provider_id": "scripted-provider",
                "model_id": "scripted-sol",
            },
            policy=AgentLoopPolicy(
                role="STAGE_3", retrieval_tools=frozenset(),
                submission_tools=frozenset({
                    "submit_candidate", "submit_blocked"}),
                max_retrieval_turns=0),
            single_turn_request=request)

        self.assertEqual({"status": "BLOCKED"}, loop.run())
        self.assertEqual(
            [{"ids": ["SPEC_AMBIGUITY"]}], selected)
        manifest = load_document(
            self.job_root / "transcripts/stage3" / session_id /
            "manifest.json")
        self.assertEqual("COMPLETED", manifest["terminal"]["status"])

    def test_local_provider_request_error_is_not_reported_as_unavailable(self):
        class InvalidRequestProvider:
            def select_tools(self, _request):
                error = RuntimeError("local adapter contract rejected request")
                error.code = "INVALID_PROVIDER_REQUEST"
                raise error

        session = self.session(
            "PLANNING.INVALID.PROVIDER.REQUEST.001",
            InvalidRequestProvider(), {},
            lambda arguments: self.fail("submission must not execute"))
        with self.assertRaises(AgentLoopError) as caught:
            session.run()
        self.assertEqual("INVALID_PROVIDER_REQUEST", caught.exception.code)
        manifest = load_document(
            self.job_root / "transcripts/orchestrator" /
            "PLANNING.INVALID.PROVIDER.REQUEST.001/manifest.json")
        self.assertEqual(
            "INVALID_PROVIDER_REQUEST", manifest["terminal"]["code"])

    def test_provider_unavailable_has_distinct_typed_mapping(self):
        class UnavailableProvider:
            def select_tools(self, _request):
                raise RuntimeError("test-only transport outage")

        session = self.session(
            "PLANNING.PROVIDER.UNAVAILABLE.001",
            UnavailableProvider(), {},
            lambda arguments: self.fail("submission must not execute"))
        with self.assertRaises(AgentLoopError) as caught:
            session.run()
        self.assertEqual("PROVIDER_UNAVAILABLE", caught.exception.code)
        manifest = load_document(
            self.job_root / "transcripts/orchestrator" /
            "PLANNING.PROVIDER.UNAVAILABLE.001/manifest.json")
        self.assertEqual("PROVIDER_UNAVAILABLE", manifest["terminal"]["code"])

    def test_terminal_exact_replay_has_zero_side_effects(self):
        session_id = "PLANNING.TERMINAL.REPLAY.001"
        provider = ScriptedProvider([[
            call(1, "submit_repair_plan", ["PLAN.A"]),
        ]])
        submissions = []
        first = self.session(
            session_id, provider, {},
            lambda arguments: submissions.append(copy.deepcopy(arguments)) or {
                "status": "ACCEPTED", "arguments": arguments,
            })
        expected = first.run()
        directory = transcript_session_dir(
            self.job_root, "ORCHESTRATOR", session_id)
        before = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in directory.iterdir()
        }

        replay_provider = ScriptedProvider([])
        replay = self.session(
            session_id, replay_provider, {},
            lambda arguments: submissions.append(arguments))
        self.assertEqual(expected, replay.run())
        after = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in directory.iterdir()
        }
        self.assertEqual(before, after)
        self.assertEqual([], replay_provider.requests)
        self.assertEqual(1, len(submissions))

    def test_cross_role_transcript_substitution_fails_closed(self):
        session_id = "PLANNING.CROSS.ROLE.001"
        self.session(
            session_id, ScriptedProvider([[
                call(1, "submit_repair_plan", ["PLAN.A"]),
            ]]), {}, lambda arguments: {"status": "ACCEPTED"}).run()
        directory = transcript_session_dir(
            self.job_root, "ORCHESTRATOR", session_id)
        with self.assertRaises(AgentLoopError) as caught:
            TranscriptStore(
                directory, job_id=self.job_id, role="STAGE_2",
                session_id=session_id,
                lineage={"source_report_fingerprint": "a" * 64})
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        for job_id, substituted_session in (
                ("JOB.PROJECT.OTHER.001", session_id),
                (self.job_id, "PLANNING.OTHER.SESSION.001")):
            with self.assertRaises(AgentLoopError) as caught:
                TranscriptStore(
                    directory, job_id=job_id, role="ORCHESTRATOR",
                    session_id=substituted_session,
                    lineage={"source_report_fingerprint": "a" * 64})
            self.assertEqual("STALE_EVIDENCE", caught.exception.code)

    def test_manifest_authority_tamper_fails_closed(self):
        session_id = "PLANNING.MANIFEST.AUTHORITY.001"
        self.session(
            session_id, ScriptedProvider([[
                call(1, "submit_repair_plan", ["PLAN.A"]),
            ]]), {}, lambda arguments: {"status": "ACCEPTED"}).run()
        manifest_path = transcript_session_dir(
            self.job_root, "ORCHESTRATOR", session_id) / "manifest.json"
        manifest = load_document(manifest_path)
        manifest["lineage"]["source_report_fingerprint"] = "b" * 64
        projected = copy.deepcopy(manifest)
        projected.pop("manifest_fingerprint")
        manifest["manifest_fingerprint"] = canonical_hash(projected)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) +
            "\n", encoding="utf-8")
        with self.assertRaises(AgentLoopError) as caught:
            self.session(
                session_id, ScriptedProvider([]), {},
                lambda arguments: self.fail("submission must not run"))
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)


if __name__ == "__main__":
    unittest.main()


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(AgentLoopTests)
