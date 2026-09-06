"""Pure Stage 1 Scenario/AC-map generation and validation rules."""
from __future__ import annotations

import copy
from typing import Any, Callable

from contracts.validator import accepted, validate
from domain._mapping import validate_semantic_completeness
from domain.artifacts import artifact_fingerprint
from domain.evidence import (
    _enrich_evidence, _provider_identity, _validate_enriched_evidence,
)
from scripts.dvlib import canonical_hash


STAGE1 = "SCENARIO_AC_MAP"


def _assigned_ids(
        prefix: str, count: int, slots: list[str] | None,
        error: Callable[..., Exception]) -> dict[int, str]:
    if slots is None:
        return {
            index: "{}.{:04d}".format(prefix, index + 1)
            for index in range(count)}
    if (len(slots) != count or len(set(slots)) != len(slots) or
            any(not isinstance(item, str) or
                not item.startswith(prefix + ".") for item in slots)):
        raise error(
            "MAPPING_SCOPE_VIOLATION",
            "replacement candidate count must match its authorized {} IDs"
            .format(prefix))
    return {index: item for index, item in enumerate(slots)}


def validate_scenario_ac_map(
        value: dict[str, Any], project_input: dict[str, Any],
        spec_sources: dict[str, str], spec_fingerprint: str,
        policy_fingerprint: str, error: Callable[..., Exception]
        ) -> dict[str, Any]:
    if not accepted(validate("scenario_ac_map", value)):
        raise error("INVALID_SCHEMA", "Scenario/AC map contract is invalid")
    if value["artifact_fingerprint"] != artifact_fingerprint(
            value, "artifact_fingerprint"):
        raise error("STALE_EVIDENCE", "Scenario/AC map fingerprint is stale")
    if (
        value["job_id"] != project_input["job_id"] or
        value["input_fingerprint"] != project_input["input_fingerprint"] or
        value["spec_fingerprint"] != spec_fingerprint or
        value["upstream_fingerprints"] != {
            "input": project_input["input_fingerprint"],
            "spec": spec_fingerprint,
        } or value["policy_fingerprint"] != policy_fingerprint
    ):
        raise error("STALE_EVIDENCE",
                    "Scenario/AC map upstream lineage is stale")
    scenarios = value["scenarios"]
    acs = value["acceptance_criteria"]
    scenario_ids = [item["scenario_id"] for item in scenarios]
    ac_ids = [item["ac_id"] for item in acs]
    if len(scenario_ids) != len(set(scenario_ids)) or \
            len(ac_ids) != len(set(ac_ids)):
        raise error("DUPLICATE_MAPPING_ID",
                    "scenario and AC IDs must be unique")
    known_scenarios = set(scenario_ids)
    for item in [*scenarios, *acs]:
        expected = artifact_fingerprint(item, "item_fingerprint")
        if item["item_fingerprint"] != expected:
            raise error("STALE_EVIDENCE", "mapping item fingerprint is stale")
        _validate_enriched_evidence(
            item["spec_evidence"], spec_sources, error)
        if item["status"] != "CHECKABLE" and \
                not item["reason"].strip():
            raise error("INVALID_MAPPING",
                        "non-checkable item requires a semantic reason")
    if any(not set(item["scenario_ids"]).issubset(known_scenarios)
           for item in acs):
        raise error("ORPHAN_MAPPING", "AC references an unknown scenario")
    complete = value["completeness"]
    if (
        complete["scenario_ids"] != sorted(scenario_ids) or
        complete["ac_ids"] != sorted(ac_ids) or
        complete["behavior_count"] != len(acs)
    ):
        raise error("INCOMPLETE_MAPPING",
                    "Scenario/AC completeness declaration is inconsistent")
    return value

