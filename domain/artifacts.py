"""Pure artifact fingerprint, unit, index, and assembly rules."""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Callable, Iterable, Mapping

from contracts.validator import accepted, validate
from scripts.dvlib import canonical_hash


UNIT_CONTRACT_VERSION = "1.0"
INDEX_CONTRACT_VERSION = "1.0"
ASSEMBLY_CONTRACT_VERSION = "1.0"
IMPACT_CONTRACT_VERSION = "1.0"
INCREMENTAL_POLICY_VERSION = "PJ-002.9-HF1"

STAGE1 = "STAGE1"
STAGE2 = "STAGE2"
STAGE3 = "STAGE3"
REVIEW = "REVIEW"

_ZERO = "0" * 64
_STAGE_SLUG = {
    STAGE1: "stage1", STAGE2: "stage2", STAGE3: "stage3",
    REVIEW: "review",
}


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def artifact_fingerprint(value: dict[str, Any], field: str) -> str:
    projected = copy.deepcopy(value)
    projected.pop(field, None)
    return canonical_hash(projected)

def _lineage_fingerprint(unit: dict[str, Any]) -> str:
    """Stable local lineage consumed by downstream units.

    It intentionally excludes append-only revision/path and provider call IDs.
    Complete provenance remains protected by ``artifact_fingerprint``.
    """
    return canonical_hash({
        "unit_id": unit["unit_id"],
        "unit_kind": unit["unit_kind"],
        "content_fingerprint": unit["content_fingerprint"],
        "dependency_fingerprint": unit["dependency_fingerprint"],
    })

def _producer_dependency(producer: dict[str, Any]) -> str:
    return canonical_hash({
        "kind": producer.get("kind", "PROVIDER"),
        "provider_id": producer.get("provider_id", "FRAMEWORK"),
        "model_id": producer.get("model_id", INCREMENTAL_POLICY_VERSION),
        "runtime_role": producer.get("runtime_role", "FRAMEWORK"),
    })

def _framework_producer() -> dict[str, Any]:
    return {
        "kind": "FRAMEWORK",
        "runtime_role": "FRAMEWORK",
        "provider_id": "FRAMEWORK",
        "model_id": INCREMENTAL_POLICY_VERSION,
    }

def _provider(value: dict[str, Any], role: str) -> dict[str, Any]:
    return {
        "kind": "PROVIDER",
        "runtime_role": role,
        **copy.deepcopy(value),
    }

def _dep(kind: str, identity: str, fingerprint: str) -> dict[str, str]:
    return {"kind": kind, "identity": identity, "fingerprint": fingerprint}

