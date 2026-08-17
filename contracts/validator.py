"""Contract validation for the provider-configured Project Job slice."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from scripts.dvlib import canonical_hash, validate_schema


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = {
    "diagnostics": ROOT / "contracts/output/diagnostics.schema.json",
    "approval_request":
        ROOT / "contracts/agent/approval_request.schema.json",
    "approval_decision":
        ROOT / "contracts/agent/approval_decision.schema.json",
    "provider_request":
        ROOT / "contracts/agent/provider_request.schema.json",
    "provider_response":
        ROOT / "contracts/agent/provider_response.schema.json",
    "provider_probe":
        ROOT / "contracts/agent/provider_probe.schema.json",
    "provider_config":
        ROOT / "contracts/agent/provider_config.schema.json",
    "project_agent_profile":
        ROOT / "contracts/agent/project_agent_profile.schema.yaml",
    "eda_probe_request":
        ROOT / "contracts/eda/eda_probe_request.schema.yaml",
    "eda_probe_evidence":
        ROOT / "contracts/eda/eda_probe_evidence.schema.yaml",
    "project_job_submission":
        ROOT / "contracts/project/project_job_submission.schema.yaml",
    "project_stage3_submission":
        ROOT / "contracts/project/project_stage3_submission.schema.yaml",
    "project_stage3_reviewer_submission":
        ROOT / "contracts/project/project_stage3_reviewer_submission.schema.yaml",
    "project_job_input":
        ROOT / "contracts/project/project_job_input.schema.yaml",
    "scenario_ac_map":
        ROOT / "contracts/project/scenario_ac_map.schema.yaml",
    "scenario_ac_candidate":
        ROOT / "contracts/project/scenario_ac_candidate.schema.yaml",
    "ac_testcase_map":
        ROOT / "contracts/project/ac_testcase_map.schema.yaml",
    "ac_testcase_candidate":
        ROOT / "contracts/project/ac_testcase_candidate.schema.yaml",
    "project_testcase_candidate":
        ROOT / "contracts/project/project_testcase_candidate.schema.yaml",
    "portable_sv_testcase_candidate":
        ROOT / "contracts/project/portable_sv_testcase_candidate.schema.yaml",
    "project_testcase_review_candidate":
        ROOT / "contracts/project/project_testcase_review_candidate.schema.yaml",
    "project_stage_blocked":
        ROOT / "contracts/project/project_stage_blocked.schema.yaml",
    "scenario_owner_review_form":
        ROOT / "contracts/project/scenario_owner_review_form.schema.yaml",
    "scenario_owner_review_submission":
        ROOT / "contracts/project/scenario_owner_review_submission.schema.yaml",
    "project_testcase_review_request":
        ROOT / "contracts/project/project_testcase_review_request.schema.yaml",
    "project_testcase_review_report":
        ROOT / "contracts/project/project_testcase_review_report.schema.yaml",
    "project_testcase_review_validation":
        ROOT / "contracts/project/project_testcase_review_validation.schema.yaml",
    "project_repair_plan":
        ROOT / "contracts/project/project_repair_plan.schema.yaml",
    "project_repair_plan_candidate":
        ROOT / "contracts/project/project_repair_plan_candidate.schema.yaml",
    "project_router_receipt":
        ROOT / "contracts/project/project_router_receipt.schema.yaml",
    "project_formal_dispatch":
        ROOT / "contracts/project/project_formal_dispatch.schema.yaml",
    "project_stage1_replacement":
        ROOT / "contracts/project/project_stage1_replacement.schema.yaml",
    "project_stage1_replacement_candidate":
        ROOT / "contracts/project/project_stage1_replacement_candidate.schema.yaml",
    "project_stage2_replacement":
        ROOT / "contracts/project/project_stage2_replacement.schema.yaml",
    "project_stage2_replacement_candidate":
        ROOT / "contracts/project/project_stage2_replacement_candidate.schema.yaml",
    "project_stage3_replacement":
        ROOT / "contracts/project/project_stage3_replacement.schema.yaml",
    "project_stage3_replacement_candidate":
        ROOT / "contracts/project/project_stage3_replacement_candidate.schema.yaml",
    "project_failure_feedback":
        ROOT / "contracts/project/project_failure_feedback.schema.yaml",
    "project_job_regeneration_state":
        ROOT / "contracts/project/project_job_regeneration_state.schema.yaml",
    "project_transcript_manifest":
        ROOT / "contracts/project/project_transcript_manifest.schema.yaml",
    "project_read_tool_result":
        ROOT / "contracts/project/project_read_tool_result.schema.yaml",
    "project_session_cancel_request":
        ROOT / "contracts/project/project_session_cancel_request.schema.yaml",
    "project_session_queue_event":
        ROOT / "contracts/project/project_session_queue_event.schema.yaml",
    "project_job_report":
        ROOT / "contracts/project/project_job_report.schema.yaml",
    "project_artifact_unit":
        ROOT / "contracts/project/project_artifact_unit.schema.yaml",
    "project_artifact_index":
        ROOT / "contracts/project/project_artifact_index.schema.yaml",
    "project_code_assembly":
        ROOT / "contracts/project/project_code_assembly.schema.yaml",
    "project_impact_manifest":
        ROOT / "contracts/project/project_impact_manifest.schema.yaml",
    "project_committed_testcase":
        ROOT / "contracts/project/project_committed_testcase.schema.yaml",
    "project_compile_validation":
        ROOT / "contracts/project/project_compile_validation.schema.yaml",
    "project_commit_manifest":
        ROOT / "contracts/project/project_commit_manifest.schema.yaml",
    "project_oches003_checkpoint":
        ROOT / "contracts/project/project_oches003_checkpoint.schema.yaml",
    "project_system_prompt":
        ROOT / "contracts/project/project_system_prompt.schema.yaml",
    "project_repair_index":
        ROOT / "contracts/project/project_repair_index.schema.yaml",
    **{
        "project_oches003_{}".format(name):
            ROOT / "contracts/project/project_oches003_{}_record.schema.yaml".format(name)
        for name in (
            "orchestrator_plan", "router_receipt", "formal_dispatch",
            "scoped_replacement", "validation_result", "group_commit",
            "impact_result", "repair_episode", "review_link",
            "replay_receipt")
    },
}


def load_document(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream) if str(path).endswith(".json") \
            else yaml.safe_load(stream)


def load_schema(kind):
    if kind not in SCHEMAS:
        raise ValueError("unsupported vertical-slice contract: {}".format(kind))
    return load_document(SCHEMAS[kind])


def diagnostic(code, message, path="", source_id="", required_owner=None,
               required_artifact_kind=None, required_decision_kind=None):
    severity = "INFO" if code == "ACCEPTED" else "ERROR"
    if severity == "INFO":
        owner, artifact, decision = "NONE", "NONE", "NONE"
    else:
        owner = required_owner or "DV_AGENT_DEVELOPER"
        artifact = required_artifact_kind or "RUNTIME_INPUT"
        decision = required_decision_kind or "NONE"
    return {
        "schema_version": "1.1",
        "code": code,
        "severity": severity,
        "message": message,
        "path": path,
        "source_id": source_id,
        "required_owner": owner,
        "required_artifact_kind": artifact,
        "required_decision_kind": decision,
    }


def eda_probe_request_fingerprint(value):
    keys = (
        "schema_version", "request_id", "job_id", "probe_kind",
        "executable_ref", "argv_template", "environment_fingerprint",
        "timeout_seconds", "resource_limits", "output_subdir",
        "expected_artifacts", "source_fingerprints", "approval_ref",
    )
    return canonical_hash({
        key: copy.deepcopy(value.get(key)) for key in keys})


def eda_probe_evidence_fingerprint(value):
    keys = (
        "schema_version", "evidence_id", "request_id",
        "request_fingerprint", "job_id", "probe_kind",
        "executable_ref", "environment_fingerprint", "started_at",
        "ended_at", "exit_code", "execution_status", "timed_out",
        "logs", "artifacts", "diagnostic_codes", "evidence_class",
        "qualification_scope",
    )
    return canonical_hash({
        key: copy.deepcopy(value.get(key)) for key in keys})


def _coded(code, message):
    return "[{}] {}".format(code, message)


def _diagnostic_errors(value):
    errors = []
    if value.get("severity") == "ERROR":
        if value.get("required_owner") == "NONE":
            errors.append(
                "diagnostics.required_owner: error must route to an owner")
        if (value.get("required_artifact_kind") == "NONE" and
                value.get("required_decision_kind") == "NONE"):
            errors.append(
                "diagnostics: error must request an artifact or decision")
    elif any(value.get(key) != "NONE" for key in (
            "required_owner", "required_artifact_kind",
            "required_decision_kind")):
        errors.append(
            "diagnostics: non-error routing fields must be NONE")
    return errors


def _provider_config_errors(value):
    errors = []
    parsed = urlsplit(value.get("endpoint", ""))
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        errors.append(
            "provider_config.endpoint: provider endpoint must be a "
            "credential-free HTTPS base URL")
    if value.get("auth_env") == "NONE":
        errors.append(
            "provider_config.auth_env: configured provider requires an "
            "environment reference")
    if value.get("api_version") not in {
            "chat-completions-v1", "responses-v1"}:
        errors.append(
            "provider_config.api_version: configured provider requires a "
            "supported API dialect")
    if value.get("schema_version") == "1.0":
        host = (parsed.hostname or "").lower()
        if value.get("api_version") != "chat-completions-v1":
            errors.append(
                "provider_config.api_version: legacy 1.0 config requires "
                "chat-completions-v1")
        if "reasoning_effort" in value:
            errors.append(
                "provider_config.reasoning_effort: legacy 1.0 config does "
                "not support this field")
        if value.get("provider_kind") != "DASHSCOPE":
            errors.append(
                "provider_config.provider_kind: legacy 1.0 config requires "
                "DASHSCOPE")
        if (not host.endswith(".aliyuncs.com") or
                parsed.path.rstrip("/") != "/compatible-mode/v1"):
            errors.append(
                "provider_config.endpoint: legacy 1.0 config requires the "
                "DashScope compatible-mode/v1 endpoint")
    else:
        if (value.get("api_version") == "responses-v1" and
                "enable_thinking" in value):
            errors.append(
                "provider_config.enable_thinking: responses-v1 uses the "
                "provider-neutral reasoning_effort field")
        if ("enable_thinking" in value and
                "reasoning_effort" in value):
            errors.append(
                "provider_config: enable_thinking and reasoning_effort are "
                "mutually exclusive")
    return errors


def _eda_probe_request_errors(value):
    errors = []
    if value.get("request_fingerprint") != \
            eda_probe_request_fingerprint(value):
        errors.append(_coded(
            "STALE_EVIDENCE",
            "eda_probe_request.request_fingerprint: canonical fingerprint "
            "mismatch"))
    tokens = value.get("argv_template", [])
    sources = value.get("source_fingerprints", [])
    source_by_path = {
        item.get("path"): item.get("fingerprint")
        for item in sources if isinstance(item, dict)}
    if (len(source_by_path) != len(sources) or
            [item.get("path") for item in sources] != sorted(source_by_path)):
        errors.append(_coded(
            "CONFLICTING_SOURCE",
            "eda_probe_request.source_fingerprints: paths must be canonical "
            "and unique"))
    forbidden = ("\n", "\r", ";", "|", "&&", "$(", "`", ">", "<")
    for token in tokens:
        text = str(token.get("value", ""))
        if (any(item in text for item in forbidden) or
                (token.get("kind") != "OUTPUT_DIR" and
                 (Path(text).is_absolute() or ".." in Path(text).parts))):
            errors.append(_coded(
                "TOOL_PERMISSION_DENIED",
                "eda_probe_request.argv_template: shell syntax and path "
                "escape are forbidden"))
        if token.get("kind") == "SOURCE" and text not in source_by_path:
            errors.append(_coded(
                "STALE_EVIDENCE",
                "eda_probe_request.argv_template: source token lacks an "
                "approved fingerprint"))
        if token.get("kind") == "OUTPUT_DIR" and text != "output_dir":
            errors.append(_coded(
                "INVALID_SCHEMA",
                "eda_probe_request.argv_template: output token must use "
                "output_dir"))
    artifact_paths = [
        item.get("relative_path")
        for item in value.get("expected_artifacts", [])
        if isinstance(item, dict)]
    if len(artifact_paths) != len(set(artifact_paths)):
        errors.append(_coded(
            "CONFLICTING_SOURCE",
            "eda_probe_request.expected_artifacts: duplicate artifact path"))
    output = value.get("output_subdir", "")
    if Path(output).is_absolute() or ".." in Path(output).parts:
        errors.append(_coded(
            "TOOL_PERMISSION_DENIED",
            "eda_probe_request.output_subdir: output escape is forbidden"))
    return errors


def _eda_probe_evidence_errors(value):
    errors = []
    if value.get("evidence_fingerprint") != \
            eda_probe_evidence_fingerprint(value):
        errors.append(_coded(
            "STALE_EVIDENCE",
            "eda_probe_evidence.evidence_fingerprint: canonical fingerprint "
            "mismatch"))
    status = value.get("execution_status")
    exit_code = value.get("exit_code")
    timed_out = value.get("timed_out")
    diagnostics = value.get("diagnostic_codes", [])
    if status == "PASS" and (exit_code != 0 or timed_out or diagnostics):
        errors.append(_coded(
            "INVALID_TRANSITION",
            "eda_probe_evidence.execution_status: PASS conflicts with "
            "failure evidence"))
    if status != "PASS" and not diagnostics:
        errors.append(_coded(
            "MISSING_REQUIRED_FIELD",
            "eda_probe_evidence.diagnostic_codes: failed probe requires "
            "diagnostics"))
    if timed_out and (
            status != "BLOCKED_TOOL" or
            "PROCESS_TIMEOUT" not in diagnostics):
        errors.append(_coded(
            "INVALID_TRANSITION",
            "eda_probe_evidence.timed_out: timeout must route as "
            "BLOCKED_TOOL"))
    logs = value.get("logs", [])
    if ({item.get("kind") for item in logs if isinstance(item, dict)} !=
            {"STDOUT", "STDERR"} or len(logs) != 2):
        errors.append(_coded(
            "MISSING_REQUIRED_FIELD",
            "eda_probe_evidence.logs: stdout and stderr evidence are required"))
    for collection, identity in (
            (logs, "relative_path"),
            (value.get("artifacts", []), "artifact_id")):
        identities = [
            item.get(identity) for item in collection
            if isinstance(item, dict)]
        if (identities != sorted(identities) or
                len(identities) != len(set(identities))):
            errors.append(_coded(
                "CONFLICTING_SOURCE",
                "eda_probe_evidence: logs and artifacts must be canonical "
                "and unique"))
    return errors


def _semantic_errors(kind, value):
    if not isinstance(value, dict):
        return []
    if kind == "diagnostics":
        return _diagnostic_errors(value)
    if kind == "provider_config":
        return _provider_config_errors(value)
    if kind == "approval_request" and \
            not value.get("validation_artifact_ids"):
        return [
            "approval_request.validation_artifact_ids: validation evidence "
            "is required"]
    if kind == "approval_decision" and not value.get("evidence_ids"):
        return [
            "approval_decision.evidence_ids: decision evidence is required"]
    if kind == "eda_probe_request":
        return _eda_probe_request_errors(value)
    if kind == "eda_probe_evidence":
        return _eda_probe_evidence_errors(value)
    if kind == "project_transcript_manifest":
        errors = []
        entries = value.get("entries", [])
        if isinstance(entries, list) and [
                item.get("sequence") for item in entries
                if isinstance(item, dict)] != list(range(1, len(entries) + 1)):
            errors.append(_coded(
                "INVALID_SCHEMA",
                "project_transcript_manifest.entries: sequence must be "
                "contiguous and ordered"))
        projected = copy.deepcopy(value)
        projected.pop("manifest_fingerprint", None)
        if value.get("manifest_fingerprint") != canonical_hash(projected):
            errors.append(_coded(
                "STALE_EVIDENCE",
                "project_transcript_manifest.manifest_fingerprint: canonical "
                "fingerprint mismatch"))
        return errors
    if kind == "project_read_tool_result":
        projected = copy.deepcopy(value)
        projected.pop("result_fingerprint", None)
        if value.get("result_fingerprint") != canonical_hash(projected):
            return [_coded(
                "STALE_EVIDENCE",
                "project_read_tool_result.result_fingerprint: canonical "
                "fingerprint mismatch")]
        return []
    if kind == "project_testcase_review_request":
        phase = value.get("review_phase")
        previous = value.get("previous_report")
        previous_fingerprint = value.get("previous_report_fingerprint")
        lineage = value.get("repair_lineage")
        if phase == "INITIAL" and (
                previous is not None or previous_fingerprint != "NONE" or
                lineage != []):
            return [_coded(
                "INVALID_SCHEMA",
                "initial Reviewer request cannot carry repair history")]
        if phase == "FINAL":
            if (not isinstance(previous, dict) or
                    previous.get("report_fingerprint") !=
                        previous_fingerprint or
                    not isinstance(lineage, list) or not lineage):
                return [_coded(
                    "MISSING_REQUIRED_FIELD",
                    "final Reviewer request requires full prior report and "
                    "repair lineage")]
            for item in lineage:
                if (not isinstance(item, dict) or
                        not isinstance(item.get("record"), dict) or
                        item.get("record_fingerprint") != canonical_hash(
                            item["record"])):
                    return [_coded(
                        "STALE_EVIDENCE",
                        "Reviewer repair lineage record is stale")]
        return []
    if kind in {"project_session_cancel_request",
                "project_session_queue_event"}:
        field = "request_fingerprint" if kind.endswith("cancel_request") \
            else "event_fingerprint"
        projected = copy.deepcopy(value)
        projected.pop(field, None)
        if value.get(field) != canonical_hash(projected):
            return [_coded(
                "STALE_EVIDENCE", "{} fingerprint is stale".format(kind))]
        return []
    if kind == "scenario_owner_review_submission":
        submitted = value.get("submitted_form")
        if isinstance(submitted, dict):
            return validate_schema(
                submitted, load_schema("scenario_owner_review_form"),
                "scenario_owner_review_submission.submitted_form")
    return []


def validate(kind, value):
    try:
        raw = validate_schema(value, load_schema(kind), kind)
    except (OSError, ValueError, TypeError) as error:
        raw = ["{}: validator unavailable ({})".format(
            kind, type(error).__name__)]
    raw.extend(_semantic_errors(kind, value))
    owner_by_kind = {
        "approval_request": "DV_REVIEWER",
        "approval_decision": "DV_REVIEWER",
        "eda_probe_request": "EDA_OWNER",
        "eda_probe_evidence": "EDA_OWNER",
        "project_job_submission": "SPEC_OWNER",
        "project_stage3_submission": "STAGE3_TEST_OWNER",
        "project_job_input": "DV_REVIEWER",
        "scenario_owner_review_form": "DV_OWNER",
        "scenario_owner_review_submission": "DV_OWNER",
        "project_testcase_candidate": "DV_REVIEWER",
        "project_testcase_review_request": "DV_REVIEWER",
        "project_testcase_review_report": "DV_REVIEWER",
        "project_testcase_review_validation": "DV_REVIEWER",
        "project_repair_plan": "ORCHESTRATOR",
        "project_router_receipt": "ROUTER",
        "project_formal_dispatch": "ROUTER",
        "project_stage1_replacement": "STAGE1_TEST_OWNER",
        "project_stage2_replacement": "STAGE2_TEST_OWNER",
        "project_stage3_replacement": "STAGE3_TEST_OWNER",
        "project_failure_feedback": "ORCHESTRATOR",
        "project_job_regeneration_state": "ROUTER",
        "project_transcript_manifest": "DV_AGENT_DEVELOPER",
        "project_read_tool_result": "DV_AGENT_DEVELOPER",
        "project_session_cancel_request": "DV_OWNER",
        "project_session_queue_event": "DV_AGENT_DEVELOPER",
        "project_job_report": "DV_REVIEWER",
        "project_committed_testcase": "DV_AGENT_DEVELOPER",
        "project_compile_validation": "EDA_OWNER",
        "project_commit_manifest": "DV_AGENT_DEVELOPER",
        "project_oches003_checkpoint": "DV_REVIEWER",
    }
    routing_by_code = {
        "STALE_EVIDENCE": ("DV_REVIEWER", "IMMUTABLE_EVIDENCE"),
        "TOOL_PERMISSION_DENIED": ("EDA_OWNER", "EDA_PERMISSION"),
        "CONFLICTING_SOURCE": ("DV_REVIEWER", "SOURCE_EVIDENCE"),
    }
    diagnostics = []
    for error in raw:
        if error.startswith("[") and "] " in error:
            code, message = error[1:].split("] ", 1)
        else:
            code = "MISSING_REQUIRED_FIELD" \
                if "missing required field" in error else "INVALID_SCHEMA"
            message = error
        owner, artifact = routing_by_code.get(
            code,
            (owner_by_kind.get(kind, "DV_AGENT_DEVELOPER"),
             kind.upper() + "_CONTRACT"))
        diagnostics.append(diagnostic(
            code, message, message.split(":", 1)[0],
            required_owner=owner, required_artifact_kind=artifact))
    return diagnostics or [
        diagnostic("ACCEPTED", "{} contract accepted".format(kind))]


def accepted(diagnostics):
    return len(diagnostics) == 1 and diagnostics[0]["code"] == "ACCEPTED"
