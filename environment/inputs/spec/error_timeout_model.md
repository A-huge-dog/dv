# Error, Timeout, and Interrupt Model

## Error codes

| Code | Name | Meaning |
|---:|---|---|
| 0 | NONE | No error |
| 1 | DEPENDENCY | Parent/child dependency violation |
| 2 | ILLEGAL_STATE | Illegal power target/transition |
| 3 | QUIESCE_DENY | Client denied quiescence |
| 4 | QUIESCE_TIMEOUT | Client did not quiesce |
| 5 | POWER_TIMEOUT | Power ack/good did not match |
| 6 | CLOCK_TIMEOUT | Clock acknowledgement timeout |
| 7 | RESET_TIMEOUT | Reset acknowledgement timeout |
| 8 | ISOLATION_TIMEOUT | Isolation acknowledgement timeout |
| 9 | RETENTION_TIMEOUT | Retention acknowledgement timeout |
| 10 | BUSY | Command collided with active operation/override |
| 11 | ILLEGAL_OPP | OPP index or OPP write is illegal |
| 12 | VOLTAGE_TIMEOUT | Voltage stable handshake timeout |
| 13 | PLL_TIMEOUT | Frequency/PLL lock handshake timeout |
| 14 | DVFS_UNSAFE | Compute power state makes DVFS unsafe |

## IRQ bits

| Bit | Name | Set event |
|---:|---|---|
| 0 | DOMAIN_DONE | Any domain transition completes |
| 1 | DOMAIN_ERROR | Any domain error |
| 2 | DOMAIN_TIMEOUT | Domain error code 4..9 |
| 3 | QUIESCE_DENY | Error code 3 |
| 4 | DEPENDENCY | Error code 1 |
| 5 | DVFS_DONE | DVFS completes, including same-OPP |
| 6 | DVFS_ERROR | Any DVFS error |
| 7 | DVFS_TIMEOUT | Voltage or PLL timeout |
| 8 | ILLEGAL_OPP | Error code 11 |
| 9 | COMMAND_BUSY | Error code 10 |
| 15:10 | RESERVED | Never set by RTL |

Event set wins over simultaneous W1C clear. Error status uses the same bit positions. `ERROR_INFO[3:0]` is the error code, `[7:4]` the source index, `[9:8]` source type (0 domain, 1 DVFS, 2 CSR), remaining bits zero.

## Timeout counter

For each waiting state the counter resets to zero on entry. If the required response is absent and timeout is nonzero, the Nth consecutive waiting edge fails for `TIMEOUT_CFG=N`. A value of zero means wait indefinitely. This definition is used by both RTL and the independent DV reference model.
