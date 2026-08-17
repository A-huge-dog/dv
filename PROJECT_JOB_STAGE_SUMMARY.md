# Project Job staged workflow summary

Updated: 2026-08-17

当前 active contract 是 workflow `6.0` / policy `OCHES001`。本文件描述从 immutable Project input
到 Human-review 路径，或从 OCHES002 validated replacement 经 OCHES003 compile/commit/impact/final review 的
当前职责。它不授权 production provider、真实 Human decision、promotion 或 DUT functional qualification。

## End-to-end flow

```text
immutable submission + Spec/RTL baseline
  -> Stage 1 Spec-only Scenario/AC generation and validation
  -> Human DV Owner Scenario routing
  -> Scenario/AC semantic units + current aggregate index/root
  -> Stage 2 Spec-only logical testcase generation and validation
  -> Framework-derived per-AC coverage units + aggregate index/root
  -> Stage 3 Spec-only code candidate generation
  -> Framework candidate-contract validation
  -> Verilator syntax/elaboration/executable build
  -> generic shared/testcase code units + deterministic complete assembly/root
  -> one independent Reviewer call
  -> per-AC/per-testcase/per-shared local certificates + aggregate report/root
  -> no repairable ERROR: AWAITING_HUMAN_REVIEW
  -> repairable ERROR: global FIFO -> Orchestrator 9-tool session
  -> Framework formalizes semantic repair-plan candidate and computes fingerprint
  -> Framework derives ordered canonical repair groups
  -> each group: just-in-time dispatch from exact current roots
  -> one dispatched Stage 1/2/3 6-tool session + scoped replacement validation
  -> exact assembled-candidate Verilator build-only validation
  -> group PASS: numbered atomic commit; group FAIL: NOT_COMMITTED
  -> roots/dependency/impact recomputation before the next group
  -> one final Spec-only Reviewer call
  -> Human review request/checkpoint
  -> AWAITING_HUMAN_REVIEW
```

Spec remains the only Scenario, AC, stimulus, checker/oracle and expected-behavior authority. RTL is snapshotted
at bootstrap, but Stage 1/2/3 and Reviewer requests may not contain RTL bytes, path, fingerprint, RTLIR or
RTL-derived interface evidence.

## Fingerprint boundaries

Each semantic unit separates:

- `content_fingerprint`: only the unit's semantic body;
- `dependency_fingerprint`: only canonical exact direct dependencies;
- `artifact_fingerprint`: the complete identity/version/lineage/evidence record;
- index `root_fingerprint`: the complete current child set, paths and completeness metadata.

Downstream local units consume direct child lineage, never an entire Stage root as their only reuse key. Aggregate
roots remain the integrity, checkpoint, Human approval and later RTL/EDA binding boundary. Exact projections,
producer/consumer relationships and stale propagation are normative in
`contracts/project/incremental_fingerprint_inventory.md`.

## Unit contracts and storage

Active incremental contracts:

- `PROJECT_ARTIFACT_UNIT 1.0`;
- `PROJECT_ARTIFACT_INDEX 1.0`;
- `PROJECT_CODE_ASSEMBLY 1.0`;
- `PROJECT_INCREMENTAL_IMPACT 1.0`;
- staged Reviewer request `7.0`，report/validation `6.0`;
- model-semantic repair plan candidate `1.0`；
- Framework-formalized repair plan/Router receipt/formal dispatch/failure feedback/regeneration state `1.0`；
- committed testcase、precommit compile validation、serial group commit 和 OCHES003 final checkpoint `1.0`；
- 十类 versioned repair records、共同 record envelope、四类 repair indexes 和五角色 system prompt contract。

```text
result/jobs/<job_id>/
├── input_baseline/                 immutable authority
├── staging/
│   ├── mappings/                   aggregate Stage 1/2 artifacts
│   ├── generated/portable_sv/      aggregate Stage 3 candidate
│   ├── reviews/                    aggregate review request/report
│   ├── validations/                deterministic validation
│   ├── orchestrator/               final repair-plan submissions
│   ├── dispatch/                   Router dispatch and exact failure feedback
│   └── units/
│       ├── stage1/{provider,current}.rNNN/*.json + indexes
│       ├── stage2/current.rNNN/*.json + index
│       ├── stage3/current.rNNN/*.json + index + assembly.rNNN.json
│       ├── review/current.rNNN/*.json + index
│       └── impact/impact.rNNN.json
└── audit/                          immutable provider/Human/checkpoint evidence
```

Paths are relative, readable and append-only. Fingerprints are stored inside artifacts/indexes, never in
filenames. Absolute/parent/hidden/symlink escape, duplicate ID/path, missing/non-regular child, cross-Job child,
index substitution and stale root all fail closed.

## Stage responsibilities

### Stage 1

The provider returns semantic Scenario/AC candidate fields and exact Spec line ranges. Framework derives formal
IDs, exact snippets/fingerprints, canonical order and aggregate completeness. It persists one `SCENARIO` unit per
Scenario and one `ACCEPTANCE_CRITERION` unit per AC. AC units depend only on cited Scenario units and exact Spec
evidence, plus the common Job/Spec/policy/Owner/provider authority.

