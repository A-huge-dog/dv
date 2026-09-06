"""Explicit-path, append-only persistence for one DV Worker state chain."""
from __future__ import annotations

import copy
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from agents.errors import AgentLoopError
from contracts.validator import accepted, load_document, validate
from infrastructure.persistence.atomic_artifact import publish_immutable_text
from scripts.dvlib import canonical_hash


_IDENTITY_FIELDS = {
    "task_id", "worker_session_id", "task_kind", "job_id",
    "authority_fingerprint",
}
_COUNTER_FIELDS = {
    "turns_used", "tokens_used", "eda_runs_used", "transcript_cursor",
}


def worker_state_fingerprint(value: Mapping[str, Any]) -> str:
    projected = copy.deepcopy(dict(value))
    projected.pop("state_fingerprint", None)
    return canonical_hash(projected)


class WorkerStateStore:
    """Persist one caller-selected task; never discover a current task."""

    def __init__(
            self, *, job_root: Path, task_path: str, task_id: str,
            worker_session_id: str, job_id: str,
            authority_fingerprint: str):
        self.job_root = Path(job_root)
        self.task_path = task_path
        self.task_id = task_id
        self.worker_session_id = worker_session_id
        self.job_id = job_id
        self.authority_fingerprint = authority_fingerprint
        self.state_dir = self._explicit_task_dir(task_path)
        self._states: list[dict[str, Any]] = []
        if self.state_dir.exists():
            self._load()

    @classmethod
    def create(
            cls, *, job_root: Path, task_path: str, task_id: str,
            worker_session_id: str, job_id: str,
            authority_fingerprint: str,
            task_kind: str = "UVM_GENERATE_AND_VALIDATE") -> "WorkerStateStore":
        store = cls(
            job_root=job_root, task_path=task_path, task_id=task_id,
            worker_session_id=worker_session_id, job_id=job_id,
            authority_fingerprint=authority_fingerprint)
        if store._states:
            raise AgentLoopError(
                "STALE_EVIDENCE", "DV Worker state already exists")
        store._append({
            "schema_version": "1.0", "artifact_kind": "DV_WORKER_STATE",
            "sequence": 1, "task_id": task_id,
            "worker_session_id": worker_session_id,
            "task_kind": task_kind, "job_id": job_id,
            "authority_fingerprint": authority_fingerprint,
            "status": "RUNNING", "current_phase": "GENERATE",
            "last_action": {}, "last_observation": {}, "current_error": {},
            "changed_files": [], "active_hypotheses": [],
            "eliminated_causes": [], "next_plan": [],
            "turns_used": 0, "tokens_used": 0, "eda_runs_used": 0,
            "transcript_cursor": 0,
            "latest_candidate_fingerprint": None,
            "latest_validation_fingerprint": None,
            "previous_state_fingerprint": "NONE",
            "state_fingerprint": "0" * 64,
        }, initial=True)
        return store

    def _explicit_task_dir(self, relative: str) -> Path:
        pure = PurePosixPath(relative)
        expected = ("audit", "workers", self.task_id)
        if (pure.is_absolute() or pure.parts != expected or
                any(part in {"", ".", ".."} or part.startswith(".")
                    for part in pure.parts)):
            raise AgentLoopError(
                "PATH_ESCAPE", "DV Worker task path is not explicit or safe")
        root = self.job_root.resolve()
        target = self.job_root.joinpath(*pure.parts)
        current = self.job_root
        for part in pure.parts[:-1]:
            current = current / part
            if current.exists() and current.is_symlink():
                raise AgentLoopError(
                    "PATH_ESCAPE", "DV Worker task parent is a symlink")
        try:
            target.resolve(strict=False).relative_to(root)
        except ValueError as error:
            raise AgentLoopError(
                "PATH_ESCAPE", "DV Worker task path escapes its Job") from error
        if target.exists() and (not target.is_dir() or target.is_symlink()):
            raise AgentLoopError(
                "STALE_EVIDENCE", "DV Worker task path is unsafe")
        return target

    @staticmethod
    def _encoded(value: Mapping[str, Any]) -> str:
        try:
            return json.dumps(
                dict(value), sort_keys=True, indent=2,
                ensure_ascii=False) + "\n"
        except (TypeError, ValueError) as error:
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "DV Worker state is not JSON serializable") \
                from error

    def _load(self) -> None:
        paths = sorted(self.state_dir.glob(
            "state-[0-9][0-9][0-9][0-9][0-9][0-9].json"))
        expected_names = [
            "state-{:06d}.json".format(index)
            for index in range(1, len(paths) + 1)
        ]
        if [path.name for path in paths] != expected_names:
            raise AgentLoopError(
                "STALE_EVIDENCE", "DV Worker state sequence has a gap")
        previous = "NONE"
        prior: dict[str, Any] | None = None
        action_records: dict[str, dict[str, Any]] = {}
        for sequence, path in enumerate(paths, 1):
            if not path.is_file() or path.is_symlink():
                raise AgentLoopError(
                    "STALE_EVIDENCE", "DV Worker state path is unsafe")
            try:
                value = load_document(path)
            except Exception as error:
                raise AgentLoopError(
                    "STALE_EVIDENCE", "DV Worker state is malformed") from error
            if not accepted(validate("dv_worker_state", value)):
                raise AgentLoopError(
                    "STALE_EVIDENCE", "DV Worker state violates its contract")
            if value.get("job_id") != self.job_id:
                raise AgentLoopError(
                    "CROSS_JOB_ARTIFACT", "DV Worker state belongs to another Job")
            if (value.get("task_id") != self.task_id or
                    value.get("worker_session_id") != self.worker_session_id or
                    value.get("authority_fingerprint") !=
                        self.authority_fingerprint):
                raise AgentLoopError(
                    "STALE_EVIDENCE", "DV Worker state authority is stale")
            if (value.get("sequence") != sequence or
                    value.get("previous_state_fingerprint") != previous or
                    value.get("state_fingerprint") !=
                        worker_state_fingerprint(value)):
                raise AgentLoopError(
                    "STALE_EVIDENCE", "DV Worker state lineage is stale")
            if prior is not None:
                if any(value[field] != prior[field]
                       for field in _IDENTITY_FIELDS):
                    raise AgentLoopError(
                        "STALE_EVIDENCE", "DV Worker identity changed")
                if any(value[field] < prior[field]
                       for field in _COUNTER_FIELDS):
                    raise AgentLoopError(
                        "STALE_EVIDENCE", "DV Worker counters moved backwards")
            action = value["last_action"]
            if action:
                self._validate_action_transition(action_records, action)
            self._states.append(copy.deepcopy(value))
            previous = value["state_fingerprint"]
            prior = value

    def _validate_action_transition(
            self, records: dict[str, dict[str, Any]],
            action: Mapping[str, Any]) -> None:
        action_id = str(action["action_id"])
        if not action_id.startswith(self.worker_session_id + ".ACTION."):
            raise AgentLoopError(
                "STALE_EVIDENCE", "DV Worker action belongs to another session")
        prior = records.get(action_id)
        if prior is not None and dict(prior) == dict(action):
            return
        if action["status"] == "INTENT":
            if (action["result"] is not None or
                    action["result_fingerprint"] is not None or
                    (prior is not None and dict(prior) != dict(action))):
                raise AgentLoopError(
                    "STALE_EVIDENCE", "DV Worker action intent is stale")
        else:
            if (prior is None or prior["status"] != "INTENT" or
                    any(action[field] != prior[field] for field in (
                        "action_id", "tool_name", "arguments_fingerprint")) or
                    action["result_fingerprint"] != canonical_hash(
                        action["result"])):
                raise AgentLoopError(
                    "STALE_EVIDENCE", "DV Worker action receipt is stale")
        records[action_id] = copy.deepcopy(dict(action))

    @property
    def current(self) -> dict[str, Any]:
        if not self._states:
            raise AgentLoopError(
                "STALE_EVIDENCE", "DV Worker state is unavailable")
        return copy.deepcopy(self._states[-1])

    @property
    def states(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._states)

    def _append(
            self, value: Mapping[str, Any], *, initial: bool = False
            ) -> dict[str, Any]:
        state = copy.deepcopy(dict(value))
        state["state_fingerprint"] = worker_state_fingerprint(state)
        if not accepted(validate("dv_worker_state", state)):
            raise AgentLoopError(
                "INVALID_SCHEMA", "generated DV Worker state is invalid")
        if not initial:
            prior = self.current
            if (state["sequence"] != prior["sequence"] + 1 or
                    state["previous_state_fingerprint"] !=
                        prior["state_fingerprint"]):
                raise AgentLoopError(
                    "STALE_EVIDENCE", "generated DV Worker lineage is stale")
        path = self.state_dir / "state-{:06d}.json".format(state["sequence"])
        publish_immutable_text(
            path, self._encoded(state),
            lambda message: AgentLoopError("STALE_EVIDENCE", message),
            "DV Worker state conflicts with persisted evidence")
        self._states.append(copy.deepcopy(state))
        return copy.deepcopy(state)

    def _transition(self, **changes: Any) -> dict[str, Any]:
        prior = self.current
        forbidden = set(changes) & (
            _IDENTITY_FIELDS | {"sequence", "previous_state_fingerprint",
                                "state_fingerprint", "schema_version",
                                "artifact_kind"})
        if forbidden:
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "DV Worker identity cannot be updated")
        for field in _COUNTER_FIELDS:
            if field in changes and changes[field] < prior[field]:
                raise AgentLoopError(
                    "STALE_EVIDENCE", "DV Worker counters cannot move backwards")
        value = copy.deepcopy(prior)
        value.update(copy.deepcopy(changes))
        value["sequence"] = prior["sequence"] + 1
        value["previous_state_fingerprint"] = prior["state_fingerprint"]
        value["state_fingerprint"] = "0" * 64
        return self._append(value)

    def verify_transcript_length(self, event_count: int) -> None:
        if (isinstance(event_count, bool) or not isinstance(event_count, int) or
                event_count < self.current["transcript_cursor"]):
            raise AgentLoopError(
                "STALE_EVIDENCE",
                "DV Worker transcript cursor is ahead of its transcript")

    def record_progress(
            self, *, transcript_cursor: int, current_phase: str | None = None,
            turns_used: int | None = None,
            tokens_used: int | None = None) -> dict[str, Any]:
        changes: dict[str, Any] = {"transcript_cursor": transcript_cursor}
        if current_phase is not None:
            changes["current_phase"] = current_phase
        if turns_used is not None:
            changes["turns_used"] = turns_used
        if tokens_used is not None:
            changes["tokens_used"] = tokens_used
        current = self.current
        if all(current.get(key) == value for key, value in changes.items()):
            return current
        return self._transition(**changes)

    def action_record(self, action_id: str) -> dict[str, Any] | None:
        matches = [
            state["last_action"] for state in self._states
            if state["last_action"].get("action_id") == action_id
        ]
        return copy.deepcopy(matches[-1]) if matches else None

    @staticmethod
    def _arguments_fingerprint(arguments: Mapping[str, Any]) -> str:
        return canonical_hash(copy.deepcopy(dict(arguments)))

    def record_action_intent(
            self, *, action_id: str, tool_name: str,
            arguments: Mapping[str, Any],
            transcript_cursor: int) -> dict[str, Any]:
        expected = {
            "action_id": action_id, "tool_name": tool_name,
            "arguments_fingerprint": self._arguments_fingerprint(arguments),
            "status": "INTENT", "result": None, "result_fingerprint": None,
        }
        existing = self.action_record(action_id)
        if existing is not None:
            if any(existing[key] != expected[key] for key in (
                    "action_id", "tool_name", "arguments_fingerprint")):
                raise AgentLoopError(
                    "STALE_EVIDENCE", "DV Worker action identity is stale")
            return existing
        return self._transition(
            last_action=expected, current_phase="ACT",
            transcript_cursor=transcript_cursor)

    def record_action_receipt(
            self, *, action_id: str, tool_name: str,
            arguments: Mapping[str, Any], result: Any,
            transcript_cursor: int) -> dict[str, Any]:
        arguments_fingerprint = self._arguments_fingerprint(arguments)
        existing = self.action_record(action_id)
        if existing is None or any(existing.get(key) != expected for key, expected in (
                ("tool_name", tool_name),
                ("arguments_fingerprint", arguments_fingerprint))):
            raise AgentLoopError(
                "STALE_EVIDENCE", "DV Worker action intent is unavailable")
        if existing["status"] == "SUCCEEDED":
            if existing["result_fingerprint"] != canonical_hash(result):
                raise AgentLoopError(
                    "STALE_EVIDENCE", "DV Worker action receipt conflicts")
            return existing
        receipt = {
            "action_id": action_id, "tool_name": tool_name,
            "arguments_fingerprint": arguments_fingerprint,
            "status": "SUCCEEDED", "result": copy.deepcopy(result),
            "result_fingerprint": canonical_hash(result),
        }
        return self._transition(
            last_action=receipt, current_phase="OBSERVE",
            transcript_cursor=transcript_cursor)

    def record_observation(
            self, *, tool_name: str, call_id: str, result: Any,
            transcript_cursor: int) -> dict[str, Any]:
        observation = {
            "kind": "TOOL_RESULT", "tool_name": tool_name,
            "call_id": call_id, "result": copy.deepcopy(result),
            "result_fingerprint": canonical_hash(result),
        }
        current = self.current
        if transcript_cursor < current["transcript_cursor"]:
            return current
        if (current["last_observation"] == observation and
                current["transcript_cursor"] == transcript_cursor):
            return current
        return self._transition(
            last_observation=observation, current_phase="OBSERVE",
            transcript_cursor=transcript_cursor)

    def record_uvm_progress(
            self, *, transcript_cursor: int,
            candidate_fingerprint: str | None = None,
            validation_fingerprint: str | None = None,
            changed_files: list[str] | None = None,
            eda_runs_used: int | None = None) -> dict[str, Any]:
        """Bind compact domain progress to the same append-only state chain."""
        changes: dict[str, Any] = {"transcript_cursor": transcript_cursor}
        if candidate_fingerprint is not None:
            changes["latest_candidate_fingerprint"] = candidate_fingerprint
        if validation_fingerprint is not None:
            changes["latest_validation_fingerprint"] = validation_fingerprint
        if changed_files is not None:
            changes["changed_files"] = sorted(set(changed_files))
        if eda_runs_used is not None:
            changes["eda_runs_used"] = eda_runs_used
        current = self.current
        if all(current.get(key) == value for key, value in changes.items()):
            return current
        return self._transition(**changes)

    def terminal_decision(self, call_id: str) -> Any | None:
        matches = [
            state["last_observation"] for state in self._states
            if state["last_observation"].get("kind") ==
                "COMPLETION_VALIDATION" and
            state["last_observation"].get("call_id") == call_id
        ]
        if not matches:
            return None
        return copy.deepcopy(matches[-1]["result"])

    def record_terminal_decision(
            self, *, tool_name: str, call_id: str, result: Any,
            transcript_cursor: int) -> dict[str, Any]:
        observation = {
            "kind": "COMPLETION_VALIDATION", "tool_name": tool_name,
            "call_id": call_id, "result": copy.deepcopy(result),
            "result_fingerprint": canonical_hash(result),
        }
        return self._transition(
            last_observation=observation, current_phase="VALIDATE",
            transcript_cursor=transcript_cursor)

    def mark_status(
            self, status: str, *, transcript_cursor: int,
            current_phase: str, error: Mapping[str, Any] | None = None
            ) -> dict[str, Any]:
        current_error = copy.deepcopy(dict(error or {}))
        current = self.current
        if (current["status"] == status and
                current["current_phase"] == current_phase and
                current["transcript_cursor"] == transcript_cursor and
                current["current_error"] == current_error):
            return current
        return self._transition(
            status=status, current_phase=current_phase,
            current_error=current_error,
            transcript_cursor=transcript_cursor)

    def resume(self) -> dict[str, Any]:
        if self.current["status"] not in {
                "PAUSED_BUDGET", "PAUSED_RETRYABLE",
                "PAUSED_RECOVERY_REQUIRED"}:
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "DV Worker is not resumable")
        return self._transition(status="RUNNING", current_error={})


__all__ = ["WorkerStateStore", "worker_state_fingerprint"]
