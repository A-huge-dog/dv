# Enhanced RSCU Phase-1 Requirements

Status: self-owned baseline v1.0  
Scope: quality-gated standalone RTL prototype plus a non-signoff AI-SoC prototype wrapper  
Authority: `input/对RSCU I功能需求和目标.md`; the legacy RSCU document and AI-SoC background are supporting context only.

## Requirement classification

- `SPEC_FACT`: directly required by the project definition.
- `SPEC_DECISION`: self-owned contract added to make Phase-1 executable and verifiable.
- `ASSUMPTION`: integration choice that must be revisited before Q4.

## System and interface requirements

| ID | Class | Requirement | Priority |
|---|---|---|---|
| REQ_SYS_001 | SPEC_FACT | The IP shall preserve bridge, GPRB, interrupt, clock, reset, power, debug/test override, and firmware-visible control/status functions. | P0 |
| REQ_SYS_002 | SPEC_FACT | The IP shall add self-owned PCK-like power-domain control and DVFS management without claiming PCK600 compatibility. | P0 |
| REQ_SYS_003 | SPEC_DECISION | Phase-1 shall use one synchronous 32-bit APB3 slave interface with 12-bit byte address. A transfer completes in the ACCESS phase with `PREADY=1`; invalid/alignment accesses assert `PSLVERR`. | P0 |
| REQ_SYS_004 | SPEC_DECISION | The reference profile shall contain four domains: COMPUTE(0), FABRIC(1), SRAM(2), and DDR_IF(3). `NUM_DOMAINS` remains a legal RTL parameter from 1 through 8. | P1 |
| REQ_SYS_005 | SPEC_FACT | Control state shall be in an always-on clock/reset domain. Reset is asynchronous active-low assertion with synchronous operation after release. | P0 |
| REQ_SYS_006 | SPEC_DECISION | All external acknowledgements are synchronous to `pclk` in Phase-1. CDC bridges are an integration responsibility before Q4. | P1 |
| REQ_BRG_001 | SPEC_DECISION | The self-owned RSCU bridge is the APB protocol endpoint. AXI/AHB translation and asynchronous SCF crossing are unsupported in Phase-1. | P1 |
| REQ_GPR_001 | SPEC_FACT | Firmware shall have general-purpose control registers and read-only subsystem status registers. The reference profile implements four 32-bit control and four 32-bit status words. | P1 |

## CSR and interrupt requirements

| ID | Class | Requirement | Priority |
|---|---|---|---|
| REQ_CSR_001 | SPEC_DECISION | CSR offsets, field access, reset values, and side effects shall match `csr.yaml`; reserved bits read zero and ignore writes. | P0 |
| REQ_CSR_002 | SPEC_DECISION | Commands are accepted only on a successful APB write of a START bit. Busy conflicts produce an error event and never overwrite an active command. | P0 |
| REQ_CSR_003 | SPEC_DECISION | W1C fields clear only bits written as one; RO fields ignore writes; write-only command fields read zero. | P0 |
| REQ_IRQ_001 | SPEC_FACT | Internal events shall set sticky raw interrupt-pending bits and a single aggregated interrupt output. | P0 |
| REQ_IRQ_002 | SPEC_DECISION | `irq_o = |(IRQ_RAW & IRQ_MASK)`. `IRQ_CLEAR` is W1C, event set has priority over simultaneous clear, and reset clears pending and mask. | P0 |
| REQ_ERR_001 | SPEC_FACT | Timeout, deny, dependency, illegal-transition, and DVFS failures shall be firmware visible and interrupt capable. | P0 |
| REQ_ERR_002 | SPEC_DECISION | `ERROR_STATUS` is sticky W1C. `ERROR_INFO` records the most recent error code, source kind, and source index. | P1 |

## Power-domain requirements

