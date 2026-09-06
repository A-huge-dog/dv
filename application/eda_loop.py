"""ProjectLoop bridge for EDA-001's single test-suite tool."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from adapters.eda import TrustedEdaBoundaryError
from application.eda_test_suite import (
    EdaTestSuiteTool, build_eda_test_suite_binding, execute_eda_test_suite,
)
from contracts.validator import accepted, validate
from infrastructure.persistence.atomic_artifact import publish_immutable_text


LOOP_CHECKPOINT_PATH = "audit/eda001/loop_checkpoint.json"


class EdaLoopError(Exception):
    """Typed application error without a reverse dependency on runtime."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _error(code: str, message: str):
    return EdaLoopError(code, message)


def _immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    publish_immutable_text(
        path, json.dumps(dict(value), sort_keys=True, indent=2,
                         ensure_ascii=False) + "\n",
        lambda message: _error("CONFLICTING_REPLAY", message),
        "immutable EDA-001 ProjectLoop checkpoint conflicts with existing bytes")


@dataclass(frozen=True)
class ExecuteEdaSuiteInLoopInput:
    job_root: Path
    workspace_root: Path
    manifest: dict[str, Any]
    review_checkpoint: dict[str, Any]
    suite: dict[str, Any]
    tool: EdaTestSuiteTool


class ExecuteEdaSuiteInLoopHandler:
    """Build an explicit binding and call the sole EDA-001 tool once."""

    def handle(self, command: ExecuteEdaSuiteInLoopInput) -> dict[str, Any]:
        review = command.review_checkpoint
        suite = copy.deepcopy(command.suite)
        required = {"binding_id", "sources", "dut_top", "testbench_top",
                    "testcases", "coverage"}
        optional = {"parameters", "include_dirs", "defines"}
        if (not isinstance(suite, dict) or set(suite) - required - optional or
                not required.issubset(suite) or not isinstance(suite["coverage"], bool)):
            raise _error("INVALID_SCHEMA", "EDA suite request is invalid")
        # CLEAN review remains a Project quality gate, not an EDA approval.
        # The EDA tool's sole execution authority is the binding built below.
        if (review.get("state") != "AWAITING_HUMAN_REVIEW" or
                review.get("job_id") != command.manifest["job_id"] or
                review.get("error_count") != 0 or
                review.get("review_verdict") != "CLEAN"):
            raise _error("STALE_EVIDENCE", "EDA suite requires the exact CLEAN review checkpoint")
        try:
            binding_path, binding = build_eda_test_suite_binding(
                job_root=command.job_root, workspace_root=command.workspace_root,
                job_id=command.manifest["job_id"], binding_id=suite["binding_id"],
                sources=suite["sources"], dut_top=suite["dut_top"],
                testbench_top=suite["testbench_top"], testcases=suite["testcases"],
                parameters=suite.get("parameters", {}),
                include_dirs=suite.get("include_dirs", ()),
                defines=suite.get("defines", ()))
            result = execute_eda_test_suite(
                command.tool, binding_path, binding["binding_fingerprint"],
                suite["coverage"])
        except TrustedEdaBoundaryError as caught:
            detail = caught.diagnostics[0] if caught.diagnostics else {}
            raise _error(str(detail.get("code", "BLOCKED_TOOL")), str(caught)) from caught
        final = result.final_result
        state = "EDA_EXECUTION_PASS" if final["all_testcases_passed"] \
            else "EDA_EXECUTION_FAIL"
        checkpoint = {
            "schema_version": "1.0", "artifact_kind": "EDA_LOOP_CHECKPOINT",
            "state": state, "job_id": command.manifest["job_id"],
            "binding_path": binding_path,
            "binding_fingerprint": binding["binding_fingerprint"],
            "final_result_path": result.final_result_path,
            "artifact_index_path": result.artifact_index_path,
        }
        if not accepted(validate("eda_loop_checkpoint", checkpoint)):
            raise _error("INVALID_SCHEMA", "EDA loop checkpoint is invalid")
        _immutable_json(command.job_root / LOOP_CHECKPOINT_PATH, checkpoint)
        return checkpoint


__all__ = ["EdaLoopError", "ExecuteEdaSuiteInLoopHandler",
           "ExecuteEdaSuiteInLoopInput", "LOOP_CHECKPOINT_PATH"]
