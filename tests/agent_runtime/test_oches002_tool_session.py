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
from runtime.agent_loop import AgentLoop, AgentLoopError, AgentLoopPolicy
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
            submission_handler=lambda arguments, _context: submitted(arguments),
            provider_binding={
                "provider_id": "scripted-provider",
                "model_id": "scripted-sol",
            },
            policy=AgentLoopPolicy(
                role="ORCHESTRATOR",
                retrieval_tools=frozenset(handlers),
                submission_tool="submit_repair_plan"),
            cancel_requested=cancel_requested,
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
