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
from domain.uvm_context import project_uvm_context
from domain.uvm_testcase import build_manifest, validate_generated_tests
from scripts.dvlib import canonical_hash


MAX_CODE_EVIDENCE_BYTES = 16384
MAX_RETRY_CORRECTION_BYTES = 1024
MAX_STAGE3_DIAGNOSTICS = 64
MAX_STAGE3_DIAGNOSTIC_BYTES = 65536

_SV_CALLABLE = re.compile(
    r"\b(?:task|function)\b(?P<header>[^;]{0,512}?)(?:\(|;)", re.IGNORECASE)
_SV_NAMED_TYPE = re.compile(
    r"\b(?:class|interface)\s+([A-Za-z_][A-Za-z0-9_$]*)\b",
    re.IGNORECASE)
_SV_LOGIC_DECLARATION = re.compile(
    r"\b(?:logic|wire|reg)\b(?P<body>[^;]{0,2048});", re.IGNORECASE)
_NEGATED_CAPABILITY = re.compile(
    r"\b(?:no|without|absent|missing|cannot|can't|does\s+not|doesn't|"
    r"do\s+not|don't|not\s+expos(?:e|ed)|unavailable|lacks?|omits?)\b",
    re.IGNORECASE)
_CAPABILITY_NOUN_OR_VERB = re.compile(
    r"\b(?:api|task|function|method|interface|signal|driver|monitor|checker|"
    r"sequence|agent|capability|access|observe|observation|drive|stimulus|"
    r"induce|read|write|wait|control|query)\w*\b", re.IGNORECASE)
_ASSET_NOUN = re.compile(
    r"\b(?:asset|image|binary|firmware|architectural\s+program|known\s+program|"
    r"program\s+(?:bytes?|stimulus)|instruction\s+encodings?|"
    r"reference\s+vectors?)\b", re.IGNORECASE)
_GENERIC_CALLABLES = {
    "new", "build_phase", "connect_phase", "run_phase", "report_phase",
    "do_copy", "do_compare", "do_print", "initialize", "body",
}
_GENERIC_CAPABILITY_WORDS = {
    "access", "agent", "automatic", "base", "configure", "create", "drive",
    "driver", "execute", "get", "set", "start", "stop", "apply", "check",
    "report", "public", "platform", "api", "task", "function", "method",
    "core", "testcase", "test", "uvm", "coral", "npu", "void", "phase",
    "unsigned", "input", "output", "sequence", "monitor", "high", "low",
    "read", "write", "wait", "load",
}
_DISTINCTIVE_CAPABILITY_WORDS = {
    "reset", "halted", "fault", "wfi", "interrupt", "backpressure", "boot",
    "debug", "memory", "signature", "scoreboard", "transaction", "response",
}


def _identifier_words(identifier: str) -> tuple[str, ...]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", identifier)
    return tuple(re.findall(r"[a-z0-9]+", expanded.replace("_", " ").casefold()))


def _public_uvm_surface(
        project_input: dict[str, Any], error: Callable[..., Exception]
        ) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]], set[str]]:
    """Extract declared authoring capabilities from the frozen UVM text.

    This is deliberately declaration-based. It does not recreate a capability
    contract or infer behavior from RTL; it only prevents an agent from saying
    that a visible declaration is absent.
    """
    files = project_uvm_context(project_input, error).files
    callables: dict[str, tuple[str, ...]] = {}
    named_types: dict[str, tuple[str, ...]] = {}
    signals: set[str] = set()
    for source in files:
        code = _sv_code_tokens(source["content"])
        for match in _SV_CALLABLE.finditer(code):
            identifiers = re.findall(
                r"[A-Za-z_][A-Za-z0-9_$]*", match.group("header"))
            if identifiers:
                name = identifiers[-1]
                if name.casefold() not in _GENERIC_CALLABLES:
                    callables[name] = _identifier_words(name)
        for name in _SV_NAMED_TYPE.findall(code):
            named_types[name] = _identifier_words(name)
        for match in _SV_LOGIC_DECLARATION.finditer(code):
            body = re.sub(r"\[[^\]]*\]", " ", match.group("body"))
            for declaration in body.split(","):
                before_assignment = declaration.split("=", 1)[0]
                identifiers = re.findall(
                    r"[A-Za-z_][A-Za-z0-9_$]*", before_assignment)
                if identifiers:
                    signals.add(identifiers[-1].casefold())
    return callables, named_types, signals


def _semantic_testcase_words(testcase: dict[str, Any]) -> set[str]:
    fields = (
        "objective", "preconditions", "stimulus", "transaction_sequence",
        "timing_intent", "checker", "expected_result", "failure_condition",
    )
    return set(re.findall(
        r"[a-z0-9]+", " ".join(
            str(testcase.get(field, "")) for field in fields).casefold()))


def _matching_public_operations(
        testcase: dict[str, Any], callables: dict[str, tuple[str, ...]]
        ) -> list[str]:
    testcase_words = _semantic_testcase_words(testcase)
    matches = []
    testcase_text = " ".join(
        str(testcase.get(field, "")) for field in (
            "objective", "preconditions", "stimulus", "transaction_sequence",
            "timing_intent", "checker", "expected_result", "failure_condition",
        )).casefold()
    for name, words in callables.items():
        meaningful = {
            word for word in words
            if len(word) >= 4 and word not in _GENERIC_CAPABILITY_WORDS}
        phrase = " ".join(words)
        overlap = meaningful & testcase_words
        if (phrase and phrase in testcase_text) or len(overlap) >= 2 or \
                overlap & _DISTINCTIVE_CAPABILITY_WORDS:
            matches.append(name)
    return sorted(matches, key=str.casefold)


