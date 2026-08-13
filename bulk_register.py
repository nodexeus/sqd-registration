#!/usr/bin/env python3
"""Bulk-register SQD worker nodes from a file of peer IDs."""

import argparse
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import NoReturn

from dotenv import load_dotenv
from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3

from sqdreg.networks import NETWORKS

MAX_CONSECUTIVE_FAILURES = 3
RECEIPT_TIMEOUT = 300
GAS_BUFFER_PERCENT = 25


def fail(message: str) -> NoReturn:
    """Report a fatal problem and exit without sending anything."""
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def default_log_path(peer_id_file: str) -> str:
    return f"{peer_id_file}.run.jsonl"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register SQD worker nodes in bulk from a file of peer IDs."
    )
    parser.add_argument(
        "peer_id_file", help="file with one 'peer_id' or 'peer_id,name' per line"
    )
    parser.add_argument(
        "--network",
        choices=sorted(NETWORKS),
        default="mainnet",
        help="which SQD deployment to register against (default: mainnet)",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=positive_int,
        help="register at most this many new nodes",
    )
    parser.add_argument(
        "--name-template",
        help=(
            "name for lines without an explicit name; supports {n} "
            "(file position) and {peer_id}, e.g. 'nodexeus-{n:03d}'"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run every check and print the plan without sending transactions",
    )
    parser.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )
    parser.add_argument("--rpc-url", help="override the network's default RPC endpoint")
    parser.add_argument("--log", help="result log path (default: <input>.run.jsonl)")
    return parser.parse_args(argv)


def load_signer() -> LocalAccount:
    """Build the signing account from PRIVATE_KEY or MNEMONIC."""
    load_dotenv()
    private_key = os.getenv("PRIVATE_KEY")
    mnemonic = os.getenv("MNEMONIC")

    if private_key and mnemonic:
        print(
            "warning: both PRIVATE_KEY and MNEMONIC are set; using PRIVATE_KEY",
            file=sys.stderr,
        )
    if private_key:
        try:
            return Account.from_key(private_key.strip())
        except Exception:
            fail("PRIVATE_KEY is not a valid private key")
    if mnemonic:
        try:
            Account.enable_unaudited_hdwallet_features()
            return Account.from_mnemonic(mnemonic.strip())
        except Exception:
            fail("MNEMONIC is not a valid BIP-39 phrase")
    fail(
        "neither PRIVATE_KEY nor MNEMONIC is set "
        "(put one in the environment or a .env file)"
    )


def connect(network, rpc_url: str | None) -> Web3:
    """Connect to the RPC and refuse to continue on the wrong chain."""
    endpoint = rpc_url or network.rpc_url
    w3 = Web3(Web3.HTTPProvider(endpoint))
    try:
        chain_id = w3.eth.chain_id
    except Exception as exc:
        fail(f"cannot reach RPC endpoint {endpoint}: {exc}")
    if chain_id != network.chain_id:
        fail(
            f"RPC reports chain {chain_id}, but network {network.name} "
            f"expects {network.chain_id}"
        )
    return w3


def select_work(prepared, runlog, registry, limit):
    """Choose which prepared peers to register.

    Drops peers a previous run logged as successful, then drops peers the
    registry already holds a live registration for. `limit` caps the result
    *after* both filters, so `--limit 10` always means ten new registrations.
    The on-chain scan stops once the limit is met to avoid needless RPC calls.

    Returns (work, skipped_logged, skipped_onchain).
    """
    already_done = runlog.succeeded_peer_ids()
    skipped_logged = [
        item.entry.peer_id for item in prepared if item.entry.peer_id in already_done
    ]

    work = []
    skipped_onchain: list[str] = []

    for item in prepared:
        if item.entry.peer_id in already_done:
            continue
        if registry.is_registered(item.entry.peer_bytes):
            skipped_onchain.append(item.entry.peer_id)
            continue
        work.append(item)
        if limit is not None and len(work) >= limit:
            break

    return work, skipped_logged, skipped_onchain


@dataclass
class FundsCheck:
    """The bond position for a planned run."""

    bond: int
    required: int
    balance: int
    allowance: int
    needs_approval: bool


def check_funds(registry, count: int) -> FundsCheck:
    """Verify the wallet can bond `count` workers; exit if it cannot."""
    bond = registry.bond_amount()
    required = bond * count
    balance = registry.sqd_balance()
    allowance = registry.allowance()

    if balance < required:
        decimals = registry.token_decimals()
        fail(
            f"insufficient SQD: need {format_units(required, decimals)} "
            f"to bond {count} workers, hold {format_units(balance, decimals)}"
        )

    return FundsCheck(
        bond=bond,
        required=required,
        balance=balance,
        allowance=allowance,
        needs_approval=allowance < required,
    )


def current_fees(w3) -> dict:
    """EIP-1559 fees with headroom for a base-fee rise mid-run."""
    base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 0)
    priority = w3.eth.max_priority_fee
    return {
        "maxFeePerGas": base_fee * 2 + priority,
        "maxPriorityFeePerGas": priority,
    }


def gas_limit_for(registry, work) -> tuple[int, bool]:
    """Pick one gas limit for every registration in the run.

    Gas scales with metadata length and one limit is reused for the whole run,
    so the estimate is taken against the *longest* metadata — the most
    expensive call. A shorter name can then never exceed it. The result is
    padded to absorb ordinary variation.
    """
    longest = max(work, key=lambda candidate: len(candidate.metadata))
    estimate, exact = registry.estimate_register_gas(
        longest.entry.peer_bytes, longest.metadata
    )
    return estimate + estimate * GAS_BUFFER_PERCENT // 100, exact


def format_units(amount: int, decimals: int) -> str:
    """Render a token amount without trailing zeros."""
    value = Decimal(amount) / (Decimal(10) ** decimals)
    return format(value.normalize(), "f")


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
