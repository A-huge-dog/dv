#!/usr/bin/env python3
"""REVIEW-HF1 deterministic Reviewer evidence preflight coverage."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from contracts.validator import load_schema
from domain.review import _enrich_code_evidence, check_review_evidence
from infrastructure.persistence.transcript_store import create_transcript_store
from runtime.agent_loop import AgentLoop, AgentLoopPolicy


class ReviewError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ReviewEvidencePreflightTests(unittest.TestCase):
    def setUp(self):
        self.candidate = {
            "candidate_fingerprint": "a" * 64,
            "content": "setup();\ncheck_reset();\nsetup();\n",
        }
        self.request = {
            "review_request_id": "REQUEST.PROJECT.REVIEW.ABC123",
            "request_fingerprint": "b" * 64,
            "coverage_scope": {"executable_ac_ids": ["AC.RESET.001"]},
        }

    def check(self, check_id: str, content: str, ac_id: str = "AC.RESET.001"):
        return {
            "check_id": check_id,
            "ac_id": ac_id,
            "evidence_kind": "CHECKER",
            "content": content,
        }

    def invoke(self, checks, **identity):
        return check_review_evidence({
            "review_request_id": identity.get(
                "review_request_id", self.request["review_request_id"]),
            "review_request_fingerprint": identity.get(
                "review_request_fingerprint", self.request["request_fingerprint"]),
            "candidate_fingerprint": identity.get(
                "candidate_fingerprint", self.candidate["candidate_fingerprint"]),
            "checks": checks,
        }, self.request, self.candidate, ReviewError)

    def test_exact_not_found_and_ambiguous_match_existing_content_resolution(self):
        result = self.invoke([
            self.check("CHECK.EXACT", "check_reset();"),
            self.check("CHECK.NOT_FOUND", "check_release();"),
            self.check("CHECK.AMBIGUOUS", "setup();"),
        ])
        self.assertEqual(
            ["EXACT_ONE", "NOT_FOUND", "AMBIGUOUS"],
            [item["status"] for item in result["checks"]])
        self.assertEqual(2, result["checks"][0]["line_start"])
        self.assertEqual(2, result["checks"][0]["line_end"])
        self.assertEqual(2, result["checks"][2]["match_count"])
        self.assertIsNone(result["checks"][2]["line_start"])
        enriched = _enrich_code_evidence(
            [{"content": "check_reset();"}], self.candidate["content"],
            ReviewError, "AC.RESET.001", "CHECKER")
        self.assertEqual(2, enriched[0]["line_start"])

    def test_identity_scope_and_duplicate_checks_do_not_expand_authority(self):
        stale = self.invoke(
            [self.check("CHECK.STALE", "check_reset();")],
            candidate_fingerprint="c" * 64)
        self.assertEqual("STALE_EVIDENCE", stale["checks"][0]["status"])
        scope = self.invoke([
            self.check("CHECK.SCOPE", "check_reset();", "AC.OUTSIDE.001")])
        self.assertEqual("SCOPE_EXPANSION", scope["checks"][0]["status"])
        duplicate = self.invoke([
            self.check("CHECK.DUPLICATE", "check_reset();"),
            self.check("CHECK.DUPLICATE", "setup();"),
        ])
        self.assertEqual(
            ["INVALID_EVIDENCE", "INVALID_EVIDENCE"],
            [item["status"] for item in duplicate["checks"]])

    def test_invalid_content_is_reported_without_guessing_a_nearest_line(self):
        result = self.invoke([self.check("CHECK.INVALID", "\ncheck_reset();")])
        item = result["checks"][0]
        self.assertEqual("INVALID_EVIDENCE", item["status"])
        self.assertEqual(0, item["match_count"])
        self.assertIsNone(item["line_start"])

    def test_schema_rejects_extra_fields_before_any_candidate_lookup(self):
        malformed = self.check("CHECK.EXTRA", "check_reset();")
        malformed["path"] = "latest/testcase.sv"
        with self.assertRaises(ReviewError) as caught:
            self.invoke([malformed])
        self.assertEqual("INVALID_EVIDENCE", caught.exception.code)

    def test_one_preflight_then_final_submission_is_one_transcript_session(self):
        submitted = []
        requests = []
        turns = [
            {
                "call_id": "CALL.PREFLIGHT",
                "name": "check_review_evidence",
                "arguments": {
                    "review_request_id": self.request["review_request_id"],
                    "review_request_fingerprint": self.request["request_fingerprint"],
                    "candidate_fingerprint": self.candidate["candidate_fingerprint"],
                    "checks": [self.check("CHECK.EXACT", "check_reset();")],
                },
            },
            {
                "call_id": "CALL.SUBMIT",
                "name": "submit_staged_project_review",
                "arguments": {"verdict": "CLEAN"},
            },
        ]

        def provider(request):
            requests.append(copy.deepcopy(request))
            call = turns.pop(0)
            return {
                "schema_version": "1.0",
                "request_id": request["request_id"],
                "operation": "SELECT_TOOLS",
                "finish_reason": "TOOL_CALLS",
                "content": "scripted Reviewer response",
                "tool_calls": [call],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "model_id": "scripted-model",
                "provider_metadata": {
                    "provider_id": "scripted-provider",
                    "response_id": "RESPONSE.{}".format(request["request_id"]),
                },
                "diagnostics": [],
            }

        with tempfile.TemporaryDirectory() as temporary:
            job_root = Path(temporary) / "JOB.PROJECT.REVIEW.001"
            job_root.mkdir()
            loop = AgentLoop(
                provider=None, provider_call=provider,
                transcript_store=create_transcript_store(
                    job_root=job_root, job_id="JOB.PROJECT.REVIEW.001",
                    role="REVIEWER", session_id="REVIEWSESSION.PREFLIGHT.001",
                    lineage={"request_fingerprint": self.request["request_fingerprint"]}),
                job_id="JOB.PROJECT.REVIEW.001",
                session_id="REVIEWSESSION.PREFLIGHT.001",
                initial_messages=[{"role": "SYSTEM", "content": "review"}],
                tools=[
                    {"name": "check_review_evidence", "description": "preflight",
                     "input_schema": load_schema("review_evidence_check")},
                    {"name": "submit_staged_project_review", "description": "submit",
                     "input_schema": {
                         "type": "object", "additionalProperties": False,
                         "required": ["verdict"],
                         "properties": {"verdict": {"enum": ["CLEAN"]}},
                     }},
                ],
                retrieval_handlers={
                    "check_review_evidence": lambda arguments:
                    check_review_evidence(
                        arguments, self.request, self.candidate, ReviewError),
                },
                submission_handlers={
                    "submit_staged_project_review": lambda arguments, _context:
                    submitted.append(arguments) or {"status": "ACCEPTED"},
                },
                provider_binding={
                    "provider_id": "scripted-provider", "model_id": "scripted-model"},
                policy=AgentLoopPolicy(
                    role="REVIEWER",
                    retrieval_tools=frozenset({"check_review_evidence"}),
                    submission_tools=frozenset({"submit_staged_project_review"}),
                    max_retrieval_turns=1),
                request_metadata={"review_round": 1},
            )
            self.assertEqual("ACCEPTED", loop.run()["status"])
            self.assertEqual([{"verdict": "CLEAN"}], submitted)
            self.assertEqual(2, len(requests))
            self.assertEqual(1, loop.retrieval_count)
            self.assertIn("TOOL_RESULT", requests[1]["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
