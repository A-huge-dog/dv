# Enhanced RSCU Architecture

## 1. Boundary and profile

`SPEC_FACT`: Enhanced RSCU is an always-on subsystem controller for power, clock, reset, isolation, retention, DVFS, interrupts, debug/test hooks, and firmware-visible state.

`UARCH_DECISION`: Phase-1 uses a synchronous APB3 control port and four logical domains. It does not implement the legacy AXI/AHB bridge variants. All handshake inputs are treated as synchronized to `pclk`.

`ASSUMPTION`: The SoC demonstration wrapper represents a 2x2 tile array as one COMPUTE power domain. Per-tile power partitioning is a future profile.

## 2. Module partition

| Module | Class | Primary ownership | Requirement IDs |
|---|---|---|---|
| `rscu_bridge` | protocol endpoint | APB setup/access qualification, alignment/error response | REQ_SYS_003, REQ_BRG_001 |
| `rscu_csr` | CSR/register | address map, command pulses, timeout/debug config, status mux | REQ_CSR_001..003, REQ_DBG_001 |
| `rscu_gprb` | register block | four control and four sampled status words | REQ_GPR_001 |
| `rscu_intr_handler` | interrupt | sticky raw, mask, W1C clear, aggregate IRQ | REQ_IRQ_001..002 |
| `pck_dependency_checker` | combinational checker | parent/child legality for one domain command | REQ_DEP_001 |
| `pck_q_mgr` | handshake manager | quiesce request, deny, timeout | REQ_QHS_001, REQ_TMO_001 |
| `pck_p_mgr` | handshake manager | power request, ack/good matching, timeout | REQ_PHS_001, REQ_TMO_001 |
| `pck_domain_ctrl` | sequencer | one domain state, sequence, containment, local error | REQ_PWR_001..005, REQ_CLK_001, REQ_RST_001, REQ_ISO_001, REQ_RET_001, REQ_SAFE_001 |
| `rscu_crpc_plus` | integration/arbiter | dependency masks, per-domain command routing, event aggregation | REQ_DEP_001, REQ_DFT_001 |
| `dvfs_opp_table` | table | four OPP entries and idle-only writes | REQ_DVFS_001 |
| `dvfs_seq_fsm` | sequencer | upscale/downscale request ordering and timeout | REQ_DVFS_002..006 |
| `dvfs_mgr` | arbiter/integration | thermal/FW/performance priority, safety check, event mapping | REQ_DVFS_004..005 |
| `enhanced_rscu_top` | IP integration | child connectivity and external contract | REQ_SYS_001..006, REQ_Q3_001 |
| `ai_soc_top` | prototype integration | RSCU plus self-owned synthesizable responder stubs | REQ_SOC_001 |

Every important state has one procedural owner. Top-level modules own connectivity only.

## 3. Clock, reset, and power architecture

- All Phase-1 RTL uses `pclk` and asynchronous active-low `presetn`.
- External response inputs must already be synchronous. No multi-bit CDC is hidden in the IP.
- Enhanced RSCU control logic is always-on. Controlled-domain outputs cross power boundaries and require integration-owned isolation cells before Q4.
- Domain control state resets to OFF. Interrupt, CSR, GPR control, and DVFS state reset deterministically.
- Test override is a final output mux; it does not rewrite sequencer state.

## 4. Internal interface contracts

### APB bridge to CSR

`wr_en`/`rd_en` are one-cycle pulses in a valid APB ACCESS phase. `addr` and `wdata` are sampled combinationally in that phase. `rdata` and `slave_error` are combinational. There is no wait state.

### CSR to CRPC

`domain_cmd_valid` is a one-cycle pulse carrying `domain_cmd_id` and `domain_cmd_target`. CRPC either launches the selected idle controller or emits a one-cycle error. Per-domain `done`, `error_valid`, and `error_code` are event pulses.

### Domain handshakes

Request controls are levels. The associated response must match the requested state before advancement. Payload/control remains stable while waiting. Timeout ownership is inside `pck_domain_ctrl`; quiescence and power waits delegate the counter behavior to `pck_q_mgr` and `pck_p_mgr`.

### CSR to DVFS

Firmware start is a one-cycle pulse. Thermal/performance inputs are one-cycle request pulses. The manager latches the chosen target. Voltage/frequency request payloads remain stable while their valid is asserted.

## 5. Storage allocation

| State | Owner | Width/depth | Reset |
|---|---|---|---|
| Domain state/FSM/control levels | each `pck_domain_ctrl` | one set/domain | OFF-safe controls |
| Dependency masks | `rscu_crpc_plus` | `NUM_DOMAINS × NUM_DOMAINS` | reference profile matrix |
| Timeout config | `rscu_csr` | 16 bits | 32 cycles |
| IRQ raw/mask | `rscu_intr_handler` | 16 bits each | 0 |
| Error status/info | `rscu_csr` | 16/16 bits | 0 |
| GPR control/status | `rscu_gprb` | 4×32 each | control=0, status sampled |
| OPP table | `dvfs_opp_table` | 4×32 | profile constants |
| Current/target OPP and FSM | `dvfs_seq_fsm` | 2 bits plus state | OPP0/idle |

## 6. Reference dependency profile

- COMPUTE(0) depends on FABRIC(1) and SRAM(2).
- FABRIC(1) depends on DDR_IF(3).
- SRAM(2) depends on DDR_IF(3).
- DDR_IF(3) has no parent.

The checker derives children from the same parent-mask matrix, preventing a parent from leaving ON while an ON child depends on it.

## 7. Error containment and recovery

Dependency/illegal/no-resource errors reject before output changes. Quiesce deny aborts before destructive changes and preserves ON. Any wait timeout enters ERROR and requests safe containment. An ERROR domain accepts only an OFF command; after power-off acknowledgement it reaches OFF. Firmware clears sticky error/IRQ state independently with W1C.

## 8. Latency and throughput

No frequency target is supplied, so none is invented. APB responses have zero inserted wait states. Command completion latency is response-dependent and bounded only when `TIMEOUT_CFG` is nonzero. One CSR command can be issued per APB transfer; independent domain controllers may operate concurrently. DVFS accepts one transition at a time.

## 9. Verification obligations

Reset-safe outputs, APB access semantics, state transition ordering, request stability, timeout cycle behavior, dependency rejection, deny-before-destructive-action, W1C set priority, DVFS ordering, and override mux behavior are measurable obligations and map to the DV plan.
