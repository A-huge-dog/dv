# Phase-1 Acceptance Criteria

## Q0/Q1/Q2/Q3 evidence

| Gate | Acceptance |
|---|---|
| Q0 | All RTL and DV source lists resolve; Xcelium parses and elaborates without errors. |
| Q1 | Reset-safe controls, CSR ID/reset reads, RW/RO/W1C behavior, and one power-on plus one DVFS normal flow pass. |
| Q2 | Power normal/off/retention, dependency, deny, every timeout class, interrupt mask/pending/clear, OPP legality, upscale/downscale, and override are checked by directed simulation and key SVA. |
| Q3 | `enhanced_rscu_top` integrates all required blocks with mock responders; the complete directed UVM test exits with its exact pass marker, zero UVM errors/fatals, and no assertion failure. |

## EDA evidence

- Xcelium compile/elaboration result and raw log.
- Standalone UVM run record, normalized `run-result.json`, and raw Xcelium log.
- Genus mapped netlist, check-design, QoR, area, timing, and power reports when infrastructure is available.
- Quality report separates EDA health, functional confidence, and PPA evidence.
- No timing closure, coverage closure, CDC/RDC, or UPF signoff is claimed.

## Traceability

Every P0/P1 requirement must map to RTL and at least one verification point/test, or have a structured open issue. Source input hashes must match before and after DV execution.
