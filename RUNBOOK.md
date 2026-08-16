# Migration runbook

Moving 999 workers from their current registering accounts to a new one.

Read `SETUP.md` first. Every command below is run from this directory.

---

## What is being done, and why it takes a month

A worker's bond belongs to whichever account registered it, and that cannot be
transferred. Moving a bond therefore means **registering a replacement worker
and releasing the old one** — two separate lifecycles, not one move.

Releasing a bond is the slow part, and the delay is the protocol's, not the
tool's:

| Step | Delay |
| --- | --- |
| `deregister` takes effect at the next epoch | up to ~14 days |
| the bond then stays locked | a further ~14 days |
| `withdraw` releases it | immediate, once unlocked |

So **14 to 28 days** between deregistering a worker and recovering its 100,000
SQD, depending where in the epoch it lands. Nothing can shorten it.

The four phases below are ordered so nothing waits unnecessarily.

---

## Before you start

**Rewards should be claimed first.** They accrue to whoever owns the workers,
and claiming is quick and independent. See phase 1.

**The 999 replacements can be registered at any time** — they need no
coordination with the old workers. That does mean both sets are bonded at once,
so the new account needs its own 99,900,000 SQD while the old bonds are still
locked.

**Each file has exactly one signing account.** The tool reads the chain and
tells you which before asking for a credential. If you give it the wrong one it
stops immediately rather than sending anything.

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

## Phase 1 — Claim rewards

Independent of everything else, and worth doing first: rewards keep accruing
and are claimed per account, not per worker, so each is a single transaction.

`claim` needs no peer ID file at all — it sweeps every worker the account owns
in one transaction. Check what is owed with a dry run, which sends nothing:

    ./sqd --action claim --dry-run

For rewards held by a vesting contract, name the contract:

    ./sqd --action claim --via-vesting 0xB35728D533Ea887862b9Ed00cfe2B7F3D36A4e71 --dry-run

Drop `--dry-run` to claim. Expect `claimable: N SQD`, a confirmation prompt,
then one transaction; `nothing to claim` means that account has none.

**Twelve runs in total** — one for each of the five wallets, and one for each of
the seven vesting contracts. The contracts are listed as `creator` in
`batches/MANIFEST.csv`; you sign as the matching `signer`.

As of writing, roughly **120,000 SQD** was claimable across the twelve.

A claim for an account with several hundred workers is a large transaction —
the contract loops over every worker it owns — but well within limits.

---

## Phase 2 — Register the replacements

Needs: the new peer IDs, and the new account funded with 100,000 SQD per node
plus ETH for gas.

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
begins** — up to ~14 days. It is not earning yet and the run has not failed.
`--action status` says so explicitly.

### Naming

By default each node is given a generated name, plus a website and description.
To use your own names, put them in the file as `peer_id,name`. To apply a
pattern, use `--name-template 'yourprefix-{n:03d}'`. To register with a name
only and no website or description:

    ./sqd new_peer_ids.txt --website '' --description ''

---

## Phase 3 — Deregister the old workers

Eleven runs, one per file, in any order. Each takes one transaction per worker.

Look first:

    ./sqd batches/peers-03-eoa-cd65B8Be-20.txt --action deregister --dry-run

It reports which account must sign, how many workers are actionable, and the
gas. Nothing is sent.

Then run it without `--dry-run`. You will be asked for that account's
credential, then to confirm.

**Start with `peers-03` (20 workers).** It is small enough to check afterwards
and simple enough that a problem is unambiguous. Then work down the list.

After each run:

    ./sqd batches/peers-03-eoa-cd65B8Be-20.txt --action status --address <that account>

Every worker in the file should read `deregistering`. That means the call
landed; the worker keeps running until the epoch ends.

**Note the date.** Withdrawal cannot happen for 14–28 days from here, and the
tool will tell you exactly when.

---

## Phase 4 — Withdraw, 14 to 28 days later

This releases 100,000 SQD per worker back to the account that registered it.

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

- `*.registered.csv` — peer ID, name, transaction hash and block for each new worker
- `*.deregistered.csv` and `*.withdrawn.csv` — the same for the old ones
- the `.run.jsonl` logs

Those are the audit trail. Every transaction hash in them can be pasted into
`https://arbiscan.io/tx/<hash>`.
