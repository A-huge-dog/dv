"""OCHES002 scope-rich dispatch and immutable-identity replacement checks."""
from __future__ import annotations

import copy
import json
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from contracts.validator import accepted, validate
from agents.project_tools import ProjectReadModel
from domain.artifacts import artifact_fingerprint
from domain.repair import (
    formalize_scoped_replacement, validate_current_dispatch_authority,
    validate_scoped_replacement,
)
from scripts.dvlib import canonical_hash
































def _load_explicit_json(
        job_root: Path, relative: Any, expected_parts: tuple[str, ...],
        error: Callable[[str, str], Exception]) -> dict[str, Any]:
    if not isinstance(relative, str):
        raise error("STALE_EVIDENCE", "authority path is missing")
    pure = PurePosixPath(relative)
    if (pure.is_absolute() or pure.parts[:len(expected_parts)] !=
            expected_parts or len(pure.parts) != len(expected_parts) + 1 or
            any(part in {"", ".", ".."} or part.startswith(".")
                for part in pure.parts)):
        raise error("STALE_EVIDENCE", "authority path is outside its directory")
    path = Path(job_root).joinpath(*pure.parts)
    try:
        if (not path.is_file() or path.is_symlink() or
                Path(job_root).resolve() not in path.resolve().parents):
            raise OSError("authority file is unavailable")
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as caught:
        raise error("STALE_EVIDENCE", "authority file is unavailable") from caught
    if not isinstance(value, dict):
        raise error("STALE_EVIDENCE", "authority file is malformed")
    return value


def validate_scoped_replacement_lineage(
        job_root: Path, checkpoint: Mapping[str, Any],
        model: ProjectReadModel, expected_stage_binding: Mapping[str, Any],
        error: Callable[[str, str], Exception]) -> dict[str, Any]:
    """Replay checkpoint → replacement → dispatch → exact response."""
    if (checkpoint.get("state") != "SCOPED_REPLACEMENT_VALIDATED" or
            checkpoint.get("job_id") != model.job_id or
            checkpoint.get("input_fingerprint") != model.input_fingerprint or
            checkpoint.get("checkpoint_fingerprint") !=
                artifact_fingerprint(checkpoint, "checkpoint_fingerprint")):
        raise error("STALE_EVIDENCE", "validated replacement checkpoint is stale")
    replacement = _load_explicit_json(
        job_root, checkpoint.get("replacement_path"),
        ("staging", "scoped_replacements"), error)
    dispatch = _load_explicit_json(
        job_root, checkpoint.get("dispatch_path"),
        ("staging", "dispatch"), error)
    plan = _load_explicit_json(
        job_root, checkpoint.get("plan_path"),
        ("staging", "orchestrator"), error)
    receipt = _load_explicit_json(
        job_root, checkpoint.get("router_receipt_path"), ("audit",), error)
    validate_current_dispatch_authority(
        dispatch, model, expected_stage_binding, error,
        plan=plan, receipt=receipt, checkpoint=checkpoint)
    if (replacement.get("replacement_fingerprint") !=
            checkpoint.get("replacement_fingerprint") or
            checkpoint.get("replacement_path") !=
                "staging/scoped_replacements/{}.json".format(
                    str(checkpoint.get("replacement_fingerprint"))[:24])):
        raise error(
            "STALE_EVIDENCE", "checkpoint does not reference exact replacement")

    stage = dispatch["stage"]
    session_id = replacement.get("session_id")
    if not isinstance(session_id, str):
        raise error("STALE_EVIDENCE", "replacement session identity is missing")
    lineage = {
        "dispatch_id": dispatch["dispatch_id"],
        "dispatch_fingerprint": dispatch["dispatch_fingerprint"],
        "scope_fingerprint": dispatch["scope_fingerprint"],
        "artifact_root": model.artifact_root,
        "runtime_role": "STAGE_AGENT",
        **copy.deepcopy(dict(expected_stage_binding)),
    }
    try:
        from infrastructure.persistence.transcript_store import (
            load_terminal_transcript_events,
        )
        transcript = load_terminal_transcript_events(
            job_root=Path(job_root), job_id=model.job_id, role=stage,
            session_id=session_id, lineage=lineage)
    except Exception as caught:
        raise error(
            "STALE_EVIDENCE", "replacement transcript is unavailable") \
            from caught
    manifest = transcript["manifest"]
    events = transcript["events"]
    if manifest.get("terminal") != {
            "status": "COMPLETED", "code": "COMPLETED",
            "result_sequence": len(manifest["entries"])}:
        raise error("STALE_EVIDENCE", "replacement transcript is not complete")
    request_id = replacement.get("stage_agent", {}).get("request_id")
    response_id = replacement.get("stage_agent", {}).get("response_id")
    request = response = tool_call = None
    for index, event in enumerate(events):
        if event["kind"] != "RESPONSE":
            continue
        value = event["value"]
        if value.get("provider_metadata", {}).get("response_id") == response_id:
            if index == 0 or events[index - 1]["kind"] != "REQUEST":
                raise error("STALE_EVIDENCE", "response has no exact request")
            request = events[index - 1]["value"]
            response = value
            if (index + 1 >= len(events) or
                    events[index + 1]["kind"] != "TOOL_CALL"):
                raise error("STALE_EVIDENCE", "response has no submission call")
            tool_call = events[index + 1]["value"]
            break
    submit_name = "submit_stage{}_replacement".format(stage[-1])
    if (request is None or response is None or tool_call is None or
            request.get("request_id") != request_id or
            tool_call.get("name") != submit_name or
            not isinstance(tool_call.get("arguments"), dict)):
        raise error(
            "STALE_EVIDENCE", "replacement does not bind exact submission turn")
    rebuilt = formalize_scoped_replacement(
        tool_call["arguments"], dispatch, model, session_id,
        request, response, error)
    if rebuilt != replacement:
        raise error(
            "STALE_EVIDENCE", "replacement differs from formalized transcript")
    return validate_scoped_replacement(
        replacement, dispatch, model, error, session_id=session_id,
        request=request, response=response)
