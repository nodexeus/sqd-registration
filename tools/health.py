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
"""

import argparse
import csv
import datetime as dt
import os
import statistics
import sys
from collections import defaultdict

# Run from anywhere: this lives one level below the package it imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    parser.add_argument("--days", type=float, default=4.0,
                        help="how much reward history to read (default 4)")
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
                  f"rotation slot are in the same state")
            share = unwell / members if members else 0
            if share > 0.02:
                print("  reading:       the cohort went out together, so this "
                      "is a network-side\n                 event rather than "
                      "this node. Workers do rejoin, sometimes\n"
                      "                 after several days.")
            else:
                print("  reading:       this node stopped while its cohort "
                      "kept earning, so it\n                 is worth looking "
                      "at the node itself.")
            print()

        rows.append((
            entry.peer_id, wid, v["state"], v["slot"],
            v["last_paid"] or "",
            f"{days_since(v['last_paid']):.2f}" if v["last_paid"] else "",
            f"{unwell}/{members}",
        ))

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
