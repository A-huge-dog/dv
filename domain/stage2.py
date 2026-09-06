"""Pure Stage 2 AC/testcase-map generation and validation rules."""
from __future__ import annotations

import copy
import json
from typing import Any, Callable

from contracts.validator import accepted, validate
from domain._mapping import validate_semantic_completeness
from domain.artifacts import artifact_fingerprint
from domain.evidence import (
    _enrich_evidence, _failure_with_context, _provider_identity,
    _validate_enriched_evidence,
)
from scripts.dvlib import canonical_hash


STAGE2 = "AC_TESTCASE_MAP"


def validate_ac_testcase_map(
        value: dict[str, Any], scenario_map: dict[str, Any],
        project_input: dict[str, Any], spec_sources: dict[str, str],
        spec_fingerprint: str, policy_fingerprint: str,
        logical_testcases: list[dict[str, Any]],
        error: Callable[..., Exception]) -> dict[str, Any]:
    if not accepted(validate("ac_testcase_map", value)):
        raise error("INVALID_SCHEMA", "AC/testcase map contract is invalid")
    if value["artifact_fingerprint"] != artifact_fingerprint(
            value, "artifact_fingerprint"):
        raise error("STALE_EVIDENCE", "AC/testcase map fingerprint is stale")
    map1_fp = scenario_map["artifact_fingerprint"]
    if (
        value["job_id"] != project_input["job_id"] or
        value["input_fingerprint"] != project_input["input_fingerprint"] or
        value["spec_fingerprint"] != spec_fingerprint or
        value["scenario_ac_map_fingerprint"] != map1_fp or
        value["upstream_fingerprints"] != {
            "input": project_input["input_fingerprint"],
            "spec": spec_fingerprint,
            "scenario_ac_map": map1_fp,
        } or value["policy_fingerprint"] != policy_fingerprint
    ):
        raise error("STALE_EVIDENCE",
                    "AC/testcase map upstream lineage is stale")
    ac_by_id = {
        item["ac_id"]: item for item in
        scenario_map["acceptance_criteria"]}
    known_scenarios = {
        item["scenario_id"] for item in scenario_map["scenarios"]}
    testcase_ids = [item["testcase_id"] for item in logical_testcases]
    if len(testcase_ids) != len(set(testcase_ids)):
        raise error("DUPLICATE_MAPPING_ID",
                    "logical testcase IDs must be unique")
    testcase_by_id = {
        item["testcase_id"]: item for item in logical_testcases}
    unauthorized_oracles = []
    for index, testcase in enumerate(logical_testcases):
        if testcase["testcase_fingerprint"] != artifact_fingerprint(
                testcase, "testcase_fingerprint"):
            raise error("STALE_EVIDENCE",
                        "logical testcase fingerprint is stale")
        if not set(testcase["scenario_ids"]).issubset(known_scenarios) or \
                not set(testcase["ac_ids"]).issubset(ac_by_id):
            raise error("ORPHAN_MAPPING",
                        "logical testcase references unknown scenario/AC")
        _validate_enriched_evidence(
            testcase["spec_evidence"], spec_sources, error)
        if testcase["status"] == "CHECKABLE" and (
            not testcase["stimulus"].strip() or
            not testcase["transaction_sequence"].strip() or
            not testcase["checker"].strip() or
            not testcase["expected_result"].strip() or
            not testcase["failure_condition"].strip()
        ):
            raise error("MISSING_STIMULUS_OR_ORACLE",
                        "checkable AC mapping requires stimulus and oracle")
        if testcase["status"] in {
                "BLOCKED_CONTRACT", "SPEC_AMBIGUITY"} and (
                    testcase["checker"].strip() or
                    testcase["expected_result"].strip()):
            unauthorized_oracles.append({
                "code": "UNAUTHORIZED_ORACLE",
                "message": (
                    "blocked/ambiguous mapping cannot define an oracle; "
                    "clear checker and expected_result"),
                "path": (
                    "ac_testcase_candidate.logical_testcases[{}]".format(
                        index)),
            })
    if unauthorized_oracles:
        raise _failure_with_context(
            error, "UNAUTHORIZED_ORACLE",
            "blocked/ambiguous mappings cannot define an oracle",
            {"correction_diagnostics": unauthorized_oracles})
    coverage = value["ac_coverage"]
    coverage_ids = [item["ac_id"] for item in coverage]
    if len(coverage_ids) != len(set(coverage_ids)) or \
            set(coverage_ids) != set(ac_by_id):
        raise error("INCOMPLETE_MAPPING",
                    "AC coverage must contain every AC exactly once")
    omitted_ac_ids = set(value["completeness"]["omitted_ac_ids"])
    missing_checkable_coverage = []
    for item in coverage:
        if item["coverage_fingerprint"] != artifact_fingerprint(
                item, "coverage_fingerprint"):
            raise error("STALE_EVIDENCE", "AC coverage fingerprint is stale")
        if not set(item["testcase_ids"]).issubset(testcase_by_id):
            raise error("UNKNOWN_TESTCASE_REFERENCE",
                        "AC coverage references an unknown testcase")
        mapped = {
            tc_id for tc_id, testcase in testcase_by_id.items()
            if item["ac_id"] in testcase["ac_ids"]}
        if set(item["testcase_ids"]) != mapped:
            raise error("MAPPING_MISMATCH",
                        "AC coverage and testcase mapping disagree")
        ac = ac_by_id[item["ac_id"]]
        if ac["status"] == "CHECKABLE" and item["ac_id"] not in omitted_ac_ids:
            if not item["testcase_ids"] or not any(
                    testcase_by_id[tc_id]["status"] == "CHECKABLE"
                    for tc_id in item["testcase_ids"]):
                for index, testcase in enumerate(logical_testcases):
                    if item["ac_id"] in testcase["ac_ids"]:
                        missing_checkable_coverage.append({
                            "code": "MISSING_TESTCASE_COVERAGE",
                            "message": (
                                "{} is CHECKABLE upstream; this mapped "
                                "testcase must be CHECKABLE with a complete "
                                "stimulus and oracle".format(item["ac_id"])),
                            "path": (
                                "ac_testcase_candidate.logical_testcases[{}]"
                                .format(index)),
                        })
    if missing_checkable_coverage:
        raise _failure_with_context(
            error, "MISSING_TESTCASE_COVERAGE",
            "checkable AC lacks a checkable logical testcase",
            {"correction_diagnostics": missing_checkable_coverage})
    complete = value["completeness"]
    if complete["ac_ids"] != sorted(ac_by_id) or \
            complete["testcase_ids"] != sorted(testcase_ids) or \
            not set(complete["omitted_ac_ids"]).issubset(ac_by_id):
        raise error("INCOMPLETE_MAPPING",
                    "AC/testcase completeness declaration is inconsistent")
    return value

