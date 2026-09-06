# Power-Domain Sequence Contract

Timeout is checked independently in every waiting step. A matching response advances on the same sampled edge; a timeout produces `ERR_*_TIMEOUT` and containment.

## OFF to ON

1. Check parent dependency; reject if any parent is not ON.
2. Assert `power_req` and wait for `power_ack && power_good`.
3. If starting from RETENTION, deassert retention and wait for `retention_ack==0`.
4. Deassert isolation and wait for `isolation_ack==0`.
5. Assert clock enable and wait for `clock_ack==1`.
6. Deassert reset (`reset_n=1`) and wait for `reset_done==1`.
7. Commit ON and emit done.

## ON to OFF

1. Check that no child is ON; reject otherwise.
2. Assert quiescence request. Deny aborts with no destructive output change; idle acknowledgement advances.
3. Disable clock and wait for `clock_ack==0`.
4. Assert reset and wait for `reset_done==1`.
5. Assert isolation and wait for `isolation_ack==1`.
6. Deassert power request and wait for `power_ack==0 && power_good==0`.
7. Commit OFF and emit done.

## ON to RETENTION

Steps 1 through 5 match ON-to-OFF. Then:

6. Assert retention and wait for `retention_ack==1`.
7. Deassert power request and wait for power off acknowledgement/good.
8. Commit RETENTION and emit done.

## RETENTION to ON

1. Check parent dependency.
2. Assert power request and wait for power acknowledgement/good.
3. Deassert retention and wait for `retention_ack==0`.
4. Deassert isolation, enable clock, and release reset using the normal acknowledgements.
5. Commit ON and emit done.

## RETENTION to OFF and ERROR recovery

RETENTION-to-OFF releases retention, waits for its acknowledgement, confirms power is off, and commits OFF. ERROR accepts only OFF; it holds containment controls and waits for power-off response before committing OFF.

## Illegal and no-op behavior

OFF-to-RETENTION and target encoding 3 are illegal. A request to the current stable state generates done with no control transition. A command while the selected domain is busy is rejected.
