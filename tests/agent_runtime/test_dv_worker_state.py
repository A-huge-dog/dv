#!/usr/bin/env python3
"""M2 persistent DV Worker state and crash/restart qualification."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from agents.errors import AgentLoopError
from contracts.validator import accepted, load_document, validate
from infrastructure.persistence.transcript_store import (
    create_transcript_store, transcript_session_dir,
)
from infrastructure.persistence.worker_state_store import WorkerStateStore
from runtime.agent_loop import AgentLoop, AgentLoopPolicy
from tests.agent_runtime.test_oches002_tool_session import (
    ScriptedProvider, call, tool,
)


class SimulatedProcessCrash(BaseException):
    """Test-only interruption that bypasses normal Agent failure handling."""


class InterruptingTranscript:
    def __init__(self, delegate, kind, occurrence=1):
        self.delegate = delegate
        self.kind = kind
        self.remaining = occurrence

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def record(self, kind, value):
        self.delegate.record(kind, value)
        if kind == self.kind:
            self.remaining -= 1
            if self.remaining == 0:
                raise SimulatedProcessCrash(kind)


class InterruptingState:
    def __init__(self, delegate, method):
        self.delegate = delegate
        self.method = method

    def __getattr__(self, name):
        attribute = getattr(self.delegate, name)
        if name != self.method:
            return attribute

        def interrupted(*args, **kwargs):
            result = attribute(*args, **kwargs)
            raise SimulatedProcessCrash(name)

        return interrupted


class DvWorkerStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.job_root = Path(self.temp.name) / "JOB.PROJECT.WORKER.001"
        self.job_root.mkdir()
        self.job_id = "JOB.PROJECT.WORKER.001"
        self.task_id = "DVTASK.UVM.001"
        self.session_id = "DVWORKER.UVM.001"
        self.authority = "d" * 64
        self.task_path = "audit/workers/{}".format(self.task_id)
        self.messages = [
            {"role": "SYSTEM", "content": "持续完成一个 UVM Worker task。"},
            {"role": "USER", "content": "只使用当前 authority。"},
        ]

    def tearDown(self):
        self.temp.cleanup()

    def create_state(self):
        return WorkerStateStore.create(
            job_root=self.job_root, task_path=self.task_path,
            task_id=self.task_id, worker_session_id=self.session_id,
            job_id=self.job_id, authority_fingerprint=self.authority)

    def load_state(self, **overrides):
        values = {
            "job_root": self.job_root, "task_path": self.task_path,
            "task_id": self.task_id,
            "worker_session_id": self.session_id,
            "job_id": self.job_id,
            "authority_fingerprint": self.authority,
        }
        values.update(overrides)
        return WorkerStateStore(**values)

    def transcript(self):
        return create_transcript_store(
            job_root=self.job_root, job_id=self.job_id,
            role="UVM_GENERATION", session_id=self.session_id,
            lineage={
                "task_id": self.task_id,
                "authority_fingerprint": self.authority,
            })

    def loop(
            self, provider, transcript, state, side_effect,
            *, recovery=None, validator=None):
        return AgentLoop(
            provider=provider, transcript_store=transcript,
            job_id=self.job_id, session_id=self.session_id,
            initial_messages=self.messages,
            tools=[tool("write"), tool("finish_task")],
            action_handlers={"write": side_effect},
            terminal_handlers={
                "finish_task": lambda _arguments, _context: {
                    "status": "REQUESTED"}},
            completion_validator=(
                validator or
                (lambda _arguments, _context: {"status": "PASS"})),
            action_recovery_handlers=(
                {"write": recovery} if recovery is not None else {}),
            worker_state_store=state,
            provider_binding={
                "provider_id": "scripted-provider",
                "model_id": "scripted-sol",
            },
            policy=AgentLoopPolicy(
                role="UVM_GENERATION",
                action_tools=frozenset({"write"}),
                terminal_tools=frozenset({"finish_task"}),
                max_turns=4, max_tokens=1000, max_actions=2),
        )

    def resume_to_success(
            self, side_effect, *, recovery=None, validator=None):
        provider = ScriptedProvider([[call(2, "finish_task")]])
        result = self.loop(
            provider, self.transcript(), self.load_state(), side_effect,
            recovery=recovery, validator=validator).run()
        self.assertEqual("PASS", result["status"])
        self.assertEqual(1, len(provider.requests))
        state = self.load_state()
        manifest = load_document(
            transcript_session_dir(
                self.job_root, "UVM_GENERATION", self.session_id) /
            "manifest.json")
        self.assertEqual(
            len(manifest["entries"]), state.current["transcript_cursor"])
        self.assertEqual(list(range(1, len(manifest["entries"]) + 1)), [
            entry["sequence"] for entry in manifest["entries"]])
        return state

    def test_resume_after_provider_response_does_not_repeat_provider(self):
        state = self.create_state()
        transcript = InterruptingTranscript(self.transcript(), "RESPONSE")
        first = ScriptedProvider([[call(1, "write")]])
        effects = []

        with self.assertRaises(SimulatedProcessCrash):
            self.loop(
                first, transcript, state,
                lambda arguments, _context: effects.append(arguments) or {
                    "status": "WRITTEN"}).run()
        self.assertEqual(1, len(first.requests))
        self.assertEqual([], effects)

        resumed = self.resume_to_success(
            lambda arguments, _context: effects.append(arguments) or {
                "status": "WRITTEN"})
        self.assertEqual(1, len(effects))
        self.assertEqual("SUCCEEDED", resumed.current["status"])

    def test_resume_after_action_intent_executes_known_absent_effect_once(self):
        state = InterruptingState(
            self.create_state(), "record_action_intent")
        effects = []
        with self.assertRaises(SimulatedProcessCrash):
            self.loop(
                ScriptedProvider([[call(1, "write")]]),
                self.transcript(), state,
                lambda arguments, _context: effects.append(arguments) or {
                    "status": "WRITTEN"}).run()
        self.assertEqual([], effects)

        recovery_calls = []
        resumed = self.resume_to_success(
            lambda arguments, _context: effects.append(arguments) or {
                "status": "WRITTEN"},
            recovery=lambda action_id, _arguments, _context:
                recovery_calls.append(action_id) or {
                    "status": "NOT_EXECUTED"})
        self.assertEqual(1, len(effects))
        self.assertEqual(1, len(recovery_calls))
        self.assertEqual("SUCCEEDED", resumed.current["status"])

    def test_side_effect_without_receipt_uses_evidence_instead_of_rerun(self):
        self.create_state()
        effects = []

        def crash_after_effect(arguments, _context):
            effects.append(copy.deepcopy(arguments))
            raise SimulatedProcessCrash("after side effect")

        with self.assertRaises(SimulatedProcessCrash):
            self.loop(
                ScriptedProvider([[call(1, "write")]]),
                self.transcript(), self.load_state(),
                crash_after_effect).run()
        self.assertEqual(1, len(effects))

        resumed = self.resume_to_success(
            lambda _arguments, _context:
                self.fail("successful evidence must prevent action replay"),
            recovery=lambda _action_id, _arguments, _context: {
                "status": "SUCCEEDED",
                "result": {"status": "WRITTEN", "source": "EVIDENCE"},
            })
        self.assertEqual(1, len(effects))
        receipts = [
            item["last_action"] for item in resumed.states
            if item["last_action"].get("status") == "SUCCEEDED"
        ]
        self.assertTrue(receipts)
        self.assertEqual("EVIDENCE", receipts[-1]["result"]["source"])

    def test_persisted_action_result_and_receipt_are_replayed_exactly(self):
        self.create_state()
        effects = []
        interrupted = InterruptingTranscript(self.transcript(), "TOOL_RESULT")
        with self.assertRaises(SimulatedProcessCrash):
            self.loop(
                ScriptedProvider([[call(1, "write")]]),
                interrupted, self.load_state(),
                lambda arguments, _context: effects.append(arguments) or {
                    "status": "WRITTEN"}).run()
        before = sorted(
            path.name for path in transcript_session_dir(
                self.job_root, "UVM_GENERATION", self.session_id).glob(
                    "[0-9][0-9][0-9][0-9].*.json"))
        self.resume_to_success(
            lambda _arguments, _context:
                self.fail("persisted action receipt must replay"))
        after = sorted(
            path.name for path in transcript_session_dir(
                self.job_root, "UVM_GENERATION", self.session_id).glob(
                    "[0-9][0-9][0-9][0-9].*.json"))
        self.assertEqual(1, len(effects))
        self.assertEqual(before, after[:len(before)])
        self.assertEqual(len(after), len(set(after)))

    def test_successful_receipt_replays_when_tool_result_was_not_recorded(self):
        effects = []
        interrupted = InterruptingState(
            self.create_state(), "record_action_receipt")
        with self.assertRaises(SimulatedProcessCrash):
            self.loop(
                ScriptedProvider([[call(1, "write")]]),
                self.transcript(), interrupted,
                lambda arguments, _context: effects.append(arguments) or {
                    "status": "WRITTEN", "version": "V1"}).run()
        self.assertEqual(1, len(effects))
        self.assertEqual(
            "SUCCEEDED",
            self.load_state().action_record(
                self.session_id + ".ACTION.001")["status"])

        self.resume_to_success(
            lambda _arguments, _context:
                self.fail("successful receipt must bypass side effect"))
        self.assertEqual(1, len(effects))

    def test_finish_validation_result_replays_without_revalidation(self):
        interrupted = InterruptingState(
            self.create_state(), "record_terminal_decision")
        validations = []
        with self.assertRaises(SimulatedProcessCrash):
            self.loop(
                ScriptedProvider([[call(1, "finish_task")]]),
                self.transcript(), interrupted,
                lambda _arguments, _context: {"status": "WRITTEN"},
                validator=lambda arguments, context:
                    validations.append((arguments, context)) or {
                        "status": "PASS"}).run()
        self.assertEqual(1, len(validations))

        replay_provider = ScriptedProvider([])
        result = self.loop(
            replay_provider, self.transcript(), self.load_state(),
            lambda _arguments, _context: {"status": "WRITTEN"},
            validator=lambda _arguments, _context:
                self.fail("persisted completion validation must replay")).run()
        self.assertEqual({"status": "PASS"}, result)
        self.assertEqual([], replay_provider.requests)
        self.assertEqual(1, len(validations))

    def test_unknown_side_effect_pauses_for_operator_recovery(self):
        self.create_state()
        effects = []

        def crash_after_effect(arguments, _context):
            effects.append(arguments)
            raise SimulatedProcessCrash("unknown effect")

        with self.assertRaises(SimulatedProcessCrash):
            self.loop(
                ScriptedProvider([[call(1, "write")]]), self.transcript(),
                self.load_state(), crash_after_effect).run()

        with self.assertRaises(AgentLoopError) as caught:
            self.loop(
                ScriptedProvider([]), self.transcript(), self.load_state(),
                lambda _arguments, _context:
                    self.fail("unknown action must not rerun")).run()
        self.assertEqual("PAUSED_RECOVERY_REQUIRED", caught.exception.code)
        paused = self.load_state().current
        self.assertEqual("PAUSED_RECOVERY_REQUIRED", paused["status"])
        self.assertEqual(1, len(effects))
        self.assertFalse((transcript_session_dir(
            self.job_root, "UVM_GENERATION", self.session_id) /
            "manifest.json").exists())

    def test_state_chain_identity_fingerprint_and_cursor_fail_closed(self):
        store = self.create_state()
        store.record_progress(
            transcript_cursor=2, current_phase="OBSERVE",
            turns_used=1, tokens_used=13)
        store.record_action_intent(
            action_id=self.session_id + ".ACTION.001", tool_name="write",
            arguments={"ids": ["V1"]}, transcript_cursor=3)
        store.record_action_receipt(
            action_id=self.session_id + ".ACTION.001", tool_name="write",
            arguments={"ids": ["V1"]}, result={"status": "WRITTEN"},
            transcript_cursor=3)

        states = store.states
        self.assertEqual(list(range(1, len(states) + 1)), [
            state["sequence"] for state in states])
        self.assertEqual("NONE", states[0]["previous_state_fingerprint"])
        for previous, current in zip(states, states[1:]):
            self.assertEqual(
                previous["state_fingerprint"],
                current["previous_state_fingerprint"])
        self.assertTrue(all(accepted(validate("dv_worker_state", state))
                            for state in states))
        self.assertTrue(all(state["task_id"] == self.task_id and
                            state["worker_session_id"] == self.session_id and
                            state["authority_fingerprint"] == self.authority
                            for state in states))

        with self.assertRaises(AgentLoopError) as caught:
            self.load_state(job_id="JOB.PROJECT.OTHER.001")
        self.assertEqual("CROSS_JOB_ARTIFACT", caught.exception.code)
        with self.assertRaises(AgentLoopError) as caught:
            self.load_state(authority_fingerprint="e" * 64)
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        with self.assertRaises(AgentLoopError) as caught:
            self.load_state(worker_session_id="DVWORKER.OTHER.001")
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        with self.assertRaises(AgentLoopError) as caught:
            store.verify_transcript_length(2)
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)

        last_path = store.state_dir / "state-{:06d}.json".format(
            states[-1]["sequence"])
        tampered = load_document(last_path)
        tampered["tokens_used"] += 1
        last_path.write_text(
            json.dumps(tampered, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        with self.assertRaises(AgentLoopError) as caught:
            self.load_state()
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)

    def test_explicit_task_path_never_discovers_another_task(self):
        self.create_state()
        with self.assertRaises(AgentLoopError) as caught:
            WorkerStateStore(
                job_root=self.job_root,
                task_path="audit/workers/DVTASK.OTHER.001",
                task_id=self.task_id,
                worker_session_id=self.session_id, job_id=self.job_id,
                authority_fingerprint=self.authority)
        self.assertEqual("PATH_ESCAPE", caught.exception.code)

        other = WorkerStateStore(
            job_root=self.job_root,
            task_path="audit/workers/DVTASK.OTHER.001",
            task_id="DVTASK.OTHER.001",
            worker_session_id="DVWORKER.OTHER.001", job_id=self.job_id,
            authority_fingerprint=self.authority)
        with self.assertRaises(AgentLoopError) as caught:
            _ = other.current
        self.assertEqual("STALE_EVIDENCE", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
