# bulk-register

Register SQD worker nodes in bulk from a file of libp2p peer IDs, optionally
naming each one.

## Setup

    python3.11 -m venv .venv
    .venv/bin/pip install -r requirements.txt

**A credential file is optional.** With no `PRIVATE_KEY` or `MNEMONIC` set, the
script prompts for one at the terminal with the input hidden, so the key never
has to exist unencrypted on disk — nor in your shell history, nor in `ps`. This
is the recommended way to run it against a wallet holding real funds:

    .venv/bin/python bulk_register.py peer_ids.txt --dry-run
    Private key or BIP-39 phrase (input hidden):

The prompt takes either form; a phrase is recognised by its spaces. If you would
rather use a file, `cp .env.example .env` and fill it in.

**Python 3.10 or newer is required**, and **always invoke it through the
virtualenv**:

    .venv/bin/python bulk_register.py ...

Running `./bulk_register.py` does *not* work: the shebang picks the system
`python3`, which on macOS is usually 3.9 and in any case is not the virtualenv,
so the dependencies are missing. The script checks both and tells you the exact
command to use instead, but it cannot fix the invocation for you.

The version floor is real rather than stylistic: the code uses PEP 604
annotations (`str | None`) that are evaluated at runtime, so 3.9 fails during
import.

When you do supply one ahead of time, provide exactly one, in `.env` or the
environment:

- `PRIVATE_KEY` — hex, with or without `0x`
- `MNEMONIC` — BIP-39 phrase, 12 or 24 words, derived at `m/44'/60'/0'/0/0`

If both are set, `PRIVATE_KEY` is used and the script warns on stderr. Quoting a
phrase in `.env` is optional. See `.env.example` for the annotated template.

A mnemonic resolves to the **first** account of that phrase — the one MetaMask
shows first. If the wallet holding your SQD is a later account, export that
account's private key and use `PRIVATE_KEY` instead; the script has no
account-index option.

`.env` is gitignored. Nothing but the derived address is ever printed: a
malformed credential produces a generic error and is never echoed back, so a
typo'd phrase cannot leak into a terminal log or CI output.

The prompt only appears when stdin is a terminal. Under cron, `nohup` or CI it
would hang until the run was killed, so there the script fails immediately
instead and tells you to set an environment variable.

## Usage

    .venv/bin/python bulk_register.py peer_ids.txt --dry-run
    .venv/bin/python bulk_register.py peer_ids.txt --limit 10
    .venv/bin/python bulk_register.py peer_ids.txt --name-template 'nodexeus-{n:03d}'
    .venv/bin/python bulk_register.py peer_ids.txt --network tethys

| Flag | Meaning |
| --- | --- |
| `--network` | `mainnet` (default) or `tethys` |
| `-n`, `--limit` | Register at most this many *new* nodes |
| `--name-template` | Name for lines without an explicit name |
| `--batch` | Nodes per generated-name batch (default 50) |
| `--dry-run` | Run every check, print the plan, send nothing |
| `--yes` | Skip the confirmation prompt |
| `--rpc-url` | Override the network's default RPC |
| `--action` | `register` (default), `deregister`, `withdraw`, `claim`, or `status` |
| `--peer-id` | Act on this peer ID only, instead of the whole file; repeat for several |
| `--signer` | `local` (default) or `fireblocks` — see below |
| `--address` | Wallet to report on for `--action status`; with `--signer fireblocks`, which vault account to use |
| `--log` | Result log path (default `<input>.<network>.run.jsonl`) |

Every real run also writes `<input>.<network>.registered.csv` — one row per
confirmed registration with `peer_id,name,tx_hash,block,registered_at`. It is
regenerated from the run log each time rather than appended to, so it cannot
drift out of sync, and it is the record to hand over when the job is done. A
dry run does not write it: the file asserts that these nodes are registered.

Always `--dry-run` first. It reports the bond total, the estimated gas, whether
an approval is needed, and exactly which peer IDs would be registered under
which names.

The gas figure in that plan is approximate, and labelled as such when the wallet
has no allowance yet: `register()` reverts without an allowance, so gas cannot be
measured until the approval has landed. The run re-measures gas immediately after
the approval receipt and prints the limit it will actually use.

