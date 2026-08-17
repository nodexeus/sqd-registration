#!/usr/bin/env python3
"""Explain why a worker is not earning, from its on-chain reward history.

    .venv/bin/python tools/health.py peer_ids.txt [--network mainnet]
    .venv/bin/python tools/health.py 12D3KooW...   [--days 4]

Read-only: no credential, nothing sent.

A worker that looks offline is usually still registered and still bonded, so
the registry says nothing useful. What does say something is the reward stream:
the network pays each worker on a rotation, and a worker that stops earning
stops appearing in those payouts. This reads that history and reports when the
worker last earned, how long it has been out, and -- the part that decides who
should be looking at it -- whether the rest of its rotation cohort went out at
the same time.

Not earning for a few days is not necessarily a fault. Workers do rejoin after
a cohort-wide event, sometimes days later, so the duration and the cohort
context matter more than the fact of a zero.

Workers also drop out continuously, so the number in a slot that are not
earning is not evidence of anything on its own -- a slot accumulates unrelated
casualties. What identifies one event is a group that stopped in the SAME
period across registering accounts with nothing to do with each other, which is
what the report keys on.

Use --days 8 or more for that. A short window hides workers that stopped before
it starts, and those are exactly the ones already down.
"""

import argparse
import csv
import datetime as dt
import os
import collections
import statistics
import sys
from collections import defaultdict

# Run from anywhere: this lives one level below the package it imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import base58
from web3 import Web3

from sqdreg.networks import NETWORKS
from sqdreg.peerids import PeerIdError, decode_peer_id, parse_file
from sqdreg.registry import Registry
from tools.delegation import batched_call

E18 = 10**18
L1_BLOCK_SECONDS = 12
LOG_CHUNK = 50_000
L2_BLOCKS_PER_DAY = 4 * 60 * 60 * 24  # Arbitrum ~4 blocks/s

DISTRIBUTED = (
    "Distributed(uint256,uint256,uint256[],uint256[],uint256[])"
)
DISTRIBUTED_ABI = [
    {"anonymous": False, "inputs": [
        {"indexed": False, "name": "fromBlock", "type": "uint256"},
        {"indexed": False, "name": "toBlock", "type": "uint256"},
        {"indexed": False, "name": "recipients", "type": "uint256[]"},
        {"indexed": False, "name": "workerRewards", "type": "uint256[]"},
        {"indexed": False, "name": "stakerRewards", "type": "uint256[]"}],
     "name": "Distributed", "type": "event"},
]

EARNING = "earning"
ZEROED = "zero-reward"
DROPPED = "dropped from payout set"
UNSEEN = "not seen in window"


def fetch_epochs(w3, network, days, progress=None):
    """Every reward distribution in the last `days`, oldest first."""
    address = w3.to_checksum_address(network.rewards_distribution)
    contract = w3.eth.contract(address=address, abi=DISTRIBUTED_ABI)
    topic = "0x" + w3.keccak(text=DISTRIBUTED).hex().lstrip("0x")

    tip = w3.eth.block_number
    span = int(days * L2_BLOCKS_PER_DAY)
    logs = []
    for start in range(tip - span, tip, LOG_CHUNK):
        logs += w3.eth.get_logs({
            "address": address, "topics": [topic],
            "fromBlock": start, "toBlock": min(start + LOG_CHUNK - 1, tip),
        })
        if progress:
            progress(min(start + LOG_CHUNK, tip) - (tip - span), span)

    epochs = []
    for log in logs:
        args = contract.events.Distributed().process_log(log)["args"]
        epochs.append({
            "from": args["fromBlock"],
            "rewards": dict(zip(args["recipients"], args["workerRewards"])),
        })
    epochs.sort(key=lambda e: e["from"])
    return epochs


def rotation_period(epochs, history):
    """How far apart a worker's payouts are, in L1 blocks.

    Derived rather than assumed: the network pays a subset of workers each
    epoch, and a worker absent from one distribution is normally just waiting
    for its turn. Hardcoding the cycle would misreport that as an outage the
    day the schedule changes.
    """
    gaps = []
    for seen in history.values():
        blocks = sorted(seen)
        gaps += [b - a for a, b in zip(blocks, blocks[1:])]
    if not gaps:
        return None
    return statistics.mode(gaps)


def build_history(epochs):
    """{worker_id: {epoch_from_block: reward_wei}}"""
    history = defaultdict(dict)
    for epoch in epochs:
        for worker_id, reward in epoch["rewards"].items():
            history[worker_id][epoch["from"]] = reward
    return history


