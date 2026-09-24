"""Pure Stage 3 candidate assembly and deterministic validation rules."""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable

from contracts.validator import accepted, validate
from domain.artifacts import artifact_fingerprint
from domain.evidence import (
    _bounded_text, _failure_with_context, _provider_identity, _sha,
)
from domain.uvm_testcase import build_manifest, validate_generated_tests
from scripts.dvlib import canonical_hash


MAX_CODE_EVIDENCE_BYTES = 16384
MAX_RETRY_CORRECTION_BYTES = 1024
MAX_STAGE3_DIAGNOSTICS = 64
MAX_STAGE3_DIAGNOSTIC_BYTES = 65536

def _stage3_diagnostic(
        code: str, message: str, ac_id: str = "NONE",
        evidence_kind: str = "CANDIDATE", offending_content: Any = "",
        match_count: int = 0, required_correction: str = "") -> dict[str, Any]:
    """Return a bounded, replay-stable Stage 3 diagnostic item."""
    return {
        "code": code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else
            "INVALID_GENERATED_ARTIFACT",
        "message": _bounded_text(message, 1024),
        "ac_id": ac_id if isinstance(ac_id, str) and ac_id else "NONE",
        "evidence_kind": evidence_kind if evidence_kind in {
            "STIMULUS", "CHECKER", "CANDIDATE"} else "CANDIDATE",
        "offending_content": _bounded_text(
            offending_content, MAX_CODE_EVIDENCE_BYTES),
        "match_count": match_count if type(match_count) is int and
            match_count >= 0 else 0,
        "required_correction": _bounded_text(
            required_correction or
            "Repair this typed Stage 3 validation failure without changing "
            "authority, lineage, or valid evidence.",
            MAX_RETRY_CORRECTION_BYTES),
    }

def _raise_stage3_diagnostics(
        error: Callable[..., Exception], diagnostics: list[dict[str, Any]],
        unexecuted_checks: list[str] | None = None) -> None:
    """Fail closed after every safe independent Stage 3 check has run."""
    canonical: dict[str, dict[str, Any]] = {}
    for item in diagnostics:
        normalized = _stage3_diagnostic(**item)
        key = canonical_hash(normalized)
        canonical[key] = normalized
    ordered = sorted(canonical.values(), key=lambda item: (
        item["code"], item["ac_id"], item["evidence_kind"],
        item["offending_content"], item["match_count"],
        item["required_correction"]))
    encoded = json.dumps(ordered, sort_keys=True, ensure_ascii=False).encode("utf-8")
    truncated = False
    if len(ordered) > MAX_STAGE3_DIAGNOSTICS or len(encoded) > MAX_STAGE3_DIAGNOSTIC_BYTES:
        ordered = ordered[:MAX_STAGE3_DIAGNOSTICS]
        while len(json.dumps(ordered, sort_keys=True, ensure_ascii=False).encode("utf-8")) > MAX_STAGE3_DIAGNOSTIC_BYTES:
            ordered.pop()
        truncated = True
    if not ordered:
        ordered = [_stage3_diagnostic(
            "INVALID_GENERATED_ARTIFACT", "Stage 3 validation failed")]
    primary = ordered[0]
    context = {key: primary[key] for key in (
        "ac_id", "evidence_kind", "offending_content", "match_count",
        "required_correction")}
    context.update({
        "diagnostics": ordered,
        "diagnostics_truncated": truncated,
        "unexecuted_checks": sorted(set(unexecuted_checks or [])),
    })
    raise _failure_with_context(
        error, primary["code"],
        "Stage 3 validation failed: {}".format(
            ",".join(item["code"] for item in ordered)), context)

def _sv_code_tokens(content: str) -> str:
    """Mask comments and quoted strings for deliberately structural checks."""
    result: list[str] = []
    index = 0
    state = "code"
    while index < len(content):
        pair = content[index:index + 2]
        char = content[index]
        if state == "code" and pair == "//":
            state = "line_comment"
            result.extend("  ")
            index += 2
        elif state == "code" and pair == "/*":
            state = "block_comment"
            result.extend("  ")
            index += 2
        elif state == "code" and char == '"':
            state = "string"
            result.append(" ")
            index += 1
        elif state == "line_comment":
            result.append("\n" if char == "\n" else " ")
            if char == "\n":
                state = "code"
            index += 1
        elif state == "block_comment":
            if pair == "*/":
                state = "code"
                result.extend("  ")
                index += 2
            else:
                result.append("\n" if char == "\n" else " ")
                index += 1
        elif state == "string":
            if char == "\\" and index + 1 < len(content):
                result.extend("  ")
                index += 2
            else:
                result.append("\n" if char == "\n" else " ")
                if char == '"':
                    state = "code"
                index += 1
        else:
            result.append(char)
            index += 1
    return "".join(result)

