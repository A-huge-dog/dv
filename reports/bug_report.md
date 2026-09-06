# Enhanced RSCU Phase-1 Bug Report

## Open bugs

No blocking RTL bug is open based on the canonical compile and standalone Q3
directed run.

## Closed verification defect

| ID | Owner | Severity | Description | Resolution | Status |
| --- | --- | --- | --- | --- | --- |
| DV-001 | DV | Medium | RAL map access used the field semantic `W1C` as a bus-map access policy, which is not valid for the UVM register map. | Kept the field W1C semantic while using the legal map access policy; reran compile and Q3 successfully. | CLOSED |

No RTL change was required for DV-001. Failed or interrupted exploratory runs
are retained under `../../../archive/pre_workspace/dv_results` for audit and
are not used as passing evidence.