The initial provider index is `PROVIDER`; after exact Human Owner routing, the executable map is persisted as the
`CURRENT` index. Owner-routed Spec issues do not enter executable testcase generation.

### Stage 2

The provider returns logical testcase semantics and selected existing Scenario/AC IDs. It cannot submit
`ac_coverage`. Framework derives one `LOGICAL_TESTCASE` unit per testcase and one `AC_COVERAGE` relation per AC
from the current testcase `ac_ids`. A testcase change dirties only that unit, affected coverage/code/review closure
and aggregate roots; exact unrelated siblings remain reusable.

### Stage 3

The provider returns one or more generic `SHARED`/`TESTCASE` SystemVerilog code units, one exact complete assembly
order and all implemented CHECKABLE testcase IDs. A complete candidate may contain one mapped `TESTCASE` unit;
`SHARED` units are optional. Stage 3 does not return AC-level stimulus/checker evidence, review findings or a
workflow decision. Framework validates schema, lineage, unit roles, complete TESTCASE mapping, exact assembly,
size and forbidden-capability/pass/fail/finish policy before any EDA call. It then runs Verilator on the exact
assembled bytes for syntax, elaboration and executable build. Only a passing candidate is frozen and published.

Framework formalizes the units as `CODE_SHARED`/`CODE_TESTCASE`. Every declared unit appears exactly once;
deterministic assembly creates the only complete candidate bytes and fingerprint. Stage 3 units retain code,
testcase dependencies and assembly lineage only; AC-level code evidence is owned exclusively by the Reviewer
report/certificates.

Stage 3 does not infer bounded-timeout semantics from a source-code regular expression and does not repeat the
Stage 2 `expected_result` gate. Stage 2 validates its complete mapping before the Stage 3 provider call; the
independent Reviewer judges timeout behavior, stimulus/checker call chains and oracle validity.

Unit-local Stage 3 records preserve code-unit identity and the complete unit segment, without Generator-owned
AC selections. Moving a unit globally changes the assembly root but not its semantic content identity. A
shared-unit change propagates through explicit shared dependencies even if an interface appears unchanged.

### Reviewer

The Reviewer receives exact baseline Spec, both current maps, complete testcase, Owner routing and executable
scope. One schema-valid response may contain all per-AC assessments and findings. Framework validates every
executable AC, evidence selection, identity, scope, affected-ID closure and verdict; then emits `REVIEW_AC`, `REVIEW_TESTCASE`
and `REVIEW_SHARED` certificates and a deterministic aggregate root.

Findings use only `ERROR`/`WARNING` and one combined `problem_and_required_change` field. Warnings do not trigger
repair. Repairable initial errors enter the append-only global FIFO; the Orchestrator may use at most three distinct
read tools before its unique plan submission, and Router validates rather than rewrites that plan. The dispatched
Stage Agent may use at most three authorized read tools before its unique replacement submission. Framework validates
the replacement without changing current revisions or roots. OCHES003 deterministically groups repairs by Stage、target
overlap and direct dependency, then runs each group serially from exact current roots. A failed group is recorded as
`NOT_COMMITTED`; a passing group publishes one numbered atomic commit manifest, then recomputes roots、dependency closure
and impact before the next dispatch. Prior successful groups remain committed if a later group fails. After all groups
reach a terminal status, exactly one final complete-bundle review produces a Human review request/checkpoint and the Job
enters `AWAITING_HUMAN_REVIEW`. The final Reviewer receives only a sanitized compile receipt; exact EDA request/evidence
with RTL paths and fingerprints remains Framework-owned audit evidence. Spec-origin errors、Warnings and Owner-routed
Spec issues never enter automatic repair。

## Impact analysis

The deterministic evaluator accepts validated old/new unit-index sets only when Job, input baseline, Spec, policy
and Owner scope are exact. It emits `dirty_units`, `reused_units`, `removed_units`, old/new fingerprints and exact
reasons, and verifies current direct-dependency links before computing transitive closure.

Impact evaluation and persistence are deterministic evidence-only operations. They do not invoke providers or Humans,
approve or promote. OCHES002 sessions are globally serial; OCHES003 binds old/new indexes and commit identity into the
persisted impact manifest after precommit compile PASS.

## Replay and standalone test Jobs

Exact replay validates provider request/response, aggregate artifacts, every unit/index, assembly/certificates and
checkpoint roots before returning persisted state. Missing, partial, tampered, stale, cross-Job or conflicting state
fails closed before a provider call or duplicate Human-gate transition.

Standalone Stage 3 and standalone Reviewer Jobs keep source Jobs read-only. They validate current source
contracts, then build self-contained local unit indexes in the test Job directory and bind those roots in the test
result. Their qualification scope remains `TEST_ONLY_NO_PROMOTION_OR_EDA`.

## Qualification ceiling

Scripted/mock tests prove Framework contract, lineage, assembly, compile gating, serial authority switching, impact,
final review and fail-closed behavior only. They do not prove production Qwen/DeepSeek output, Human approval,
production Verilator execution, simulation, DUT correctness, full UVM, four-state semantics or coverage closure.
PJ-003 remains paused and requires exact Human/EDA authority.
