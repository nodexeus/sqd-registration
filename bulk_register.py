#!/usr/bin/env python3
"""Bulk-register SQD worker nodes from a file of peer IDs."""

import argparse
import os
import shlex
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import NoReturn

from dotenv import load_dotenv
from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.exceptions import ProviderConnectionError, TimeExhausted

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

# Arbitrum's suggested priority fee is routinely 0, which would leave
# maxFeePerGas at exactly 2x the base fee with no tip at all. 0.01 gwei is a
# rounding error against a ~600,000-gas registration (6e-6 ETH) and keeps every
# transaction attractive to any node that does order by tip.
MIN_PRIORITY_FEE_WEI = 10_000_000

# Re-read the fee cap every this many registrations. At roughly one receipt
# every few seconds, 25 items is a minute or two of wall clock, so the cap is
# never badly stale; the cost is one get_block per 25 sends (12 extra reads on a
# 300-node run) versus a stalled run if the base fee doubles mid-flight.
FEE_REFRESH_INTERVAL = 25

# Read-only RPC retries. select_work alone makes up to two reads per peer ID —
# 600 calls for a 300-peer file — against a free public endpoint, so one 429 or
# transient 502 is likely at this scale and must not end the run.
RPC_ATTEMPTS = 4
RPC_BACKOFF_SECONDS = 1.0


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
    parser.add_argument(
        "--log", help="result log path (default: <input>.<network>.run.jsonl)"
    )
    return parser.parse_args(argv)


def read_rpc(call, *args, what: str):
    """Run one read-only RPC call, retrying transient failures with backoff.

    Reads are idempotent, so retrying is always safe — this must never wrap a
    send. Exhaustion is fatal via fail(), keeping this file's error:/exit-2
    convention instead of a traceback out of a 600-call scan.
    """
    delay = RPC_BACKOFF_SECONDS
    for attempt in range(1, RPC_ATTEMPTS + 1):
        try:
            return call(*args)
        except Exception as exc:
            if attempt == RPC_ATTEMPTS:
                fail(f"{what} failed after {RPC_ATTEMPTS} attempts: {exc}")
            print(
                f"warning: {what} failed ({exc}); retrying in {delay:g}s",
                file=sys.stderr,
            )
            time.sleep(delay)
            delay *= 2


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
        if read_rpc(
            registry.is_registered,
            item.entry.peer_bytes,
            what=f"registration lookup for {item.entry.peer_id}",
        ):
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
    bond = read_rpc(registry.bond_amount, what="bondAmount() read")
    required = bond * count
    balance = read_rpc(registry.sqd_balance, what="SQD balance read")
    allowance = read_rpc(registry.allowance, what="SQD allowance read")

    if balance < required:
        decimals = read_rpc(registry.token_decimals, what="token decimals read")
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
    """EIP-1559 fees with headroom for a base-fee rise mid-run.

    2x the base fee is only headroom against the base fee *at the moment of the
    read*, so callers must refresh this during a long run rather than reuse one
    result: past the cap nothing mines, every receipt wait burns its full
    timeout, and the run strands an in-flight transaction.
    """
    base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 0)
    priority = max(w3.eth.max_priority_fee, MIN_PRIORITY_FEE_WEI)
    return {
        "maxFeePerGas": base_fee * 2 + priority,
        "maxPriorityFeePerGas": priority,
    }


def refreshed_fees(w3, fees: dict) -> dict:
    """Re-read fees mid-run, keeping the previous values if the read fails.

    A hiccup on one optional read must not abort a healthy run; the previous cap
    is still usable and the next refresh will try again.
    """
    try:
        return current_fees(w3)
    except Exception as exc:  # noqa: BLE001 - any read failure is non-fatal here
        print(f"warning: could not refresh gas fees: {exc}", file=sys.stderr)
        return fees


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
    interrupted: bool = False


class SendFailed(Exception):
    """A transaction could not be sent, or its receipt could not be read.

    Carries the transaction hash whenever signing got far enough to know it, so
    the caller can name a transaction that may nevertheless be in flight.
    """

    def __init__(self, cause: BaseException, tx_hash: str | None):
        super().__init__(str(cause))
        self.cause = cause
        self.tx_hash = tx_hash


def sign_tx(account, tx) -> tuple[bytes, object]:
    """Sign one transaction, returning (raw payload, hash).

    The hash comes from the signed payload, not from the send's return value, so
    it is known *before* the transaction is broadcast. That is the only way an
    attempt whose send outcome is unknown stays traceable.
    """
    signed = account.sign_transaction(tx)
    return signed.raw_transaction, signed.hash


