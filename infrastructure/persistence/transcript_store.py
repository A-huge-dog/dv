"""Append-only storage and verified loading for Agent transcript events."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from contracts.validator import accepted, validate
from core.atomic_artifact import publish_immutable_bytes
from runtime.agent_loop import AgentLoopError
from scripts.dvlib import canonical_hash


ROLE_DIRECTORIES = {
    "ORCHESTRATOR": "orchestrator",
    "STAGE_1": "stage1",
    "STAGE_2": "stage2",
    "STAGE_3": "stage3",
    "REVIEWER": "reviewer",
}
_EVENT_KINDS = {"REQUEST", "RESPONSE", "TOOL_CALL", "TOOL_RESULT"}


def transcript_session_dir(
        job_root: Path, role: str, session_id: str) -> Path:
    if role not in ROLE_DIRECTORIES:
        raise AgentLoopError(
            "TOOL_PERMISSION_DENIED", "transcript role is not supported")
    if not re.fullmatch(r"[A-Z][A-Z0-9_.-]+", session_id):
        raise AgentLoopError(
            "INVALID_TOOL_CALL", "transcript session identity is invalid")
    return Path(job_root) / "transcripts" / ROLE_DIRECTORIES[role] / session_id


class TranscriptStore:
    """Persist events at one explicit session path; never decide protocol state."""

    def __init__(
            self, session_dir: Path, *, job_id: str, role: str,
            session_id: str, lineage: Mapping[str, Any]):
        if not re.fullmatch(r"JOB\.PROJECT\.[A-Z0-9_.-]+", job_id):
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "transcript Job identity is invalid")
        if role not in ROLE_DIRECTORIES:
            raise AgentLoopError(
                "TOOL_PERMISSION_DENIED", "transcript role is not supported")
        if not re.fullmatch(r"[A-Z][A-Z0-9_.-]+", session_id):
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "transcript session identity is invalid")
        self.session_dir = Path(session_dir)
        self.job_id = job_id
        self.role = role
        self.session_id = session_id
        self.lineage = copy.deepcopy(dict(lineage))
        self.entries: list[dict[str, Any]] = []
        self.manifest: dict[str, Any] | None = None
        if self.session_dir.exists():
            self._load()
        else:
            self.session_dir.mkdir(parents=True, exist_ok=False)

    @staticmethod
    def _encoded(value: Any) -> bytes:
        try:
            return (json.dumps(
                value, ensure_ascii=False, indent=2, sort_keys=True,
            ) + "\n").encode("utf-8")
        except (TypeError, ValueError) as error:
            raise AgentLoopError(
                "INVALID_TOOL_CALL",
                "transcript event is not losslessly JSON serializable") \
                from error

    def _entry_value(self, entry: Mapping[str, Any]) -> Any:
        path = self.session_dir / str(entry["path"])
        if (not path.is_file() or path.is_symlink() or
                hashlib.sha256(path.read_bytes()).hexdigest() !=
                    entry["content_fingerprint"]):
            raise AgentLoopError(
                "STALE_EVIDENCE", "transcript raw event is stale")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise AgentLoopError(
                "STALE_EVIDENCE", "transcript event is malformed") from error

    def _load(self) -> None:
        if not self.session_dir.is_dir() or self.session_dir.is_symlink():
            raise AgentLoopError(
                "STALE_EVIDENCE", "transcript session path is invalid")
        manifest_path = self.session_dir / "manifest.json"
        if manifest_path.exists():
            if not manifest_path.is_file() or manifest_path.is_symlink():
                raise AgentLoopError(
                    "STALE_EVIDENCE", "transcript manifest path is invalid")
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise AgentLoopError(
                    "STALE_EVIDENCE", "transcript manifest is malformed") \
                    from error
            if (not accepted(validate("project_transcript_manifest", manifest)) or
                    manifest.get("job_id") != self.job_id or
                    manifest.get("role") != self.role or
                    manifest.get("session_id") != self.session_id or
                    manifest.get("lineage") != self.lineage):
                raise AgentLoopError(
                    "STALE_EVIDENCE", "transcript manifest authority is stale")
            self.entries = copy.deepcopy(manifest["entries"])
            self.manifest = manifest
        else:
            paths = sorted(
                self.session_dir.glob("[0-9][0-9][0-9][0-9].*.json"))
            for sequence, path in enumerate(paths, 1):
                match = re.fullmatch(
                    r"([0-9]{4})\.([a-z_]+)\.json", path.name)
                kind = match.group(2).upper() if match else ""
                if (match is None or int(match.group(1)) != sequence or
                        kind not in _EVENT_KINDS or path.is_symlink() or
                        not path.is_file()):
                    raise AgentLoopError(
                        "STALE_EVIDENCE", "transcript sequence is invalid")
                self.entries.append({
                    "sequence": sequence, "kind": kind, "path": path.name,
                    "content_fingerprint": hashlib.sha256(
                        path.read_bytes()).hexdigest(),
                })
        for entry in self.entries:
            self._entry_value(entry)

    def value(self, index: int, kind: str) -> Any | None:
        if index >= len(self.entries):
            return None
        entry = self.entries[index]
        if entry["kind"] != kind:
            raise AgentLoopError(
                "STALE_EVIDENCE", "transcript event order is invalid")
        return self._entry_value(entry)

    def record(self, kind: str, value: Any) -> None:
        if self.manifest is not None:
            raise AgentLoopError(
                "STALE_EVIDENCE", "terminal transcript cannot be extended")
        if kind not in _EVENT_KINDS:
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "transcript event kind is invalid")
        sequence = len(self.entries) + 1
        filename = "{:04d}.{}.json".format(sequence, kind.lower())
        encoded = self._encoded(value)
        publish_immutable_bytes(
            self.session_dir / filename, encoded,
            lambda message: AgentLoopError("STALE_EVIDENCE", message),
            "transcript event already exists with conflicting bytes")
        self.entries.append({
            "sequence": sequence, "kind": kind, "path": filename,
            "content_fingerprint": hashlib.sha256(encoded).hexdigest(),
        })

    def finalize(
            self, status: str, code: str,
            result_sequence: int | None = None) -> dict[str, Any]:
        if self.manifest is not None:
            return copy.deepcopy(self.manifest)
        manifest = {
            "schema_version": "1.0", "session_id": self.session_id,
            "job_id": self.job_id, "role": self.role,
            "lineage": copy.deepcopy(self.lineage),
            "entries": copy.deepcopy(self.entries),
            "terminal": {
                "status": status, "code": code,
                "result_sequence": result_sequence,
            },
            "manifest_fingerprint": "0" * 64,
        }
        manifest["manifest_fingerprint"] = canonical_hash({
            key: value for key, value in manifest.items()
            if key != "manifest_fingerprint"})
        if not accepted(validate("project_transcript_manifest", manifest)):
            raise AgentLoopError(
                "INVALID_SCHEMA", "generated transcript manifest is invalid")
        encoded = self._encoded(manifest)
        publish_immutable_bytes(
            self.session_dir / "manifest.json", encoded,
            lambda message: AgentLoopError("STALE_EVIDENCE", message),
            "transcript manifest already exists with conflicting bytes")
        self.manifest = manifest
        return copy.deepcopy(manifest)


def create_transcript_store(
        *, job_root: Path, job_id: str, role: str, session_id: str,
        lineage: Mapping[str, Any]) -> TranscriptStore:
    return TranscriptStore(
        transcript_session_dir(job_root, role, session_id), job_id=job_id,
        role=role, session_id=session_id, lineage=lineage)


def load_terminal_transcript(
        *, job_root: Path, job_id: str, role: str, session_id: str,
        lineage: dict[str, Any]) -> dict[str, Any]:
    """Load one existing terminal transcript without creating evidence."""
    session_dir = transcript_session_dir(job_root, role, session_id)
    if not session_dir.exists():
        raise AgentLoopError(
            "STALE_EVIDENCE", "terminal transcript session is unavailable")
    transcript = TranscriptStore(
        session_dir, job_id=job_id, role=role,
        session_id=session_id, lineage=lineage)
    if transcript.manifest is None:
        raise AgentLoopError("STALE_EVIDENCE", "transcript is not terminal")
    return copy.deepcopy(transcript.manifest)


def load_terminal_transcript_events(
        *, job_root: Path, job_id: str, role: str, session_id: str,
        lineage: dict[str, Any]) -> dict[str, Any]:
    """Load one terminal manifest and every fingerprint-verified raw event."""
    session_dir = transcript_session_dir(job_root, role, session_id)
    if not session_dir.exists():
        raise AgentLoopError(
            "STALE_EVIDENCE", "terminal transcript session is unavailable")
    transcript = TranscriptStore(
        session_dir, job_id=job_id, role=role,
        session_id=session_id, lineage=lineage)
    if transcript.manifest is None:
        raise AgentLoopError("STALE_EVIDENCE", "transcript is not terminal")
    return {
        "manifest": copy.deepcopy(transcript.manifest),
        "events": [
            {
                "kind": entry["kind"],
                "value": transcript.value(index, entry["kind"]),
            }
            for index, entry in enumerate(transcript.manifest["entries"])
        ],
    }


__all__ = [
    "ROLE_DIRECTORIES", "TranscriptStore", "create_transcript_store",
    "load_terminal_transcript", "load_terminal_transcript_events",
    "transcript_session_dir",
]