def _canonical_dependencies(
        values: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    by_key: dict[tuple[str, str], dict[str, str]] = {}
    for value in values:
        key = (value["kind"], value["identity"])
        prior = by_key.get(key)
        if prior is not None and prior != value:
            raise ValueError("conflicting direct dependency: {}:{}".format(*key))
        by_key[key] = copy.deepcopy(value)
    return [by_key[key] for key in sorted(by_key)]

def _evidence_dependencies(
        evidence: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        _dep(
            "SPEC_EVIDENCE",
            "{}:{}:{}".format(
                item["path"], item["line_start"], item["line_end"]),
            item["snippet_fingerprint"],
        )
        for item in evidence
    ]

def _authority_dependencies(
        *, input_fingerprint: str, spec_fingerprint: str,
        policy_fingerprint: str, owner_scope_fingerprint: str,
        producer: dict[str, Any]) -> list[dict[str, str]]:
    return [
        _dep("JOB_INPUT", "PROJECT_INPUT", input_fingerprint),
        _dep("SPEC_BASELINE", "SPEC_BASELINE", spec_fingerprint),
        _dep("POLICY", INCREMENTAL_POLICY_VERSION, policy_fingerprint),
        _dep("OWNER_SCOPE", "OWNER_ROUTING", owner_scope_fingerprint),
        _dep("PROVIDER", "{}:{}:{}".format(
            producer.get("runtime_role", "FRAMEWORK"),
            producer.get("provider_id", "FRAMEWORK"),
            producer.get("model_id", INCREMENTAL_POLICY_VERSION)),
            _producer_dependency(producer)),
    ]

def _make_unit(
        *, stage: str, unit_kind: str, unit_id: str, job_id: str,
        revision: int, input_fingerprint: str, spec_fingerprint: str,
        policy_fingerprint: str, owner_scope_fingerprint: str,
        semantic_body: dict[str, Any], spec_evidence: list[dict[str, Any]],
        local_evidence: list[dict[str, Any]] | None,
        producer: dict[str, Any], dependencies: Iterable[dict[str, str]],
        error: Callable[[str, str], Exception]) -> dict[str, Any]:
    try:
        direct = _canonical_dependencies([
            *_authority_dependencies(
                input_fingerprint=input_fingerprint,
                spec_fingerprint=spec_fingerprint,
                policy_fingerprint=policy_fingerprint,
                owner_scope_fingerprint=owner_scope_fingerprint,
                producer=producer),
            *_evidence_dependencies(spec_evidence),
            *dependencies,
        ])
    except (KeyError, TypeError, ValueError) as caught:
        raise error(
            "UNDECLARED_DEPENDENCY",
            "incremental unit has conflicting direct dependencies") from caught
    unit = {
        "schema_version": UNIT_CONTRACT_VERSION,
        "artifact_kind": "PROJECT_ARTIFACT_UNIT",
        "stage": stage,
        "unit_kind": unit_kind,
        "unit_id": unit_id,
        "job_id": job_id,
        "revision": revision,
        "input_fingerprint": input_fingerprint,
        "spec_fingerprint": spec_fingerprint,
        "policy_fingerprint": policy_fingerprint,
        "owner_scope_fingerprint": owner_scope_fingerprint,
        "semantic_body": copy.deepcopy(semantic_body),
        "spec_evidence": copy.deepcopy(spec_evidence),
        "local_evidence": copy.deepcopy(local_evidence or []),
        "producer": copy.deepcopy(producer),
        "dependency_fingerprints": direct,
        "content_fingerprint": canonical_hash(semantic_body),
        "dependency_fingerprint": canonical_hash(direct),
        "artifact_fingerprint": _ZERO,
    }
    unit["artifact_fingerprint"] = artifact_fingerprint(
        unit, "artifact_fingerprint")
    validate_unit(unit, error)
    return unit

def validate_unit(
        unit: dict[str, Any],
        error: Callable[[str, str], Exception]) -> dict[str, Any]:
    if not accepted(validate("project_artifact_unit", unit)):
        raise error("INVALID_SCHEMA", "incremental artifact unit schema is invalid")
    if unit["content_fingerprint"] != canonical_hash(unit["semantic_body"]):
        raise error("STALE_EVIDENCE", "unit semantic content fingerprint is stale")
    dependencies = unit["dependency_fingerprints"]
    try:
        if dependencies != _canonical_dependencies(dependencies):
            raise error(
                "NON_CANONICAL_INDEX",
                "unit direct dependencies are not canonical and unique")
    except ValueError as caught:
        raise error(
            "UNDECLARED_DEPENDENCY",
            "unit contains conflicting dependency identities") from caught
    if unit["dependency_fingerprint"] != canonical_hash(dependencies):
        raise error("STALE_EVIDENCE", "unit dependency fingerprint is stale")
    if unit["artifact_fingerprint"] != artifact_fingerprint(
            unit, "artifact_fingerprint"):
        raise error("STALE_EVIDENCE", "unit complete artifact fingerprint is stale")
    for evidence in unit["spec_evidence"]:
        if (evidence["line_end"] < evidence["line_start"] or
                evidence["snippet_fingerprint"] !=
                _sha_text(evidence["snippet"])):
            raise error("SPEC_EVIDENCE_MISMATCH", "unit Spec evidence is stale")
    if unit["unit_kind"] in {"CODE_SHARED", "CODE_TESTCASE"}:
        segments = unit["semantic_body"].get("segments")
        if not isinstance(segments, list) or not segments:
            raise error("INVALID_GENERATED_ARTIFACT", "code unit has no segments")
        local_by_segment = {
            item.get("segment_index"): item for item in unit["local_evidence"]}
        if (len(local_by_segment) != len(unit["local_evidence"]) or
                set(local_by_segment) != set(range(len(segments)))):
            raise error("MISSING_TRACEABILITY", "code unit local evidence is incomplete")
        for segment_index, segment in enumerate(segments):
            local = local_by_segment[segment_index]
            lines = segment.splitlines()
            if (local.get("code_unit_id") != unit["unit_id"] or
                    local.get("code_unit_fingerprint") !=
                        unit["content_fingerprint"] or
                    local.get("unit_line_start") != 1 or
                    local.get("unit_line_end") != len(lines) or
                    local.get("segment_fingerprint") != _sha_text(segment)):
                raise error("STALE_EVIDENCE", "code unit local segment evidence is stale")
            for selection in local.get("selections", []):
                selected = selection.get("unit_selection", {})
                start = selected.get("line_start")
                end = selected.get("line_end")
                if (type(start) is not int or type(end) is not int or
                        not 1 <= start <= end <= len(lines)):
                    raise error("TESTCASE_EVIDENCE_MISMATCH", "unit selection range is invalid")
                snippet = "\n".join(lines[start - 1:end])
                if (selected.get("snippet") != snippet or
                        selected.get("snippet_fingerprint") != _sha_text(snippet) or
                        selection.get("selection") != snippet or
                        selection.get("selection_fingerprint") != _sha_text(snippet)):
                    raise error("TESTCASE_EVIDENCE_MISMATCH", "unit selection content is stale")
    return unit

def _unit_slug(unit_id: str) -> str:
    slug = re.sub(r"[^a-z0-9_.-]+", "-", unit_id.casefold()).strip(".-")
    if not slug:
        raise ValueError("unit ID has no safe path representation")
    return slug[:240]

def _unit_relative(stage: str, variant: str, revision: int, unit_id: str) -> str:
    return "staging/units/{}/{}.r{:03d}/{}.json".format(
        _STAGE_SLUG[stage], variant.casefold(), revision, _unit_slug(unit_id))

def _index_relative(stage: str, variant: str, revision: int) -> str:
    return "staging/units/{}/index.{}.r{:03d}.json".format(
        _STAGE_SLUG[stage], variant.casefold(), revision)

def _make_index(
        *, stage: str, variant: str, job_id: str, revision: int,
        input_fingerprint: str, spec_fingerprint: str,
        policy_fingerprint: str, owner_scope_fingerprint: str,
        producer: dict[str, Any], units: list[dict[str, Any]],
        metadata: dict[str, Any],
        error: Callable[[str, str], Exception]) -> dict[str, Any]:
    children = []
    for unit in sorted(units, key=lambda item: (item["unit_kind"], item["unit_id"])):
        children.append({
            "unit_id": unit["unit_id"],
            "unit_kind": unit["unit_kind"],
            "path": _unit_relative(stage, variant, revision, unit["unit_id"]),
            "content_fingerprint": unit["content_fingerprint"],
            "dependency_fingerprint": unit["dependency_fingerprint"],
            "artifact_fingerprint": unit["artifact_fingerprint"],
        })
    unit_ids = [item["unit_id"] for item in children]
    index = {
        "schema_version": INDEX_CONTRACT_VERSION,
        "artifact_kind": "PROJECT_ARTIFACT_INDEX",
        "index_id": "INDEX.{}.{}.R{:03d}".format(stage, variant, revision),
        "stage": stage,
        "variant": variant,
        "job_id": job_id,
        "revision": revision,
        "input_fingerprint": input_fingerprint,
        "spec_fingerprint": spec_fingerprint,
        "policy_fingerprint": policy_fingerprint,
        "owner_scope_fingerprint": owner_scope_fingerprint,
        "producer": copy.deepcopy(producer),
        "children": children,
        "completeness": {
            "unit_ids": unit_ids,
            "unit_count": len(unit_ids),
        },
        "metadata": copy.deepcopy(metadata),
        "root_fingerprint": _ZERO,
    }
    index["root_fingerprint"] = artifact_fingerprint(index, "root_fingerprint")
    if not accepted(validate("project_artifact_index", index)):
        raise error("INVALID_SCHEMA", "incremental artifact index schema is invalid")
    return index

def unrouted_owner_scope(job_id: str, input_fingerprint: str) -> str:
    return canonical_hash({
        "state": "UNROUTED", "job_id": job_id,
        "input_fingerprint": input_fingerprint,
    })

def build_stage1_bundle(
        map1: dict[str, Any], owner_scope_fingerprint: str,
        error: Callable[[str, str], Exception], variant: str = "PROVIDER",
        job_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    actual_job_id = job_id or map1["job_id"]
    producer = _provider(map1["provider"], "GENERATOR")
    common = {
        "job_id": actual_job_id,
        "revision": map1["revision"],
        "input_fingerprint": map1["input_fingerprint"],
        "spec_fingerprint": map1["spec_fingerprint"],
        "policy_fingerprint": map1["policy_fingerprint"],
        "owner_scope_fingerprint": owner_scope_fingerprint,
        "producer": producer,
        "error": error,
    }
    units: list[dict[str, Any]] = []
    scenario_units: dict[str, dict[str, Any]] = {}
    for source in map1["scenarios"]:
        semantic = {key: copy.deepcopy(source[key]) for key in (
            "objective", "verification_level", "status", "reason")}
        unit = _make_unit(
            stage=STAGE1, unit_kind="SCENARIO",
            unit_id=source["scenario_id"], semantic_body=semantic,
            spec_evidence=source["spec_evidence"], local_evidence=[],
            dependencies=[], **common)
        units.append(unit)
        scenario_units[unit["unit_id"]] = unit
    for source in map1["acceptance_criteria"]:
        semantic = {key: copy.deepcopy(source[key]) for key in (
            "behavior", "verification_level", "status", "reason")}
        deps = [
            _dep("SCENARIO", scenario_id,
                 _lineage_fingerprint(scenario_units[scenario_id]))
            for scenario_id in source["scenario_ids"]
        ]
        unit = _make_unit(
            stage=STAGE1, unit_kind="ACCEPTANCE_CRITERION",
            unit_id=source["ac_id"], semantic_body=semantic,
            spec_evidence=source["spec_evidence"], local_evidence=[],
            dependencies=deps, **common)
        units.append(unit)
    index = _make_index(
        stage=STAGE1, variant=variant, units=units,
        metadata={
            "source_map_fingerprint": map1["artifact_fingerprint"],
            "scenario_ids": sorted(item["scenario_id"] for item in map1["scenarios"]),
            "ac_ids": sorted(item["ac_id"] for item in map1["acceptance_criteria"]),
            "declared_complete": map1["completeness"]["declared_complete"],
        }, **common)
    return units, index

def _stage1_units_by_kind(
        units: dict[str, dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    return {key: value for key, value in units.items() if value["unit_kind"] == kind}

def build_stage2_bundle(
        map2: dict[str, Any], logical_testcases: list[dict[str, Any]],
        stage1_units: dict[str, dict[str, Any]], owner_scope_fingerprint: str,
        error: Callable[[str, str], Exception], job_id: str | None = None
        ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    actual_job_id = job_id or map2["job_id"]
    producer = _provider(map2["provider"], "GENERATOR")
    scenarios = _stage1_units_by_kind(stage1_units, "SCENARIO")
    acs = _stage1_units_by_kind(stage1_units, "ACCEPTANCE_CRITERION")
    common = {
        "job_id": actual_job_id,
        "revision": map2["revision"],
        "input_fingerprint": map2["input_fingerprint"],
        "spec_fingerprint": map2["spec_fingerprint"],
        "policy_fingerprint": map2["policy_fingerprint"],
        "owner_scope_fingerprint": owner_scope_fingerprint,
        "producer": producer,
        "error": error,
    }
    units: list[dict[str, Any]] = []
    testcase_units: dict[str, dict[str, Any]] = {}
    semantic_fields = (
        "objective", "preconditions", "stimulus", "transaction_sequence",
        "timing_intent", "checker", "expected_result", "failure_condition",
        "timeout_cycles", "status", "reason",
    )
    for source in logical_testcases:
        deps = []
        for scenario_id in source["scenario_ids"]:
            if scenario_id not in scenarios:
                raise error("UNDECLARED_DEPENDENCY", "testcase Scenario unit is absent")
            deps.append(_dep(
                "SCENARIO", scenario_id,
                _lineage_fingerprint(scenarios[scenario_id])))
        for ac_id in source["ac_ids"]:
            if ac_id not in acs:
                raise error("UNDECLARED_DEPENDENCY", "testcase AC unit is absent")
            deps.append(_dep(
                "ACCEPTANCE_CRITERION", ac_id,
                _lineage_fingerprint(acs[ac_id])))
        unit = _make_unit(
            stage=STAGE2, unit_kind="LOGICAL_TESTCASE",
            unit_id=source["testcase_id"],
            semantic_body={
                **{key: copy.deepcopy(source.get(key, ""))
                   for key in semantic_fields},
                "scenario_ids": copy.deepcopy(source["scenario_ids"]),
                "ac_ids": copy.deepcopy(source["ac_ids"]),
            },
            spec_evidence=source["spec_evidence"], local_evidence=[],
            dependencies=deps, **common)
        units.append(unit)
        testcase_units[unit["unit_id"]] = unit
    framework_common = {**common, "producer": _framework_producer()}
    for source in map2["ac_coverage"]:
        ac_id = source["ac_id"]
        if ac_id not in acs:
            raise error("UNDECLARED_DEPENDENCY", "coverage AC unit is absent")
        deps = [_dep(
            "ACCEPTANCE_CRITERION", ac_id,
            _lineage_fingerprint(acs[ac_id]))]
        for testcase_id in source["testcase_ids"]:
            if testcase_id not in testcase_units:
                raise error("UNDECLARED_DEPENDENCY", "coverage testcase unit is absent")
            deps.append(_dep(
                "LOGICAL_TESTCASE", testcase_id,
                _lineage_fingerprint(testcase_units[testcase_id])))
        unit = _make_unit(
            stage=STAGE2, unit_kind="AC_COVERAGE",
            unit_id="COVERAGE.{}".format(ac_id),
            semantic_body={
                "ac_id": ac_id,
                "testcase_ids": copy.deepcopy(source["testcase_ids"]),
            }, spec_evidence=[], local_evidence=[], dependencies=deps,
            **framework_common)
        units.append(unit)
    index = _make_index(
        stage=STAGE2, variant="CURRENT", units=units,
        metadata={
            "source_map_fingerprint": map2["artifact_fingerprint"],
            "logical_testcase_ids": sorted(testcase_units),
            "coverage_ids": sorted(
                item["unit_id"] for item in units
                if item["unit_kind"] == "AC_COVERAGE"),
            "declared_complete": map2["completeness"]["declared_complete"],
            "coverage_authority": "FRAMEWORK_DERIVED_FROM_TESTCASE_AC_IDS",
        }, **common)
    return units, index

def _code_unit_id(testcase_ids: tuple[str, ...]) -> str:
    if not testcase_ids:
        return "CODE.SHARED"
    readable = ".".join(
        item.removeprefix("TC.") for item in testcase_ids)
    candidate = "CODE.TESTCASE.{}".format(readable)
    if len(candidate) <= 255 and re.fullmatch(r"[A-Z][A-Z0-9_.-]*", candidate):
        return candidate
    return "CODE.TESTCASE.GROUP.{}".format(
        canonical_hash(list(testcase_ids))[:16].upper())

def _build_explicit_stage3_bundle(
        candidate: dict[str, Any], stage1_units: dict[str, dict[str, Any]],
        stage2_units: dict[str, dict[str, Any]], owner_scope_fingerprint: str,
        error: Callable[[str, str], Exception], job_id: str | None
        ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    actual_job_id = job_id or candidate["job_id"]
    producer = _provider(candidate["provider"], "GENERATOR")
    testcase_units = _stage1_units_by_kind(stage2_units, "LOGICAL_TESTCASE")
    formal_by_id = {
        item["code_unit_id"]: item for item in candidate["code_units"]}
    if (len(formal_by_id) != len(candidate["code_units"]) or
            set(candidate["assembly_manifest"]) != set(formal_by_id)):
        raise error("ASSEMBLY_MISMATCH", "formal Stage 3 code units are incomplete")
    common = {
        "job_id": actual_job_id,
        "revision": candidate["revision"],
        "input_fingerprint": candidate["input_fingerprint"],
        "spec_fingerprint": candidate["spec_fingerprint"],
        "policy_fingerprint": candidate["policy_fingerprint"],
        "owner_scope_fingerprint": owner_scope_fingerprint,
        "producer": producer,
        "error": error,
    }
    offsets: dict[str, int] = {}
    line = 1
    for unit_id in candidate["assembly_manifest"]:
        offsets[unit_id] = line
        line += len(formal_by_id[unit_id]["content"].splitlines())
    shared_units: dict[str, dict[str, Any]] = {}
    units: list[dict[str, Any]] = []
    for formal in candidate["code_units"]:
        if formal["role"] != "SHARED":
            continue
        semantic = {"segments": [formal["content"]]}
        local = [{
            "code_unit_id": formal["code_unit_id"],
            "code_unit_fingerprint": canonical_hash(semantic),
            "global_line_start": offsets[formal["code_unit_id"]],
            "global_line_end": offsets[formal["code_unit_id"]] +
                len(formal["content"].splitlines()) - 1,
            "segment_index": 0,
            "unit_line_start": 1,
            "unit_line_end": len(formal["content"].splitlines()),
            "segment_fingerprint": _sha_text(formal["content"]),
        }]
        unit = _make_unit(
            stage=STAGE3, unit_kind="CODE_SHARED",
            unit_id=formal["code_unit_id"], semantic_body=semantic,
            spec_evidence=[], local_evidence=local,
            dependencies=[], **common)
        shared_units[unit["unit_id"]] = unit
        units.append(unit)
    for formal in candidate["code_units"]:
        if formal["role"] != "TESTCASE":
            continue
        semantic = {"segments": [formal["content"]]}
        deps = [
            _dep("CODE_SHARED", shared["unit_id"],
                 _lineage_fingerprint(shared))
            for shared in shared_units.values()
        ]
        for testcase_id in formal["testcase_ids"]:
            testcase = testcase_units.get(testcase_id)
            if testcase is None:
                raise error("UNDECLARED_DEPENDENCY", "code testcase unit is absent")
            deps.append(_dep(
                "LOGICAL_TESTCASE", testcase_id,
                _lineage_fingerprint(testcase)))
        local = [{
            "code_unit_id": formal["code_unit_id"],
            "code_unit_fingerprint": canonical_hash(semantic),
            "global_line_start": offsets[formal["code_unit_id"]],
            "global_line_end": offsets[formal["code_unit_id"]] +
                len(formal["content"].splitlines()) - 1,
            "segment_index": 0,
            "unit_line_start": 1,
            "unit_line_end": len(formal["content"].splitlines()),
            "segment_fingerprint": _sha_text(formal["content"]),
        }]
        units.append(_make_unit(
            stage=STAGE3, unit_kind="CODE_TESTCASE",
            unit_id=formal["code_unit_id"], semantic_body=semantic,
            spec_evidence=[], local_evidence=local,
            dependencies=deps, **common))
    sequence = [{"unit_id": unit_id, "segment_index": 0}
                for unit_id in candidate["assembly_manifest"]]
    metadata = {
        "source_candidate_fingerprint": candidate["candidate_fingerprint"],
        "assembly_path": "staging/units/stage3/assembly.r{:03d}.json".format(
            candidate["revision"]),
        "assembly_sequence": sequence,
        "complete_content_fingerprint": candidate["content_fingerprint"],
        "implemented_testcase_ids": copy.deepcopy(
            candidate["implemented_testcase_ids"]),
        "skipped_testcases": copy.deepcopy(candidate["skipped_testcases"]),
    }
    index = _make_index(
        stage=STAGE3, variant="CURRENT", units=units,
        metadata=metadata, **common)
    assembly = {
        "schema_version": ASSEMBLY_CONTRACT_VERSION,
        "artifact_kind": "PROJECT_CODE_ASSEMBLY",
        "assembly_id": "ASSEMBLY.{}.R{:03d}".format(
            re.sub(r"[^A-Z0-9_.-]", ".", actual_job_id.upper()),
            candidate["revision"]),
        "job_id": actual_job_id,
        "revision": candidate["revision"],
        "unit_index_fingerprint": index["root_fingerprint"],
        "top": candidate["top"],
        "sequence": sequence,
        "content": candidate["content"],
        "content_fingerprint": candidate["content_fingerprint"],
        "assembly_fingerprint": _ZERO,
    }
    assembly["assembly_fingerprint"] = artifact_fingerprint(
        assembly, "assembly_fingerprint")
    validate_assembly(
        assembly, index, {item["unit_id"]: item for item in units}, error)
    return units, index, assembly

def build_stage3_bundle(
        candidate: dict[str, Any], stage1_units: dict[str, dict[str, Any]],
        stage2_units: dict[str, dict[str, Any]], owner_scope_fingerprint: str,
        error: Callable[[str, str], Exception], job_id: str | None = None
        ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    if candidate.get("schema_version") != "7.0":
        raise error("INVALID_SCHEMA", "Stage 3 candidate schema is invalid")
    return _build_explicit_stage3_bundle(
        candidate, stage1_units, stage2_units,
        owner_scope_fingerprint, error, job_id)

def validate_assembly(
        assembly: dict[str, Any], index: dict[str, Any],
        units: dict[str, dict[str, Any]],
        error: Callable[[str, str], Exception]) -> dict[str, Any]:
    if not accepted(validate("project_code_assembly", assembly)):
        raise error("INVALID_SCHEMA", "Stage 3 code assembly contract is invalid")
    if (assembly["assembly_fingerprint"] !=
            artifact_fingerprint(assembly, "assembly_fingerprint") or
            assembly["unit_index_fingerprint"] != index["root_fingerprint"] or
            assembly["job_id"] != index["job_id"] or
            assembly["revision"] != index["revision"]):
        raise error("STALE_EVIDENCE", "Stage 3 assembly lineage is stale")
    used: set[tuple[str, int]] = set()
    fragments: list[str] = []
    global_line = 1
    for ref in assembly["sequence"]:
        unit = units.get(ref["unit_id"])
        if unit is None or unit["unit_kind"] not in {"CODE_SHARED", "CODE_TESTCASE"}:
            raise error("INDEX_SUBSTITUTION", "assembly references an unknown code unit")
        segments = unit["semantic_body"].get("segments")
        position = ref["segment_index"]
        if (not isinstance(segments, list) or position >= len(segments) or
                (ref["unit_id"], position) in used):
            raise error("DUPLICATE_ASSEMBLY_REFERENCE", "assembly unit segment is invalid")
        used.add((ref["unit_id"], position))
        segment = segments[position]
        local = next((item for item in unit["local_evidence"]
                      if item.get("segment_index") == position), None)
        line_count = len(segment.splitlines())
        if (local is None or
                local.get("global_line_start") != global_line or
                local.get("global_line_end") != global_line + line_count - 1):
            raise error("ASSEMBLY_MISMATCH", "global code evidence is not derived from assembly")
        for selection in local.get("selections", []):
            selected = selection["unit_selection"]
            if (selection.get("global_line_start") !=
                    global_line + selected["line_start"] - 1 or
                    selection.get("global_line_end") !=
                    global_line + selected["line_end"] - 1):
                raise error("ASSEMBLY_MISMATCH", "global selection range is stale")
        fragments.append(segment)
        global_line += line_count
    expected = {
        (unit["unit_id"], index)
        for unit in units.values()
        for index, _ in enumerate(unit["semantic_body"].get("segments", []))
    }
    content = "".join(fragments)
    if (used != expected or content != assembly["content"] or
            _sha_text(content) != assembly["content_fingerprint"] or
            index["metadata"].get("assembly_sequence") != assembly["sequence"] or
            index["metadata"].get("complete_content_fingerprint") !=
                assembly["content_fingerprint"]):
        raise error("ASSEMBLY_MISMATCH", "code units do not assemble exact complete testcase")
    return assembly

def build_review_bundle(
        report: dict[str, Any], stage1_units: dict[str, dict[str, Any]],
        stage2_units: dict[str, dict[str, Any]],
        stage3_units: dict[str, dict[str, Any]],
        owner_scope_fingerprint: str, policy_fingerprint: str,
        spec_fingerprint: str, error: Callable[[str, str], Exception],
        job_id: str | None = None, *, storage_revision: int | None = None
        ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    actual_job_id = job_id or report["job_id"]
    producer = _provider(report["reviewer"], "REVIEWER")
    ac_units = _stage1_units_by_kind(stage1_units, "ACCEPTANCE_CRITERION")
    scenario_units = _stage1_units_by_kind(stage1_units, "SCENARIO")
    testcase_units = _stage1_units_by_kind(stage2_units, "LOGICAL_TESTCASE")
    coverage_units = _stage1_units_by_kind(stage2_units, "AC_COVERAGE")
    code_testcase = _stage1_units_by_kind(stage3_units, "CODE_TESTCASE")
    code_shared = _stage1_units_by_kind(stage3_units, "CODE_SHARED")
    common = {
        "job_id": actual_job_id,
        "revision": (report["review_round"] - 1
                     if storage_revision is None else storage_revision),
        "input_fingerprint": report["input_fingerprint"],
        "spec_fingerprint": spec_fingerprint,
        "policy_fingerprint": policy_fingerprint,
        "owner_scope_fingerprint": owner_scope_fingerprint,
        "producer": producer,
        "error": error,
    }
    issues_by_ac: dict[str, list[dict[str, Any]]] = {}
    global_issues = []
    for issue in report["findings"]:
        if issue["affected"]["ac_ids"]:
            for ac_id in issue["affected"]["ac_ids"]:
                issues_by_ac.setdefault(ac_id, []).append(issue)
        else:
            global_issues.append(issue)
    units: list[dict[str, Any]] = []
    ac_certificates: dict[str, dict[str, Any]] = {}
    for review in report["ac_reviews"]:
        ac_id = review["ac_id"]
        coverage_id = "COVERAGE.{}".format(ac_id)
        if ac_id not in ac_units or coverage_id not in coverage_units:
            raise error("UNDECLARED_DEPENDENCY", "review AC lineage unit is absent")
        deps = [
            _dep("ACCEPTANCE_CRITERION", ac_id, _lineage_fingerprint(ac_units[ac_id])),
            _dep("AC_COVERAGE", coverage_id, _lineage_fingerprint(coverage_units[coverage_id])),
        ]
        for scenario_id in review["scenario_ids"]:
            deps.append(_dep(
                "SCENARIO", scenario_id,
                _lineage_fingerprint(scenario_units[scenario_id])))
        for testcase_id in review["testcase_ids"]:
            if testcase_id not in testcase_units:
                raise error("UNDECLARED_DEPENDENCY", "review testcase unit is absent")
            deps.append(_dep(
                "LOGICAL_TESTCASE", testcase_id,
                _lineage_fingerprint(testcase_units[testcase_id])))
            for code_unit in code_testcase.values():
                direct_ids = {
                    item["identity"] for item in code_unit["dependency_fingerprints"]
                    if item["kind"] == "LOGICAL_TESTCASE"}
                if testcase_id in direct_ids:
                    deps.append(_dep(
                        "CODE_TESTCASE", code_unit["unit_id"],
                        _lineage_fingerprint(code_unit)))
        for shared in code_shared.values():
            deps.append(_dep(
                "CODE_SHARED", shared["unit_id"], _lineage_fingerprint(shared)))
        local_issues = sorted(
            copy.deepcopy(issues_by_ac.get(ac_id, [])),
            key=lambda item: item["issue_id"])
        semantic = {
            "ac_id": ac_id,
            "status": review["status"],
            "omission": review["omission"],
            "findings": [{
                key: copy.deepcopy(issue[key]) for key in (
                    "issue_id", "severity", "suspected_origin_stage",
                    "affected", "problem_and_required_change")}
                for issue in local_issues],
        }
        local_evidence = [{
            "kind": kind,
            **copy.deepcopy(item),
        } for kind, values in (
            ("STIMULUS", review["stimulus_evidence"]),
            ("CHECKER", review["checker_evidence"]),
        ) for item in values]
        unit = _make_unit(
            stage=REVIEW, unit_kind="REVIEW_AC",
            unit_id="CERT.AC.{}".format(ac_id), semantic_body=semantic,
            spec_evidence=review["spec_evidence"],
            local_evidence=local_evidence, dependencies=deps, **common)
        units.append(unit)
        ac_certificates[ac_id] = unit
    for testcase_id, testcase in sorted(testcase_units.items()):
        ac_ids = sorted(
            item["identity"] for item in testcase["dependency_fingerprints"]
            if item["kind"] == "ACCEPTANCE_CRITERION")
        deps = [_dep(
            "LOGICAL_TESTCASE", testcase_id, _lineage_fingerprint(testcase))]
        statuses = []
        for ac_id in ac_ids:
            certificate = ac_certificates.get(ac_id)
            if certificate is None:
                raise error("INCOMPLETE_REVIEW", "testcase lacks an AC certificate")
            deps.append(_dep(
                "REVIEW_AC", certificate["unit_id"],
                _lineage_fingerprint(certificate)))
            statuses.append({
                "ac_id": ac_id,
                "status": certificate["semantic_body"]["status"],
            })
        for code_unit in code_testcase.values():
            if any(item["kind"] == "LOGICAL_TESTCASE" and
                   item["identity"] == testcase_id
                   for item in code_unit["dependency_fingerprints"]):
                deps.append(_dep(
                    "CODE_TESTCASE", code_unit["unit_id"],
                    _lineage_fingerprint(code_unit)))
        units.append(_make_unit(
            stage=REVIEW, unit_kind="REVIEW_TESTCASE",
            unit_id="CERT.TESTCASE.{}".format(testcase_id),
            semantic_body={"testcase_id": testcase_id, "ac_statuses": statuses},
            spec_evidence=[], local_evidence=[], dependencies=deps, **common))
    for shared_id, shared in sorted(code_shared.items()):
        deps = [_dep("CODE_SHARED", shared_id, _lineage_fingerprint(shared))]
        units.append(_make_unit(
            stage=REVIEW, unit_kind="REVIEW_SHARED",
            unit_id="CERT.SHARED.{}".format(shared_id),
            semantic_body={
                "code_unit_id": shared_id,
                "findings": [{
                    key: copy.deepcopy(issue[key]) for key in (
                        "issue_id", "severity", "suspected_origin_stage",
                        "affected", "problem_and_required_change")}
                    for issue in sorted(global_issues, key=lambda item: item["issue_id"])],
            }, spec_evidence=[
                evidence for issue in global_issues
                for evidence in issue["spec_evidence"]],
            local_evidence=[], dependencies=deps, **common))
    index = _make_index(
        stage=REVIEW, variant="CURRENT", units=units,
        metadata={
            "source_report_fingerprint": report["report_fingerprint"],
            "verdict": report["verdict"],
            "certificate_scope": "PER_AC_PER_TESTCASE_PER_SHARED_UNIT",
            "complete_ac_ids": sorted(ac_certificates),
        }, **common)
    return units, index

def project_input_fingerprint(value: dict[str, Any]) -> str:
    return artifact_fingerprint(value, "input_fingerprint")


def project_authority_fingerprint(value: dict[str, Any]) -> str:
    return canonical_hash({
        "schema_version": value.get("schema_version"),
        "manifest_kind": value.get("manifest_kind"),
        "project_id": value.get("project_id"),
        "spec": copy.deepcopy(value.get("spec")),
        "rtl": copy.deepcopy(value.get("rtl")),
        "uvm_testcase_context": copy.deepcopy(
            value.get("uvm_testcase_context")),
        "agent_profile": copy.deepcopy(value.get("agent_profile")),
        "eda": copy.deepcopy(value.get("eda")),
        "input_authority": {
            key: copy.deepcopy(value.get("input_authority", {}).get(key))
            for key in ("actor_type", "identity", "roles", "decision")
        },
    })


def project_candidate_fingerprint(value: dict[str, Any]) -> str:
    return artifact_fingerprint(value, "candidate_fingerprint")


def project_report_fingerprint(value: dict[str, Any]) -> str:
    return artifact_fingerprint(value, "report_fingerprint")


def _artifact_paths(
        map1: Mapping[str, Any], map2: Mapping[str, Any],
        candidate: Mapping[str, Any]) -> dict[str, str]:
    return {
        "scenario_ac_map":
            "staging/mappings/scenario_ac_map.checked.r{:03d}.json".format(
                map1["revision"]),
        "ac_testcase_map":
            "staging/mappings/ac_testcase_map.r{:03d}.json".format(
                map2["revision"]),
        "testcase":
            "staging/generated/portable_sv/testcase.r{:03d}.json".format(
                candidate["revision"]),
    }

def _artifact_roots(
        map1: Mapping[str, Any], map2: Mapping[str, Any],
        candidate: Mapping[str, Any]) -> dict[str, str]:
    return {
        "scenario_ac_map": map1["artifact_fingerprint"],
        "ac_testcase_map": map2["artifact_fingerprint"],
        "testcase": candidate["candidate_fingerprint"],
        "effective_uvm": candidate["effective_uvm_root"],
    }

def _rebuild_stage1(
        source: Mapping[str, Any], replacements: Mapping[str, Mapping[str, Any]],
        provider: Mapping[str, Any], revision: int,
        error: Callable[..., Exception]) -> dict[str, Any]:
    value = copy.deepcopy(dict(source))
    value["revision"] = revision
    value["provider"] = copy.deepcopy(dict(provider))
    for collection, identity in (
            ("scenarios", "scenario_id"),
            ("acceptance_criteria", "ac_id")):
        for item in value[collection]:
            replacement = replacements.get(item[identity])
            if replacement is not None:
                item.update(copy.deepcopy(replacement["semantic_body"]))
            item["item_fingerprint"] = artifact_fingerprint(
                item, "item_fingerprint")
    value["map_id"] = "SCENARIOACMAP.{}".format(canonical_hash({
        "job": value["job_id"], "revision": revision,
        "scenarios": value["scenarios"],
        "acceptance_criteria": value["acceptance_criteria"],
    })[:16].upper())
    value["artifact_fingerprint"] = artifact_fingerprint(
        value, "artifact_fingerprint")
    return value

def _rebuild_stage2(
        source: Mapping[str, Any], logical_testcases: list[dict[str, Any]],
        replacements: Mapping[str, Mapping[str, Any]],
        map1: Mapping[str, Any], provider: Mapping[str, Any], revision: int
        ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    testcases = copy.deepcopy(logical_testcases)
    for testcase in testcases:
        replacement = replacements.get(testcase["testcase_id"])
        if replacement is not None:
            body = copy.deepcopy(replacement["semantic_body"])
            body.pop("reason", None)
            testcase.update(body)
        testcase["testcase_fingerprint"] = artifact_fingerprint(
            testcase, "testcase_fingerprint")
    coverage = []
    for ac_id in sorted(
            item["ac_id"] for item in map1["acceptance_criteria"]):
        item = {
            "ac_id": ac_id,
            "testcase_ids": sorted(
                testcase["testcase_id"] for testcase in testcases
                if ac_id in testcase["ac_ids"]),
            "coverage_fingerprint": "0" * 64,
        }
        item["coverage_fingerprint"] = artifact_fingerprint(
            item, "coverage_fingerprint")
        coverage.append(item)
    value = copy.deepcopy(dict(source))
    value.update({
        "revision": revision,
        "map_id": "ACTESTCASEMAP.{}".format(canonical_hash({
            "job": source["job_id"], "revision": revision,
            "map1": map1["artifact_fingerprint"],
            "testcases": testcases,
        })[:16].upper()),
        "scenario_ac_map_fingerprint": map1["artifact_fingerprint"],
        "upstream_fingerprints": {
            "input": source["input_fingerprint"],
            "spec": source["spec_fingerprint"],
            "scenario_ac_map": map1["artifact_fingerprint"],
        },
        "storage": "INLINE",
        "logical_testcases": testcases,
        "shards": [],
        "ac_coverage": coverage,
        "completeness": {
            **copy.deepcopy(source["completeness"]),
            "ac_ids": sorted(item["ac_id"] for item in
                             map1["acceptance_criteria"]),
            "testcase_ids": sorted(item["testcase_id"] for item in testcases),
        },
        "provider": copy.deepcopy(dict(provider)),
        "artifact_fingerprint": "0" * 64,
    })
    value["artifact_fingerprint"] = artifact_fingerprint(
        value, "artifact_fingerprint")
    return value, testcases

def _validate_committed_candidate(
        value: Mapping[str, Any],
        error: Callable[..., Exception]) -> None:
    if (not accepted(validate("project_committed_testcase", value)) or
            value["candidate_fingerprint"] != artifact_fingerprint(
                value, "candidate_fingerprint") or
            value["content_fingerprint"] != _sha_text(value["content"])):
        raise error(
            "INVALID_SCHEMA", "committed testcase contract is invalid")
    by_id = {item["code_unit_id"]: item for item in value["code_units"]}
    if (len(by_id) != len(value["code_units"]) or
            set(by_id) != set(value["assembly_manifest"]) or
            "".join(by_id[item]["content"]
                    for item in value["assembly_manifest"]) != value["content"] or
            any(item["content_fingerprint"] != _sha_text(item["content"])
                for item in value["code_units"])):
        raise error(
            "ASSEMBLY_MISMATCH", "committed testcase assembly is invalid")

def _rebuild_stage3(
        source: Mapping[str, Any], replacements: Mapping[str, Mapping[str, Any]],
        map1: Mapping[str, Any], map2: Mapping[str, Any],
        provider: Mapping[str, Any], revision: int,
        error: Callable[..., Exception]) -> dict[str, Any]:
    code_units = copy.deepcopy(source["code_units"])
    for unit in code_units:
        replacement = replacements.get(unit["code_unit_id"])
        if replacement is not None:
            unit["content"] = replacement["semantic_body"]["segments"][0]
        unit["content_fingerprint"] = _sha_text(unit["content"])
    by_id = {item["code_unit_id"]: item for item in code_units}
    content = "".join(
        by_id[unit_id]["content"] for unit_id in source["assembly_manifest"])
    content_fp = _sha_text(content)
    checks = sorted([
        "ASSEMBLY_COMPLETE", "CONTENT_FINGERPRINT", "FRAMEWORK_FORMALIZED",
        "FINAL_REVIEW_TRACEABILITY_REQUIRED",
    ])
    validation = {
        "status": "PASS", "checks": checks,
        "validation_fingerprint": canonical_hash({
            "content_fingerprint": content_fp,
            "input_fingerprint": source["input_fingerprint"],
            "scenario_ac_map_fingerprint": map1["artifact_fingerprint"],
            "ac_testcase_map_fingerprint": map2["artifact_fingerprint"],
            "checks": checks,
        }),
    }
    value = {
        "schema_version": "1.0",
        "artifact_kind": "COMMITTED_UVM_TESTCASE_BUNDLE",
        "candidate_id": "PROJECTTESTCOMMIT.{}".format(canonical_hash({
            "job": source["job_id"], "revision": revision,
            "content": content_fp,
        })[:16].upper()),
        "job_id": source["job_id"],
        "revision": revision,
        "state": "COMMIT_PREPARED",
        "output_path":
            "staging/generated/uvm/generated_tests.r{:03d}.sv".format(
                revision),
        "top": source["top"],
        "content": content,
        "content_fingerprint": content_fp,
        "input_fingerprint": source["input_fingerprint"],
        "spec_fingerprint": source["spec_fingerprint"],
        "scenario_ac_map_fingerprint": map1["artifact_fingerprint"],
        "ac_testcase_map_fingerprint": map2["artifact_fingerprint"],
        "effective_uvm_root": source["effective_uvm_root"],
        "upstream_fingerprints": {
            "input": source["input_fingerprint"],
            "spec": source["spec_fingerprint"],
            "scenario_ac_map": map1["artifact_fingerprint"],
            "ac_testcase_map": map2["artifact_fingerprint"],
            "effective_uvm": source["effective_uvm_root"],
        },
        "policy_fingerprint": source["policy_fingerprint"],
        "code_units": sorted(code_units, key=lambda item: item["code_unit_id"]),
        "assembly_manifest": copy.deepcopy(source["assembly_manifest"]),
        "implemented_testcase_ids": copy.deepcopy(
            source["implemented_testcase_ids"]),
        "skipped_testcases": copy.deepcopy(source["skipped_testcases"]),
        "traceability_status": "FINAL_REVIEW_REQUIRED",
        "provider": copy.deepcopy(dict(provider)),
        "validation": validation,
        "candidate_fingerprint": "0" * 64,
    }
    value["candidate_fingerprint"] = artifact_fingerprint(
        value, "candidate_fingerprint")
    _validate_committed_candidate(value, error)
    return value

def validate_compile_validation(
        value: Mapping[str, Any], *, job_id: str,
        checkpoint_fingerprint: str, replacement_fingerprint: str,
        candidate_fingerprint: str,
        error: Callable[..., Exception]) -> dict[str, Any]:
    if (not accepted(validate("project_compile_validation", value)) or
            value.get("validation_fingerprint") != artifact_fingerprint(
                value, "validation_fingerprint") or
            value.get("job_id") != job_id or
            value.get("source_checkpoint_fingerprint") !=
                checkpoint_fingerprint or
            value.get("replacement_fingerprint") != replacement_fingerprint or
            value.get("candidate_fingerprint") != candidate_fingerprint or
            not accepted(validate("eda_probe_request", value.get("request"))) or
            not accepted(validate("eda_probe_evidence", value.get("evidence"))) or
            value["evidence"].get("request_fingerprint") !=
                value["request"].get("request_fingerprint") or
            value.get("status") != value["evidence"].get("execution_status")):
        raise error(
            "STALE_EVIDENCE", "precommit compile validation is stale")
    return copy.deepcopy(dict(value))
