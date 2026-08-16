# Migration runbook

Moving 999 workers from their current registering accounts to a new one.

Read `SETUP.md` first. Every command below is run from this directory.

---

## What is being done, and why it takes two weeks

A worker's bond belongs to whichever account registered it, and that cannot be
transferred. Moving a bond therefore means **registering a replacement worker
and releasing the old one** — two separate lifecycles, not one move.

Only one step is slow, and the delay is the protocol's rather than the tool's:

| Step | When it takes effect |
| --- | --- |
| `deregister` | the next epoch — about **20 minutes** |
| the bond then stays locked | **~13.9 days** |
| `withdraw` | immediately, once unlocked |

So the worker stops earning within the hour, and its 100,000 SQD becomes
recoverable about **two weeks** later. Nothing can shorten the lock.

Two similarly named on-chain values are easy to confuse, and only one of them
is the fortnight: epochs are 100 blocks (~20 minutes), while the lock period is
99,999 blocks (~13.9 days).

## Before you start

**The order matters, and it is deregister, claim, register.** Deregistering
first frees each host so its replacement key can be deployed; claiming after
that captures every reward the old worker ever earned; registering last puts the
replacements live.

**Both sets are bonded at once.** The old bonds stay locked for ~14 days after
deregistration, so the new account needs its own 99,900,000 SQD during that
window — the old SQD is not available to fund the new registrations.

**Each file has exactly one signing account.** The tool reads the chain and
tells you which before asking for a credential. If you give it the wrong one it
stops immediately rather than sending anything.

**These accounts are not all in the same place.** The account registering the
replacements is in Fireblocks; the nine that hold the existing workers may not
be. Leave `fireblocks.env` in place throughout, and add `--signer local` to any
run whose account is a key you hold:

    ./sqd batches/peers-01-eoa-5CF5A099-275.txt --action deregister --signer local

That skips Fireblocks entirely for that run and asks for the key instead. If you
forget it, the run stops and says which account it needs and that `--signer
local` is how to provide it — nothing is sent either way.

Dry runs need no credential at all, so the whole pre-flight works regardless.

### The eleven files and who signs them

| File | Workers | Signed by |
| --- | --- | --- |
| `peers-01-eoa-5CF5A099-275.txt` | 275 | `0x5CF5A099…54c41b` |
| `peers-02-eoa-43d6A791-65.txt` | 65 | `0x43d6A791…D8478f` |
| `peers-03-eoa-cd65B8Be-20.txt` | 20 | `0xcd65B8Be…037aD3` |
| `peers-04-eoa-dADB8013-1.txt` | 1 | `0xdADB8013…089744` |
| `peers-05-vesting-A205c6e3-188.txt` | 188 | `0xA205c6e3…7f27F3` |
| `peers-06-vesting-80c88d21-130.txt` | 130 | `0x80c88d21…4B9aAd` |
| `peers-07-vesting-678d14c7-100.txt` | 100 | `0x678d14c7…9D1942` |
| `peers-08-vesting-2aA9ADb8-100.txt` | 100 | `0x2aA9ADb8…983751` |
| `peers-09-vesting-51FF3579-61.txt` | 61 | `0x51FF3579…92b629` |
| `peers-10-vesting-dADB8013-49.txt` | 49 | `0xdADB8013…089744` |
| `peers-11-vesting-5CF5A099-10.txt` | 10 | `0x5CF5A099…54c41b` |

Nine accounts, eleven files: three accounts appear twice, once holding workers
directly and once as the owner of a vesting contract. Same key, two runs.

Files marked `vesting` hold workers registered by a contract. The contract is
the worker's owner, so the calls are routed through it automatically — you sign
as the contract's owner, shown above.

---

## Phase 1 — Deregister the old workers

Eleven runs, one per file, in any order. Each takes one transaction per worker.

Look first:

    ./sqd batches/peers-03-eoa-cd65B8Be-20.txt --action deregister --dry-run

It reports which account must sign, how many workers are actionable, and the
gas. Nothing is sent, and **a dry run needs no credential at all** — the tool
reads the workers to learn whose they are, so you can inspect every file before
unlocking anything.

A dry run reports problems rather than stopping at the first, and always exits
0. A line beginning `SHORTFALL:` means a real run would refuse:

    SHORTFALL:   insufficient ETH for gas: need up to 0.0002 ETH, hold 0 ETH

Expect it to take a while. Each worker costs two or three chain reads, roughly
a second each against a public endpoint — so about 25 seconds for the 20-worker
file and five minutes for the 275-worker one. A private RPC endpoint passed with
`--rpc-url` is markedly faster, and worth using for the real runs.

Then run it without `--dry-run`. You will be asked for that account's
credential, then to confirm.

**Start with `peers-03` (20 workers).** It is small enough to check afterwards
and simple enough that a problem is unambiguous. Then work down the list.

After each run:

    ./sqd batches/peers-03-eoa-cd65B8Be-20.txt --action status --address <that account>

Every worker in the file should read `deregistering`. That means the call
landed; the worker keeps running until the epoch ends.

**Note the date.** The bond unlocks about 13.9 days from the epoch the
deregistration landed in, and `--action status` will tell you exactly when.

