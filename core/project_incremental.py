"""PJ-002.9-HF1 incremental semantic units and direct-lineage fingerprints.

Aggregate maps, candidates, and reports remain the completeness boundary.
This module adds the smaller, independently comparable records used to decide
whether one semantic scope is dirty without treating an aggregate root as a
local reuse key.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from contracts.validator import accepted, load_document, validate
from core.atomic_artifact import publish_immutable_text
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


def _artifact_fingerprint(value: dict[str, Any], field: str) -> str:
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
    unit["artifact_fingerprint"] = _artifact_fingerprint(
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
    if unit["artifact_fingerprint"] != _artifact_fingerprint(
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


def _safe_target(
        job_root: Path, relative: str,
        error: Callable[[str, str], Exception]) -> Path:
    pure = PurePosixPath(relative)
    if (pure.is_absolute() or not pure.parts or
            any(part in {"", ".", ".."} or part.startswith(".")
                for part in pure.parts)):
        raise error("PATH_ESCAPE", "incremental artifact path is unsafe")
    root = job_root.resolve()
    target = job_root.joinpath(*pure.parts)
    current = job_root
    for part in pure.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise error("PATH_ESCAPE", "incremental artifact parent is a symlink")
    try:
        target.resolve(strict=False).relative_to(root)
    except ValueError as caught:
        raise error("PATH_ESCAPE", "incremental artifact escapes its Job") from caught
    return target


def _immutable_json(
        path: Path, value: dict[str, Any], max_file_bytes: int,
        error: Callable[[str, str], Exception]) -> None:
    content = json.dumps(
        value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    if len(content.encode("utf-8")) > max_file_bytes:
        raise error("FILE_LIMIT_EXCEEDED", "incremental artifact exceeds file budget")
    publish_immutable_text(
        path, content,
        lambda message: error("STALE_EVIDENCE", message),
        "append-only incremental artifact conflicts")


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
    index["root_fingerprint"] = _artifact_fingerprint(index, "root_fingerprint")
    if not accepted(validate("project_artifact_index", index)):
        raise error("INVALID_SCHEMA", "incremental artifact index schema is invalid")
    return index


def _persist_bundle(
        *, job_root: Path, stage: str, variant: str, revision: int,
        units: list[dict[str, Any]], index: dict[str, Any],
        max_file_bytes: int, error: Callable[[str, str], Exception]
        ) -> dict[str, Any]:
    if len({item["unit_id"] for item in units}) != len(units):
        raise error("DUPLICATE_MAPPING_ID", "incremental unit IDs collide")
    # Child artifacts are written first. A crash before the index is a typed
    # partial state; an index can therefore never authorize missing children.
    for unit in units:
        relative = _unit_relative(stage, variant, revision, unit["unit_id"])
        _immutable_json(
            _safe_target(job_root, relative, error), unit,
            max_file_bytes, error)
    relative = _index_relative(stage, variant, revision)
    _immutable_json(
        _safe_target(job_root, relative, error), index,
        max_file_bytes, error)
    validate_index(index, job_root, error)
    return {
        "path": relative,
        "index": index,
        "units": {item["unit_id"]: item for item in units},
    }


def validate_index(
        index: dict[str, Any], job_root: Path,
        error: Callable[[str, str], Exception],
        expected_stage: str | None = None,
        expected_job_id: str | None = None) -> dict[str, dict[str, Any]]:
    if not accepted(validate("project_artifact_index", index)):
        raise error("INVALID_SCHEMA", "incremental artifact index schema is invalid")
    if index["root_fingerprint"] != _artifact_fingerprint(
            index, "root_fingerprint"):
        raise error("STALE_EVIDENCE", "incremental aggregate root is stale")
    if ((expected_stage is not None and index["stage"] != expected_stage) or
            (expected_job_id is not None and index["job_id"] != expected_job_id)):
        raise error("CROSS_JOB_ARTIFACT", "incremental index identity is stale")
    children = index["children"]
    canonical = sorted(children, key=lambda item: (item["unit_kind"], item["unit_id"]))
    ids = [item["unit_id"] for item in children]
    paths = [item["path"] for item in children]
    if (children != canonical or len(ids) != len(set(ids)) or
            len(paths) != len(set(paths)) or
            index["completeness"] != {
                "unit_ids": ids, "unit_count": len(ids)}):
        raise error("NON_CANONICAL_INDEX", "incremental child index is incomplete")
    units: dict[str, dict[str, Any]] = {}
    for child in children:
        path = _safe_target(job_root, child["path"], error)
        if not path.is_file() or path.is_symlink():
            raise error("PARTIAL_ARTIFACT", "indexed incremental child is missing")
        try:
            unit = load_document(path)
        except Exception as caught:
            raise error("INVALID_SCHEMA", "indexed incremental child is malformed") from caught
        validate_unit(unit, error)
        if (unit["unit_id"] != child["unit_id"] or
                unit["unit_kind"] != child["unit_kind"] or
                unit["stage"] != index["stage"] or
                unit["job_id"] != index["job_id"] or
                unit["revision"] != index["revision"] or
                unit["input_fingerprint"] != index["input_fingerprint"] or
                unit["spec_fingerprint"] != index["spec_fingerprint"] or
                unit["policy_fingerprint"] != index["policy_fingerprint"] or
                unit["owner_scope_fingerprint"] != index["owner_scope_fingerprint"] or
                any(unit[field] != child[field] for field in (
                    "content_fingerprint", "dependency_fingerprint",
                    "artifact_fingerprint"))):
            raise error("INDEX_SUBSTITUTION", "incremental child/index lineage differs")
        units[unit["unit_id"]] = unit
    return units


def load_index(
        job_root: Path, relative: str,
        error: Callable[[str, str], Exception],
        expected_stage: str | None = None,
        expected_job_id: str | None = None
        ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    path = _safe_target(job_root, relative, error)
    if not path.is_file() or path.is_symlink():
        raise error("PARTIAL_ARTIFACT", "incremental index is missing")
    try:
        index = load_document(path)
    except Exception as caught:
        raise error("INVALID_SCHEMA", "incremental index is malformed") from caught
    units = validate_index(index, job_root, error, expected_stage, expected_job_id)
    return index, units


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
    assembly["assembly_fingerprint"] = _artifact_fingerprint(
        assembly, "assembly_fingerprint")
    validate_assembly(
        assembly, index, {item["unit_id"]: item for item in units}, error)
    return units, index, assembly


def build_stage3_bundle(
        candidate: dict[str, Any], stage1_units: dict[str, dict[str, Any]],
        stage2_units: dict[str, dict[str, Any]], owner_scope_fingerprint: str,
        error: Callable[[str, str], Exception], job_id: str | None = None
        ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    if candidate.get("schema_version") != "6.0":
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
            _artifact_fingerprint(assembly, "assembly_fingerprint") or
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


class IncrementalArtifactStore:
    """Append-only storage and replay validation for PJ-002.9-HF1 units."""

    def __init__(
            self, job_root: Path, max_file_bytes: int,
            error: Callable[[str, str], Exception]):
        self.job_root = Path(job_root)
        self.max_file_bytes = max_file_bytes
        self.error = error

    def persist_stage1(
            self, map1: dict[str, Any], owner_scope_fingerprint: str,
            variant: str = "PROVIDER", job_id: str | None = None
            ) -> dict[str, Any]:
        units, index = build_stage1_bundle(
            map1, owner_scope_fingerprint, self.error, variant, job_id)
        return _persist_bundle(
            job_root=self.job_root, stage=STAGE1, variant=variant,
            revision=map1["revision"], units=units, index=index,
            max_file_bytes=self.max_file_bytes, error=self.error)

    def persist_stage2(
            self, map2: dict[str, Any], logical_testcases: list[dict[str, Any]],
            stage1_units: dict[str, dict[str, Any]],
            owner_scope_fingerprint: str, job_id: str | None = None
            ) -> dict[str, Any]:
        units, index = build_stage2_bundle(
            map2, logical_testcases, stage1_units,
            owner_scope_fingerprint, self.error, job_id)
        return _persist_bundle(
            job_root=self.job_root, stage=STAGE2, variant="CURRENT",
            revision=map2["revision"], units=units, index=index,
            max_file_bytes=self.max_file_bytes, error=self.error)

    def persist_stage3(
            self, candidate: dict[str, Any],
            stage1_units: dict[str, dict[str, Any]],
            stage2_units: dict[str, dict[str, Any]],
            owner_scope_fingerprint: str, job_id: str | None = None
            ) -> dict[str, Any]:
        units, index, assembly = build_stage3_bundle(
            candidate, stage1_units, stage2_units,
            owner_scope_fingerprint, self.error, job_id)
        result = _persist_bundle(
            job_root=self.job_root, stage=STAGE3, variant="CURRENT",
            revision=candidate["revision"], units=units, index=index,
            max_file_bytes=self.max_file_bytes, error=self.error)
        relative = index["metadata"]["assembly_path"]
        _immutable_json(
            _safe_target(self.job_root, relative, self.error), assembly,
            self.max_file_bytes, self.error)
        validate_assembly(assembly, index, result["units"], self.error)
        result.update({
            "assembly_path": relative,
            "assembly": assembly,
        })
        return result

    def persist_review(
            self, report: dict[str, Any],
            stage1_units: dict[str, dict[str, Any]],
            stage2_units: dict[str, dict[str, Any]],
            stage3_units: dict[str, dict[str, Any]],
            owner_scope_fingerprint: str, policy_fingerprint: str,
            spec_fingerprint: str, job_id: str | None = None, *,
            storage_revision: int | None = None
            ) -> dict[str, Any]:
        units, index = build_review_bundle(
            report, stage1_units, stage2_units, stage3_units,
            owner_scope_fingerprint, policy_fingerprint, spec_fingerprint,
            self.error, job_id, storage_revision=storage_revision)
        return _persist_bundle(
            job_root=self.job_root, stage=REVIEW, variant="CURRENT",
            revision=(report["review_round"] - 1 if storage_revision is None
                      else storage_revision),
            units=units, index=index,
            max_file_bytes=self.max_file_bytes, error=self.error)

    def load(
            self, stage: str, variant: str, revision: int,
            job_id: str | None = None
            ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        return load_index(
            self.job_root, _index_relative(stage, variant, revision),
            self.error, stage, job_id)

    def load_stage3(
            self, revision: int, job_id: str | None = None
            ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
        index, units = self.load(STAGE3, "CURRENT", revision, job_id)
        relative = index["metadata"].get("assembly_path", "")
        path = _safe_target(self.job_root, relative, self.error)
        if not path.is_file() or path.is_symlink():
            raise self.error("PARTIAL_ARTIFACT", "Stage 3 assembly is missing")
        assembly = load_document(path)
        validate_assembly(assembly, index, units, self.error)
        return index, units, assembly

    def compare_and_persist(
            self, old_index_paths: dict[str, str],
            new_index_paths: dict[str, str], revision: int
            ) -> dict[str, Any]:
        """Validate two exact revision sets and append one impact manifest."""
        if set(old_index_paths) != set(new_index_paths):
            raise self.error(
                "PARTIAL_ARTIFACT",
                "old/new impact index sets must name the same stages")
        old_indexes = {
            name: load_index(self.job_root, path, self.error)
            for name, path in sorted(old_index_paths.items())}
        new_indexes = {
            name: load_index(self.job_root, path, self.error)
            for name, path in sorted(new_index_paths.items())}
        manifest = evaluate_impact(old_indexes, new_indexes, self.error)
        path = persist_impact(
            self.job_root, manifest, revision,
            self.max_file_bytes, self.error)
        return {"path": path, "manifest": manifest}


def evaluate_impact(
        old_indexes: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]],
        new_indexes: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]],
        error: Callable[[str, str], Exception], *,
        allow_stale_stages: Iterable[str] = ()) -> dict[str, Any]:
    """Compare validated direct-lineage units without executing any repair."""
    if not old_indexes or not new_indexes:
        raise error("PARTIAL_ARTIFACT", "impact analysis requires old and new indexes")
    ordered_old = [old_indexes[key][0] for key in sorted(old_indexes)]
    ordered_new = [new_indexes[key][0] for key in sorted(new_indexes)]
    authority_fields = (
        "job_id", "input_fingerprint", "spec_fingerprint",
        "policy_fingerprint", "owner_scope_fingerprint",
    )
    authority = {field: ordered_new[0][field] for field in authority_fields}
    for index in [*ordered_old, *ordered_new]:
        if any(index[field] != authority[field] for field in authority_fields):
            code = "CROSS_JOB_ARTIFACT" if index["job_id"] != authority["job_id"] \
                else "AUTHORITY_DRIFT"
            raise error(code, "impact comparison authority is not exact")
    def flatten(
            values: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]]
            ) -> dict[str, dict[str, Any]]:
        result = {}
        for _, (index, units) in sorted(values.items()):
            for unit in units.values():
                key = "{}:{}:{}".format(
                    unit["stage"], unit["unit_kind"], unit["unit_id"])
                if key in result:
                    raise error("INDEX_COLLISION", "impact unit identity collides")
                result[key] = unit
        return result
    old_units = flatten(old_indexes)
    new_units = flatten(new_indexes)
    current_lineage = {
        (unit["unit_kind"], unit["unit_id"]): _lineage_fingerprint(unit)
        for unit in new_units.values()
    }
    allowed_stale = set(allow_stale_stages)
    for unit in new_units.values():
        if unit.get("stage") in allowed_stale:
            continue
        for dependency in unit["dependency_fingerprints"]:
            target = current_lineage.get((
                dependency["kind"], dependency["identity"]))
            if target is not None and target != dependency["fingerprint"]:
                raise error(
                    "UNDECLARED_DEPENDENCY",
                    "unit direct dependency does not bind the current child")
    dirty: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    dirty_lineages: set[tuple[str, str]] = set()
    pending: dict[str, list[str]] = {}
    for key, unit in new_units.items():
        prior = old_units.get(key)
        reasons = []
        if prior is None:
            reasons.append("ADDED")
        else:
            if prior["content_fingerprint"] != unit["content_fingerprint"]:
                reasons.append("CONTENT_CHANGED")
            if prior["dependency_fingerprint"] != unit["dependency_fingerprint"]:
                reasons.append("DIRECT_DEPENDENCY_CHANGED")
            if _producer_dependency(prior["producer"]) != _producer_dependency(unit["producer"]):
                if "DIRECT_DEPENDENCY_CHANGED" not in reasons:
                    reasons.append("DIRECT_DEPENDENCY_CHANGED")
        pending[key] = reasons
        if reasons:
            dirty_lineages.add((unit["unit_kind"], unit["unit_id"]))
    changed = True
    while changed:
        changed = False
        for key, unit in new_units.items():
            if pending[key]:
                continue
            if any((dependency["kind"], dependency["identity"]) in dirty_lineages
                   for dependency in unit["dependency_fingerprints"]):
                pending[key].append("TRANSITIVE_DEPENDENCY_CHANGED")
                dirty_lineages.add((unit["unit_kind"], unit["unit_id"]))
                changed = True
    def change_record(
            key: str, old: dict[str, Any] | None,
            new: dict[str, Any] | None, reasons: list[str]) -> dict[str, Any]:
        source = new or old
        assert source is not None
        return {
            "unit_key": key,
            "unit_id": source["unit_id"],
            "stage": source["stage"],
            "unit_kind": source["unit_kind"],
            "old_fingerprint": old["artifact_fingerprint"] if old else None,
            "new_fingerprint": new["artifact_fingerprint"] if new else None,
            "reasons": sorted(set(reasons)),
        }
    for key in sorted(new_units):
        record = change_record(
            key, old_units.get(key), new_units[key], pending[key] or
            ["UNCHANGED"])
        if pending[key]:
            dirty.append(record)
        else:
            reused.append(record)
    for key in sorted(set(old_units) - set(new_units)):
        removed.append(change_record(
            key, old_units[key], None, ["REMOVED"]))
    old_roots = {key: value[0]["root_fingerprint"]
                 for key, value in sorted(old_indexes.items())}
    new_roots = {key: value[0]["root_fingerprint"]
                 for key, value in sorted(new_indexes.items())}
    seed = {
        "job_id": authority["job_id"],
        "old_roots": old_roots, "new_roots": new_roots,
        "dirty": dirty, "reused": reused, "removed": removed,
    }
    manifest = {
        "schema_version": IMPACT_CONTRACT_VERSION,
        "artifact_kind": "PROJECT_INCREMENTAL_IMPACT",
        "impact_id": "IMPACT.{}".format(canonical_hash(seed)[:16].upper()),
        **authority,
        "old_roots": old_roots,
        "new_roots": new_roots,
        "dirty_units": dirty,
        "reused_units": reused,
        "removed_units": removed,
        "impact_fingerprint": _ZERO,
    }
    manifest["impact_fingerprint"] = _artifact_fingerprint(
        manifest, "impact_fingerprint")
    if not accepted(validate("project_impact_manifest", manifest)):
        raise error("INVALID_SCHEMA", "incremental impact manifest is invalid")
    return manifest


def persist_impact(
        job_root: Path, manifest: dict[str, Any], revision: int,
        max_file_bytes: int, error: Callable[[str, str], Exception]) -> str:
    if manifest["impact_fingerprint"] != _artifact_fingerprint(
            manifest, "impact_fingerprint"):
        raise error("STALE_EVIDENCE", "impact manifest fingerprint is stale")
    relative = "staging/units/impact/impact.r{:03d}.json".format(revision)
    _immutable_json(
        _safe_target(job_root, relative, error), manifest,
        max_file_bytes, error)
    return relative


__all__ = [
    "ASSEMBLY_CONTRACT_VERSION", "IMPACT_CONTRACT_VERSION",
    "INDEX_CONTRACT_VERSION", "INCREMENTAL_POLICY_VERSION",
    "IncrementalArtifactStore", "REVIEW", "STAGE1", "STAGE2", "STAGE3",
    "UNIT_CONTRACT_VERSION", "build_review_bundle", "build_stage1_bundle",
    "build_stage2_bundle", "build_stage3_bundle", "evaluate_impact",
    "load_index", "persist_impact", "unrouted_owner_scope",
    "validate_assembly", "validate_index", "validate_unit",
]
