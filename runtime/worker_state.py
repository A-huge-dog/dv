"""Runtime protocol for one persistent DV Worker state chain."""
from __future__ import annotations

from typing import Any, Mapping, Protocol


RECOVERY_SUCCEEDED = "SUCCEEDED"
RECOVERY_NOT_EXECUTED = "NOT_EXECUTED"
RECOVERY_UNKNOWN = "UNKNOWN"


class WorkerStatePersistence(Protocol):
    """State operations required by the continuous :class:`AgentLoop`."""

    task_id: str
    worker_session_id: str
    job_id: str
    authority_fingerprint: str

    @property
    def current(self) -> dict[str, Any]: ...

    def verify_transcript_length(self, event_count: int) -> None: ...

    def record_progress(
            self, *, transcript_cursor: int, current_phase: str | None = None,
            turns_used: int | None = None,
            tokens_used: int | None = None) -> dict[str, Any]: ...

    def action_record(self, action_id: str) -> dict[str, Any] | None: ...

    def record_action_intent(
            self, *, action_id: str, tool_name: str,
            arguments: Mapping[str, Any],
            transcript_cursor: int) -> dict[str, Any]: ...

    def record_action_receipt(
            self, *, action_id: str, tool_name: str,
            arguments: Mapping[str, Any], result: Any,
            transcript_cursor: int) -> dict[str, Any]: ...

    def record_observation(
            self, *, tool_name: str, call_id: str, result: Any,
            transcript_cursor: int) -> dict[str, Any]: ...

    def record_uvm_progress(
            self, *, transcript_cursor: int,
            candidate_fingerprint: str | None = None,
            validation_fingerprint: str | None = None,
            changed_files: list[str] | None = None,
            eda_runs_used: int | None = None) -> dict[str, Any]: ...

    def terminal_decision(self, call_id: str) -> Any | None: ...

    def record_terminal_decision(
            self, *, tool_name: str, call_id: str, result: Any,
            transcript_cursor: int) -> dict[str, Any]: ...

    def mark_status(
            self, status: str, *, transcript_cursor: int,
            current_phase: str, error: Mapping[str, Any] | None = None
            ) -> dict[str, Any]: ...


def validate_recovery_observation(value: Any) -> tuple[str, Any | None]:
    """Validate one evidence-query answer without guessing side effects."""
    if not isinstance(value, Mapping) or set(value) not in (
            {"status"}, {"status", "result"}):
        raise ValueError("action recovery observation is malformed")
    status = value.get("status")
    if status not in {
            RECOVERY_SUCCEEDED, RECOVERY_NOT_EXECUTED, RECOVERY_UNKNOWN}:
        raise ValueError("action recovery observation has an invalid status")
    if status == RECOVERY_SUCCEEDED and "result" not in value:
        raise ValueError("successful action recovery requires its result")
    if status != RECOVERY_SUCCEEDED and "result" in value:
        raise ValueError("non-successful action recovery cannot carry a result")
    return str(status), value.get("result")


__all__ = [
    "RECOVERY_NOT_EXECUTED", "RECOVERY_SUCCEEDED", "RECOVERY_UNKNOWN",
    "WorkerStatePersistence", "validate_recovery_observation",
]