## The run log is per network

The log path includes the network (`peer_ids.txt.mainnet.run.jsonl`), and every
record stores the network it was written for. This matters:

> **Rehearsing on tethys used to be able to cancel the mainnet run.** Registering
> a file on tethys wrote 300 `success` records; running the same file on mainnet
> then read them, reported `to register: 0` / `nothing to register`, and exited 0
> with **zero** mainnet nodes registered. The chain-ID guard does not catch this,
> because it validates the RPC, not the log.

So rehearse freely: a tethys `success` never satisfies a mainnet run's skip
filter, and the two runs do not share a log file by default. If you override
`--log`, the network in each record still keeps the two apart — but give the two
networks separate paths anyway.

Records written by an earlier version have no network field. Those still count
for whatever network you are running, so existing logs keep resuming.

## Resuming

Any run that leaves work behind prints the exact command to continue, rebuilt
from the flags you gave it — `--limit`, `--name-template`, `--log`, `--rpc-url`,
and `--network` are all echoed back, because dropping any one of them changes
what a resume spends. `--yes` is deliberately *not* echoed: a resume prompts
again with its own fresh plan.

Under `--limit`, the on-chain scan stops as soon as the limit is met, so peers
past that point were never examined. The summary says `up to N ... may still be
unregistered` in that case rather than asserting a figure it cannot know.

## Input file

One entry per line, either `peer_id` or `peer_id,name`:

    12D3KooW...aaa,prod-worker-01
    12D3KooW...bbb,prod-worker-02
    12D3KooW...ccc

Blank lines and `#` comments are ignored. Duplicates are collapsed with a
warning, keeping the first line's name. A name may contain commas; only the
first comma separates the fields. A line ending in a bare comma is an error.

`peer_ids.txt.example` shows the shape with every line commented out, so it
cannot be run as-is: an unowned but valid peer ID would still bond 100,000 SQD.

## Naming

A node's displayed name is the `name` key of the JSON metadata the contract
stores, which the network indexer parses and exposes over GraphQL. Names come
from two places, explicit beating generated:

1. The optional second column in the input file.
2. `--name-template`, applied to any line without a name.

The template supports `{n}` and `{peer_id}`, including format specs, so
`--name-template 'nodexeus-{n:03d}'` yields `nodexeus-001`, `nodexeus-002`, and
so on.

`{n}` is **the lowest number that name has not already used on this network**,
read from the run log. Two consequences, both deliberate:

- **Each template starts its own sequence.** Register 100 as
  `nodexeus-{n:03d}`, then 100 more as `newname-{n:03d}`, and the second group
  begins at `newname-001` rather than continuing the first group's count.
- **An interrupted group resumes rather than colliding.** If a run stops after
  50, the next run with the same template starts at `051`.

A `failed` attempt never landed, so its number is released and reused. A
`pending` one might have landed, so its number stays taken — reusing it could
put two workers under the same name.

Names are allocated only to the peers a run actually registers, after the
already-registered filter, so skipped peers never consume numbers.

With neither, names are **generated in batches** rather than leaving nodes
nameless. Each batch of `--batch` nodes (50 by default) draws one random word,
and every node in it is named `<word>-<last 6 characters of its peer ID>`:

    12D3KooW...NUQmGRNYnLKN  ->  influence-NYnLKN
    12D3KooW...afpF6ZXxVuHR  ->  influence-XxVuHR
    12D3KooW...Gzf5esnybPQT  ->  influence-nybPQT
    12D3KooW...AH2AcdhKkNna  ->  goldfish-hKkNna     <- next batch
    12D3KooW...ZDiUgZepJ3Kg  ->  goldfish-epJ3Kg