def enrich_stage2(
        raw: dict[str, Any], value: dict[str, Any],
        map1: dict[str, Any], sources: dict[str, str], spec_fp: str,
        revision: int, response: dict[str, Any], *, max_items: int,
        max_per_shard: int, max_file_bytes: int,
        policy_fingerprint: str, error: Callable[..., Exception]
        ) -> tuple[dict[str, Any], list[dict[str, Any]],
                   list[dict[str, Any]]]:
    if set(raw) != {"logical_testcases", "completeness"}:
        raise error("INVALID_MAPPING",
                         "AC/testcase candidate shape is invalid")
    if len(raw["logical_testcases"]) > max_items:
        raise error("ITEM_LIMIT_EXCEEDED",
                         "AC/testcase count budget exceeded")
    validate_semantic_completeness(
        raw["completeness"], "omissions", error)
    omission_ids = [
        item["ac_id"] for item in raw["completeness"]["omissions"]]
    if len(omission_ids) != len(set(omission_ids)):
        raise error("DUPLICATE_LOCAL_REFERENCE",
                         "each omitted AC may be reported only once")
    testcases = []
    expected_keys = {
        "objective", "scenario_ids", "ac_ids",
        "preconditions", "stimulus", "transaction_sequence",
        "timing_intent", "checker", "expected_result",
        "failure_condition", "timeout_cycles", "status", "reason",
        "spec_evidence"}
    testcase_order = range(len(raw["logical_testcases"]))
    for rank, index in enumerate(testcase_order):
        source = raw["logical_testcases"][index]
        if not isinstance(source, dict) or set(source) != expected_keys:
            raise error("INVALID_MAPPING",
                             "logical testcase shape is invalid")
        if source["status"] not in {"CHECKABLE", "PLANNED"} and \
                not source["reason"].strip():
            raise error(
                "INVALID_MAPPING",
                "non-checkable testcase requires a semantic reason")
        item = {
            "testcase_id": "TC.{:04d}".format(rank + 1),
            **{key: copy.deepcopy(source[key]) for key in (
                "objective", "preconditions", "stimulus",
                "transaction_sequence", "timing_intent", "checker",
                "expected_result", "failure_condition", "timeout_cycles",
                "status")},
            "scenario_ids": sorted(source["scenario_ids"]),
            "ac_ids": sorted(source["ac_ids"]),
            "spec_evidence": _enrich_evidence(
                source["spec_evidence"], sources, error),
        }
        item["testcase_fingerprint"] = "0" * 64
        item["testcase_fingerprint"] = artifact_fingerprint(
            item, "testcase_fingerprint")
        testcases.append(item)
    testcases.sort(key=lambda item: item["testcase_id"])
    known_ac_ids = sorted(
        item["ac_id"] for item in map1["acceptance_criteria"])
    if not set(omission_ids).issubset(known_ac_ids):
        raise error("UNKNOWN_AC_REFERENCE",
                         "omission references an unknown AC")
    coverage = []
    for ac_id in known_ac_ids:
        item = {
            "ac_id": ac_id,
            "testcase_ids": sorted(
                testcase["testcase_id"] for testcase in testcases
                if ac_id in testcase["ac_ids"]),
        }
        item["coverage_fingerprint"] = "0" * 64
        item["coverage_fingerprint"] = artifact_fingerprint(
            item, "coverage_fingerprint")
        coverage.append(item)
    shards: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    inline = testcases
    storage = "INLINE"
    if len(testcases) > max_per_shard:
        storage = "SHARDED"
        inline = []
        for index, offset in enumerate(
                range(0, len(testcases), max_per_shard), start=1):
            chunk = testcases[offset:offset + max_per_shard]
            shard = {
                "schema_version": "1.0",
                "artifact_kind": "AC_TESTCASE_MAP_SHARD",
                "shard_id": "ACTESTCASESHARD.R{:03d}.S{:03d}".format(
                    revision, index),
                "job_id": value["job_id"],
                "revision": revision,
                "logical_testcases": chunk,
                "content_fingerprint": "0" * 64,
            }
            shard["content_fingerprint"] = artifact_fingerprint(
                shard, "content_fingerprint")
            path = (
                "staging/mappings/shards/"
                "ac_testcases.r{:03d}.s{:03d}.json".format(
                    revision, index))
            encoded = json.dumps(
                shard, sort_keys=True, ensure_ascii=False).encode("utf-8")
            if len(encoded) > max_file_bytes:
                raise error("FILE_LIMIT_EXCEEDED",
                                 "mapping shard exceeds file budget")
            shards.append(shard)
            refs.append({
                "shard_id": shard["shard_id"],
                "path": path,
                "testcase_ids": [
                    item["testcase_id"] for item in chunk],
                "content_fingerprint": shard["content_fingerprint"],
            })
    identity = canonical_hash({
        "job": value["job_id"], "revision": revision,
        "map1": map1["artifact_fingerprint"], "testcases": testcases})
    result = {
        "schema_version": "1.0",
        "artifact_kind": STAGE2,
        "map_id": "ACTESTCASEMAP.{}".format(identity[:16].upper()),
        "job_id": value["job_id"],
        "revision": revision,
        "state": "STAGING",
        "input_fingerprint": value["input_fingerprint"],
        "spec_fingerprint": spec_fp,
        "scenario_ac_map_fingerprint":
            map1["artifact_fingerprint"],
        "upstream_fingerprints": {
            "input": value["input_fingerprint"], "spec": spec_fp,
            "scenario_ac_map": map1["artifact_fingerprint"]},
        "policy_fingerprint": policy_fingerprint,
        "storage": storage,
        "logical_testcases": inline,
        "shards": refs,
        "ac_coverage": coverage,
        "completeness": {
            "declared_complete": True,
            "ac_ids": known_ac_ids,
            "testcase_ids": [
                item["testcase_id"] for item in testcases],
            "omitted_ac_ids": sorted(omission_ids),
        },
        "provider": _provider_identity(response),
        "artifact_fingerprint": "0" * 64,
    }
    result["artifact_fingerprint"] = artifact_fingerprint(
        result, "artifact_fingerprint")
    validate_ac_testcase_map(
        result, map1, value, sources, spec_fp,
        policy_fingerprint, testcases, error)
    return result, testcases, shards
