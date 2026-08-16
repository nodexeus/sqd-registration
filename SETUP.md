# Setup

One-time setup for running `bulk_register.py`. Takes about ten minutes.

## Requirements

- **Python 3.10 or newer.** The code uses annotations that Python 3.9 cannot
  evaluate, so it fails at import there. On macOS the system `python3` is
  usually 3.9, so invoke a newer one explicitly.
- **Node 18 or newer** — only if any account is held in Fireblocks.
- An RPC endpoint. The defaults are the public Arbitrum ones, which are fine at
  this scale; `--rpc-url` overrides them. With Fireblocks, `./sqd` also hands
  the same endpoint to the signing proxy, so both read the same node.

## Install

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt

If any account is in Fireblocks, also:

    npm install @fireblocks/fireblocks-json-rpc

Check it works:

    ./sqd --help

Note `./sqd`, not `python bulk_register.py`. Running the script directly picks
the system Python and misses the virtualenv; the wrapper handles both.

## Two ways to sign

The tool never stores a key. Which method applies depends on where each account
lives, and **a single job may use both** — one file signed from Fireblocks,
another from a wallet whose key you hold.

### A key you hold

Nothing to configure. When a run needs a credential it asks, with the input
hidden:

    Private key or BIP-39 phrase (input hidden):

Paste a private key or a 12/24-word phrase. Nothing is written to disk, and a
malformed entry is reported without echoing what you typed.

For an unattended run, set `PRIVATE_KEY` or `MNEMONIC` in the environment or a
`.env` file instead. The prompt only appears at a terminal — under cron or CI
the run fails immediately rather than hanging.

### Fireblocks

Fireblocks holds keys as MPC shares and cannot export them, so there is nothing
to paste. Transactions go out unsigned for Fireblocks to sign.

From your Fireblocks administrator:

| | |
| --- | --- |
| An **API user** allowed to initiate transactions | The tool authenticates as this user |
| Its **API key** (a UUID) | Identifies that user |
| Its **RSA private key** file | Signs API requests. Generated when the API user is created |
| Nothing about vault accounts | They are discovered automatically — see below |

The vault account also needs the chain's native asset enabled — **ETH on
Arbitrum One** — or the tool cannot see an address to act as.

Then:

    cp fireblocks.env.example fireblocks.env

and fill in the four values. The presence of that file is what routes signing
through Fireblocks. It is gitignored, and so is `*.key`.

**A job may mix the two.** Leave `fireblocks.env` in place and add
`--signer local` to any individual run whose account is a key you hold — that
run skips Fireblocks entirely. There is no need to create and delete the file
between phases.

**You do not need to know which vault accounts you have.** Leave
`FIREBLOCKS_VAULT_ACCOUNT_IDS` commented out and every vault account holding
the asset is found automatically, in a single API call. Each run then selects
whichever of them it needs. Accounts without the asset are skipped quietly.

Set the variable only to deliberately restrict a run to certain accounts. It is
easy to get wrong and rarely worth it: **every id listed must exist and hold the
asset**, and one that does not aborts the entire run, including the valid
accounts beside it —

    Failed to populate accounts: No ETH-AETH asset wallet found for
    vault account with id 0

Ids are also 0-based, so the first account is `0`. Listing ids costs about
0.2 s each as well, checked one at a time, where discovery is one flat call.

If a run needs an account the workspace does not hold at all, the tool says so
and points at `--signer local`.

Discovery reads the first 20 vault accounts holding the asset. Past that, list
the ids you need explicitly.

> **Those credentials are signing authority.** An API key plus its RSA key,
> combined with a policy that approves these calls, can move funds from that
> vault. Keep them with whoever owns the wallet.

#### Will each transaction need approving by hand?

Settle this before a large run. Fireblocks checks every transaction against its
Transaction Authorization Policy. Unattended signing needs an **API Co-Signer**
deployed plus a TAP rule that approves these calls. Without both, each
transaction waits for a person in the console — fine for one node, impossible
for hundreds.

## Funding

| For | What is needed |
| --- | --- |
| Registering | 100,000 SQD per node, in the registering account |
| Any action | ETH on Arbitrum One for gas |

The tool checks both before sending and stops if either is short. About
**0.05 ETH** covers 1000 registrations with wide margin; deregistering,
withdrawing and claiming cost far less.

Registration also needs an ERC-20 allowance, which the tool handles: it sends
one `approve` for exactly `bond × count` and never an unlimited amount. Through
a vesting contract even that is unnecessary — the contract approves each bond
immediately before use.

## Checking without doing anything

Two read-only commands that need no credential:

    ./sqd peer_ids.txt --action status --address 0xYourWallet
    .venv/bin/python tools/owners.py peer_ids.txt --csv owners.csv

The first reports where each peer ID sits in the worker lifecycle. The second
reports which account registered each one, and whether that account is a wallet
or a holding contract.

## Reading a run

Every run prints a plan and asks for confirmation before sending anything.
`--dry-run` stops after the plan.

Each run appends to `<file>.<network>.run.jsonl` and rewrites a CSV of confirmed
results. Re-running the same command skips whatever already succeeded, so an
interrupted run is resumed by repeating it — the tool prints the exact command.

Nothing is ever double-spent: before acting on a peer ID the tool checks its
current on-chain state, so a repeated run is safe.