def validate_testcase_candidate(
        value: dict[str, Any], map1: dict[str, Any], map2: dict[str, Any],
        logical_testcases: list[dict[str, Any]],
        project_input: dict[str, Any], spec_fingerprint: str,
        policy_fingerprint: str, error: Callable[..., Exception],
        initial_diagnostics: list[dict[str, Any]] | None = None,
        skip_schema: bool = False
        ) -> dict[str, Any]:
    if not skip_schema and not accepted(validate("project_testcase_candidate", value)):
        raise error("INVALID_SCHEMA", "testcase candidate contract is invalid")
    if value["candidate_fingerprint"] != artifact_fingerprint(
            value, "candidate_fingerprint"):
        raise error("STALE_EVIDENCE", "testcase candidate fingerprint is stale")
    if value["content_fingerprint"] != _sha(value["content"]):
        raise error("STALE_EVIDENCE", "testcase content fingerprint is stale")
    map1_fp = map1["artifact_fingerprint"]
    map2_fp = map2["artifact_fingerprint"]
    if (
        value["job_id"] != project_input["job_id"] or
        value["input_fingerprint"] != project_input["input_fingerprint"] or
        value["spec_fingerprint"] != spec_fingerprint or
        value["scenario_ac_map_fingerprint"] != map1_fp or
        value["ac_testcase_map_fingerprint"] != map2_fp or
        value["policy_fingerprint"] != policy_fingerprint or
        value["upstream_fingerprints"] != {
            "input": project_input["input_fingerprint"],
            "spec": spec_fingerprint,
            "scenario_ac_map": map1_fp,
            "ac_testcase_map": map2_fp,
            "effective_uvm": value["effective_uvm_root"],
        }
    ):
        raise error("STALE_EVIDENCE",
                    "testcase candidate upstream lineage is stale")
    content = value["content"]
    diagnostics: list[dict[str, Any]] = list(initial_diagnostics or [])
    code_units = value["code_units"]
    code_by_id = {item["code_unit_id"]: item for item in code_units}
    if (len(code_by_id) != len(code_units) or
            code_units != sorted(code_units, key=lambda item: item["code_unit_id"]) or
            len(value["assembly_manifest"]) != len(set(value["assembly_manifest"])) or
            set(value["assembly_manifest"]) != set(code_by_id)):
        diagnostics.append(_stage3_diagnostic(
            "ASSEMBLY_MISMATCH", "formal code-unit manifest is not complete/canonical",
            required_correction="Submit unique generic code units and one complete assembly order."))
    elif (any(item["content_fingerprint"] != _sha(item["content"])
              for item in code_units) or
          "".join(code_by_id[unit_id]["content"]
                  for unit_id in value["assembly_manifest"]) != content):
        diagnostics.append(_stage3_diagnostic(
            "ASSEMBLY_MISMATCH", "formal code units do not reconstruct complete content",
            required_correction="Make the code-unit assembly reproduce exact candidate bytes."))
    implemented_ids = set(value["implemented_testcase_ids"])
    skipped = value["skipped_testcases"]
    skipped_ids = [item["testcase_id"] for item in skipped]
    skipped_id_set = set(skipped_ids)
    testcase_unit_ids = set().union(*(set(item["testcase_ids"])
                                      for item in code_units
                                      if item["role"] == "TESTCASE")) \
        if code_units else set()
    if (any((item["role"] == "SHARED" and item["testcase_ids"]) or
            (item["role"] == "TESTCASE" and not item["testcase_ids"]) or
            not set(item["testcase_ids"]).issubset(implemented_ids)
            for item in code_units) or testcase_unit_ids != implemented_ids):
        diagnostics.append(_stage3_diagnostic(
            "TESTCASE_MAPPING_OVERREACH", "formal generic code-unit roles are invalid",
            required_correction="Use one or more mapped TESTCASE units and optional SHARED units."))
    if len(content.encode("utf-8")) > 262144 or "\x00" in content:
        diagnostics.append(_stage3_diagnostic(
            "FILE_LIMIT_EXCEEDED", "testcase exceeds the portable file budget",
            required_correction="Submit portable testcase content within the file budget."))
    code = _sv_code_tokens(content)
    try:
        uvm_manifest = build_manifest(
            logical_testcases, implemented_testcase_ids=implemented_ids,
            skipped_testcases=skipped)
        validate_generated_tests(content, uvm_manifest, error)
    except ValueError as caught:
        diagnostics.append(_stage3_diagnostic(
            "INVALID_GENERATED_ARTIFACT", str(caught),
            required_correction="Generate one UVM-context-defined base-test subclass per logical testcase."))
    except Exception as caught:
        # The validator deliberately returns typed boundary errors.  Convert
        # those to the existing aggregate Stage 3 diagnostic form so repair
        # still receives a precise, bounded correction request.
        diagnostics.append(_stage3_diagnostic(
            "INVALID_GENERATED_ARTIFACT", str(caught),
            required_correction=("Use the frozen Project YAML UVM context and "
                                 "do not print or control pass markers.")))
    forbidden = {
        "INCLUDE": re.search(r"`include\b", code),
        "SHELL": re.search(r"\$system\b", code),
        "DPI": re.search(r"\bDPI-C\b|\bimport\s+\"DPI", content),
    }
    for name, match in forbidden.items():
        if match:
            diagnostics.append(_stage3_diagnostic(
                "INVALID_GENERATED_ARTIFACT", "forbidden testcase construct: {}".format(name),
                offending_content=match.group(0), match_count=1,
                required_correction="Remove the forbidden {} construct.".format(name)))
    tc_by_id = {
        item["testcase_id"]: item for item in logical_testcases}
    allowed_tc = {
        tc_id for tc_id, item in tc_by_id.items()
        if item["status"] == "CHECKABLE"}
    if (len(skipped_ids) != len(skipped_id_set) or
            implemented_ids & skipped_id_set or
            implemented_ids | skipped_id_set != allowed_tc):
        diagnostics.append(_stage3_diagnostic(
            "TESTCASE_MAPPING_OVERREACH",
            "each CHECKABLE testcase must be implemented or explicitly skipped",
            offending_content=",".join(sorted(
                allowed_tc - (implemented_ids | skipped_id_set))),
            required_correction=("Partition every CHECKABLE logical testcase into "
                                 "implemented_testcase_ids or skipped_testcases.")))
    # Skip feasibility is assessed by semantic review, not declaration matching.
    checks = sorted(["UVM_CONTEXT_CLASS_DECLARATION", "NO_PLATFORM_MARKER_CONTROL",
                     "NO_FORBIDDEN_CONSTRUCT", "MAPPING_LINEAGE",
                     "ASSEMBLY_COMPLETE", "TESTCASE_BINDING",
                     "PARTIAL_TESTCASE_ROUTING"])
    expected_validation = {
        "status": "PASS",
        "checks": checks,
        "validation_fingerprint": canonical_hash({
            "content_fingerprint": value["content_fingerprint"],
            "input_fingerprint": value["input_fingerprint"],
            "scenario_ac_map_fingerprint": map1_fp,
            "ac_testcase_map_fingerprint": map2_fp,
            "checks": checks,
        }),
    }
    if value["validation"] != expected_validation:
        diagnostics.append(_stage3_diagnostic(
            "STALE_EVIDENCE", "testcase deterministic validation is stale",
            required_correction="Runtime derives validation evidence; do not provide it."))
    if diagnostics:
        _raise_stage3_diagnostics(error, diagnostics)
    return value

