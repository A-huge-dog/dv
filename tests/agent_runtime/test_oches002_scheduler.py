#!/usr/bin/env python3
"""OCHES002 FIFO, cancel and restart qualification."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.session_scheduler import (
    SerialSessionScheduler, SessionSchedulerError,
)
from scripts.dvlib import canonical_hash


class Oches002SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "result"
        self.authority = {
            "JOB.PROJECT.FIFO.A": ("CHECKPOINT.FIFO.A", "a" * 64),
            "JOB.PROJECT.FIFO.B": ("CHECKPOINT.FIFO.B", "b" * 64),
            "JOB.PROJECT.FIFO.C": ("CHECKPOINT.FIFO.C", "c" * 64),
        }
        self.scheduler = SerialSessionScheduler(
            self.root, lambda job_id: self.authority[job_id])

    def tearDown(self):
        self.temp.cleanup()

    def enqueue(self, suffix):
        job_id = "JOB.PROJECT.FIFO.{}".format(suffix)
        checkpoint_id, fingerprint = self.authority[job_id]
        return self.scheduler.enqueue(job_id, checkpoint_id, fingerprint)

    def test_global_fifo_restart_and_terminal_replay_have_no_side_effect(self):
        self.enqueue("B")
        self.enqueue("A")
        executed = []

        first = self.scheduler.run_next(
            lambda job_id, checkpoint_id, cancelled: (
                executed.append(job_id) or {"job_id": job_id}))
        self.assertEqual("COMPLETED", first["event"])
        self.assertEqual(["JOB.PROJECT.FIFO.B"], executed)

        restarted = SerialSessionScheduler(
            self.root, lambda job_id: self.authority[job_id])
        second = restarted.run_next(
            lambda job_id, checkpoint_id, cancelled: (
                executed.append(job_id) or {"job_id": job_id}))
        self.assertEqual("COMPLETED", second["event"])
        self.assertEqual([
            "JOB.PROJECT.FIFO.B", "JOB.PROJECT.FIFO.A"], executed)
        self.assertIsNone(restarted.run_next(
            lambda *_: self.fail("terminal replay must not execute")))
        self.assertEqual("COMPLETED", restarted.state()[
            "JOB.PROJECT.FIFO.B"]["state"])

    def test_only_exact_human_or_operator_cancel_is_authoritative(self):
        self.enqueue("A")
        with self.assertRaises(SessionSchedulerError) as denied:
            self.scheduler.request_cancel(
                job_id="JOB.PROJECT.FIFO.A",
                checkpoint_id="CHECKPOINT.FIFO.A",
                checkpoint_fingerprint="a" * 64,
                requester_kind="ORCHESTRATOR",
                requester_identity="MODEL")
        self.assertEqual("TOOL_PERMISSION_DENIED", denied.exception.code)
        with self.assertRaises(SessionSchedulerError) as stale:
            self.scheduler.request_cancel(
                job_id="JOB.PROJECT.FIFO.A",
                checkpoint_id="CHECKPOINT.FIFO.A",
                checkpoint_fingerprint="f" * 64,
                requester_kind="HUMAN", requester_identity="OWNER.1")
        self.assertEqual("STALE_EVIDENCE", stale.exception.code)

        request = self.scheduler.request_cancel(
            job_id="JOB.PROJECT.FIFO.A",
            checkpoint_id="CHECKPOINT.FIFO.A",
            checkpoint_fingerprint="a" * 64,
            requester_kind="HUMAN", requester_identity="OWNER.1")
        self.assertEqual(request, self.scheduler.request_cancel(
            job_id="JOB.PROJECT.FIFO.A",
            checkpoint_id="CHECKPOINT.FIFO.A",
            checkpoint_fingerprint="a" * 64,
            requester_kind="HUMAN", requester_identity="OWNER.1"))
        self.assertEqual("CANCELLED", self.scheduler.state()[
            "JOB.PROJECT.FIFO.A"]["state"])
        self.assertIsNone(self.scheduler.run_next(
            lambda *_: self.fail("cancelled queued Job must not execute")))

    def test_inflight_result_is_discarded_after_cancel(self):
        self.enqueue("C")

        def execute(job_id, checkpoint_id, cancelled):
            self.scheduler.request_cancel(
                job_id=job_id, checkpoint_id=checkpoint_id,
                checkpoint_fingerprint="c" * 64,
                requester_kind="OPERATOR", requester_identity="OPS.1")
            self.assertTrue(cancelled())
            return {"must_not_commit": True}

        terminal = self.scheduler.run_next(execute)
        self.assertEqual("CANCELLED", terminal["event"])
        self.assertEqual("NONE", terminal["result_path"])
        self.assertEqual("CANCELLED", self.scheduler.state()[
            "JOB.PROJECT.FIFO.C"]["state"])

    def test_recovered_active_job_runs_before_later_fifo_job(self):
        self.enqueue("A")
        self.enqueue("B")
        with self.scheduler._lock():
            events = self.scheduler._events()
            self.scheduler._append(
                events, event="STARTED", job_id="JOB.PROJECT.FIFO.A",
                checkpoint_id="CHECKPOINT.FIFO.A",
                checkpoint_fingerprint="a" * 64,
                actor={"kind": "FRAMEWORK", "identity": "OCHES002_FIFO"})
        restarted = SerialSessionScheduler(
            self.root, lambda job_id: self.authority[job_id])
        order = []
        restarted.run_next(lambda job_id, *_: order.append(job_id) or {
            "fingerprint": canonical_hash(job_id)})
        restarted.run_next(lambda job_id, *_: order.append(job_id) or {
            "fingerprint": canonical_hash(job_id)})
        self.assertEqual([
            "JOB.PROJECT.FIFO.A", "JOB.PROJECT.FIFO.B"], order)

    def test_failed_attempt_requires_explicit_exact_requeue(self):
        self.enqueue("A")
        failed = self.scheduler.run_next(
            lambda *_: (_ for _ in ()).throw(
                SessionSchedulerError("TEST_FAILURE", "scripted")))
        self.assertEqual("FAILED", failed["event"])
        with self.assertRaises(SessionSchedulerError) as wrong:
            self.scheduler.retry_failed(
                job_id="JOB.PROJECT.FIFO.A",
                checkpoint_id="CHECKPOINT.FIFO.A",
                checkpoint_fingerprint="a" * 64,
                diagnostic_code="OTHER_FAILURE")
        self.assertEqual("INVALID_TRANSITION", wrong.exception.code)

        event = self.scheduler.retry_failed(
            job_id="JOB.PROJECT.FIFO.A",
            checkpoint_id="CHECKPOINT.FIFO.A",
            checkpoint_fingerprint="a" * 64,
            diagnostic_code="TEST_FAILURE")
        self.assertEqual("REQUEUED", event["event"])
        self.assertEqual(2, self.scheduler.state()[
            "JOB.PROJECT.FIFO.A"]["attempt"])
        completed = self.scheduler.run_next(
            lambda job_id, *_: {"job_id": job_id})
        self.assertEqual("COMPLETED", completed["event"])


if __name__ == "__main__":
    unittest.main()


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(Oches002SchedulerTests)
