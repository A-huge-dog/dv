"""Deterministic, resumable exact-one sequential tool sessions."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from contracts.validator import accepted, validate
from core.atomic_artifact import publish_immutable_bytes
from scripts.dvlib import canonical_hash, validate_schema
from core.project_oches003 import map_provider_stop


ROLE_DIRECTORIES = {
    "ORCHESTRATOR": "orchestrator",
    "STAGE_1": "stage1",
    "STAGE_2": "stage2",
    "STAGE_3": "stage3",
    "REVIEWER": "reviewer",
}
_EVENT_KINDS = {"REQUEST", "RESPONSE", "TOOL_CALL", "TOOL_RESULT"}


class ToolSessionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__("[{}] {}".format(code, message))
        self.code = code
        self.message = message


class _RawTranscript:
    """Append raw events and resume only fingerprint-valid persisted work."""

    def __init__(
            self, job_root: Path, job_id: str, role: str, session_id: str,
            lineage: dict[str, Any]):
        self.session_dir = (
            Path(job_root) / "transcripts" / ROLE_DIRECTORIES[role] / session_id)
        self.job_id = job_id
        self.role = role
        self.session_id = session_id
        self.lineage = copy.deepcopy(lineage)
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
            raise ToolSessionError(
                "INVALID_TOOL_CALL",
                "transcript event is not losslessly JSON serializable") \
                from error

    def _entry_value(self, entry: Mapping[str, Any]) -> Any:
        path = self.session_dir / str(entry["path"])
        if (not path.is_file() or path.is_symlink() or
                hashlib.sha256(path.read_bytes()).hexdigest() !=
                    entry["content_fingerprint"]):
            raise ToolSessionError(
                "STALE_EVIDENCE", "transcript raw event is stale")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ToolSessionError(
                "STALE_EVIDENCE", "transcript event is malformed") from error

    def _load(self) -> None:
        if not self.session_dir.is_dir() or self.session_dir.is_symlink():
            raise ToolSessionError(
                "STALE_EVIDENCE", "transcript session path is invalid")
        manifest_path = self.session_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ToolSessionError(
                    "STALE_EVIDENCE", "transcript manifest is malformed") \
                    from error
            if (not accepted(validate("project_transcript_manifest", manifest)) or
                    manifest.get("job_id") != self.job_id or
                    manifest.get("role") != self.role or
                    manifest.get("session_id") != self.session_id or
                    manifest.get("lineage") != self.lineage):
                raise ToolSessionError(
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
                    raise ToolSessionError(
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
            raise ToolSessionError(
                "STALE_EVIDENCE", "transcript event order is invalid")
        return self._entry_value(entry)

    def record(self, kind: str, value: Any) -> None:
        sequence = len(self.entries) + 1
        filename = "{:04d}.{}.json".format(sequence, kind.lower())
        encoded = self._encoded(value)
        publish_immutable_bytes(
            self.session_dir / filename, encoded,
            lambda message: ToolSessionError("STALE_EVIDENCE", message),
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
            raise ToolSessionError(
                "INVALID_SCHEMA", "generated transcript manifest is invalid")
        encoded = self._encoded(manifest)
        publish_immutable_bytes(
            self.session_dir / "manifest.json", encoded,
            lambda message: ToolSessionError("STALE_EVIDENCE", message),
            "transcript manifest already exists with conflicting bytes")
        self.manifest = manifest
        return copy.deepcopy(manifest)


class SequentialToolSession:
    """Run at most three distinct retrievals and one final submission."""

    MAX_RETRIEVAL_TURNS = 3

    def __init__(
            self, *, provider: Any, job_root: Path, job_id: str, role: str,
            session_id: str, lineage: dict[str, Any],
            initial_messages: list[dict[str, str]],
            tools: list[dict[str, Any]],
            retrieval_handlers: Mapping[str, Callable[[dict[str, Any]], Any]],
            submission_tool: str,
            submission_handler: Callable[[dict[str, Any], dict[str, Any]], Any],
            provider_binding: Mapping[str, str],
            request_metadata: Mapping[str, Any] | None = None,
            cancel_requested: Callable[[], bool] | None = None):
        if role not in ROLE_DIRECTORIES:
            raise ToolSessionError(
                "TOOL_PERMISSION_DENIED", "session role is not supported")
        if not re.fullmatch(r"JOB\.PROJECT\.[A-Z0-9_.-]+", job_id):
            raise ToolSessionError(
                "INVALID_TOOL_CALL", "session Job identity is invalid")
        if not re.fullmatch(r"[A-Z][A-Z0-9_.-]+", session_id):
            raise ToolSessionError(
                "INVALID_TOOL_CALL", "session identity is invalid")
        if not initial_messages:
            raise ToolSessionError(
                "INVALID_TOOL_CALL", "session requires initial messages")
        names = [item.get("name") for item in tools]
        retrieval_names = set(retrieval_handlers)
        if (any(not isinstance(name, str) or not name for name in names) or
                len(names) != len(set(names)) or
                submission_tool in retrieval_names or
                submission_tool not in names or
                not retrieval_names.issubset(set(names))):
            raise ToolSessionError(
                "INVALID_TOOL_CALL", "session tool registration is invalid")
        if (set(provider_binding) != {"provider_id", "model_id"} or
                any(not isinstance(value, str) or not value
                    for value in provider_binding.values())):
            raise ToolSessionError(
                "INVALID_TOOL_CALL", "session provider binding is invalid")
        self.provider = provider
        self.job_root = Path(job_root)
        self.job_id = job_id
        self.role = role
        self.session_id = session_id
        self.lineage = copy.deepcopy(lineage)
        self.initial_messages = copy.deepcopy(initial_messages)
        allowed = retrieval_names | {submission_tool}
        self.tools = [copy.deepcopy(item) for item in tools
                      if item["name"] in allowed]
        self.retrieval_handlers = dict(retrieval_handlers)
        self.submission_tool = submission_tool
        self.submission_handler = submission_handler
        self.provider_binding = dict(provider_binding)
        self.request_metadata = copy.deepcopy(dict(request_metadata or {}))
        if set(self.request_metadata) & {
                "job_id", "role", "session_id", "retrieval_turns_completed",
                "parallel_tool_calls"}:
            raise ToolSessionError(
                "INVALID_TOOL_CALL", "session request metadata is reserved")
        self.cancel_requested = cancel_requested or (lambda: False)
        self.retrieval_count = 0
        self._terminal = False

    @staticmethod
    def _history_message(label: str, value: Any, role: str) -> dict[str, str]:
        try:
            content = label + "\n" + json.dumps(
                value, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ToolSessionError(
                "INVALID_TOOL_CALL",
                "tool session history is not losslessly JSON serializable") \
                from error
        return {"role": role, "content": content}

    def _request(
            self, turn: int, messages: list[dict[str, str]]) -> dict[str, Any]:
        legal = sorted(list(self.retrieval_handlers) + [self.submission_tool])
        return {
            "schema_version": "1.0",
            "request_id": "{}.TURN.{:03d}".format(self.session_id, turn),
            "operation": "SELECT_TOOLS",
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(self.tools),
            "tool_choice_policy": "REQUIRED", "legal_tool_names": legal,
            "metadata": {
                **copy.deepcopy(self.request_metadata),
                "job_id": self.job_id, "role": self.role,
                "session_id": self.session_id,
                "retrieval_turns_completed": self.retrieval_count,
                "parallel_tool_calls": False,
            },
        }

    def _validate_response(
            self, request: Mapping[str, Any], response: Mapping[str, Any]
            ) -> None:
        if (not accepted(validate("provider_response", response)) or
                response.get("request_id") != request["request_id"] or
                response.get("operation") != "SELECT_TOOLS" or
                response.get("model_id") != self.provider_binding["model_id"] or
                response.get("provider_metadata", {}).get("provider_id") !=
                    self.provider_binding["provider_id"]):
            raise ToolSessionError(
                "INVALID_AGENT_BINDING",
                "provider response does not match the fixed session binding")

    def run(self) -> Any:
        if self._terminal:
            raise ToolSessionError(
                "INVALID_TOOL_CALL", "session already reached a terminal result")
        self._terminal = True
        transcript = _RawTranscript(
            self.job_root, self.job_id, self.role, self.session_id,
            self.lineage)
        if transcript.manifest is not None:
            terminal = transcript.manifest["terminal"]
            if terminal["status"] == "COMPLETED":
                sequence = terminal["result_sequence"]
                if sequence is None:
                    raise ToolSessionError(
                        "STALE_EVIDENCE", "completed session has no result")
                return transcript.value(sequence - 1, "TOOL_RESULT")
            raise ToolSessionError(
                terminal["code"], "persisted session is terminal")

        messages = copy.deepcopy(self.initial_messages)
        used_names: set[str] = set()
        cursor = 0
        turn = 0
        final_status, final_code = "FAILED", "SESSION_FAILED"
        result_sequence = None
        try:
            while True:
                turn += 1
                expected = self._request(turn, messages)
                request = transcript.value(cursor, "REQUEST")
                if request is None:
                    request = expected
                    transcript.record("REQUEST", request)
                elif request != expected:
                    raise ToolSessionError(
                        "STALE_EVIDENCE", "persisted session request is stale")
                cursor += 1

                response = transcript.value(cursor, "RESPONSE")
                if response is None:
                    try:
                        response = self.provider.select_tools(request)
                    except Exception as provider_error:
                        if isinstance(provider_error, ToolSessionError):
                            raise
                        if getattr(provider_error, "code", "") == \
                                "INVALID_PROVIDER_REQUEST":
                            raise ToolSessionError(
                                "INVALID_PROVIDER_REQUEST",
                                "Provider request violates the local adapter "
                                "contract") from provider_error
                        raise ToolSessionError(
                            "PROVIDER_UNAVAILABLE",
                            "Provider failed after its configured retry policy") \
                            from provider_error
                    transcript.record("RESPONSE", response)
                self._validate_response(request, response)
                cursor += 1
                calls = response.get("tool_calls", [])
                stop = map_provider_stop(
                    finish_reason=response.get("finish_reason"),
                    exception_code=(next((
                        item.get("code") for item in response.get(
                            "diagnostics", [])
                        if isinstance(item, dict) and item.get("code")), None)
                        if response.get("finish_reason") == "ERROR" else None),
                    tool_calls=calls,
                    legal_tools=self.retrieval_handlers,
                    submission_tool=self.submission_tool,
                    used_retrievals=used_names,
                    retrieval_count=self.retrieval_count,
                    arguments_valid=(len(calls) == 1 and isinstance(
                        calls[0].get("arguments"), dict)))
                if stop not in {"COMPLETED", "TOOL_RESULT_REQUIRED"}:
                    raise ToolSessionError(
                        stop, "Provider stopped without one legal next action")
                call = copy.deepcopy(calls[0])
                persisted_call = transcript.value(cursor, "TOOL_CALL")
                if persisted_call is None:
                    transcript.record("TOOL_CALL", call)
                elif persisted_call != call:
                    raise ToolSessionError(
                        "STALE_EVIDENCE", "persisted tool call is stale")
                cursor += 1
                name, arguments = call.get("name"), call.get("arguments")
                if not isinstance(arguments, dict):
                    raise ToolSessionError(
                        "MALFORMED_MODEL_OUTPUT", "tool arguments must be an object")
                selected_tools = [item for item in self.tools
                                  if item["name"] == name]
                if (len(selected_tools) != 1 or validate_schema(
                        arguments, selected_tools[0]["input_schema"],
                        "tool_arguments")):
                    raise ToolSessionError(
                        "MALFORMED_MODEL_OUTPUT",
                        "tool arguments violate the selected tool schema")
                persisted_result = transcript.value(cursor, "TOOL_RESULT")
                if persisted_result is None and self.cancel_requested():
                    raise ToolSessionError(
                        "CANCELLED", "session was cancelled before tool execution")

                if name == self.submission_tool:
                    result = persisted_result
                    if result is None:
                        result = self.submission_handler(
                            copy.deepcopy(arguments), {
                                "request": copy.deepcopy(request),
                                "response": copy.deepcopy(response),
                                "retrieval_rounds": self.retrieval_count,
                            })
                        transcript.record("TOOL_RESULT", result)
                    cursor += 1
                    final_status, final_code = "COMPLETED", "COMPLETED"
                    result_sequence = cursor
                    return result
                if name not in self.retrieval_handlers:
                    raise ToolSessionError(
                        "TOOL_PROTOCOL_VIOLATION",
                        "tool is outside the role-specific retrieval allow-list")
                if name in used_names:
                    raise ToolSessionError(
                        "TOOL_PROTOCOL_VIOLATION",
                        "a retrieval tool name may be used only once per session")
                if self.retrieval_count >= self.MAX_RETRIEVAL_TURNS:
                    raise ToolSessionError(
                        "TOOL_PROTOCOL_VIOLATION",
                        "session requested a fourth retrieval turn")
                used_names.add(str(name))
                self.retrieval_count += 1
                result = persisted_result
                if result is None:
                    result = self.retrieval_handlers[str(name)](
                        copy.deepcopy(arguments))
                    transcript.record("TOOL_RESULT", result)
                cursor += 1
                messages.append(self._history_message(
                    "MODEL_RESPONSE", response, "ASSISTANT"))
                messages.append(self._history_message(
                    "TOOL_RESULT", {
                        "call_id": call.get("call_id", ""),
                        "tool_name": name, "result": result,
                    }, "USER"))
        except Exception as caught:
            caught_code = getattr(caught, "code", type(caught).__name__)
            final_status = "CANCELLED" if caught_code == "CANCELLED" else "FAILED"
            final_code = caught_code
            raise
        finally:
            transcript.finalize(final_status, final_code, result_sequence)


def persist_single_submission_transcript(
        *, job_root: Path, job_id: str, role: str, session_id: str,
        lineage: dict[str, Any], request: dict[str, Any],
        response: dict[str, Any], result: Any | None,
        status: str = "COMPLETED", code: str = "COMPLETED"
        ) -> dict[str, Any]:
    """Persist one exact-one Provider submission without invoking Provider."""
    transcript = _RawTranscript(job_root, job_id, role, session_id, lineage)
    calls = response.get("tool_calls", [])
    if status == "COMPLETED" and len(calls) != 1:
        raise ToolSessionError(
            "MALFORMED_MODEL_OUTPUT",
            "single-submission transcript requires exactly one tool call")
    values = [("REQUEST", request), ("RESPONSE", response)]
    if len(calls) == 1:
        values.append(("TOOL_CALL", calls[0]))
    if result is not None:
        values.append(("TOOL_RESULT", result))
    for index, (kind, value) in enumerate(values):
        existing = transcript.value(index, kind)
        if existing is None:
            transcript.record(kind, value)
        elif existing != value:
            raise ToolSessionError(
                "CONFLICTING_REPLAY", "single-turn transcript replay conflicts")
    result_sequence = len(values) if result is not None else None
    return transcript.finalize(status, code, result_sequence)


def load_terminal_transcript(
        *, job_root: Path, job_id: str, role: str, session_id: str,
        lineage: dict[str, Any]) -> dict[str, Any]:
    """Load one existing terminal transcript without creating new evidence."""
    session_dir = (
        Path(job_root) / "transcripts" / ROLE_DIRECTORIES[role] / session_id)
    if not session_dir.exists():
        raise ToolSessionError(
            "STALE_EVIDENCE", "terminal transcript session is unavailable")
    transcript = _RawTranscript(
        job_root, job_id, role, session_id, lineage)
    if transcript.manifest is None:
        raise ToolSessionError(
            "STALE_EVIDENCE", "transcript is not terminal")
    return copy.deepcopy(transcript.manifest)


def load_terminal_transcript_events(
        *, job_root: Path, job_id: str, role: str, session_id: str,
        lineage: dict[str, Any]) -> dict[str, Any]:
    """Load one terminal manifest and every fingerprint-verified raw event."""
    session_dir = (
        Path(job_root) / "transcripts" / ROLE_DIRECTORIES[role] / session_id)
    if not session_dir.exists():
        raise ToolSessionError(
            "STALE_EVIDENCE", "terminal transcript session is unavailable")
    transcript = _RawTranscript(
        job_root, job_id, role, session_id, lineage)
    if transcript.manifest is None:
        raise ToolSessionError(
            "STALE_EVIDENCE", "transcript is not terminal")
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
    "ROLE_DIRECTORIES", "SequentialToolSession", "ToolSessionError",
    "load_terminal_transcript", "load_terminal_transcript_events",
    "persist_single_submission_transcript",
]
