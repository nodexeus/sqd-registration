#!/usr/bin/env python3
"""Bulk-register SQD worker nodes from a file of peer IDs."""

import argparse
import os
import shlex
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import NoReturn

from dotenv import load_dotenv
from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.exceptions import TimeExhausted

from sqdreg.naming import NamingError, prepare
from sqdreg.networks import NETWORKS
from sqdreg.peerids import PeerIdError, parse_file
from sqdreg.registry import Registry
from sqdreg.runlog import (
    FAILED,
    PENDING,
    SUCCESS,
    Record,
    RunLog,
    RunLogError,
    utc_now,
)

PROG = "bulk_register.py"
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


def default_log_path(peer_id_file: str, network: str) -> str:
    """Default log path for this input file *on this network*.

    The network is part of the name because a log is trusted to say which peer
    IDs are already done: rehearsing a file on tethys and then running the same
    file on mainnet must not share one log.
    """
    return f"{peer_id_file}.{network}.run.jsonl"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        # Fixed rather than derived from sys.argv[0], so usage text and the
        # resume hint name this script even when main() is called
        # programmatically from another program.
        prog=PROG,
        description="Register SQD worker nodes in bulk from a file of peer IDs.",
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


def resume_command(args: argparse.Namespace) -> str:
    """The command that continues this run, echoing back every flag given.

    Every flag here bounds what a resume does, so dropping one changes the
    spend: without `--limit` the plan covers the whole file instead of the cap
    the operator chose, without `--name-template` the remaining nodes register
    unnamed (recoverable only one updateMetadata transaction at a time), and
    without `--log` the resume reads a different log and stops skipping what is
    already done.

    `--yes` is deliberately *not* echoed even when it was supplied: a resume
    starts from a new plan with new counts, and that deserves a fresh look.
    """
    parts = [PROG, shlex.quote(args.peer_id_file), "--network", args.network]
    if args.limit is not None:
        parts += ["--limit", str(args.limit)]
    if args.name_template:
        # Quoted, or the shell would try to glob or brace-expand {n:03d}.
        parts += ["--name-template", shlex.quote(args.name_template)]
    if args.log:
        parts += ["--log", shlex.quote(args.log)]
    if args.rpc_url:
        parts += ["--rpc-url", shlex.quote(args.rpc_url)]
    return " ".join(parts)


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


def select_work(prepared, runlog, registry, limit, network):
    """Choose which prepared peers to register.

    Drops peers a previous run logged as successful *on this network*, then
    drops peers the registry already holds a live registration for. `limit`
    caps the result *after* both filters, so `--limit 10` always means ten new
    registrations. The on-chain scan stops once the limit is met to avoid
    needless RPC calls.

    Returns (work, skipped_logged, skipped_onchain).
    """
    already_done = runlog.succeeded_peer_ids(network)
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
    if not work:
        fail("no peers to register")
    longest = max(work, key=lambda candidate: len(candidate.metadata.encode()))
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


@dataclass
class RunResult:
    """Outcome of a registration loop."""

    registered: int = 0
    failed: int = 0
    pending: int = 0
    gas_used: int = 0
    aborted: str | None = None


def send_tx(w3, account, tx):
    """Sign and send one transaction, returning its hash."""
    signed = account.sign_transaction(tx)
    return w3.eth.send_raw_transaction(signed.raw_transaction)


def wait_for(w3, tx_hash) -> dict:
    """Wait for one receipt. Raises TimeExhausted past RECEIPT_TIMEOUT."""
    return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT)


def send_and_wait(w3, account, tx) -> tuple[str, dict]:
    """Send and wait as one step. For standalone transactions like the approval.

    The registration loop calls send_tx and wait_for separately, because it
    needs the hash inside its timeout handler.
    """
    tx_hash = send_tx(w3, account, tx)
    return tx_hash.hex(), wait_for(w3, tx_hash)


