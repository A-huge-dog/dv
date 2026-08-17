# PJ-002.9-HF1 fingerprint and direct-dependency inventory

This inventory is normative for the active PJ-002.9-HF1 runtime. A fingerprint has
one job only; aggregate integrity and local reuse are deliberately separate.

## Fingerprint meanings

| Fingerprint | Exact projection | Producer | Consumers | Stale propagation |
|---|---|---|---|---|
| `content_fingerprint` | Only `semantic_body` of one unit, canonical JSON | Framework after validating provider semantics | impact evaluator; direct-lineage fingerprint | Dirties this unit when its local meaning/code changes. Provider call IDs, revision, paths, global line numbers, and aggregate roots are excluded. |
| `dependency_fingerprint` | Canonically sorted `dependency_fingerprints[]`, each exactly `{kind, identity, fingerprint}` | Framework | impact evaluator; downstream direct-lineage fingerprint | Dirties this unit when an authority or direct upstream changes. An aggregate Stage root is never substituted for relevant child dependencies. |
| `artifact_fingerprint` | Complete unit record except `artifact_fingerprint` | Framework | child index; replay/tamper validator | Protects Job/revision, evidence, actual producer call record, local evidence and the two local fingerprints. It is an integrity fingerprint, not a local reuse key. |
| `root_fingerprint` | Complete canonical index except `root_fingerprint`, including every child identity/path/content/dependency/artifact fingerprint and completeness metadata | Framework | checkpoint, Human request, replay and later binding boundary | Any child, child path, completeness declaration, producer record or assembly projection changes the root. It does not make unrelated siblings dirty. |
| `assembly_fingerprint` | Complete assembly record except `assembly_fingerprint` | Framework | Human bundle and replay validator | Changes when sequence, selected unit segment, top, complete bytes or unit-index root changes. |
| `impact_fingerprint` | Complete dirty/reused/removed manifest except `impact_fingerprint` | Framework | audit/replay only | Protects one comparison result. It never triggers a provider call or workflow transition. |

The stable direct-lineage value consumed by another unit is exactly:

```text
hash(unit_id, unit_kind, content_fingerprint, dependency_fingerprint)
```

This lets an append-only new revision reuse an unchanged semantic unit while
the complete artifact still records its new revision/path/provider call.

## Direct dependency projection by unit

Every unit directly binds `JOB_INPUT`, `SPEC_BASELINE`, `POLICY`,
`OWNER_SCOPE`, and stable provider/model identity. Exact cited Spec ranges are
additional `SPEC_EVIDENCE` dependencies.

| Unit kind | Semantic body | Additional direct dependencies | Consumer / stale closure |
|---|---|---|---|
| `SCENARIO` | objective, verification level, status, reason | exact Scenario Spec evidence | referenced AC units |
| `ACCEPTANCE_CRITERION` | behavior, verification level, status, reason | referenced Scenario units and exact AC Spec evidence | logical testcase, coverage, code and review units |
| `LOGICAL_TESTCASE` | objective, preconditions, stimulus, sequence, timing, checker/oracle, expected result, failure, timeout, status/reason and selected Scenario/AC identities | only selected Scenario and AC units plus exact testcase Spec evidence | derived coverage, mapped code and local review units |
| `AC_COVERAGE` | exact `{ac_id, testcase_ids}` relation | one AC unit and exactly the testcase units whose `ac_ids` contain that AC | mapped code and AC review certificate |
| `CODE_SHARED` | ordered shared code segments only | common authority/provider dependencies | Stage 3 assembly and the conservatively dependent code/review closure |
| `CODE_TESTCASE` | ordered testcase-local code segments only | exact mapped testcase, AC and coverage units plus every SHARED unit present in the candidate | AC/testcase review certificates |
| `REVIEW_AC` | local status, omission and issues for one AC | exact Scenario, AC, coverage, relevant testcase/code/shared units and cited Spec evidence | testcase certificates and aggregate Reviewer root |
| `REVIEW_TESTCASE` | local AC statuses for one testcase | exact testcase, relevant code and AC certificate units | aggregate Reviewer root |
| `REVIEW_SHARED` | global/shared findings for one shared code unit | exact shared code unit and global issue Spec evidence | aggregate Reviewer root |

`AC_COVERAGE` is always emitted by the Framework from the validated current
logical-testcase `ac_ids`; it is not accepted from a provider candidate.

## Stage 3 assembly and evidence

The provider emits one or more generic code-unit candidates, one exact complete
assembly order, and unit-local complete-line selections. At least one mapped
`TESTCASE` unit is required; `SHARED` units are optional. The Framework
validates and formalizes them as `CODE_SHARED` and `CODE_TESTCASE` units. Each
code unit stores unit-local segments and local selection evidence. The assembly
sequence refers to each unit segment exactly once and must reproduce the
complete candidate bytes and content fingerprint exactly.

Global ranges are evidence views derived from the complete assembly. Moving a
unit globally changes assembly/root evidence but does not change that unit's
semantic content fingerprint. Missing, duplicate, partial, comment-only,
non-executable, zero-match or multiple-match selections remain fail closed.
Evidence may resolve to either an existing SHARED unit or an in-scope TESTCASE
unit. Reusing one valid physical selection is allowed; identical records within
one evidence array are canonicalized before persistence.

## Storage and validation

```text
staging/units/<stage>/<provider|current>.rNNN/<readable-unit-id>.json
staging/units/<stage>/index.<provider|current>.rNNN.json
staging/units/stage3/assembly.rNNN.json
staging/units/impact/impact.rNNN.json
```

Fingerprints never appear in filenames. Validators reject absolute paths,
parent/hidden/symlink escape, duplicate child IDs/paths, missing or non-regular
children, non-canonical order, cross-Job identity, child/index substitution,
stale dependency references, conflicting replay and aggregate-root mismatch.

## Impact classification

Impact comparison first requires exact Job, immutable input, Spec, policy and
Owner scope equality. A unit is:

- `reused` only when identity, semantic content, direct dependencies and stable
  provider/model identity are unchanged;
- `dirty` when added, content-changed, direct-dependency-changed, or reached by
  declared transitive dependency closure;
- `removed` when its old identity is absent from the new complete index set.

The evaluator persists evidence only. It does not call a Generator/Reviewer,
create routing/dispatch, change Stage state, request Human approval, promote an
artifact, bind RTL, or invoke EDA.
