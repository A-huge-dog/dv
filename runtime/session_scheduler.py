"""Append-only global FIFO scheduler for OCHES002 serial sessions."""
from __future__ import annotations

import copy
import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from contracts.validator import accepted, load_document, validate
from infrastructure.persistence.atomic_artifact import publish_immutable_bytes
from scripts.dvlib import canonical_hash


class SessionSchedulerError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__("[{}] {}".format(code, message))
        self.code = code
        self.message = message


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fingerprinted(value: dict[str, Any], field: str) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result[field] = "0" * 64
    projected = copy.deepcopy(result)
    projected.pop(field)
    result[field] = canonical_hash(projected)
    return result


class SerialSessionScheduler:
    """One global active Job, FIFO order, exact checkpoint cancellation."""

    def __init__(
            self, result_root: Path,
            checkpoint_authority: Callable[[str], tuple[str, str]]):
        self.result_root = Path(result_root)
        self.root = self.result_root / "scheduler"
        self.events_dir = self.root / "events"
        self.cancel_dir = self.root / "cancel_requests"
        self.results_dir = self.root / "results"
        for directory in (self.events_dir, self.cancel_dir, self.results_dir):
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise SessionSchedulerError(
                    "TOOL_PERMISSION_DENIED", "scheduler path is invalid")
        self.lock_path = self.root / "scheduler.lock"
        self.checkpoint_authority = checkpoint_authority

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with self.lock_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _encoded(value: Mapping[str, Any]) -> bytes:
        return (json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")) + "\n").encode("utf-8")

    def _events(self) -> list[dict[str, Any]]:
        paths = sorted(self.events_dir.glob("*.json"))
        events, previous = [], "NONE"
        for sequence, path in enumerate(paths, 1):
            if (path.is_symlink() or not path.is_file() or
                    path.name != "{:08d}.json".format(sequence)):
                raise SessionSchedulerError(
                    "STALE_EVIDENCE", "queue event sequence is invalid")
            value = load_document(path)
            if (not accepted(validate("project_session_queue_event", value)) or
                    value["sequence"] != sequence or
                    value["previous_event_fingerprint"] != previous):
                raise SessionSchedulerError(
                    "STALE_EVIDENCE", "queue event lineage is stale")
            events.append(value)
            previous = value["event_fingerprint"]
        self._derive(events)
        return events

    @staticmethod
    def _derive(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        states: dict[str, dict[str, Any]] = {}
        active_job = None
        for event in events:
            job_id, kind = event["job_id"], event["event"]
            prior = states.get(job_id)
            if kind == "ENQUEUED":
                if prior is not None:
                    raise SessionSchedulerError(
                        "INVALID_TRANSITION", "Job was enqueued more than once")
                states[job_id] = {
                    "state": "QUEUED", "active": False,
                    "attempt": 1,
                    "checkpoint_id": event["checkpoint_id"],
                    "checkpoint_fingerprint":
                        event["checkpoint_fingerprint"],
                    "enqueue_sequence": event["sequence"], "event": event,
                }
            elif prior is None or (
                    prior["checkpoint_id"] != event["checkpoint_id"] or
                    prior["checkpoint_fingerprint"] !=
                        event["checkpoint_fingerprint"]):
                raise SessionSchedulerError(
                    "STALE_EVIDENCE", "queue event checkpoint changed")
            elif kind == "REQUEUED":
                if prior["state"] not in {"FAILED", "CANCELLED"} or \
                        prior["active"]:
                    raise SessionSchedulerError(
                        "INVALID_TRANSITION",
                        "only a failed or paused inactive Job may be requeued")
                prior.update(
                    state="QUEUED", active=False,
                    attempt=prior["attempt"] + 1,
                    enqueue_sequence=event["sequence"], event=event)
            elif kind == "STARTED":
                if prior["state"] != "QUEUED" or active_job is not None:
                    raise SessionSchedulerError(
                        "INVALID_TRANSITION", "queue started out of FIFO state")
                prior.update(state="ACTIVE", active=True, event=event)
                active_job = job_id
            elif kind == "CANCEL_REQUESTED":
                if prior["state"] not in {"QUEUED", "ACTIVE"}:
                    raise SessionSchedulerError(
                        "INVALID_TRANSITION", "cancel request is not current")
                prior.update(state="CANCEL_REQUESTED", event=event)
            elif kind == "CANCELLED":
                if prior["state"] != "CANCEL_REQUESTED":
                    raise SessionSchedulerError(
                        "INVALID_TRANSITION", "cancel terminal is invalid")
                if prior["active"]:
                    active_job = None
                prior.update(state="CANCELLED", active=False, event=event)
            elif kind in {"COMPLETED", "FAILED"}:
                if prior["state"] != "ACTIVE" or not prior["active"]:
                    raise SessionSchedulerError(
                        "INVALID_TRANSITION", "session terminal is invalid")
                active_job = None
                prior.update(state=kind, active=False, event=event)
        return states

    def _append(
            self, events: list[dict[str, Any]], *, event: str, job_id: str,
            checkpoint_id: str, checkpoint_fingerprint: str,
            actor: Mapping[str, str], result_fingerprint: str = "NONE",
            result_path: str = "NONE", diagnostic_code: str = "NONE"
            ) -> dict[str, Any]:
        sequence = len(events) + 1
        value = _fingerprinted({
            "schema_version": "1.0",
            "event_id": "QUEUEEVENT.{:08d}.{}".format(sequence, event),
            "sequence": sequence, "event": event, "job_id": job_id,
            "checkpoint_id": checkpoint_id,
            "checkpoint_fingerprint": checkpoint_fingerprint,
            "actor": copy.deepcopy(dict(actor)),
            "result_fingerprint": result_fingerprint,
            "result_path": result_path,
            "diagnostic_code": diagnostic_code,
            "previous_event_fingerprint": (
                events[-1]["event_fingerprint"] if events else "NONE"),
            "created_at": _utc(),
        }, "event_fingerprint")
        if not accepted(validate("project_session_queue_event", value)):
            raise SessionSchedulerError(
                "INVALID_SCHEMA", "generated queue event is invalid")
        path = self.events_dir / "{:08d}.json".format(sequence)
        publish_immutable_bytes(
            path, self._encoded(value),
            lambda message: SessionSchedulerError(
                "CONFLICTING_REPLAY", message),
            "scheduler event replay conflicts")
        events.append(value)
        return copy.deepcopy(value)

    def _assert_checkpoint(
            self, job_id: str, checkpoint_id: str,
            checkpoint_fingerprint: str) -> None:
        if self.checkpoint_authority(job_id) != (
                checkpoint_id, checkpoint_fingerprint):
            raise SessionSchedulerError(
                "STALE_EVIDENCE", "scheduler checkpoint authority is stale")

    def enqueue(
            self, job_id: str, checkpoint_id: str,
            checkpoint_fingerprint: str) -> dict[str, Any]:
        with self._lock():
            self._assert_checkpoint(
                job_id, checkpoint_id, checkpoint_fingerprint)
            events = self._events()
            state = self._derive(events).get(job_id)
            if state is not None:
                if (state["checkpoint_id"], state["checkpoint_fingerprint"]) != (
                        checkpoint_id, checkpoint_fingerprint):
                    raise SessionSchedulerError(
                        "CONFLICTING_REPLAY", "Job queue replay conflicts")
                return copy.deepcopy(state["event"])
            return self._append(
                events, event="ENQUEUED", job_id=job_id,
                checkpoint_id=checkpoint_id,
                checkpoint_fingerprint=checkpoint_fingerprint,
                actor={"kind": "FRAMEWORK", "identity": "OCHES002_FIFO"})

    def request_cancel(
            self, *, job_id: str, checkpoint_id: str,
            checkpoint_fingerprint: str, requester_kind: str,
            requester_identity: str) -> dict[str, Any]:
        if requester_kind not in {"HUMAN", "OPERATOR"}:
            raise SessionSchedulerError(
                "TOOL_PERMISSION_DENIED", "only Human/operator may cancel")
        with self._lock():
            self._assert_checkpoint(
                job_id, checkpoint_id, checkpoint_fingerprint)
            events = self._events()
            state = self._derive(events).get(job_id)
            if state is None or state["state"] not in {
                    "QUEUED", "ACTIVE", "CANCEL_REQUESTED", "CANCELLED"}:
                raise SessionSchedulerError(
                    "INVALID_TRANSITION", "Job cannot be cancelled")
            seed = canonical_hash({
                "job_id": job_id, "checkpoint_id": checkpoint_id,
                "checkpoint_fingerprint": checkpoint_fingerprint,
                "requester_kind": requester_kind,
                "requester_identity": requester_identity,
            })[:20].upper()
            request_id = "CANCELREQUEST.{}".format(seed)
            path = self.cancel_dir / "{}.json".format(request_id.casefold())
            if path.exists():
                request = load_document(path)
                if not accepted(validate(
                        "project_session_cancel_request", request)):
                    raise SessionSchedulerError(
                        "STALE_EVIDENCE", "cancel request is stale")
                return request
            request = _fingerprinted({
                "schema_version": "1.0", "request_id": request_id,
                "job_id": job_id, "checkpoint_id": checkpoint_id,
                "checkpoint_fingerprint": checkpoint_fingerprint,
                "requester": {
                    "kind": requester_kind, "identity": requester_identity},
                "created_at": _utc(),
            }, "request_fingerprint")
            if not accepted(validate("project_session_cancel_request", request)):
                raise SessionSchedulerError(
                    "INVALID_SCHEMA", "generated cancel request is invalid")
            publish_immutable_bytes(
                path, self._encoded(request),
                lambda message: SessionSchedulerError(
                    "CONFLICTING_REPLAY", message),
                "scheduler cancel request replay conflicts")
            actor = request["requester"]
            self._append(
                events, event="CANCEL_REQUESTED", job_id=job_id,
                checkpoint_id=checkpoint_id,
                checkpoint_fingerprint=checkpoint_fingerprint,
                actor=actor, diagnostic_code="CANCEL_REQUESTED")
            if state["state"] == "QUEUED":
                self._append(
                    events, event="CANCELLED", job_id=job_id,
                    checkpoint_id=checkpoint_id,
                    checkpoint_fingerprint=checkpoint_fingerprint,
                    actor={"kind": "FRAMEWORK", "identity": "OCHES002_FIFO"},
                    diagnostic_code="CANCELLED")
            return request

    def retry_failed(
            self, *, job_id: str, checkpoint_id: str,
            checkpoint_fingerprint: str, diagnostic_code: str
            ) -> dict[str, Any]:
        """Append one explicit retry after a failed or Human-paused attempt."""
        with self._lock():
            self._assert_checkpoint(
                job_id, checkpoint_id, checkpoint_fingerprint)
            events = self._events()
            state = self._derive(events).get(job_id)
            if (state is None or state["state"] not in {"FAILED", "CANCELLED"} or
                    state["event"].get("diagnostic_code") != diagnostic_code):
                raise SessionSchedulerError(
                    "INVALID_TRANSITION",
                    "FIFO retry does not bind the exact failed attempt")
            return self._append(
                events, event="REQUEUED", job_id=job_id,
                checkpoint_id=checkpoint_id,
                checkpoint_fingerprint=checkpoint_fingerprint,
                actor={"kind": "FRAMEWORK", "identity": "OCHES002_FIFO"},
                diagnostic_code=diagnostic_code)

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._lock():
            state = self._derive(self._events()).get(job_id)
            return state is not None and state["state"] in {
                "CANCEL_REQUESTED", "CANCELLED"}

    def run_next(
            self, executor: Callable[
                [str, str, Callable[[], bool]], Any]
            ) -> dict[str, Any] | None:
        with self._lock():
            events = self._events()
            states = self._derive(events)
            active = next(
                (item for item in states.values() if item["active"]), None)
            if active is None:
                queued = sorted(
                    (item for item in states.values()
                     if item["state"] == "QUEUED"),
                    key=lambda item: item["enqueue_sequence"])
                if not queued:
                    return None
                active = queued[0]
                event = active["event"]
                self._append(
                    events, event="STARTED", job_id=event["job_id"],
                    checkpoint_id=active["checkpoint_id"],
                    checkpoint_fingerprint=active["checkpoint_fingerprint"],
                    actor={"kind": "FRAMEWORK", "identity": "OCHES002_FIFO"})
            job_id = active["event"]["job_id"]
            checkpoint_id = active["checkpoint_id"]
            checkpoint_fingerprint = active["checkpoint_fingerprint"]

        caught = None
        result = None
        try:
            result = executor(
                job_id, checkpoint_id,
                lambda: self.is_cancel_requested(job_id))
        except Exception as error:  # typed below, after cancel state wins
            caught = error

        with self._lock():
            events = self._events()
            state = self._derive(events)[job_id]
            if state["state"] == "CANCEL_REQUESTED":
                return self._append(
                    events, event="CANCELLED", job_id=job_id,
                    checkpoint_id=checkpoint_id,
                    checkpoint_fingerprint=checkpoint_fingerprint,
                    actor={"kind": "FRAMEWORK", "identity": "OCHES002_FIFO"},
                    diagnostic_code="CANCELLED")
            if caught is not None:
                return self._append(
                    events, event="FAILED", job_id=job_id,
                    checkpoint_id=checkpoint_id,
                    checkpoint_fingerprint=checkpoint_fingerprint,
                    actor={"kind": "FRAMEWORK", "identity": "OCHES002_FIFO"},
                    diagnostic_code=getattr(caught, "code", type(caught).__name__))
            result_fingerprint = canonical_hash(result)
            relative = "scheduler/results/{}.{}.json".format(
                job_id.casefold(), checkpoint_fingerprint[:16])
            result_path = self.result_root / relative
            encoded = self._encoded({
                "job_id": job_id, "checkpoint_id": checkpoint_id,
                "checkpoint_fingerprint": checkpoint_fingerprint,
                "result": result, "result_fingerprint": result_fingerprint,
            })
            publish_immutable_bytes(
                result_path, encoded,
                lambda message: SessionSchedulerError(
                    "CONFLICTING_REPLAY", message),
                "scheduler result replay conflicts")
            return self._append(
                events, event="COMPLETED", job_id=job_id,
                checkpoint_id=checkpoint_id,
                checkpoint_fingerprint=checkpoint_fingerprint,
                actor={"kind": "FRAMEWORK", "identity": "OCHES002_FIFO"},
                result_fingerprint=result_fingerprint,
                result_path=relative, diagnostic_code="COMPLETED")

    def state(self) -> dict[str, dict[str, Any]]:
        with self._lock():
            return copy.deepcopy(self._derive(self._events()))

    def events_for_job(self, job_id: str) -> list[dict[str, Any]]:
        """Return one Job's exact validated append-only queue history."""
        with self._lock():
            return copy.deepcopy([
                event for event in self._events()
                if event["job_id"] == job_id
            ])


__all__ = ["SerialSessionScheduler", "SessionSchedulerError"]