def enrich_stage3(
        raw: dict[str, Any], value: dict[str, Any],
        map1: dict[str, Any], map2: dict[str, Any],
        testcases: list[dict[str, Any]], spec_fp: str,
        revision: int, response: dict[str, Any], policy_fingerprint: str,
        effective_uvm_root: str,
        error: Callable[..., Exception]) -> dict[str, Any]:
    if not accepted(validate("uvm_testcase_candidate", raw)):
        _raise_stage3_diagnostics(error, [_stage3_diagnostic(
            "INVALID_GENERATED_ARTIFACT",
            "testcase candidate contract is invalid",
            required_correction="Submit the complete Stage 3 candidate schema.")], [
            "ASSEMBLY", "MAPPING", "LINEAGE", "POLICY"])
    if set(raw) != {
            "code_units", "assembly", "implemented_testcase_ids",
            "skipped_testcases"}:
        raise error("INVALID_GENERATED_ARTIFACT",
                         "testcase candidate shape is invalid")
    if (raw["assembly"] != list(dict.fromkeys(raw["assembly"])) or
            set(raw["assembly"]) != set(range(len(raw["code_units"])))):
        raise error(
            "ASSEMBLY_MISMATCH",
            "Stage 3 assembly must reference every code unit exactly once")
    code_by_index: dict[int, dict[str, Any]] = {}
    code_units: list[dict[str, Any]] = []
    allowed_testcase_ids = set(raw["implemented_testcase_ids"])
    role_counts = {"SHARED": 0, "TESTCASE": 0}
    for index, source in enumerate(raw["code_units"]):
        role = source["role"]
        role_counts[role] += 1
        testcase_ids = sorted(source["testcase_ids"])
        if ((role == "SHARED" and testcase_ids) or
                (role == "TESTCASE" and not testcase_ids) or
                not set(testcase_ids).issubset(allowed_testcase_ids)):
            raise error(
                "TESTCASE_MAPPING_OVERREACH",
                "generic code-unit role/testcase mapping is invalid")
        code_unit_id = "CODE.{}.{:04d}".format(
            role, role_counts[role])
        item = {
            "code_unit_id": code_unit_id,
            "role": role,
            "testcase_ids": testcase_ids,
            "content": source["content"],
            "content_fingerprint": _sha(source["content"]),
        }
        code_by_index[index] = item
        code_units.append(item)
    testcase_unit_ids = set().union(*(set(item["testcase_ids"])
                                      for item in code_units
                                      if item["role"] == "TESTCASE")) \
        if code_units else set()
    if testcase_unit_ids != allowed_testcase_ids:
        raise error(
            "MISSING_TRACEABILITY",
            "Stage 3 requires complete TESTCASE code-unit coverage")
    assembly_manifest = [
        code_by_index[index]["code_unit_id"] for index in raw["assembly"]]
    content = "".join(
        code_by_index[index]["content"] for index in raw["assembly"])
    if len(content.encode("utf-8")) > 262144:
        raise error(
            "FILE_LIMIT_EXCEEDED", "assembled Stage 3 content exceeds budget")
    code_units.sort(key=lambda item: item["code_unit_id"])
    content_fp = _sha(content)
    identity = canonical_hash({
        "job": value["job_id"], "revision": revision,
        "map1": map1["artifact_fingerprint"],
        "map2": map2["artifact_fingerprint"],
        "content": content_fp})
    checks = sorted([
        "UVM_CONTEXT_CLASS_DECLARATION", "NO_PLATFORM_MARKER_CONTROL",
        "NO_FORBIDDEN_CONSTRUCT", "MAPPING_LINEAGE", "ASSEMBLY_COMPLETE",
        "TESTCASE_BINDING", "PARTIAL_TESTCASE_ROUTING"])
    validation = {
        "status": "PASS",
        "checks": checks,
        "validation_fingerprint": canonical_hash({
            "content_fingerprint": content_fp,
            "input_fingerprint": value["input_fingerprint"],
            "scenario_ac_map_fingerprint":
                map1["artifact_fingerprint"],
            "ac_testcase_map_fingerprint":
                map2["artifact_fingerprint"],
            "checks": checks,
        }),
    }
    result = {
        "schema_version": "7.0",
        "artifact_kind": "UVM_TESTCASE_BUNDLE",
        "candidate_id": "PROJECTTESTCAND.{}".format(
            identity[:16].upper()),
        "job_id": value["job_id"],
        "revision": revision,
        "state": "STAGING",
        "output_path":
            "staging/generated/uvm/generated_tests.r{:03d}.sv".format(
                revision),
        "top": value["testcase"]["top"],
        "content": content,
        "content_fingerprint": content_fp,
        "input_fingerprint": value["input_fingerprint"],
        "spec_fingerprint": spec_fp,
        "scenario_ac_map_fingerprint":
            map1["artifact_fingerprint"],
        "ac_testcase_map_fingerprint":
            map2["artifact_fingerprint"],
        "effective_uvm_root": effective_uvm_root,
        "upstream_fingerprints": {
            "input": value["input_fingerprint"], "spec": spec_fp,
            "scenario_ac_map": map1["artifact_fingerprint"],
            "ac_testcase_map": map2["artifact_fingerprint"],
            "effective_uvm": effective_uvm_root},
        "policy_fingerprint": policy_fingerprint,
        "code_units": code_units,
        "assembly_manifest": assembly_manifest,
        "implemented_testcase_ids":
            sorted(raw["implemented_testcase_ids"]),
        "skipped_testcases": sorted(
            (copy.deepcopy(item) for item in raw["skipped_testcases"]),
            key=lambda item: item["testcase_id"]),
        "provider": _provider_identity(response),
        "validation": validation,
        "candidate_fingerprint": "0" * 64,
    }
    result["candidate_fingerprint"] = artifact_fingerprint(
        result, "candidate_fingerprint")
    return validate_testcase_candidate(
        result, map1, map2, testcases, value, spec_fp,
        policy_fingerprint, error)