def assess(worker_id, history, epochs, period):
    """What state this worker is in, and since when."""
    seen = history.get(worker_id, {})
    if not seen:
        return {"state": UNSEEN, "slot": None, "last_paid": None,
                "last_amount": 0, "zeros": 0, "missed": 0, "appearances": 0}

    slot = min(seen) % period if period else None
    paid = sorted(b for b, v in seen.items() if v > 0)
    last_paid = paid[-1] if paid else None

    # Zero payouts since the last real one, and slots missed entirely since.
    after = sorted(b for b in seen if last_paid is None or b > last_paid)
    zeros = len(after)
    slot_epochs = [
        e["from"] for e in epochs
        if period and e["from"] % period == slot
    ]
    missed = len([
        b for b in slot_epochs
        if b > (max(seen) if seen else 0)
    ])

    # Missing your own slots is checked first. A worker can go straight from
    # earning to absent without a zero payout in between, and testing "was the
    # last thing I saw a payment?" first would call that healthy.
    if missed:
        state = DROPPED
    elif zeros:
        state = ZEROED
    else:
        state = EARNING
    return {"state": state, "slot": slot, "last_paid": last_paid,
            "last_amount": seen.get(last_paid, 0) if last_paid else 0,
            "zeros": zeros, "missed": missed, "appearances": len(seen)}


def worker_details_for(w3, network, worker_ids, progress=None):
    """{worker_id: (peer_id, creator)} for any worker, including strangers'.

    getWorker() carries the peer ID as the same identity multihash the input
    file uses, so base58 gives the string back. Batched, because a network-wide
    event can put a few hundred workers in the same state.
    """
    from tools.delegation import MULTICALL3, MULTICALL3_ABI

    registration = w3.to_checksum_address(network.worker_registration)
    contract = Registry(w3, network, "0x" + "0" * 40).contract
    worker_ids = list(worker_ids)

    def decode(data):
        worker = w3.codec.decode(
            ["(address,bytes,uint256,uint128,uint128,string)"], data
        )[0]
        return worker[1], worker[0]          # peerId bytes, creator

    out = {}
    if len(w3.eth.get_code(w3.to_checksum_address(MULTICALL3))) <= 2:
        for worker_id in worker_ids:
            worker = contract.functions.getWorker(worker_id).call()
            out[worker_id] = (worker[1], worker[0])
    else:
        multicall = w3.eth.contract(
            address=w3.to_checksum_address(MULTICALL3), abi=MULTICALL3_ABI
        )
        for start in range(0, len(worker_ids), 250):
            chunk = worker_ids[start:start + 250]
            calls = [
                (registration, False, contract.encode_abi("getWorker", args=[w]))
                for w in chunk
            ]
            for worker_id, (ok, data) in zip(
                chunk, multicall.functions.aggregate3(calls).call()
            ):
                out[worker_id] = decode(data) if ok else (b"", "")
            if progress:
                progress("named", len(out), len(worker_ids))
    return {
        worker_id: (base58.b58encode(raw).decode() if raw else "?", creator)
        for worker_id, (raw, creator) in out.items()
    }


def cohort_members(history, epochs, period, slot, verdicts, states):
    """Every worker sharing this slot whose state is in `states`."""
    return sorted(
        w for w, seen in history.items()
        if period and seen and min(seen) % period == slot
        and verdicts[w]["state"] in states
    )


def cutoff_groups(history, epochs, period, slot, verdicts):
    """{last_paid_block: [worker_id, ...]} for the slot's not-earning members.

    Counting how many workers share a state says nothing about whether they
    stopped together -- workers drop out continuously, so a slot accumulates
    unrelated casualties. Only a shared cutoff is evidence of one event.
    """
    groups = defaultdict(list)
    for worker_id in cohort_members(
        history, epochs, period, slot, verdicts, {ZEROED, DROPPED}
    ):
        groups[verdicts[worker_id]["last_paid"]].append(worker_id)
    return groups


