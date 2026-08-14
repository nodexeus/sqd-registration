# bulk-register

Register SQD worker nodes in bulk from a file of libp2p peer IDs, optionally
naming each one.

## Setup

    python3.11 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    cp .env.example .env      # then put your key in it

**Python 3.10 or newer is required.** The code uses PEP 604 type annotations
(e.g. `str | None`) which are evaluated at runtime, so Python 3.9 will fail at
import with a `TypeError`. If your system `python3` is 3.9, invoke the venv
with an explicit version like `python3.11` or `python3.10` instead.

Provide exactly one credential in `.env` or the environment:

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
| `--dry-run` | Run every check, print the plan, send nothing |
| `--yes` | Skip the confirmation prompt |
| `--rpc-url` | Override the network's default RPC |
| `--log` | Result log path (default `<input>.<network>.run.jsonl`) |

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
so on. `{n}` is the peer ID's position in the *file*, not in the work list, so a
given peer ID gets the same name regardless of which subset a run registers.

Lines with neither register unnamed. The contract's `updateMetadata` can name
them later without re-bonding, so a missing or wrong name is not permanent.

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