def is_transport_error(exc: BaseException) -> bool:
    """Whether a send failure leaves the transaction's fate unknown.

    A JSON-RPC error response means the node evaluated the transaction and
    refused it: nothing reached the mempool, so the nonce stays free. A
    transport-level failure — connection reset, read timeout — says nothing,
    because the node may have accepted the raw transaction and failed only when
    replying, in which case the nonce is consumed and the peer may in fact be
    registered. `OSError` covers the stdlib socket errors and requests'
    exception tree alike, since RequestException subclasses IOError.
    """
    return isinstance(exc, (OSError, ProviderConnectionError))


def wait_for(w3, tx_hash) -> dict:
    """Wait for one receipt. Raises TimeExhausted past RECEIPT_TIMEOUT."""
    return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT)


def send_and_wait(w3, account, tx, label: str = "transaction") -> tuple[str, dict]:
    """Sign, send, and wait for one standalone transaction, e.g. the approval.

    Raises SendFailed carrying the hash whenever signing got far enough to know
    it, and prints the hash as soon as the transaction is broadcast, so the
    operator always ends up holding the hash of anything that may still be in
    flight — including after a Ctrl-C during the wait.

    The registration loop drives sign/send/wait itself, because it has to tell a
    rejected send from an unresolved one.
    """
    tx_hash = None
    try:
        raw, raw_hash = sign_tx(account, tx)
        tx_hash = raw_hash.hex()
        w3.eth.send_raw_transaction(raw)
    except Exception as exc:
        raise SendFailed(exc, tx_hash) from exc
    print(f"  {label} sent ({tx_hash}); waiting for the receipt", flush=True)
    try:
        return tx_hash, wait_for(w3, raw_hash)
    except Exception as exc:
        raise SendFailed(exc, tx_hash) from exc