def register_all(w3, account, registry, work, runlog, fees, gas, network) -> RunResult:
    """Register each peer in turn, logging every attempt as it resolves."""
    result = RunResult()
    nonce = w3.eth.get_transaction_count(account.address)
    consecutive_failures = 0
    total = len(work)

    for position, item in enumerate(work, start=1):
        peer_id = item.entry.peer_id
        label = f"{peer_id} as {item.name}" if item.name else peer_id
        tx = registry.build_register(
            peer_bytes=item.entry.peer_bytes,
            metadata=item.metadata,
            nonce=nonce,
            fees=fees,
            gas=gas,
        )
        print(f"[{position}/{total}] {label}", flush=True)

        try:
            raw_hash = send_tx(w3, account, tx)
        except Exception as exc:
            # Nothing reached the mempool, so the nonce stays free for the
            # next attempt.
            runlog.append(
                Record(
                    peer_id=peer_id,
                    status=FAILED,
                    name=item.name,
                    error=str(exc),
                    timestamp=utc_now(),
                    network=network,
                )
            )
            result.failed += 1
            consecutive_failures += 1
            print(f"  send failed: {exc}", file=sys.stderr)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                result.aborted = (
                    f"stopped after {consecutive_failures} consecutive failures"
                )
                break
            continue

        tx_hash = raw_hash.hex()

        try:
            receipt = wait_for(w3, raw_hash)
        except Exception as exc:
            # The transaction was broadcast and its outcome is now unknown,
            # so the nonce is consumed either way. Record the hash — it is
            # the only way to ever resolve this transaction — and abort,
            # because everything queued behind an unresolved nonce is
            # unresolvable too.
            runlog.append(
                Record(
                    peer_id=peer_id,
                    status=PENDING,
                    name=item.name,
                    tx_hash=tx_hash,
                    timestamp=utc_now(),
                    network=network,
                )
            )
            result.pending += 1
            if isinstance(exc, TimeExhausted):
                result.aborted = (
                    "receipt timed out; later transactions would queue behind a "
                    "stuck nonce, so the run stopped"
                )
                print(f"  timed out waiting for receipt: {exc}", file=sys.stderr)
            else:
                result.aborted = (
                    f"receipt lookup failed ({exc}); later transactions would "
                    "queue behind an unresolved nonce, so the run stopped"
                )
                print(f"  receipt lookup failed: {exc}", file=sys.stderr)
            break

        nonce += 1
        result.gas_used += receipt["gasUsed"]

        if receipt["status"] == 1:
            runlog.append(
                Record(
                    peer_id=peer_id,
                    status=SUCCESS,
                    name=item.name,
                    tx_hash=tx_hash,
                    block=receipt["blockNumber"],
                    timestamp=utc_now(),
                    network=network,
                )
            )
            result.registered += 1
            consecutive_failures = 0
            print(f"  registered in block {receipt['blockNumber']} ({tx_hash})")
        else:
            runlog.append(
                Record(
                    peer_id=peer_id,
                    status=FAILED,
                    name=item.name,
                    tx_hash=tx_hash,
                    block=receipt["blockNumber"],
                    error="transaction reverted",
                    timestamp=utc_now(),
                    network=network,
                )
            )
            result.failed += 1
            consecutive_failures += 1
            print(f"  reverted ({tx_hash})", file=sys.stderr)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                result.aborted = (
                    f"stopped after {consecutive_failures} consecutive failures"
                )
                break

    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    network = NETWORKS[args.network]
    log_path = args.log or default_log_path(args.peer_id_file, network.name)

    account = load_signer()
    w3 = connect(network, args.rpc_url)
    registry = Registry(w3, network, account.address)
    runlog = RunLog(log_path)

    try:
        entries, duplicates = parse_file(args.peer_id_file)
    except PeerIdError as exc:
        fail(str(exc))
    except OSError as exc:
        fail(f"cannot read {args.peer_id_file}: {exc}")

    for warning in duplicates:
        print(f"warning: {warning}", file=sys.stderr)
    if not entries:
        print(f"{args.peer_id_file} contains no peer IDs")
        return 0

    try:
        prepared = prepare(entries, args.name_template)
    except NamingError as exc:
        fail(str(exc))

    try:
        work, skipped_logged, skipped_onchain = select_work(
            prepared, runlog, registry, args.limit, network.name
        )
    except RunLogError as exc:
        fail(str(exc))
    except OSError as exc:
        fail(f"cannot read the run log {log_path}: {exc}")

    print(f"network:     {network.name} (chain {network.chain_id})")
    print(f"wallet:      {account.address}")
    print(f"log:         {log_path}")
    print(f"in file:     {len(entries)}")
    print(f"skipped:     {len(skipped_logged)} logged, {len(skipped_onchain)} on-chain")
    print(f"to register: {len(work)}")

    if not work:
        print("nothing to register")
        return 0

    funds = check_funds(registry, len(work))
    decimals = registry.token_decimals()
    gas, exact = gas_limit_for(registry, work)
    fees = current_fees(w3)
    gas_cost_wei = gas * fees["maxFeePerGas"] * len(work)

    print(f"bond:        {format_units(funds.bond, decimals)} SQD each")
    print(f"bond total:  {format_units(funds.required, decimals)} SQD")
    print(f"balance:     {format_units(funds.balance, decimals)} SQD")
    print(
        f"gas:         ~{format_units(gas_cost_wei, 18)} ETH max"
        f"{'' if exact else ' (estimate unavailable, using fallback)'}"
    )
    if funds.needs_approval:
        print(
            f"approval:    needed — allowance is "
            f"{format_units(funds.allowance, decimals)} SQD, "
            f"will approve {format_units(funds.required, decimals)} SQD"
        )

    if args.dry_run:
        print("\n-- dry run, nothing sent --")
        for item in work:
            print(f"  {item.entry.peer_id} -> {item.name or '(unnamed)'}")
        return 0

    if not confirm(f"\nRegister {len(work)} worker(s) on {network.name}?", args.yes):
        print("aborted")
        return 0

    if funds.needs_approval:
        approve_tx = registry.build_approve(
            amount=funds.required,
            nonce=w3.eth.get_transaction_count(account.address),
            fees=fees,
        )
        print("approving bond transfer...")
        tx_hash, receipt = send_and_wait(w3, account, approve_tx)
        if receipt["status"] != 1:
            fail(f"approval reverted ({tx_hash})")
        print(f"  approved ({tx_hash})")

    result = register_all(
        w3, account, registry, work, runlog, fees, gas, network.name
    )

    remaining = (
        len(entries)
        - len(skipped_logged)
        - len(skipped_onchain)
        - result.registered
    )
    print(
        f"\nregistered {result.registered}, failed {result.failed}, "
        f"pending {result.pending}, gas used {result.gas_used}"
    )
    if result.aborted:
        print(f"run stopped: {result.aborted}", file=sys.stderr)
    if remaining > 0:
        print(f"{remaining} peer ID(s) still unregistered; resume with:")
        print(f"  {resume_command(args)}")

    return 1 if result.failed or result.pending else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted; progress is in the run log", file=sys.stderr)
        raise SystemExit(130) from None