def cohort_state(history, epochs, period, slot, verdicts):
    """How many workers share this slot, and how many are in the same trouble.

    This is what separates "your node" from "the network": a worker that
    stopped alone is a worker to go and look at, while a worker that stopped
    alongside its whole cohort is somebody else's incident.
    """
    members = [
        w for w, seen in history.items()
        if period and seen and min(seen) % period == slot
    ]
    unwell = [w for w in members if verdicts[w]["state"] in (ZEROED, DROPPED)]
    return len(members), len(unwell)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("peer_ids", help="a peer ID file, or a single peer ID")
    parser.add_argument("--network", choices=sorted(NETWORKS), default="mainnet")
    parser.add_argument("--days", type=float, default=8.0,
                        help="how much reward history to read (default 8). "
                             "A shorter window hides workers that stopped "
                             "before it starts.")
    parser.add_argument("--cohort", action="store_true",
                        help="list every worker sharing the state, including "
                             "other operators' -- what a network-side report needs")
    parser.add_argument("--csv", help="also write a per-peer CSV here")
    args = parser.parse_args(argv)

    network = NETWORKS[args.network]
    w3 = Web3(Web3.HTTPProvider(network.rpc_url))

    # Accept a bare peer ID so checking one node needs no file.
    if os.path.exists(args.peer_ids):
        try:
            entries, duplicates = parse_file(args.peer_ids)
        except PeerIdError as exc:
            sys.exit(f"error: {exc}")
        for warning in duplicates:
            print(f"warning: {warning}", file=sys.stderr)
    else:
        try:
            decode_peer_id(args.peer_ids)
        except PeerIdError:
            sys.exit(
                f"error: {args.peer_ids!r} is neither a readable file nor a "
                "peer ID"
            )
        from sqdreg.peerids import parse_lines
        entries, _ = parse_lines([args.peer_ids])

    print(f"network: {network.name}    peer IDs: {len(entries)}")

    ids = batched_call(
        w3, w3.to_checksum_address(network.worker_registration),
        Registry(w3, network, "0x" + "0" * 40).contract, "workerIds",
        [e.peer_bytes for e in entries], "resolved",
        lambda label, done, total: print(f"  {label} {done}/{total}", flush=True),
    )
    worker_ids = dict(zip((e.peer_id for e in entries), ids))

    epochs = fetch_epochs(
        w3, network, args.days,
        lambda done, total: print(
            f"  reward history {min(100, int(100 * done / total))}%", flush=True
        ),
    )
    if not epochs:
        sys.exit("error: no reward distributions found in that window")

    history = build_history(epochs)
    period = rotation_period(epochs, history)
    verdicts = {w: assess(w, history, epochs, period) for w in history}
    for wid in ids:
        if wid and wid not in verdicts:
            verdicts[wid] = assess(wid, history, epochs, period)

    # Wall clock, so an incident can be lined up against somebody else's logs.
    block = w3.eth.get_block("latest")
    now = block["timestamp"]
    # Arbitrum reports this as a hex string; a bare int() on it silently
    # becomes a TypeError several frames later, so normalise it here.
    raw = block.get("l1BlockNumber")
    if raw is None:
        l1_now = w3.eth.block_number
    else:
        l1_now = int(raw, 16) if isinstance(raw, str) else int(raw)

    def when(l1_block):
        return dt.datetime.utcfromtimestamp(
            now - (l1_now - l1_block) * L1_BLOCK_SECONDS
        )

    def days_since(l1_block):
        return (l1_now - l1_block) * L1_BLOCK_SECONDS / 86400

    print(
        f"window:  {len(epochs)} distributions, "
        f"{when(epochs[0]['from']):%Y-%m-%d %H:%M} -> "
        f"{when(epochs[-1]['from']):%Y-%m-%d %H:%M} UTC"
    )
    if period:
        print(f"rotation: each worker is paid every {period} L1 blocks "
              f"(~{period * L1_BLOCK_SECONDS / 3600:.0f}h)\n")

    rows = []
    affected_slots = {}          # slot -> set of states seen among the input
    for entry in entries:
        wid = worker_ids[entry.peer_id]
        if not wid:
            print(f"{entry.peer_id}\n  not registered on {network.name}\n")
            rows.append((entry.peer_id, "", "unregistered", "", "", "", ""))
            continue

        v = verdicts[wid]
        creator, _, bond, _, dereg_at, meta = (
            Registry(w3, network, "0x" + "0" * 40)
            .contract.functions.getWorker(wid).call()
        )
        name = ""
        if meta:
            try:
                import json
                name = json.loads(meta).get("name", "")
            except Exception:
                name = meta[:40]

        print(f"worker {wid}  {name}")
        print(f"  registration:  "
              f"{'DEREGISTERING' if dereg_at else 'registered'}, "
              f"bond {bond / E18:,.0f} SQD")
        if v["state"] == UNSEEN:
            print("  reward history: never appeared in this window — either "
                  "newly registered\n                  or out longer than "
                  f"{args.days:g} days (try --days)\n")
            rows.append((entry.peer_id, wid, UNSEEN, "", "", "", ""))
            continue

        print(f"  rotation slot: {v['slot']}   payouts seen: {v['appearances']}")
        if v["last_paid"]:
            print(f"  last earned:   {when(v['last_paid']):%Y-%m-%d %H:%M} UTC "
                  f"({days_since(v['last_paid']):.1f} days ago, "
                  f"{v['last_amount'] / E18:.2f} SQD)")
        else:
            print("  last earned:   never, in this window")

        members, unwell = cohort_state(
            history, epochs, period, v["slot"], verdicts
        )
        if v["state"] == EARNING:
            print("  status:        earning normally\n")
        else:
            detail = f"{v['zeros']} zero payout(s)"
            if v["missed"]:
                detail += f", then dropped from the last {v['missed']} payout(s)"
            print(f"  status:        NOT EARNING — {detail}")
            print(f"  cohort:        {unwell} of {members} workers in this "
                  f"rotation slot are not earning")
            affected_slots.setdefault(v["slot"], set()).update({ZEROED, DROPPED})

            # Workers drop out continuously, so a slot accumulates unrelated
            # casualties. What distinguishes an incident from background churn
            # is a group that stopped in the SAME period, across accounts that
            # have nothing to do with each other.
            groups = cutoff_groups(history, epochs, period, v["slot"], verdicts)
            peers = [w for w in groups.get(v["last_paid"], []) if w != wid]
            if v["last_paid"] and len(groups) > 1:
                spread = ", ".join(
                    f"{len(g)}@{when(b):%m-%d %H:%M}" if b else f"{len(g)}@never"
                    for b, g in sorted(groups.items(), key=lambda kv: kv[0] or 0)
                )
                print(f"  when they went: {spread}")
            if peers:
                owners = {
                    c.lower() for _, c in worker_details_for(
                        w3, network, peers + [wid]
                    ).values()
                }
                print(f"  same cutoff:   {len(peers)} other worker(s) stopped "
                      f"in this same period,\n                 across "
                      f"{len(owners)} distinct registering account(s)")
                if len(owners) > 1:
                    print("  reading:       a shared cutoff across unrelated "
                          "accounts is a network-side\n                 event, "
                          "not this node. Workers do rejoin, sometimes after\n"
                          "                 several days.")
                else:
                    print("  reading:       they stopped together but all "
                          "belong to one account, so a\n                 "
                          "shared cause on your side is not ruled out.")
            else:
                print("  same cutoff:   no other worker in this slot stopped "
                      "in that period")
                print("  reading:       it stopped alone while the rest of its "
                      "cohort carried on,\n                 so the node itself "
                      "is worth checking.")
            print()

        rows.append((
            entry.peer_id, wid, v["state"], v["slot"],
            v["last_paid"] or "",
            f"{days_since(v['last_paid']):.2f}" if v["last_paid"] else "",
            f"{unwell}/{members}",
        ))

    if args.cohort and affected_slots:
        my_creators = {
            c.lower() for c in (
                worker_details_for(w3, network, [w for w in ids if w])[w][1]
                for w in ids if w
            )
        }
        print("\ncohort detail — every worker sharing these states.")
        print("  \"yours\" means it shares a registering account with "
              "something you passed in,\n  so pass the whole fleet to label "
              "all of it.")
        for slot in sorted(affected_slots):
            members = cohort_members(
                history, epochs, period, slot, verdicts, affected_slots[slot]
            )
            detail = worker_details_for(w3, network, members)
            print(f"\n  rotation slot {slot} — {len(members)} worker(s):")
            for worker_id in members:
                v = verdicts[worker_id]
                stamp = (
                    f"last earned {when(v['last_paid']):%Y-%m-%d %H:%M} UTC "
                    f"({days_since(v['last_paid']):.1f}d)"
                    if v["last_paid"] else "never earned in window"
                )
                peer_id, creator = detail[worker_id]
                whose = "yours" if creator.lower() in my_creators else "other"
                print(f"    {peer_id}  worker {worker_id:<6} {whose:<6} "
                      f"{v['state']:<24} {stamp}")
        # A shared last-earned timestamp is the evidence that this was one
        # event rather than a coincidence, so make it impossible to miss.
        stamps = collections.Counter(
            verdicts[w]["last_paid"]
            for slot in affected_slots
            for w in cohort_members(
                history, epochs, period, slot, verdicts, affected_slots[slot]
            )
            if verdicts[w]["last_paid"]
        )
        if stamps:
            block, count = stamps.most_common(1)[0]
            if count > 2:
                print(f"\n  {count} of them last earned in the same period "
                      f"(L1 {block}, {when(block):%Y-%m-%d %H:%M} UTC).")
                print("  A shared cutoff across separate accounts is one "
                      "event, not independent\n  node failures — that is the "
                      "part worth reporting.")

    if len(entries) > 1:
        counts = {}
        for row in rows:
            counts[row[2]] = counts.get(row[2], 0) + 1
        print("summary:")
        for state, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>5}  {state}")

    if args.csv:
        with open(args.csv, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("peer_id", "worker_id", "state", "rotation_slot",
                             "last_paid_l1_block", "days_since_paid",
                             "cohort_affected"))
            writer.writerows(rows)
        print(f"\nwritten to {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