| ID | Class | Requirement | Priority |
|---|---|---|---|
| REQ_PWR_001 | SPEC_DECISION | Each domain has OFF, RETENTION, ON, and ERROR states encoded 0, 1, 2, and 3. Reset state is OFF with clock off, reset asserted, isolation asserted, retention off, and power request off. | P0 |
| REQ_PWR_002 | SPEC_FACT | OFF/RETENTION to ON shall sequence power acknowledgement/good, retention release when needed, isolation removal, clock enable acknowledgement, and reset release acknowledgement. | P0 |
| REQ_PWR_003 | SPEC_FACT | ON to OFF shall sequence quiescence, clock disable, reset assertion, isolation assertion, and power removal. | P0 |
| REQ_PWR_004 | SPEC_FACT | ON to RETENTION shall sequence quiescence, clock disable, reset assertion, isolation assertion, retention enable, and power removal. RETENTION to ON shall restore power before releasing retention. | P0 |
| REQ_PWR_005 | SPEC_DECISION | OFF to RETENTION is illegal. A no-op request to the current stable state completes without changing controls. ERROR accepts only an OFF recovery request. | P1 |
| REQ_QHS_001 | SPEC_FACT | A quiescence request shall remain asserted until client acknowledgement, deny, or timeout. Deny aborts before destructive clock/reset/power changes. | P0 |
| REQ_PHS_001 | SPEC_FACT | Power request shall remain at the requested level until power acknowledgement and power-good both match the requested state, or until timeout. | P0 |
| REQ_DEP_001 | SPEC_FACT | A domain may turn ON only when every configured parent is ON. A domain may leave ON only when no configured child remains ON. A violation rejects the request without changing controls. | P0 |
| REQ_CLK_001 | SPEC_FACT | Clock enable changes shall be acknowledged before the sequence advances. | P0 |
| REQ_RST_001 | SPEC_FACT | Reset assertion/deassertion shall be acknowledged before the sequence advances. | P0 |
| REQ_ISO_001 | SPEC_FACT | Isolation assertion/removal shall be acknowledged before the sequence advances. | P0 |
| REQ_RET_001 | SPEC_FACT | Retention enable/release shall be acknowledged before the sequence advances. | P0 |
| REQ_TMO_001 | SPEC_DECISION | All sequencer timeouts use `pclk` cycles. `TIMEOUT_CFG=0` disables timeout; otherwise failure occurs after that many consecutive waiting cycles. Reset value is 32 cycles. | P0 |
| REQ_SAFE_001 | SPEC_DECISION | A power-sequence timeout enters ERROR and requests containment: clock off, reset asserted, isolation asserted, retention off, and power off. | P0 |

## DVFS requirements

| ID | Class | Requirement | Priority |
|---|---|---|---|
| REQ_DVFS_001 | SPEC_FACT | The manager shall hold four OPP entries, each containing a 16-bit voltage code and 16-bit frequency code; entries are firmware-writable only while DVFS is idle. | P0 |
| REQ_DVFS_002 | SPEC_FACT | An upscale shall complete the target voltage handshake/stability before requesting target frequency and PLL lock. | P0 |
| REQ_DVFS_003 | SPEC_FACT | A downscale shall complete target frequency/PLL lock before requesting the lower voltage and stability. | P0 |
| REQ_DVFS_004 | SPEC_DECISION | Requests are accepted only while COMPUTE is ON, no power transition is active, and target OPP is valid. Thermal request has priority over firmware, which has priority over performance request. | P0 |
| REQ_DVFS_005 | SPEC_FACT | Illegal OPP, unsafe-state request, voltage timeout, and PLL timeout shall not change `current_opp`; each produces status, error, and interrupt evidence. | P0 |
| REQ_DVFS_006 | SPEC_DECISION | A same-OPP request completes immediately. Voltage and frequency requests stay valid and payload-stable until acknowledgement or timeout. | P1 |

## Override, debug, profile, and quality requirements

| ID | Class | Requirement | Priority |
|---|---|---|---|
| REQ_DFT_001 | SPEC_FACT | Test override shall force requested domain controls to power on, clock on, reset released, isolation removed, and retention off. Functional FSM state is preserved and new functional commands are rejected while override is active. | P0 |
| REQ_DBG_001 | SPEC_FACT | Firmware shall be able to observe domain state/busy/error, DVFS state/busy/error, interrupt pending, and last-error information. | P1 |
| REQ_SOC_001 | ASSUMPTION | `ai_soc_top` shall integrate the IP with a synthesizable four-tile control stub, SRAM stub, DDR-interface stub, and PMIC/PLL stub. It is a Phase-1 demonstration wrapper, not a Q4 subsystem. | P1 |
| REQ_CFG_001 | SPEC_FACT | The design shall be profile-driven. The reference profile and parameter legality shall be documented; unsupported profiles shall fail elaboration or be listed as unsupported. | P1 |
| REQ_Q2_001 | SPEC_FACT | Core modules shall have directed normal, error, timeout/deny, interrupt, and CSR-side-effect checks with key simulation assertions. | P0 |
| REQ_Q3_001 | SPEC_FACT | The standalone top shall integrate bridge, CSR/GPRB, interrupt, CRPC, DVFS, and mock external responders in an end-to-end simulation. | P0 |

## Explicit non-goals

No PCK600-compatible interface/register/FSM is defined. Phase-1 excludes Q4 integration, full CDC/RDC, UPF, timing closure, gate-level simulation, DFT insertion signoff, and tape-out signoff.
