#!/usr/bin/env python3
"""Report who registered each peer ID, and whether that owner is a contract.

    .venv/bin/python tools/owners.py peer_ids.txt [--network mainnet]

Read-only: no credential, nothing sent. The question it answers is whether
deregister() and withdraw() can be called directly or must be wrapped in a
vesting contract's execute(), because both require `worker.creator ==
msg.sender` and a vesting-registered worker's creator is the contract.
"""

import argparse
import csv
import os
import sys
from collections import Counter

# Run from anywhere: this lives one level below the package it imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web3 import Web3

from sqdreg.networks import NETWORKS
from sqdreg.peerids import PeerIdError, parse_file
from sqdreg.registry import Registry

# Fields that identify a SubsquidVesting / TemporaryHolding rather than any
# other contract. Checked by call rather than by bytecode hash, so a redeployed
# or upgraded version still matches.
VESTING_PROBE = [
    {"inputs": [], "name": "owner", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "beneficiary", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "expectedTotalAmount",
     "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "depositedIntoProtocol",
     "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]


def classify_owner(w3, address):
    """(kind, controller) for a creator address.

    kind is "eoa", "vesting" or "contract"; controller is the beneficiary for a
    vesting contract, which is the account that can drive it.
    """
    if int(address, 16) == 0:
        return "vacated", None
    if len(w3.eth.get_code(w3.to_checksum_address(address))) <= 2:
        return "eoa", address

    probe = w3.eth.contract(
        address=w3.to_checksum_address(address), abi=VESTING_PROBE
    )
    controller = None
    for fn in ("owner", "beneficiary"):
        try:
            controller = getattr(probe.functions, fn)().call()
            break
        except Exception:
            continue
    for fn in ("expectedTotalAmount", "depositedIntoProtocol"):
        try:
            getattr(probe.functions, fn)().call()
            return "vesting", controller
        except Exception:
            continue
    return "contract", controller


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

    cache = {}
    rows = []
    for entry in entries:
        worker_id = registry.contract.functions.workerIds(entry.peer_bytes).call()
        if worker_id == 0:
            rows.append((entry.peer_id, 0, "", "unregistered", ""))
            continue
        creator, _, bond, registered_at, _, _ = (
            registry.contract.functions.getWorker(worker_id).call()
        )
        if creator not in cache:
            cache[creator] = classify_owner(w3, creator)
        kind, controller = cache[creator]
        if registered_at == 0:
            kind = "withdrawn"
        rows.append((entry.peer_id, worker_id, creator, kind, controller or ""))

    print(f"network: {network.name}    peer IDs: {len(entries)}\n")
    print(f"{'worker':>7}  {'kind':<13}{'creator':<44}controller")
    for peer_id, worker_id, creator, kind, controller in rows:
        print(f"{worker_id or '-':>7}  {kind:<13}{creator or '-':<44}{controller}")

    print("\nby owner kind:")
    for kind, count in Counter(r[3] for r in rows).most_common():
        print(f"  {count:>5}  {kind}")

    vesting = {r[2] for r in rows if r[3] == "vesting"}
    if vesting:
        print(
            f"\n{len(vesting)} distinct vesting contract(s) hold these workers.\n"
            "  deregister() and withdraw() both require worker.creator == msg.sender,\n"
            "  so they must be called through the vesting contract's execute(),\n"
            "  driven by its beneficiary — not sent from the beneficiary directly."
        )
    eoas = {r[2] for r in rows if r[3] == "eoa"}
    if eoas:
        print(
            f"\n{len(eoas)} plain wallet(s) hold these workers: those can "
            "deregister and withdraw directly."
        )

    if args.csv:
        with open(args.csv, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ("peer_id", "worker_id", "creator", "owner_kind", "controller")
            )
            writer.writerows(rows)
        print(f"\nwritten to {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
