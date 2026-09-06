# Open Issues and Assumptions

No blocking Phase-1 specification ambiguity remains in baseline v1.0. The following items are intentionally deferred and do not block standalone Q3:

| ID | Class | Item | Required owner/action before Q4 |
|---|---|---|---|
| OPEN_001 | ASSUMPTION | APB is synchronous to the RSCU AON clock; the legacy asynchronous SCF bridge is absent. | SoC integration must select and verify AXI/AHB/APB and CDC bridge. |
| OPEN_002 | ASSUMPTION | All external ack/status signals are pre-synchronized. | CDC/RDC owner must add and sign off synchronizers/handshakes. |
| OPEN_003 | ASSUMPTION | Four tiles share one COMPUTE domain. | Profile owner must define per-tile domains if required. |
| OPEN_004 | UNSUPPORTED | No UPF, isolation/retention cell instantiation, real ICG, PLL driver, regulator bus, or analog control. | Physical/power architects must provide technology and protocol contracts. |
| OPEN_005 | UNSUPPORTED | AXI burst bridge, AHB, APB chaining, and legacy 16-KiB bridge profiles are not implemented. | Bridge product owner must define a later profile. |
| OPEN_006 | NON_GOAL | No timing target or signoff constraints are authoritative. | Integration owner must provide clocks and I/O constraints. |
| OPEN_007 | NON_GOAL | `ai_soc_top` stubs are demonstration endpoints, not functional compute/fabric/memory IP. | Replace with real subsystem RTL for Q4. |
| OPEN_008 | NON_GOAL | Simulation functional coverage is modeled but IMC coverage is not collected by the provided DV skill. | Run an authorized coverage flow separately if closure is required. |
