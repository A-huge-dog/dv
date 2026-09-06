# Enhanced RSCU Phase-1 Verification Plan

## Scope and evidence boundary

The read-only inputs are `inputs/spec` and `inputs/rtl`; their SHA-256 ledger is
`plan/source_fingerprints.sha256`. The DUT is `enhanced_rscu_top`, and all
external subsystem responses are supplied by deterministic mock agents. The
plan verifies the standalone Q3 contract. `ai_soc_top` prototype elaboration is
recorded by the Design EDA flow and is not treated as Q4 subsystem verification.

The executable traceability contract is `verification_plan.json`, validated
against the SoC-DV skill schema. This Markdown is the review-oriented view.

## Verification environment

- APB active agent: sequence, driver, monitor, and X/Z response scoreboard.
- Independent expected values: constants, reset values, sequences, and error
  encodings come from the frozen Spec, not from DUT outputs.
- Mock agents: quiescence, power, clock, reset, isolation, retention,
  voltage-stable, and PLL-lock responders with per-interface hold controls.
- RAL: complete Phase-1 register-address model.
- SVA: APB completion, reset safety, DVFS payload stability, deny containment,
  override controls, known state, and IRQ set-priority over W1C.
- Coverage model: CSR address/read-write/error, state transitions, OPP values,
  IRQ, handshake activity, and override. It is a planned/collected simulation
  model; no IMC coverage-closure claim is made.

## Directed scenarios

| Scenario | Main requirements | Independent checks | Test |
| --- | --- | --- | --- |
| Reset, ID, reset values, RW/RO, invalid/aligned APB | REQ_SYS_003/005, REQ_BRG_001, REQ_CSR_001/003, REQ_GPR_001 | APB readback, PSLVERR, reset-safe SVA | `reset_csr`, `q3` |
| IRQ mask/raw/status/W1C and error W1C | REQ_IRQ_001/002, REQ_ERR_001/002 | CSR readback, `irq`, set-priority bind SVA | `q3` |
| Power on/off/retention/no-op | REQ_PWR_001..005, REQ_CLK/RST/ISO/RET_001 | State and output ordering, transition coverage | `power`, `q3` |
| Dependency and quiesce deny | REQ_DEP_001, REQ_QHS_001 | State preservation, error/IRQ, deny SVA | `power`, `q3` |
| Power/clock/reset/isolation/retention/quiesce timeout | REQ_PHS_001, REQ_TMO_001, REQ_SAFE_001 | Exact short timeout, error code, ERROR state, containment | `errors`, `q3` |
| ERROR-to-OFF recovery and reset during wait | REQ_PWR_005, REQ_SAFE_001 | Recovery state and reset-safe outputs | `errors`, `q3` |
| DVFS same-OPP/upscale/downscale | REQ_DVFS_002/003/006 | Request order, code, stable-payload SVA, committed OPP | `dvfs`, `q3` |
| DVFS priority, illegal/unsafe request | REQ_DVFS_004/005 | Selected OPP and sticky error/IRQ | `dvfs`, `q3` |
| Voltage/PLL timeout | REQ_DVFS_005 | OPP preservation, error code, timeout IRQ | `dvfs`, `q3` |
| OPP idle write, busy write, command busy | REQ_DVFS_001, REQ_CSR_002 | Readback, PSLVERR, ERR_BUSY, IRQ | `q3` |
| External and CSR test override | REQ_DFT_001 | Forced controls, preserved functional state, override SVA | `q3` |
| Complete standalone integration | REQ_Q2_001, REQ_Q3_001 | Exact marker, zero UVM error/fatal, no SVA failure | `q3` |

## Pass criteria

Compile PASS requires qualified Xcelium 26.03, exit code zero, and a generated
simulation snapshot. Test PASS additionally requires the exact configured
marker, no timeout, no recognized failure signature, and zero UVM errors and
fatals. Source hashes are rechecked after execution. The single Q3 directed
test is the default bounded execution; the multi-test regression requires an
explicit `--regression` CI invocation.

## Quality claims excluded

No measured coverage closure, formal proof, CDC/RDC, UPF, gate-level, timing
closure, DFT, physical-design, Q4 integration, or tape-out claim is made.
