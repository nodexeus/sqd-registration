# Can operators inflate reports, or administrators reduce worker rewards?

**Administrative ability to lower individual rewards is confirmed. A worker-telemetry integrity concern exists, but a working current-mainnet payout exploit is not established. Neither finding shows that manipulation caused the APR/payout differences in the original analysis.**

Read-only follow-up to NODEX-259, 25 September 2026. No modified worker, fabricated log, transaction submission or production exploit was used.

## Network-side discretion

The mainnet distributor at `0x4de282bD18aE4987B3070F4D5eF8c80756362AEa` accepts an authorized proposal containing worker IDs, worker amounts and delegator amounts. Other distributors approve the same proposal hash. Once the threshold is reached, it credits exactly those amounts. The contract checks authorization, approvals, array lengths and continuity of reward block ranges. It does **not** recompute per-worker traffic, uptime, tenure or fair reward amounts, and does not require every eligible worker to appear.

Consequently, an authorized approving group can submit a smaller amount for one worker, zero its new reward, or omit it. This affects future distributed rewards; the inspected path does not subtract an already-credited worker balance. An ordinary node operator does not have this authority.

This is grounded in the deployed contract, not just a current repository version. Sourcify reports an **exact runtime and creation match**. Its verified distribution source is identical to the inspected upstream file except for a leading artwork comment; the verified source is saved as `verified-distributor.sol`. [Verification service](https://sourcify.dev/server/v2/contract/42161/0x4de282bD18aE4987B3070F4D5eF8c80756362AEa), [commit/approve logic](https://github.com/subsquid/subsquid-network-contracts/blob/861cfccdb3a7b2bfe94a49e4fb64ede4f0319401/packages/contracts/src/DistributedRewardDistribution.sol#L123), [crediting rewards](https://github.com/subsquid/subsquid-network-contracts/blob/861cfccdb3a7b2bfe94a49e4fb64ede4f0319401/packages/contracts/src/DistributedRewardDistribution.sol#L224).

At Arbitrum block **508789255**, read at **14:23:45 UTC**, the live contract had:

| Setting | Value |
|---|---:|
| Required approvals, including the committer's approval | 2 |
| Simultaneous eligible committers | 1 |
| Rotation interval | 256 Arbitrum blocks |
| Paused | false |

A recent distribution for submitted range `26054188–26054787` shows approvals from two distinct addresses, followed by `Distributed`. See [initial commitment](https://arbiscan.io/tx/0x6956b7e92eff1b0f0c2dba19b01843feef51aa675df7c59768ca9c5cb458f229) and [second approval/distribution](https://arbiscan.io/tx/0x3e75eb768896776ccaa37a26710fccd48484c873e5eb14ae03b51f0af8cb7b98). Distinct signing addresses do not establish independent organizations or decision makers.

Administrators can also change the approved distributor set and required approval count. Those actions emit events. Separately, the public `NetworkController` source contains admin-only target-capacity and yearly reward-cap settings, which affect network-wide reward calculations rather than a specific worker's score. These global mechanisms should not be confused with per-worker selection. [Distributor administration](https://github.com/subsquid/subsquid-network-contracts/blob/861cfccdb3a7b2bfe94a49e4fb64ede4f0319401/packages/contracts/src/DistributedRewardDistribution.sol#L69), [approval threshold](https://github.com/subsquid/subsquid-network-contracts/blob/861cfccdb3a7b2bfe94a49e4fb64ede4f0319401/packages/contracts/src/DistributedRewardDistribution.sol#L270), [global parameters](https://github.com/subsquid/subsquid-network-contracts/blob/861cfccdb3a7b2bfe94a49e4fb64ede4f0319401/packages/contracts/src/NetworkController.sol#L117).

The published reward bot does independently calculate the proposed distribution before approving matching amounts. That is a useful off-chain check if the signers and their input data are independent; it is not an on-chain guarantee that the inputs are truthful. [Approval implementation](https://github.com/subsquid/subsquid-network-contracts/blob/861cfccdb3a7b2bfe94a49e4fb64ede4f0319401/packages/rewards-calculator/src/rewardBot.ts#L117).

## Scheduler and dashboard status

The indexer derives `jailed` and `jailReason` from the scheduler's published worker status. A status other than `online` becomes `jailed = true`. This is an off-chain classification, not proof of an on-chain slashing decision. The fetched scheduler snapshot contained 1,851 online and 31 offline entries, but exposed no per-worker manual penalty setting. I did not establish a manual scheduler override API or the production scheduler's complete implementation. [Status mapping](https://github.com/subsquid/squid-subsquid-network/blob/302b8f19bbe7280dd0e3f60eb5242acbb19576bb/packages/workers/src/handlers/metrics.ts#L204).

The distribution events reveal who approved and how much was credited, but do not contain a per-worker reason or supporting performance evidence. A low payout by itself therefore cannot establish deliberate suppression.

## Operator-controlled telemetry

The inspected worker builds a query-execution log containing output size, execution timings, result summary and its version. Those values originate inside the operator's process. The stock worker derives output size from actual returned data; an operator controlling the binary can change what it reports. There is no direct worker-supplied APR setting that the distribution contract uses. [Worker log construction](https://github.com/subsquid/worker-rs/blob/03fdb65c8d3edda9fbd2e1182032007f8e0bc31d/src/controller/p2p.rs#L765).

The current collector validates the client's signed request and timestamp consistency, then stores the worker's claimed output size and timings. It does not re-execute the query or compare the claimed output size with the full response in this conversion path. A signature authenticates the signer; it does not independently measure the work. Successful logs are assigned one read chunk by this collector, so the current path is not a freely supplied scanned-chunk counter. [Collector validation and storage](https://github.com/subsquid/network-components/blob/418c84f54e8e516707a73bece33e87c02bd61667/crates/collector-utils/src/storage.rs#L213).

The public reward calculator sums output size and read-chunk counts into traffic weight, then applies a capped traffic factor. This establishes a plausible dependency on worker-originated telemetry, but **not a proven current-mainnet exploit**. Its legacy log/signature schema differs from the current collector schema. The deployed calculator revision, database views/deduplication and any portal-log reconciliation are not verified. Portal-side logs exist in the public components; the reviewed calculator query does not show a join to them. [Traffic and reward calculation](https://github.com/subsquid/subsquid-network-contracts/blob/861cfccdb3a7b2bfe94a49e4fb64ede4f0319401/packages/rewards-calculator/src/worker.ts#L33), [input queries](https://github.com/subsquid/subsquid-network-contracts/blob/861cfccdb3a7b2bfe94a49e4fb64ede4f0319401/packages/rewards-calculator/src/clickhouseClient.ts#L34).

Heartbeats are collected over the network and timestamped by the collector. Thus an operator's claim of uptime is not sufficient to invent periods during which the collector received no heartbeat. Reported storage and assignment progress still originate from the worker. [Heartbeat collection](https://github.com/subsquid/network-components/blob/418c84f54e8e516707a73bece33e87c02bd61667/crates/pings-collector/src/server.rs#L112), [collector timestamp](https://github.com/subsquid/network-components/blob/418c84f54e8e516707a73bece33e87c02bd61667/crates/collector-utils/src/storage.rs#L371).

## What would distinguish manipulation from ordinary differences?

Obtain the deployed reward-calculator revision/configuration and per-epoch input/output records. For suspect workers, reconcile worker query logs with independently collected portal results, distinguish missing collection from missed service, and independently recompute reward amounts. Compare those results with the actual approved distribution and scheduler-status history. The present payout dataset can identify discrepancies worth investigating, but cannot establish intent or telemetry falsification.

The live reads, source revisions and approval events are recorded in `trust-evidence.json`. This was a targeted source trace, not a complete security audit of all network services.
