# Enhanced RSCU Interface Contract

All ports are synchronous to `pclk` unless explicitly noted. Active-low signals carry `_n` suffix.

## APB control interface

| Port | Dir | Width | Meaning |
|---|---:|---:|---|
| `pclk` | in | 1 | Always-on control clock |
| `presetn` | in | 1 | Asynchronous active-low reset |
| `psel`, `penable`, `pwrite` | in | 1 | APB3 transfer controls |
| `paddr` | in | 12 | Byte address, word aligned |
| `pwdata` | in | 32 | Write data |
| `prdata` | out | 32 | Read data |
| `pready` | out | 1 | Always 1 in ACCESS phase |
| `pslverr` | out | 1 | Invalid address/alignment/command access |

Transfer acceptance is `psel && penable`. `pslverr` is meaningful only for an accepted transfer.

## Per-domain control and response

All vectors use `[NUM_DOMAINS-1:0]`, bit index equal to domain ID.

| Port | Dir | Semantics |
|---|---:|---|
| `client_idle_i` | in | Client is idle/quiescent |
| `client_deny_i` | in | Client denies active quiescence request |
| `quiesce_req_o` | out | Held until idle, deny, or timeout |
| `power_ack_i`, `power_good_i` | in | Both must match requested power state |
| `power_req_o` | out | Requested controlled-domain power state |
| `clock_ack_i` | in | Applied clock enable level |
| `clock_enable_o` | out | Requested functional clock enable |
| `reset_done_i` | in | One means requested reset level is applied |
| `reset_n_o` | out | Requested controlled-domain reset level |
| `isolation_ack_i` | in | Applied isolation level |
| `isolation_o` | out | One requests isolation |
| `retention_ack_i` | in | Applied retention level |
| `retention_o` | out | One requests retention |

The response owner may stall. When timeout is enabled, the RSCU terminates a stalled step with error containment.

## DVFS external interface

| Port | Dir | Width | Protocol |
|---|---:|---:|---|
| `voltage_req_valid_o` | out | 1 | Held until `voltage_ack_i && voltage_stable_i` or timeout |
| `voltage_code_o` | out | 16 | Stable while voltage valid |
| `voltage_ack_i` | in | 1 | Voltage controller accepted/applied request |
| `voltage_stable_i` | in | 1 | Requested voltage is stable |
| `frequency_req_valid_o` | out | 1 | Held until `frequency_ack_i && pll_lock_i` or timeout |
| `frequency_code_o` | out | 16 | Stable while frequency valid |
| `frequency_ack_i` | in | 1 | Clock controller accepted/applied request |
| `pll_lock_i` | in | 1 | PLL locked to requested frequency |
| `thermal_req_valid_i`, `perf_req_valid_i` | in | 1 | One-cycle request pulse; thermal has higher priority |
| `thermal_req_opp_i`, `perf_req_opp_i` | in | 3 | Requested OPP index; values 4..7 exercise illegal-OPP handling |

Request sources observe completion/error through CSR and interrupts; Phase-1 provides no separate source-specific response channel.

## General status and integration

| Port | Dir | Width | Meaning |
|---|---:|---:|---|
| `gpr_status_i` | in | 128 | Four packed 32-bit status words |
| `gpr_control_o` | out | 128 | Four packed firmware control words |
| `test_override_i` | in | 1 | External DFT override; ORed with CSR override |
| `irq_o` | out | 1 | Aggregated masked interrupt |
| `domain_state_o` | out | `2*NUM_DOMAINS` | Packed power states for debug/integration |
| `current_opp_o` | out | 2 | Current committed OPP |

## Signal ownership and unsupported crossings

The RSCU drives only request/control outputs. The environment owns all response/status inputs. All inputs must meet `pclk` timing in Phase-1. Asynchronous synchronizers, isolation cells, retention cells, real ICGs, PLL programming buses, voltage-regulator protocols, and UPF intent are Q4 integration responsibilities.
