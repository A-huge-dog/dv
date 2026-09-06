# DVFS Sequence Contract

## Arbitration and admission

On an idle cycle the manager selects at most one request: thermal, firmware, then performance. It snapshots the target OPP. Admission requires target 0..3, COMPUTE state ON, no domain transition active, and no test override. Failure emits the specified error without changing `current_opp`.

## Upscale

1. Read target voltage/frequency codes from the OPP table.
2. Assert voltage request with the target code.
3. Wait for voltage acknowledgement and voltage stable.
4. Deassert voltage request and assert frequency request with target code.
5. Wait for frequency acknowledgement and PLL lock.
6. Commit `current_opp=target_opp`; deassert request; emit done interrupt event.

## Downscale

1. Assert frequency request with target code.
2. Wait for frequency acknowledgement and PLL lock.
3. Deassert frequency request and assert voltage request with target code.
4. Wait for voltage acknowledgement and voltage stable.
5. Commit `current_opp=target_opp`; deassert request; emit done event.

## Same OPP and failure

A same-OPP request emits done without an external request. Timeout in either step drops valid, leaves `current_opp` unchanged, records voltage/PLL timeout, and emits an error/timeout event. OPP table writes are ignored with APB error while busy.