This is done first because the node hosts are reused: the replacement worker's
key cannot be deployed to a host until the old identity has been released.

---

## Phase 2 — Claim rewards

Done **after** deregistering, not before. A deregistered worker stops earning
at the next epoch, so claiming afterwards captures everything it ever earned in
one go. Claiming first would leave a final slice of rewards accruing between the
claim and the deregistration — not lost, but easily forgotten once the migration
has moved on.

Rewards are claimed per account rather than per worker, so each account is a
single transaction regardless of how many workers it holds.

Pass the same batch file used for deregistration. `claim` does not act on the
peer IDs — it sweeps every worker the account owns in one transaction — but the
file tells it whose rewards to claim, so no address or contract has to be looked
up:

    ./sqd batches/peers-01-eoa-5CF5A099-275.txt --action claim --dry-run

    registered by: 0x5CF5A099A9089b31689B16cd83d06b6ce154c41b
    claimable:     35005.34 SQD

A vesting-held file is handled the same way, with the contract and its owner
detected automatically:

    ./sqd batches/peers-09-vesting-51FF3579-61.txt --action claim --dry-run

    rewards held by: 0xC99B581a…7094f (a vesting contract)
    must be signed by its owner: 0x51FF3579…2b629

Neither needs a credential. Drop `--dry-run` to claim, and you will be asked for
the account named.

**Eleven runs, one per file** — the same files as phase 1, in the same order.
Each covers one account, and no account appears twice.

As of writing, roughly **120,000 SQD** was claimable across the twelve.

A claim for an account with several hundred workers is a large transaction —
the contract loops over every worker it owns — but well within limits.

---

## Phase 3 — Register the replacements

Needs: the new peer IDs, and the new account funded with 100,000 SQD per node
plus ETH for gas.

Done after deregistration because the node hosts are reused — a replacement key
cannot go onto a host still running the old identity.

Check the plan first. This sends nothing:

    ./sqd new_peer_ids.txt --dry-run

Read the `bond total` and `to register` lines and confirm they are what you
expect. Then register one node and confirm it appears in the SQD dashboard:

    ./sqd new_peer_ids.txt --limit 1

Then ten:

    ./sqd new_peer_ids.txt --limit 10

Then the rest:

    ./sqd new_peer_ids.txt

Each run skips what is already done, so this staging costs nothing but time.

**A newly registered worker shows as `registering` until the next epoch
begins** — about 20 minutes. It is not earning yet and the run has not failed.
`--action status` says so explicitly.

### Naming

By default each node is given a generated name, plus a website and description.
To use your own names, put them in the file as `peer_id,name`. To apply a
pattern, use `--name-template 'yourprefix-{n:03d}'`. To register with a name
only and no website or description:

    ./sqd new_peer_ids.txt --website '' --description ''

---

## Phase 4 — Withdraw, about two weeks later

This releases 100,000 SQD per worker back to the account that registered it —
99,900,000 SQD in total across the eleven files.

The wait is ~13.9 days from the epoch in which each deregistration landed, so
files deregistered on the same day unlock on the same day.

Check readiness at any time — read-only, no credential:

    ./sqd batches/peers-03-eoa-cd65B8Be-20.txt --action status --address <that account>

Each worker will read one of:

| State | Meaning |
| --- | --- |
| `deregistering` | Still running; waiting for the epoch to end |
| `locked` | Inactive; the report says how many days remain |
| `withdrawable` | Ready — its bond can be recovered now |

When the report shows `withdrawable`, and states the total SQD ready:

    ./sqd batches/peers-03-eoa-cd65B8Be-20.txt --action withdraw --dry-run
    ./sqd batches/peers-03-eoa-cd65B8Be-20.txt --action withdraw

Workers that are not yet ready are skipped rather than attempted, so running
early is harmless — it simply reports how many are still locked. You can run
the same file repeatedly as tranches unlock.

---

## If a run stops

Every stop is safe. Nothing is ever half-sent, and no bond can be paid twice.

| What you see | What it means | What to do |
| --- | --- | --- |
| `insufficient SQD` / `insufficient ETH` | Checked before sending; nothing was sent | Fund the account and re-run |
| `can only be acted on by 0x…` | The wrong credential for this file | Use the account named |
| `nothing to register` / `nothing to deregister` | Already done, or not yet eligible | Run `--action status` to see why |
| `stopped after 3 consecutive failures` | Something systemic | Read the errors; fix; re-run |
| A `pending` record, run stopped | A transaction's outcome is unknown | Wait for it to settle, then re-run: the tool re-checks on chain |

**To resume, re-run the same command.** Anything already done is skipped. The
tool prints the exact command to continue.

Two files are written next to each peer ID file: a `.run.jsonl` log of every
attempt, and a CSV of confirmed results. Keep both — the log is what makes a
re-run safe, and the CSV is the record of what was done.

---

## What to keep

At the end you should have, per phase:

- `*.deregistered.csv` — peer ID, transaction hash and block for each old worker
- `*.registered.csv` — the same for each replacement, plus its name
- `*.withdrawn.csv` — the same, once the bonds come back
- the `.run.jsonl` logs

Those are the audit trail. Every transaction hash in them can be pasted into
`https://arbiscan.io/tx/<hash>`.
