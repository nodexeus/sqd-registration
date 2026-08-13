#!/usr/bin/env python3
"""Bulk-register SQD worker nodes from a file of peer IDs."""

import argparse
import os
import sys
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
