# Enhanced RSCU Phase-1 DV Quality Report

## Decision

The project-defined standalone IP-level Q3 directed acceptance test passes.
All planned requirement entries in `../plan/verification_plan.json` are marked
`verified`, and that document validates against the soc-dv verification-plan
schema.

## Evidence

| Check | Result |
| --- | --- |
| Frozen input integrity | PASS: all 30 Spec/RTL SHA-256 entries match |
| DV compile | PASS, exit 0, completion marker found |
| Q3 directed test | PASS, seed 105, exact marker `DV_TESTCASE_PASS:RSCU_Q3` |
| UVM result | 0 warnings, 0 errors, 0 fatals |
| Key bound SVA | No assertion failure in the passing run |
| Blocking RTL bugs | None observed/open |
| Blocking spec ambiguities | None |

The complete directed Q3 sequence checks reset, CSR reset/RW/RO/WO/W1C and
side effects, invalid/misaligned access, interrupt mask/pending/clear and set
priority, power on/off/retention/no-op/illegal/busy/dependency/deny/timeout and
ERROR recovery, DVFS same-OPP/upscale/downscale/arbitration/illegal/unsafe/
timeout/busy behavior, and external/CSR override behavior. Mock agents model
the Phase-1 external handshakes.

Canonical records:

- `../../../dv/results/compile-final/run-result.json`
- `../../../dv/results/q3-final3/run-result.json`
- `../../../dv/results/q3-final3/runs/q3-final3.q3/run/xrun.log`

## Scope qualification

The passing evidence is one comprehensive directed Q3 run, not a multi-seed
random regression. Functional confidence is therefore reported as `SMOKE` in
the combined EDA report. A coverage model exists, but IMC coverage was not
collected and no coverage-closure percentage is claimed. Q4 subsystem
integration, CDC/RDC, UPF, GLS, STA, and tape-out signoff are not Phase-1 exit
criteria.
