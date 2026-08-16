#!/usr/bin/env python3
"""Report how much third-party SQD is delegated to each peer ID.

    .venv/bin/python tools/delegation.py peer_ids.txt [--network mainnet]

Read-only: no credential, nothing sent. Delegation is keyed to a worker's
numeric id, and a replacement worker gets a new one, so nothing here transfers
across a migration. The total is what delegators would have to re-delegate by
hand, which is why it is worth knowing before deregistering rather than after.
"""

import argparse
import csv
import os
import statistics
import sys

# Run from anywhere: this lives one level below the package it imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web3 import Web3

from sqdreg.networks import NETWORKS
from sqdreg.peerids import PeerIdError, parse_file
from sqdreg.registry import Registry

E18 = 10**18

# Deployed at the same address on every chain SQD runs on, Arbitrum One and
# Sepolia included. Reading 1000 workers one call at a time takes ~17 minutes
# against a public endpoint; batched it is a handful of requests.
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
CHUNK = 250

ROUTER_ABI = [
    {"inputs": [], "name": "router", "outputs": [{"type": "address"}],
     "stateMutability": "view", "type": "function"},
]
STAKING_LOOKUP_ABI = [
    {"inputs": [], "name": "staking", "outputs": [{"type": "address"}],
     "stateMutability": "view", "type": "function"},
]
STAKING_ABI = [
    {"inputs": [{"type": "uint256"}], "name": "delegated",
     "outputs": [{"type": "uint256"}], "stateMutability": "view",
     "type": "function"},
]
MULTICALL3_ABI = [
    {"inputs": [{"components": [
        {"name": "target", "type": "address"},
        {"name": "allowFailure", "type": "bool"},
        {"name": "callData", "type": "bytes"}], "name": "calls",
        "type": "tuple[]"}],
     "name": "aggregate3",
     "outputs": [{"components": [
         {"name": "success", "type": "bool"},
         {"name": "returnData", "type": "bytes"}], "type": "tuple[]"}],
     "stateMutability": "payable", "type": "function"},
]


def staking_address(w3, network):
    """Find the Staking contract by asking the chain, not a constant here.

    WorkerRegistration knows the Router and the Router knows every subsystem,
    so this keeps working across networks and redeployments without another
    address to maintain.
    """
    registration = w3.eth.contract(
        address=w3.to_checksum_address(network.worker_registration),
        abi=ROUTER_ABI,
    )
    router = registration.functions.router().call()
    return w3.eth.contract(
        address=w3.to_checksum_address(router), abi=STAKING_LOOKUP_ABI
    ).functions.staking().call()


def batched_call(w3, target, contract, fn, args_list, label, progress=None):
    """Return one uint256 per entry in args_list, batched through Multicall3.

    Both reads this tool needs are the same shape: a thousand independent
    single-argument view calls. Serially that is ~1s each against a public
    endpoint, which is twenty minutes for the pair; batched it is seconds.
    """
    multicall = w3.eth.contract(
        address=w3.to_checksum_address(MULTICALL3), abi=MULTICALL3_ABI
    )
    have_multicall = len(w3.eth.get_code(w3.to_checksum_address(MULTICALL3))) > 2

    out = []
    if not have_multicall:
        # No Multicall3 on this chain. Slow rather than broken.
        for arg in args_list:
            out.append(getattr(contract.functions, fn)(arg).call())
            if progress:
                progress(label, len(out), len(args_list))
        return out

    for start in range(0, len(args_list), CHUNK):
        chunk = args_list[start:start + CHUNK]
        calls = [
            (target, False, contract.encode_abi(fn, args=[arg])) for arg in chunk
        ]
        for arg, (ok, data) in zip(
            chunk, multicall.functions.aggregate3(calls).call()
        ):
            if not ok:
                sys.exit(f"error: {fn}({arg!r}) reverted")
            out.append(int.from_bytes(data, "big"))
        if progress:
            progress(label, len(out), len(args_list))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("peer_id_file")
    parser.add_argument("--network", choices=sorted(NETWORKS), default="mainnet")
    parser.add_argument("--csv", help="also write a per-peer CSV here")
    args = parser.parse_args(argv)

    network = NETWORKS[args.network]
    w3 = Web3(Web3.HTTPProvider(network.rpc_url))
    registry = Registry(w3, network, "0x" + "0" * 40)

    try:
        entries, duplicates = parse_file(args.peer_id_file)
    except PeerIdError as exc:
        sys.exit(f"error: {exc}")
    for warning in duplicates:
        print(f"warning: {warning}", file=sys.stderr)

    staking = w3.to_checksum_address(staking_address(w3, network))
    print(f"network: {network.name}    peer IDs: {len(entries)}")
    print(f"staking: {staking}\n")

    def progress(label, done, total):
        print(f"  {label} {done}/{total}", flush=True)

    ids = batched_call(
        w3,
        w3.to_checksum_address(network.worker_registration),
        registry.contract,
        "workerIds",
        [e.peer_bytes for e in entries],
        "resolved",
        progress,
    )
    worker_ids = dict(zip((e.peer_id for e in entries), ids))
    registered = [wid for wid in ids if wid]
    if not registered:
        print("none of these peer IDs are registered, so nothing is delegated.")
        return 0

    values = batched_call(
        w3,
        staking,
        w3.eth.contract(address=staking, abi=STAKING_ABI),
        "delegated",
        registered,
        "read",
        progress,
    )
    amounts = dict(zip(registered, values))

    rows = [
        (peer_id, wid, amounts.get(wid, 0))
        for peer_id, wid in worker_ids.items()
    ]
    total = sum(amounts.values())
    delegated = sorted(v for v in amounts.values() if v > 0)

    print(f"\ntotal delegated: {total / E18:,.2f} SQD")
    print(f"workers with delegation: {len(delegated)}/{len(registered)}")
    if delegated:
        print(f"largest single worker:   {delegated[-1] / E18:,.2f} SQD")
        print(
            "median (delegated only): "
            f"{statistics.median(delegated) / E18:,.2f} SQD"
        )
    unregistered = len(entries) - len(registered)
    if unregistered:
        print(f"not registered (ignored): {unregistered}")

    if total:
        print(
            "\nDelegation is keyed to the worker id, and a replacement worker "
            "gets a new one.\n"
            "  It does not move with a migration and is not paused: every "
            "delegator has to\n"
            "  re-delegate to the new peer ID by hand. Treat the total above "
            "as what would\n"
            "  have to be won back, not as a balance that is waiting somewhere."
        )

    if args.csv:
        with open(args.csv, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("peer_id", "worker_id", "delegated_sqd"))
            # No thousands separators: a comma inside a value forces quoting,
            # which trips naive importers and shell tools like cut and awk.
            writer.writerows(
                (peer_id, wid or "", f"{wei / E18:.18f}".rstrip("0").rstrip(".") or "0")
                for peer_id, wid, wei in rows
            )
        print(f"\nwritten to {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
