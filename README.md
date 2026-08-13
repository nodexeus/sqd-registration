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
- `MNEMONIC` — BIP-39 phrase, derived at `m/44'/60'/0'/0/0`

`.env` is gitignored. Nothing but the derived address is ever printed.

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
| `--log` | Result log path (default `<input>.run.jsonl`) |

Always `--dry-run` first. It reports the bond total, the estimated gas, whether
an approval is needed, and exactly which peer IDs would be registered under
which names.

## Input file

One entry per line, either `peer_id` or `peer_id,name`:

    12D3KooW...aaa,prod-worker-01
    12D3KooW...bbb,prod-worker-02
    12D3KooW...ccc

Blank lines and `#` comments are ignored. Duplicates are collapsed with a
warning, keeping the first line's name. A name may contain commas; only the
first comma separates the fields. A line ending in a bare comma is an error.

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
- Already-registered peer IDs are skipped, so re-running a partly finished file
  wastes no gas.
- Every attempt is appended to a JSONL log immediately, so an interrupted run
  resumes cleanly.
- Three consecutive failures abort the run rather than burning gas down a long
  file.
- A receipt wait failure stops the run: this covers timeouts and any other
  lookup error. Nonces are sequential, so a stuck transaction would block
  everything behind it. The run logs a `pending` record with the real
  transaction hash and aborts rather than continuing behind a possibly-stuck
  nonce.

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
