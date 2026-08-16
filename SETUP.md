# Setup

For the operator running `bulk_register.py` against a Fireblocks-held wallet.
One-time setup, then a single command per run.

## What you will need

| From your Fireblocks admin | Why |
| --- | --- |
| An **API user** with permission to initiate transactions | The tool authenticates as this user |
| Its **API key** (a UUID) | Identifies the API user |
| Its **RSA private key** file | Signs API requests. Generated when the API user is created, and never leaves your side |
| The **vault account ID** holding the SQD and the gas | Which account to act as |

Also required in that vault account:

- The chain's native asset enabled — **ETH on Arbitrum One** for mainnet. Without
  it the tool cannot see an address to act as.
- Enough **ETH for gas**: about 0.05 ETH covers 1000 registrations with wide
  margin.
- Enough **SQD**: 100,000 per node. 1000 nodes is 100,000,000 SQD.

> **The API key plus its RSA key is signing authority.** Combined with a policy
> that auto-approves these calls, whoever holds them can move funds from that
> vault. Treat them exactly as you would a private key: they belong with
> whoever owns the wallet, not with a contractor.

## Will every transaction need approving by hand?

That is the one question to settle before a large run. Fireblocks checks each
transaction against its Transaction Authorization Policy. Automated signing
needs an **API Co-Signer** deployed, plus a TAP rule that approves these calls.
Without both, every transaction waits for a human in the console — fine for one
node, untenable for a thousand.

Ask your Fireblocks admin to confirm before committing to a full run.

## Install

Needs **Python 3.10+** and **Node 18+**.

    python3.11 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    npm install @fireblocks/fireblocks-json-rpc

## Configure

Put the RSA key somewhere this directory can read, then:

    cp fireblocks.env.example fireblocks.env

Fill in four values: the API key, the path to the RSA key, the vault account ID,
and — for a sandbox only — the sandbox base URL. That file is gitignored.

The presence of `fireblocks.env` is what routes everything through Fireblocks.
Nothing else has to be exported, and no proxy has to be started by hand.

## Run

    ./sqd peer_ids.txt --action status
    ./sqd peer_ids.txt --network tethys --dry-run
    ./sqd peer_ids.txt --limit 1
    ./sqd peer_ids.txt

`./sqd` starts the Fireblocks proxy, runs the command through it, and shuts it
down again. Every argument is passed straight through, so anything in the README
works unchanged.

## Suggested order for a large run

1. `./sqd peer_ids.txt --action status` — reads only; confirms the wallet, the
   file, and how many nodes are actually outstanding.
2. `./sqd peer_ids.txt --dry-run` — the full plan, including the bond total and
   whether an approval is needed. Sends nothing.
3. `./sqd peer_ids.txt --limit 1` — one node, end to end. Confirm it appears in
   the SQD dashboard before going further.
4. `./sqd peer_ids.txt --limit 10` — ten more.
5. `./sqd peer_ids.txt` — the rest.

Each run skips what is already done, so this costs nothing but time. Do steps
1-3 on `--network tethys` first if you have testnet funds.

A freshly registered worker shows as `registering` until the next epoch begins,
which on mainnet can be up to ~14 days. That is the protocol, not a failed run;
`--action status` will say so.
