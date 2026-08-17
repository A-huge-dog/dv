# PJ-002 candidate field ownership

This inventory is normative for configured provider tool input. Candidate
schemas contain only the fields in the Model-owned column. OCHES001 persists
the aggregate map/testcase envelopes together with `PROJECT_ARTIFACT_UNIT
1.0`, `PROJECT_ARTIFACT_INDEX 1.0`, `PROJECT_CODE_ASSEMBLY 1.0`, and local
Reviewer certificate units. Staged Reviewer request/report/validation is `6.0`.
Every final report, including one with findings, enters `AWAITING_HUMAN_REVIEW`;
this contract does not express EDA eligibility.

| Stage | Model-owned candidate fields | Framework-derived formal fields |
|---|---|---|
| Stage 1 Scenario | `objective`, `verification_level`, `status`, `reason`, exact Spec `path/line_start/line_end` | candidate/formal version and kind, position-derived `scenario_id`, canonical order, exact snippet and snippet fingerprint, item/artifact fingerprints, revision, provider, path, Job/policy/upstream lineage |
| Stage 1 AC | selected Framework Scenario positions, `behavior`, `verification_level`, `status`, `reason`, exact Spec ranges | position-derived `ac_id`, mapped/sorted `scenario_ids`, reverse indexes, exact snippets/fingerprints, item fingerprint |
| Stage 1 completeness | semantic `declared_complete`, explicit `omitted_behaviors` | `behavior_count`, sorted Scenario/AC ID lists and consistency bookkeeping |
| Stage 2 testcase | selected existing Scenario/AC IDs, objective, preconditions, stimulus, transaction sequence, timing intent, checker/oracle, expected result, failure condition, timeout, semantic status/reason, exact Spec ranges | position-derived `testcase_id`, canonical ID ordering, exact snippets/fingerprints, testcase fingerprint, storage/shards and lineage |
| Stage 2 completeness | semantic `declared_complete`, explicit per-AC omission/reason | reverse AC coverage, coverage fingerprints, sorted AC/testcase/omitted ID lists |
| Stage 3 | one or more generic code units, mapped testcase IDs, exact zero-based unit assembly order, selected existing testcase/AC IDs, and unit-local complete-line content; a simple candidate may contain one `TESTCASE` unit, while `SHARED` units are optional | formal code-unit IDs/content fingerprints, unique complete SystemVerilog assembly, canonicalized evidence records, global ranges/snippets/fingerprints, validation, provider, content/artifact/upstream fingerprints |
| Reviewer finding | `ERROR`/`WARNING`, suspected origin, affected Scenario/AC/testcase/code-unit IDs, one combined `problem_and_required_change`, exact Spec ranges and testcase complete-line content | report-local `issue_id`, canonical ordering, exact testcase ranges/snippets/fingerprints, current roots, review round, previous-report lineage, identity and report fingerprint; Reviewer suspicion is not a dispatch decision |
| Reviewer per-AC result | existing executable `ac_id`, coverage/omission status and reason, exact Spec ranges and stimulus/checker complete-line content | copied Scenario/testcase identities, mapping/coverage fingerprints, exact testcase ranges/snippets/fingerprints, review fingerprint, exact Owner routing/Spec-issue partition and executable/full-Spec scope lineage |
| Owner direct-review form | exact Human identity/role, destination and comment entered in the prefilled form | all Scenario/AC IDs/status/reason/evidence review context, immutable submitted-form snapshot, three-way partition, partition/revision fingerprints and paths |

Provider candidate fields never include `ac_coverage`, unit fingerprints,
dependency fingerprints, aggregate roots, global code ranges, review
certificate identity, impact classification, routing, approval or execution
authority. Framework derives those fields after deterministic validation.

The Stage 3 provider may emit only generic `SHARED` and `TESTCASE` code roles.
At least one mapped `TESTCASE` unit is required; `SHARED` is optional. Framework
validates every unit/ref and concatenates the declared complete order into the
unique formal candidate. Exact evidence may reference either role. A
`TESTCASE` reference remains limited to its mapped testcase IDs, while a
`SHARED` reference must resolve to an existing exact unit. No bus, memory, IP,
DUT or SoC kind is a code-unit role.

Models never create candidate or formal identities. Runtime assigns new IDs
from validated candidate slots; relationship fields can only select a supplied
existing ID or a bounded slot in the same candidate. Unknown or duplicate
slots fail closed. Testcase content selections must match
one exact complete-line sequence; zero or multiple matches fail closed.
Reusing one valid physical selection within an AC, across stimulus/checker
kinds, or across ACs is not a semantic failure; identical records within one
evidence array are canonicalized. Array order has no authority: runtime sorts
formal items, references, derived evidence ranges, coverage, shards, and
review items before hashing. Router separately verifies that affected and planned
targets exist in the current Job and cannot enlarge the Owner-routed scope.

Semantic completeness is valid only as either `true` with no omissions or
`false` with one or more explicit omissions. Runtime never changes a false
semantic declaration into full coverage and never creates a missing behavior,
testcase, stimulus, checker, oracle, finding, verdict, waiver, or Human route.