A 1000-node run therefore produces 20 visibly distinct groups. The words come
from [`coolname`](https://pypi.org/project/coolname/), drawn across all of its
categories — adjectives, colours, minerals, abstract nouns, animals — so batches
are not all of one kind. A word already used by an earlier run is never redrawn,
which would make two separate batches look like one.

The suffix is the peer ID's own tail, so names are **unique by construction**
(base58, ~38 billion combinations for six characters) and a name in the
dashboard maps straight back to its node without consulting the CSV.

Words are drawn fresh each run. An interrupted batch simply ends where it
stopped and the next run starts a new word; nothing has to be reconstructed
from the log. This does mean `--dry-run` previews different words than the real
run will use — the dry run verifies the mechanics and the counts, not the exact
strings.

Precedence is: explicit column, then `--name-template`, then batch-generated.
There is deliberately no way to register nameless; `updateMetadata` can rename a
worker later without re-bonding.

## How `--limit` works

The limit applies to the *actionable* set, after peer IDs already registered
have been filtered out. `--limit 10` means ten new registrations, so running it
twice against the same file registers ten, then the next ten.

## Safety

- The RPC's chain ID must match the chosen network, or the run aborts. This is
  the guard against firing mainnet bonds at a tethys-intended list.
- The whole input file — peer IDs, names, and metadata sizes — is validated
  before any transaction is sent.
- Registration bonds 100,000 SQD per worker and needs an ERC-20 allowance. The
  script approves exactly `bond × count`, never an unlimited amount. (Bond
  amount verified against both mainnet and tethys.)
- **The wallet's ETH is checked before anything is sent.** Running dry partway
  through would abort the run, so the shortfall is reported up front instead.
  See "Gas" below.
- Already-registered peer IDs are skipped, so re-running a partly finished file
  wastes no gas.
- Every attempt is appended to a JSONL log immediately, so an interrupted run
  resumes cleanly. A `success` in the log is *trusted* and not re-checked
  on-chain — that is what makes a 300-node resume cheap — so in the extremely
  unlikely event of a reorg past a receipt, that peer ID would be skipped
  permanently and would need registering by hand.
- The log is scoped per network; see above.
- A truncated final line (from a crash mid-write) is reported as an error naming
  the line, rather than a traceback. Repair that one line; never delete the log,
  it is the record of what has already been bonded.
- Three consecutive failures abort the run rather than burning gas down a long
  file.
- A receipt wait failure stops the run: this covers timeouts and any other
  lookup error. Nonces are sequential, so a stuck transaction would block
  everything behind it. The run logs a `pending` record with the real
  transaction hash and the reason, and aborts rather than continuing behind a
  possibly-stuck nonce.
- Transactions are signed before they are sent, so the hash is known even when
  the send itself fails. A send the node *rejects* is logged `failed` and the run
  continues on the same nonce. A send that fails at the transport level
  (connection reset, read timeout) is logged `pending` and stops the run: the
  node may have accepted the transaction and failed only when replying, so the
  log must not claim `failed` for a peer that may in fact be registered.
- Ctrl-C mid-run logs a `pending` record for the in-flight transaction, with its
  hash, prints the resume command, and exits 130.
- If the approval itself fails after being broadcast, the run exits 2 naming the
  approval's hash and tells you to let it settle first: a new run reads the nonce
  at `latest`, so it would otherwise reuse that nonce and be rejected as an
  underpriced replacement.
- `maxFeePerGas` is 2x the base fee at the time of the read, so it is re-read
  after the confirmation prompt and every 25 registrations. A 300-node run spans
  15-30 minutes, and once the base fee passes the cap nothing mines.
  `maxPriorityFeePerGas` has a small floor, since Arbitrum suggests 0.
- Read-only RPC calls are retried with backoff (a 300-peer scan makes up to 600
  of them), and a persistent failure exits 2 rather than raising a traceback. No
  send is ever retried.
- Without `--yes` and without a terminal (nohup, cron, CI), the confirmation
  reads EOF and declines.

## Note on re-registering withdrawn peer IDs

The registry keeps `workerIds[peerId]` populated after a worker is withdrawn,
even though the worker slot itself is vacated and the peer ID can be registered
again. The skip check therefore reads both `workerIds` and the worker's
`registeredAt`; a peer ID you previously cycled out is correctly offered for
re-registration rather than skipped forever.

One caveat from the contract: re-registering a vacated slot only works for the
account that originally created it. Someone else's withdrawn peer ID reverts,
and shows up as a normal failure.

## Networks

| Network | Chain | WorkerRegistration |
| --- | --- | --- |
| `mainnet` | Arbitrum One (42161) | `0x36e2b147db67e76ab67a4d07c293670ebefcae4e` |
| `tethys` | Arbitrum Sepolia (421614) | `0xCD8e983F8c4202B0085825Cf21833927D1e2b6Dc` |

The bond token address is read from the registry's `SQD()` getter, not
hardcoded.

## Tests

    .venv/bin/pytest

`web3` is mocked throughout. No test contacts an RPC endpoint or needs a key.

## Gas

One transaction per node, sent sequentially. A real mainnet `register()` used
**370,138 gas** at 0.0200 gwei — about **0.0000074 ETH** per node, so roughly
**0.0075 ETH for 1000 nodes** at typical Arbitrum prices.

Before sending anything the script compares the wallet's ETH against a
worst-case budget and aborts if it is short:

    gas limit (measured estimate + 25%) x maxFeePerGas x transaction count

That figure is deliberately pessimistic — around 0.023 ETH for 1000 nodes,
roughly 3x the expected spend. It uses the padded gas *limit* rather than the
gas actually consumed, and the full fee cap rather than the current base fee.
Unused gas is never charged, so the real cost stays near the 0.0075 ETH figure;
the budget only has to be coverable. Funding the wallet with **0.05 ETH** leaves
comfortable room for a fee spike.

Reducing gas is not worth pursuing. Metadata costs about 915 gas per byte, so
across 1000 nodes shorter names save ~0.0002 ETH, dropping names entirely saves
~0.0004 ETH, and batching via a Safe saves ~0.0004 ETH — each under a cent per
node, and each costs something real (worse names, or a permanent change to which
address owns the workers). Batching is worth considering for hardware-wallet
signing convenience, turning 1000 confirmations into ~15, but not for gas.

## Signing with Fireblocks (or any signing proxy)

Fireblocks holds keys as MPC shares and **cannot export a private key**, so
`PRIVATE_KEY`, `MNEMONIC` and the prompt are all unusable for such a wallet.
`--signer fireblocks` instead sends transactions *unsigned* over
`eth_sendTransaction`, for the RPC endpoint to sign:

    npm install -g @fireblocks/fireblocks-json-rpc
    fireblocks-json-rpc --http -- \
        .venv/bin/python bulk_register.py peer_ids.txt \
            --signer fireblocks --rpc-url $FIREBLOCKS_JSON_RPC_URL

The mechanism is generic — anything that speaks `eth_sendTransaction` and signs
works — but Fireblocks is the case it was built for.

Two things differ from local signing:

- **The signer owns the nonce.** Fireblocks keeps its own sequence per vault
  account, so no nonce is supplied and none is tracked.
- **A failed send yields no transaction hash**, because the hash only exists
  once the remote side has signed. Such an attempt is logged `pending` with no
  hash and the run stops; recovery is the Fireblocks console, which records
  every transaction it was asked to sign.

The receipt wait is also longer (900 s vs 300 s), because each transaction is
queued for policy evaluation and MPC signing before it is even broadcast.

`--address` selects which vault account to use when the endpoint offers several.

> **Check the approval policy before a large run.** Fireblocks evaluates every
> transaction against its Transaction Authorization Policy. Automated signing
> needs an **API Co-Signer** plus a TAP rule that approves these calls;
> otherwise each transaction waits for a human in the console — untenable for
> 1000 registrations. That is a policy change in their workspace, not a code
> change here.

## The other three actions

`deregister` and `withdraw` take the same `bytes peerId` argument as `register`,
so **the same peer ID file drives all four actions**. Neither bonds anything:
deregister costs only gas, and withdraw *returns* 100,000 SQD per worker.

    .venv/bin/python bulk_register.py peer_ids.txt --action status --address 0x...
    .venv/bin/python bulk_register.py peer_ids.txt --action deregister --dry-run
    .venv/bin/python bulk_register.py peer_ids.txt --action withdraw --limit 10

Each action reads every peer ID's on-chain state and acts only on the ones whose
state permits it, so a mixed file is safe — nothing reverts because it wasn't
eligible.

| State | Meaning | Next action |
| --- | --- | --- |
| `unregistered` | Never registered, or a slot this account vacated | `register` |
| `registering` | Registered, waiting for its epoch to begin | wait |
| `active` | Live and earning | `deregister` |
| `deregistering` | Deregistered, running until the epoch ends | wait |
| `locked` | Inactive, bond still locked | wait |
| `withdrawable` | Lock expired, bond claimable | `withdraw` |
| `foreign` | Registered by a different account | nothing — see below |

Only the account that registered a worker can deregister or withdraw it. A peer
ID someone else registered shows as `foreign` and is skipped rather than
attempted.

### `--action status`

Read-only, and the only action that needs no credential — pass `--address` and
it reports on any wallet. It prints a state breakdown, totals the SQD that is
withdrawable right now, says when the next lock expires, and writes
`<input>.<network>.status.csv` with a row per peer ID. No field contains a
comma, so nothing needs quoting and the file imports cleanly into a spreadsheet
or splits correctly with `cut` and `awk`.

A freshly registered worker does not go live immediately: `register()` sets
`registeredAt = nextEpoch()`, so it shows as `registering` until that boundary
arrives — the SQD dashboard calls the same gap "REGISTERING". It cannot be
deregistered until then, because the contract requires the worker to be active.

This is the mode to run during the wait. `deregister` takes effect at the next
epoch and the bond then stays locked for `lockPeriod`, both **99,999 L1 blocks
(~13.9 days)** on mainnet, so deregister → withdraw spans roughly 14–28 days.

> **On timing:** the lock is measured in the block number the *contract* sees,
> which on Arbitrum is the **L1** block number, not the L2 one `eth_blockNumber`
> returns (~25,700,000 against ~494,000,000). Comparing the wrong one marks every
> locked worker withdrawable, and each `withdraw()` then reverts "Worker is
> locked". The script reads `l1BlockNumber` for this.

### Acting on one node

`--peer-id` acts on specific peer IDs, which is what you want to retry a single
failure or rehearse one node. **The file is optional** — if you already know the
ID, just name it:

    .venv/bin/python bulk_register.py --action deregister \
        --network tethys --peer-id 12D3KooW...

Repeat the flag for several. Given *with* a file it narrows that file, and each
ID must appear in it — one that does not is an error rather than an empty
selection, because "nothing to do" reads exactly like "already done".

Without a file, artifacts are named `adhoc.<network>.*` instead of after the
input. That keeps a one-off action from appending to the run log a bulk run
depends on. Pass `--log` explicitly if you want them to share one.

`--address` is a *wallet* address and only applies to `--action status`. Passing
it to a write action is an error: those act as whoever holds the credential, and
silently ignoring it would run against the whole file instead of the node you
meant.

### `--action claim`

Rewards are earned **per wallet, not per peer ID**, so there is no sweep to
perform — one transaction claims everything:

    .venv/bin/python bulk_register.py --action claim --network tethys

No peer ID file and no `--peer-id`; this is the one action that needs neither.
`RewardTreasury.claim()` takes no peer ID, and the distributor loops over
`getOwnedWorkers(msg.sender)` internally, zeroing every worker's balance and
adding staking rewards.

Gas scales with the fleet because of that loop. Measured on mainnet: 82,011 gas
for a wallet with no workers and 1,619,348 for one with 201, i.e. roughly 7,650
per worker. A 1000-worker sweep is therefore about **7.7M gas, ~0.00015 ETH** —
well inside Arbitrum's block limit.

`--action status` also reports the claimable total, since reading it is free.

If the SQD is held in a vesting contract, claiming goes through it in the same
way registration does: `RewardTreasury` is whitelisted as a vested target, so
the call becomes `vesting.execute(rewardTreasury, claim(distribution), 0)`.

### The log distinguishes actions

Each record stores which action produced it, so a successful deregistration
never makes a later `register` run skip that peer ID. Records written before the
field existed read as registrations. Each action writes its own CSV:
`registered.csv`, `deregistered.csv`, `withdrawn.csv`.