def _has_exact_public_operation(
        testcase: dict[str, Any], callables: dict[str, tuple[str, ...]]) -> bool:
    text = " ".join(
        str(testcase.get(field, "")) for field in (
            "objective", "stimulus", "transaction_sequence", "checker",
            "expected_result", "failure_condition",
        )).casefold()
    return any(" ".join(words) in text for words in callables.values())


def _contradicted_public_declarations(
        reason: str, callables: dict[str, tuple[str, ...]],
        named_types: dict[str, tuple[str, ...]], signals: set[str]
        ) -> list[str]:
    """Find explicit absence claims contradicted by visible declarations."""
    contradicted: set[str] = set()
    clauses = re.split(r"[.;\n]|\b(?:but|however)\b", reason.casefold())
    for clause in clauses:
        negative = _NEGATED_CAPABILITY.search(clause)
        if not negative:
            continue
        negative_clause = clause[negative.start():]
        clause_words = set(re.findall(r"[a-z0-9]+", negative_clause))
        for signal in signals & clause_words:
            contradicted.add(signal)
        for name, words in named_types.items():
            phrase = " ".join(words)
            if name.casefold() in negative_clause or (
                    phrase and phrase in negative_clause):
                contradicted.add(name)
        if _ASSET_NOUN.search(negative_clause):
            # A callable that loads an asset does not prove that the asset
            # itself (for example a program image) was supplied.
            continue
        for name, words in callables.items():
            phrase = " ".join(words)
            meaningful = {
                word for word in words
                if len(word) >= 4 and word not in _GENERIC_CAPABILITY_WORDS}
            overlap = meaningful & clause_words
            explicitly_callable = re.search(
                r"\b(?:api|task|function|method|capability)\b",
                negative_clause)
            if name.casefold() in negative_clause or (
                    phrase and phrase in negative_clause) or \
                    len(overlap) >= 2 or (
                    overlap and explicitly_callable and
                    _CAPABILITY_NOUN_OR_VERB.search(negative_clause)):
                contradicted.add(name)
    return sorted(contradicted, key=str.casefold)


def _blocked_contract_diagnostics(
        skipped: list[dict[str, Any]], logical_testcases: list[dict[str, Any]],
        project_input: dict[str, Any], error: Callable[..., Exception]
        ) -> list[dict[str, Any]]:
    """Validate Stage 3 skip reasons against this Job's frozen UVM text."""
    callables, named_types, signals = _public_uvm_surface(
        project_input, error)
    by_id = {item["testcase_id"]: item for item in logical_testcases}
    diagnostics: list[dict[str, Any]] = []
    blocked = [
        item for item in skipped
        if item.get("reason_kind") == "BLOCKED_CONTRACT"]
    for item in blocked:
        contradicted = _contradicted_public_declarations(
            item["reason"], callables, named_types, signals)
        if contradicted:
            diagnostics.append(_stage3_diagnostic(
                "UVM_CONTEXT_SKIP_CONTRADICTION",
                "BLOCKED_CONTRACT contradicts declarations in the frozen UVM context",
                offending_content="{}: {}".format(
                    item["testcase_id"], ",".join(contradicted)),
                match_count=len(contradicted),
                required_correction=(
                    "Implement this testcase using the visible public UVM declarations, "
                    "or identify an exact still-missing drive or observation point.")))
    checkable_ids = {
        item["testcase_id"] for item in logical_testcases
        if item.get("status") == "CHECKABLE"}
    skipped_ids = {item.get("testcase_id") for item in skipped}
    if (checkable_ids and skipped_ids == checkable_ids and
            len(blocked) == len(skipped)):
        matching = {
            testcase_id: _matching_public_operations(
                by_id[testcase_id], callables)
            for testcase_id in sorted(checkable_ids)
            if testcase_id in by_id
        }
        matching = {key: value for key, value in matching.items() if value}
        distinct_operations = {
            operation for operations in matching.values()
            for operation in operations}
        exact_match = any(
            _has_exact_public_operation(by_id[testcase_id], callables)
            for testcase_id in matching)
        if exact_match or len(distinct_operations) >= 2:
            diagnostics.append(_stage3_diagnostic(
                "ALL_TESTCASES_SKIPPED",
                "all CHECKABLE testcases were skipped despite matching public UVM capabilities",
                offending_content=json.dumps(
                    matching, sort_keys=True, ensure_ascii=False),
                match_count=sum(len(value) for value in matching.values()),
                required_correction=(
                    "Generate every testcase supported by the frozen UVM context; keep only "
                    "genuinely unsupported testcase IDs in skipped_testcases.")))
    return diagnostics


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
    diagnostics.extend(_blocked_contract_diagnostics(
        skipped, logical_testcases, project_input, error))
    checks = sorted(["UVM_CONTEXT_CLASS_DECLARATION", "NO_PLATFORM_MARKER_CONTROL",
                     "NO_FORBIDDEN_CONSTRUCT", "MAPPING_LINEAGE",
                     "ASSEMBLY_COMPLETE", "TESTCASE_BINDING",
                     "PARTIAL_TESTCASE_ROUTING",
                     "UVM_CONTEXT_SKIP_CONSISTENCY"])
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
        "TESTCASE_BINDING", "PARTIAL_TESTCASE_ROUTING",
        "UVM_CONTEXT_SKIP_CONSISTENCY"])
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