def register_all(w3, account, registry, work, runlog, fees, gas, network) -> RunResult:
    """Register each peer in turn, logging every attempt as it resolves."""
    result = RunResult()
    nonce = read_rpc(
        w3.eth.get_transaction_count, account.address, what="nonce read"
    )
    consecutive_failures = 0
    total = len(work)

    def log_pending(item, tx_hash: str, error: str) -> None:
        """Record an attempt whose outcome is unknown, with its hash and reason.

        `pending` is the hardest state to diagnose, so the reason is persisted
        too. A `pending` record never satisfies the skip filter, so the next run
        re-checks that peer ID on-chain instead of assuming either outcome.
        """
        runlog.append(
            Record(
                peer_id=item.entry.peer_id,
                status=PENDING,
                name=item.name,
                tx_hash=tx_hash,
                error=error,
                timestamp=utc_now(),
                network=network,
            )
        )
        result.pending += 1

    for position, item in enumerate(work, start=1):
        # The fee cap was read against one block's base fee; a 300-transaction
        # run spans 15-30 minutes, and a >2x base-fee rise is ordinary on
        # Arbitrum under load. Refresh periodically or everything after the rise
        # sits unmined until its receipt wait times out.
        if position > 1 and position % FEE_REFRESH_INTERVAL == 1:
            fees = refreshed_fees(w3, fees)

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

        # Sign before sending so the hash is known even if the send fails: it is
        # a function of the signed payload, so it identifies the transaction
        # whether or not the node ever answers.
        raw, raw_hash = sign_tx(account, tx)
        tx_hash = raw_hash.hex()

        try:
            w3.eth.send_raw_transaction(raw)
        except KeyboardInterrupt:
            log_pending(item, tx_hash, "interrupted while sending")
            result.interrupted = True
            result.aborted = (
                "interrupted while sending; that transaction may still be in "
                "flight, so the run stopped"
            )
            print("\n  interrupted while sending", file=sys.stderr)
            break
        except Exception as exc:
            if is_transport_error(exc):
                # The node may have accepted the raw transaction and failed only
                # when replying, so the nonce may be consumed and this peer may
                # in fact be registered. Recording `failed` here would be a
                # confident claim the log cannot support, and anything queued
                # behind a possibly-consumed nonce is unresolvable, so stop.
                log_pending(item, tx_hash, str(exc))
                result.aborted = (
                    f"send failed at the transport level ({exc}); whether the "
                    "transaction reached the mempool is unknown, so the run "
                    "stopped"
                )
                print(f"  send outcome unknown: {exc} ({tx_hash})", file=sys.stderr)
                break
            # A JSON-RPC error response means the node evaluated the transaction
            # and refused it, so nothing reached the mempool and the nonce stays
            # free for the next attempt.
            runlog.append(
                Record(
                    peer_id=peer_id,
                    status=FAILED,
                    name=item.name,
                    tx_hash=tx_hash,
                    error=str(exc),
                    timestamp=utc_now(),
                    network=network,
                )
            )
            result.failed += 1
            consecutive_failures += 1
            print(f"  send rejected: {exc}", file=sys.stderr)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                result.aborted = (
                    f"stopped after {consecutive_failures} consecutive failures"
                )
                break
            continue

        try:
            receipt = wait_for(w3, raw_hash)
        except KeyboardInterrupt:
            # The transaction is broadcast and unresolved. Keep its hash rather
            # than losing it to the interrupt.
            log_pending(item, tx_hash, "interrupted while waiting for the receipt")
            result.interrupted = True
            result.aborted = (
                "interrupted; the last transaction may still be in flight"
            )
            print(f"\n  interrupted, still unresolved ({tx_hash})", file=sys.stderr)
            break
        except Exception as exc:
            # The transaction was broadcast and its outcome is now unknown,
            # so the nonce is consumed either way. Record the hash — it is
            # the only way to ever resolve this transaction — and abort,
            # because everything queued behind an unresolved nonce is
            # unresolvable too.
            log_pending(item, tx_hash, str(exc))
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
    decimals = read_rpc(registry.token_decimals, what="token decimals read")
    gas, exact = gas_limit_for(registry, work)
    fees = read_rpc(current_fees, w3, what="fee read")
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

    # The prompt can sit for minutes; the fees above are only headroom over the
    # base fee at the moment they were read.
    fees = read_rpc(current_fees, w3, what="fee read")

    if funds.needs_approval:
        approve_tx = registry.build_approve(
            amount=funds.required,
            nonce=read_rpc(
                w3.eth.get_transaction_count, account.address, what="nonce read"
            ),
            fees=fees,
        )
        print("approving bond transfer...")
        try:
            tx_hash, receipt = send_and_wait(
                w3, account, approve_tx, label="approval"
            )
        except SendFailed as exc:
            named = f" {exc.tx_hash}" if exc.tx_hash else ""
            fail(
                f"approval failed: {exc}. Approval transaction{named} may have "
                "been broadcast and could still mine. Wait for it to settle "
                "before re-running: a new run reads the nonce at 'latest', "
                "which excludes a pending approval, so it would reuse that "
                "nonce and be rejected as an underpriced replacement."
            )
        if receipt["status"] != 1:
            fail(f"approval reverted ({tx_hash})")
        print(f"  approved ({tx_hash})")

    # Re-measure gas now that the allowance exists. register() reverts while the
    # allowance is missing, so estimation reverts too, so on a first run the
    # figure in the plan above is FALLBACK_REGISTER_GAS rather than a
    # measurement. After the approval the estimate is real. When no approval was
    # needed the first estimate was already exact and this simply repeats it,
    # which keeps one code path.
    gas, exact = gas_limit_for(registry, work)
    print(
        f"gas limit:   {gas} per registration"
        f"{'' if exact else ' (estimate unavailable, using fallback)'}"
    )

    result = register_all(
        w3,
        account,
        registry,
        work,
        runlog,
        fees=fees,
        gas=gas,
        network=network.name,
    )

    remaining = (
        len(entries)
        - len(skipped_logged)
        - len(skipped_onchain)
        - result.registered
    )
    # select_work stops scanning once --limit is met, so under a limit the peers
    # past that point were never examined and their status is genuinely unknown.
    # A precise "246 still unregistered" was a 6x over-report when 40 were left;
    # only claim a figure when the scan covered the whole file.
    scan_truncated = args.limit is not None and len(work) >= args.limit

    print(
        f"\nregistered {result.registered}, failed {result.failed}, "
        f"pending {result.pending}, gas used {result.gas_used}"
    )
    if result.aborted:
        print(f"run stopped: {result.aborted}", file=sys.stderr)
    if remaining > 0:
        if scan_truncated:
            print(
                f"up to {remaining} peer ID(s) may still be unregistered "
                f"(--limit stopped the on-chain scan early); resume with:"
            )
        else:
            print(f"{remaining} peer ID(s) still unregistered; resume with:")
        print(f"  {resume_command(args)}")

    if result.interrupted:
        return 130
    return 1 if result.failed or result.pending else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted; progress is in the run log", file=sys.stderr)
        raise SystemExit(130) from None