def enrich_stage1(
        raw: dict[str, Any], value: dict[str, Any],
        sources: dict[str, str], spec_fp: str, revision: int,
        response: dict[str, Any], *, max_items: int,
        policy_fingerprint: str, error: Callable[..., Exception],
        scenario_id_slots: list[str] | None = None,
        ac_id_slots: list[str] | None = None,
        ) -> dict[str, Any]:
    if set(raw) != {
            "scenarios", "acceptance_criteria", "completeness"}:
        raise error("INVALID_MAPPING",
                         "Scenario/AC candidate shape is invalid")
    if len(raw["scenarios"]) > max_items or \
            len(raw["acceptance_criteria"]) > max_items:
        raise error("ITEM_LIMIT_EXCEEDED",
                         "Scenario/AC count budget exceeded")
    validate_semantic_completeness(
        raw["completeness"], "omitted_behaviors", error)
    if (scenario_id_slots is None) != (ac_id_slots is None):
        raise error(
            "MAPPING_SCOPE_VIOLATION",
            "replacement Scenario and AC ID slots must be supplied together")
    scenario_order = range(len(raw["scenarios"]))
    scenario_ids = _assigned_ids(
        "SCENARIO", len(raw["scenarios"]), scenario_id_slots, error)
    scenarios = []
    for index in scenario_order:
        source = raw["scenarios"][index]
        if not isinstance(source, dict) or set(source) != {
                "objective", "verification_level",
                "status", "reason", "spec_evidence"}:
            raise error("INVALID_MAPPING",
                             "scenario candidate shape is invalid")
        item = {
            "scenario_id": scenario_ids[index],
            **{key: copy.deepcopy(source[key]) for key in (
                "objective", "verification_level", "status", "reason")},
            "spec_evidence": _enrich_evidence(
                source["spec_evidence"], sources, error),
        }
        item["item_fingerprint"] = "0" * 64
        item["item_fingerprint"] = artifact_fingerprint(
            item, "item_fingerprint")
        scenarios.append(item)
    scenarios.sort(key=lambda item: item["scenario_id"])
    acs = []
    ac_order = range(len(raw["acceptance_criteria"]))
    ac_ids = _assigned_ids(
        "AC", len(raw["acceptance_criteria"]), ac_id_slots, error)
    for rank, index in enumerate(ac_order):
        source = raw["acceptance_criteria"][index]
        if not isinstance(source, dict) or set(source) != {
                "scenario_indexes", "behavior",
                "verification_level", "status", "reason",
                "spec_evidence"}:
            raise error("INVALID_MAPPING",
                             "AC candidate shape is invalid")
        if any(slot >= len(scenario_ids)
               for slot in source["scenario_indexes"]):
            raise error("UNKNOWN_FRAMEWORK_SLOT",
                             "AC references an unknown Scenario slot")
        item = {
            "ac_id": ac_ids[rank],
            "scenario_ids": sorted(
                scenario_ids[slot]
                for slot in source["scenario_indexes"]),
            **{key: copy.deepcopy(source[key]) for key in (
                "behavior", "verification_level", "status", "reason")},
            "spec_evidence": _enrich_evidence(
                source["spec_evidence"], sources, error),
        }
        item["item_fingerprint"] = "0" * 64
        item["item_fingerprint"] = artifact_fingerprint(
            item, "item_fingerprint")
        acs.append(item)
    acs.sort(key=lambda item: item["ac_id"])
    identity = canonical_hash({
        "job": value["job_id"], "revision": revision,
        "scenarios": scenarios, "acs": acs})
    result = {
        "schema_version": "1.0",
        "artifact_kind": STAGE1,
        "map_id": "SCENARIOACMAP.{}".format(identity[:16].upper()),
        "job_id": value["job_id"],
        "revision": revision,
        "state": "STAGING",
        "input_fingerprint": value["input_fingerprint"],
        "spec_fingerprint": spec_fp,
        "upstream_fingerprints": {
            "input": value["input_fingerprint"], "spec": spec_fp},
        "policy_fingerprint": policy_fingerprint,
        "scenarios": scenarios,
        "acceptance_criteria": acs,
        "completeness": {
            "declared_complete": True,
            "behavior_count": len(acs),
            "scenario_ids": [item["scenario_id"] for item in scenarios],
            "ac_ids": [item["ac_id"] for item in acs],
            "omitted_behaviors": sorted(
                copy.deepcopy(raw["completeness"]["omitted_behaviors"])),
        },
        "provider": _provider_identity(response),
        "artifact_fingerprint": "0" * 64,
    }
    result["artifact_fingerprint"] = artifact_fingerprint(
        result, "artifact_fingerprint")
    return validate_scenario_ac_map(
        result, value, sources, spec_fp,
        policy_fingerprint, error)
