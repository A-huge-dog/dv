#!/usr/bin/env python3
"""Recovery-state and crash-safe immutable publication tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.atomic_artifact import (
    PENDING_ARTIFACT_PREFIX, is_pending_artifact,
    publish_immutable_bytes,
)
from core.recovery import stop_status


class RecoveryAndAtomicTests(unittest.TestCase):
    def test_only_two_diagnostics_are_nonrecoverable(self):
        self.assertEqual("TERMINAL", stop_status("ABANDONED_BY_HUMAN"))
        self.assertEqual("TERMINAL", stop_status("AUTHORITY_INPUT_LOST"))
        self.assertEqual("PAUSED_RETRYABLE", stop_status("ATTEMPT_PAUSED"))
        self.assertEqual("PAUSED_RETRYABLE", stop_status("BLOCKED_TOOL"))
        self.assertEqual(
            "PAUSED_RECOVERY_REQUIRED", stop_status("SCOPE_EXPANSION"))
        self.assertEqual(
            "PAUSED_RECOVERY_REQUIRED", stop_status("STALE_EVIDENCE"))
        self.assertEqual(
            "PAUSED_RECOVERY_REQUIRED", stop_status("CROSS_JOB_ARTIFACT"))

    def test_atomic_publication_is_immutable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audit" / "record.json"

            def conflict(message):
                return RuntimeError(message)

            publish_immutable_bytes(path, b"complete\n", conflict, "conflict")
            self.assertEqual(b"complete\n", path.read_bytes())
            publish_immutable_bytes(path, b"complete\n", conflict, "conflict")
            with self.assertRaises(RuntimeError):
                publish_immutable_bytes(path, b"different\n", conflict, "conflict")
            self.assertEqual(b"complete\n", path.read_bytes())
            self.assertEqual([], list(path.parent.glob(
                "{}*".format(PENDING_ARTIFACT_PREFIX))))

    def test_orphan_atomic_temporary_is_not_authority(self):
        path = Path("/tmp") / "{}dead".format(PENDING_ARTIFACT_PREFIX)
        self.assertTrue(is_pending_artifact(path))


if __name__ == "__main__":
    unittest.main()
