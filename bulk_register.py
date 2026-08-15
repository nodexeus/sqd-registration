#!/usr/bin/env python3
"""Bulk-register SQD worker nodes from a file of peer IDs."""

# Checked before anything else is imported. Every module below uses PEP 604
# annotations (`str | None`) that are evaluated at runtime, so on Python 3.9
# the import itself dies with `TypeError: unsupported operand type(s) for |`
# pointing at a dataclass field — which says nothing about the real cause.
#
# The usual way to hit this is running ./bulk_register.py, whose shebang picks
# the system python3 rather than the virtualenv.
import os as _os
import sys as _sys

if _sys.version_info < (3, 10):
    _venv = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".venv")
    _python = _os.path.join(_venv, "bin", "python")
    _hint = (
        _python if _os.path.exists(_python) else "python3.11 -m venv .venv  # then"
    )
    _sys.stderr.write(
        "error: this needs Python 3.10 or newer, but it is running under "
        "{}.{}.{}\n"
        "       Running it as ./bulk_register.py uses the system python3.\n"
        "       Use the virtualenv instead:\n"
        "           {} bulk_register.py {}\n".format(
            _sys.version_info[0],
            _sys.version_info[1],
            _sys.version_info[2],
            _hint,
            " ".join(_sys.argv[1:]) or "peer_ids.txt --action status",
        )
    )
    raise SystemExit(2)

import argparse
import csv
import json
import os
import shlex
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from getpass import getpass
from typing import NoReturn

try:
    from dotenv import load_dotenv
    from eth_account import Account
    from eth_account.signers.local import LocalAccount
    from web3 import Web3
    from web3.exceptions import ProviderConnectionError, TimeExhausted
except ImportError as _exc:  # pragma: no cover - exercised by a subprocess test
    # The other way ./bulk_register.py goes wrong: the right Python, but not
    # the virtualenv, so none of the dependencies are importable.
    _venv_python = _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)), ".venv", "bin", "python"
    )
    _sys.stderr.write(
        "error: {}\n"
        "       Dependencies are missing, so this is not running inside the\n"
        "       project virtualenv.{}\n".format(
            _exc,
            "\n       Use: {} bulk_register.py {}".format(
                _venv_python, " ".join(_sys.argv[1:]) or "peer_ids.txt --action status"
            )
            if _os.path.exists(_venv_python)
            else "\n       Create it: python3.11 -m venv .venv && "
            ".venv/bin/pip install -r requirements.txt",
        )
    )
    raise SystemExit(2) from None

from sqdreg.naming import DEFAULT_BATCH_SIZE, NamingError, prepare
from sqdreg.networks import NETWORKS
from sqdreg.peerids import PeerIdError, parse_file, parse_peer_ids
from sqdreg.registry import (
    ACTIVE,
    REGISTERING,
    FOREIGN,
    LOCKED,
    UNREGISTERED,
    WITHDRAWABLE,
    Registry,
)
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

REGISTER = "register"
DEREGISTER = "deregister"
WITHDRAW = "withdraw"
STATUS = "status"
ACTIONS = (REGISTER, DEREGISTER, WITHDRAW, STATUS)

# The worker state each action requires. register() also accepts a slot this
# account previously vacated, which reads as UNREGISTERED.
ACTIONABLE_STATE = {
    REGISTER: UNREGISTERED,
    DEREGISTER: ACTIVE,
    WITHDRAW: WITHDRAWABLE,
}

# What the CSV of confirmed results is called, per action.
CSV_NOUN = {REGISTER: "registered", DEREGISTER: "deregistered", WITHDRAW: "withdrawn"}
MAX_CONSECUTIVE_FAILURES = 3
RECEIPT_TIMEOUT = 300
GAS_BUFFER_PERCENT = 25
# A live estimate against the mainnet SQD token put approve() at 47,137 gas.
# Rounded up for headroom; it is one transaction in a run of hundreds, so its
# precision barely moves the budget.
APPROVAL_GAS = 60_000

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


# Artifact base name for runs driven by --peer-id alone. Distinct from any
# file-driven run's artifacts, so an ad-hoc action cannot quietly append to the
# log a 1000-node run depends on.
ADHOC_BASE = "adhoc"


def artifact_base(peer_id_file: str | None) -> str:
    return peer_id_file or ADHOC_BASE


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
        "peer_id_file",
        nargs="?",
        help=(
            "file with one 'peer_id' or 'peer_id,name' per line. Optional if "
            "--peer-id is given"
        ),
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
            "name for lines without an explicit name; supports {n} (the next "
            "number this template has not used) and {peer_id}, "
            "e.g. 'nodexeus-{n:03d}'"
        ),
    )
    parser.add_argument(
        "--action",
        choices=ACTIONS,
        default=REGISTER,
        help=(
            "register (default), deregister, withdraw, or status — a read-only "
            "report of where every peer ID sits in the worker lifecycle"
        ),
    )
    parser.add_argument(
        "--batch",
        type=positive_int,
        default=DEFAULT_BATCH_SIZE,
        metavar="N",
        help=(
            f"nodes per batch when names are generated (default: "
            f"{DEFAULT_BATCH_SIZE}). Each batch gets one random word, so a "
            "1000-node run makes 20 visibly distinct groups"
        ),
    )
    parser.add_argument(
        "--peer-id",
        action="append",
        dest="peer_ids",
        metavar="PEER_ID",
        help=(
            "act on this peer ID only, instead of the whole file; repeat for "
            "several. Each must appear in the file"
        ),
    )
    parser.add_argument(
        "--address",
        help=(
            "wallet to report on for --action status, so no credential is "
            "needed. Not valid for the other actions, which act as whoever "
            "holds the credential"
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
    args = parser.parse_args(argv)
    if not args.peer_id_file and not args.peer_ids:
        parser.error("give a peer ID file, or --peer-id, or both")
    return args


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
    parts = [PROG]
    if args.peer_id_file:
        parts.append(shlex.quote(args.peer_id_file))
    parts += ["--network", args.network]
    if args.action != REGISTER:
        parts += ["--action", args.action]
    if args.limit is not None:
        parts += ["--limit", str(args.limit)]
    if args.name_template:
        # Quoted, or the shell would try to glob or brace-expand {n:03d}.
        parts += ["--name-template", shlex.quote(args.name_template)]
    if args.peer_ids:
        for peer_id in args.peer_ids:
            parts += ["--peer-id", shlex.quote(peer_id)]
    if args.log:
        parts += ["--log", shlex.quote(args.log)]
    if args.rpc_url:
        parts += ["--rpc-url", shlex.quote(args.rpc_url)]
    return " ".join(parts)


def account_from_secret(secret: str, source: str) -> LocalAccount:
    """Build an account from a private key or a BIP-39 phrase.

    Whitespace decides which: a phrase has spaces, a key does not. Errors name
    only `source`, never the value — eth-account's mnemonic error embeds the
    phrase verbatim, so interpolating it would put the secret on stderr.
    """
    secret = secret.strip()
    if " " in secret:
        try:
            Account.enable_unaudited_hdwallet_features()
            return Account.from_mnemonic(secret)
        except Exception:
            fail(f"{source} is not a valid BIP-39 phrase")
    try:
        return Account.from_key(secret)
    except Exception:
        fail(f"{source} is not a valid private key")


def prompt_for_secret() -> LocalAccount:
    """Ask for a key at the terminal, so none has to exist on disk.

    Only when stdin is a terminal: under cron, nohup or CI a prompt would hang
    until the run is killed, which is worse than a clear failure.
    """
    if not sys.stdin.isatty():
        fail(
            "neither PRIVATE_KEY nor MNEMONIC is set, and stdin is not a "
            "terminal so there is nobody to ask (set one in the environment "
            "or a .env file)"
        )
    try:
        secret = getpass("Private key or BIP-39 phrase (input hidden): ")
    except (EOFError, KeyboardInterrupt):
        fail("no credential provided")
    if not secret.strip():
        fail("no credential provided")
    return account_from_secret(secret, "the credential you entered")


def load_signer() -> LocalAccount:
    """Build the signing account from PRIVATE_KEY, MNEMONIC, or a prompt."""
    load_dotenv()
    private_key = os.getenv("PRIVATE_KEY")
    mnemonic = os.getenv("MNEMONIC")

    if private_key and mnemonic:
        print(
            "warning: both PRIVATE_KEY and MNEMONIC are set; using PRIVATE_KEY",
            file=sys.stderr,
        )
    if private_key:
        return account_from_secret(private_key, "PRIVATE_KEY")
    if mnemonic:
        return account_from_secret(mnemonic, "MNEMONIC")
    return prompt_for_secret()


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


def restrict_to(entries, wanted: list[str]):
    """Narrow the file to specific peer IDs, rejecting any that are not in it.

    Requiring membership is the point: a typo'd or wrong-network peer ID would
    otherwise silently select nothing, and "nothing to do" reads exactly like
    "already done".
    """
    present = {e.peer_id for e in entries}
    unknown = [p for p in wanted if p not in present]
    if unknown:
        fail(
            "--peer-id not found in the input file: "
            + ", ".join(unknown)
            + f" ({len(present)} peer ID(s) in the file)"
        )
    chosen = set(wanted)
    return [e for e in entries if e.peer_id in chosen]


def l1_block_number(w3) -> int:
    """The block number the *contract* sees.

    On Arbitrum, Solidity's `block.number` is the L1 block number, while
    `eth_blockNumber` returns the L2 one — currently ~494,000,000 against
    ~25,700,000. Comparing the L2 number with `deregisteredAt + lockPeriod`
    would call every locked worker withdrawable and produce a run of "Worker is
    locked" reverts, so the lock arithmetic must use this.
    """
    value = w3.eth.get_block("latest").get("l1BlockNumber")
    if value is None:
        # Not an Arbitrum-style chain; its block.number is the one we see.
        return w3.eth.block_number
    return int(value, 16) if isinstance(value, str) else int(value)


def classify(entries, registry, l1_block, lock_period, owned):
    """Read every peer ID's on-chain state, in file order."""
    return [
        read_rpc(
            registry.worker_state,
            entry.peer_bytes,
            l1_block,
            lock_period,
            owned,
            what=f"state lookup for {entry.peer_id}",
        )
        for entry in entries
    ]


def select_by_state(entries, states, runlog, limit, network, action):
    """Pick the entries whose on-chain state permits `action`.

    Unlike the register path this cannot stop early: a status report wants the
    whole picture, and deregister/withdraw need each peer's state to explain
    why it was skipped. The reads are cheap and nothing is sent.
    """
    already_done = runlog.succeeded_peer_ids(network, action)
    wanted = ACTIONABLE_STATE[action]

    work, skipped_logged, not_ready = [], [], []
    for entry, state in zip(entries, states):
        if entry.peer_id in already_done:
            skipped_logged.append(entry.peer_id)
            continue
        if state.state != wanted:
            not_ready.append((entry, state))
            continue
        if limit is None or len(work) < limit:
            work.append(entry)
    return work, skipped_logged, not_ready


def select_work(entries, runlog, registry, limit, network):
    """Choose which peer entries to register.

    Drops peers a previous run logged as successful *on this network*, then
    drops peers the registry already holds a live registration for. `limit`
    caps the result *after* both filters, so `--limit 10` always means ten new
    registrations. The on-chain scan stops once the limit is met to avoid
    needless RPC calls.

    Filtering happens before naming, deliberately: template numbers are handed
    out from the first unused value, so allocating them to peers that are
    already registered would burn numbers and shift every later name.

    Returns (work, skipped_logged, skipped_onchain).
    """
    already_done = runlog.succeeded_peer_ids(network)
    skipped_logged = [e.peer_id for e in entries if e.peer_id in already_done]

    work = []
    skipped_onchain: list[str] = []

    for entry in entries:
        if entry.peer_id in already_done:
            continue
        if read_rpc(
            registry.is_registered,
            entry.peer_bytes,
            what=f"registration lookup for {entry.peer_id}",
        ):
            skipped_onchain.append(entry.peer_id)
            continue
        work.append(entry)
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


CSV_COLUMNS = ("peer_id", "name", "tx_hash", "block", "registered_at")


def default_csv_path(peer_id_file: str, network: str) -> str:
    return f"{peer_id_file}.{network}.registered.csv"


def write_registered_csv(path: str, runlog, network: str, action: str = "register") -> int:
    """Rewrite the CSV of confirmed registrations from the log.

    Derived, never appended to in parallel: the JSONL log is the single
    write-ahead record, and regenerating from it each run means the two cannot
    drift apart. Returns the row count.
    """
    rows = runlog.completed(network, action)
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for r in rows:
            writer.writerow(
                [r.peer_id, r.name or "", r.tx_hash or "", r.block or "", r.timestamp or ""]
            )
    return len(rows)


@dataclass
class EthCheck:
    """The gas budget for a planned run, against the wallet's ETH."""

    balance: int
    required: int
    sufficient: bool


def check_eth(
    w3, address: str, gas: int, fees: dict, count: int, needs_approval: bool
) -> EthCheck:
    """Worst-case ETH the run can be charged, against what the wallet holds.

    Reports rather than exiting, unlike `check_funds`: the caller prints the
    bond and gas figures first, so an operator sees the shortfall in context
    instead of an error on its own.

    The budget is deliberately the worst case — the full gas limit at the full
    `maxFeePerGas`, for every transaction. Actual spend is lower, since unused
    gas is not charged and the base fee is usually below the cap. Being
    conservative is the point: running dry at node 700 of 1000 aborts the run,
    and the remedy (send more ETH) is trivial by comparison.
    """
    transactions = gas * count
    if needs_approval:
        transactions += APPROVAL_GAS
    required = transactions * fees["maxFeePerGas"]
    balance = read_rpc(w3.eth.get_balance, address, what="ETH balance read")
    return EthCheck(
        balance=balance, required=required, sufficient=balance >= required
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
    try:
        reply = input(f"{prompt} [y/N] ")
    except EOFError:
        # No terminal — nohup, cron, CI — and no --yes. Declining is the only
        # safe reading of "nobody is here to answer".
        print("\nno input available to confirm; not sending", file=sys.stderr)
        return False
    return reply.strip().lower() in ("y", "yes")


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

    `JSONDecodeError` belongs here too, and is easy to miss because it is a
    `ValueError` and so looks like a rejection. web3 calls `raise_for_status()`
    before decoding, so a non-200 never reaches the decoder — but a proxy or
    load balancer that returns HTTP 200 with an HTML error body does, and the
    transaction's fate is just as unknown as a dropped connection's.
    """
    return isinstance(exc, (OSError, ProviderConnectionError, json.JSONDecodeError))


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


def register_all(
    w3, account, registry, work, runlog, fees, gas, network, action=REGISTER
) -> RunResult:
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
                action=action,
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
        if action == REGISTER:
            tx = registry.build_register(
                peer_bytes=item.entry.peer_bytes,
                metadata=item.metadata,
                nonce=nonce,
                fees=fees,
                gas=gas,
            )
        elif action == DEREGISTER:
            tx = registry.build_deregister(
                peer_bytes=item.entry.peer_bytes, nonce=nonce, fees=fees, gas=gas
            )
        else:
            tx = registry.build_withdraw(
                peer_bytes=item.entry.peer_bytes, nonce=nonce, fees=fees, gas=gas
            )
        print(f"[{position}/{total}] {label}", flush=True)

        # Sign before sending so the hash is known even if the send fails: it is
        # a function of the signed payload, so it identifies the transaction
        # whether or not the node ever answers. A signing failure is local and
        # leaves tx_hash None, which the handlers below allow for.
        raw = raw_hash = None
        tx_hash = None

        try:
            raw, raw_hash = sign_tx(account, tx)
            tx_hash = raw_hash.hex()
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
            if tx_hash is not None and is_transport_error(exc):
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
            # and refused it — and a signing failure never reached the node at
            # all — so nothing reached the mempool and the nonce stays free for
            # the next attempt.
            runlog.append(
                Record(
                    peer_id=peer_id,
                    status=FAILED,
                    name=item.name,
                    tx_hash=tx_hash,
                    error=str(exc),
                    timestamp=utc_now(),
                    network=network,
                    action=action,
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
                    action=action,
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
                    action=action,
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


STATE_LABEL = {
    UNREGISTERED: "not registered",
    ACTIVE: "active",
    REGISTERING: "registering (waiting for the epoch to start)",
    "deregistering": "deregistering (waiting for the epoch)",
    LOCKED: "locked",
    WITHDRAWABLE: "withdrawable",
    FOREIGN: "owned by another account",
}


def status_rows(entries, states, lock_period, l1_block):
    """One printable row per peer ID, plus the CSV record behind it."""
    rows = []
    for entry, st in zip(entries, states):
        detail = ""
        if st.state == LOCKED and st.unlock_block:
            days = (st.unlock_block - l1_block) * 12 / 86400
            detail = f"unlocks in ~{days:.1f} days (L1 block {st.unlock_block:,})"
        elif st.state == WITHDRAWABLE:
            detail = f"{format_units(st.bond, 18)} SQD ready to withdraw"
        elif st.state == REGISTERING and st.registered_at:
            hours = (st.registered_at - l1_block) * 12 / 3600
            detail = (
                f"goes live at L1 block {st.registered_at:,} (~{hours:.1f} h)"
                if st.registered_at > l1_block
                else f"goes live at L1 block {st.registered_at:,}"
            )
        elif st.state == FOREIGN:
            detail = f"creator {st.creator}"
        rows.append((entry.peer_id, st, detail))
    return rows


def write_status_csv(path: str, rows) -> int:
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ("peer_id", "state", "worker_id", "bond", "unlock_block", "detail")
        )
        for peer_id, st, detail in rows:
            writer.writerow(
                [
                    peer_id,
                    st.state,
                    st.worker_id or "",
                    format_units(st.bond, 18) if st.bond else "",
                    st.unlock_block or "",
                    detail,
                ]
            )
    return len(rows)


def run_state_action(
    args, network, w3, registry, runlog, account, owner, entries, log_path
):
    """Handle status, deregister and withdraw.

    All three turn on the same on-chain classification, so they share the read
    pass. None of them bonds anything, so there is no SQD balance, allowance or
    approval step — only gas.
    """
    action = args.action
    base = artifact_base(args.peer_id_file)
    lock_period = read_rpc(registry.lock_period, what="lockPeriod read")
    owned = read_rpc(registry.owned_worker_ids, what="owned workers read")
    l1 = read_rpc(l1_block_number, w3, what="L1 block number read")
    states = classify(entries, registry, l1, lock_period, owned)

    print(f"network:     {network.name} (chain {network.chain_id})")
    print(f"wallet:      {owner}")
    label = "in file" if args.peer_id_file else "peer IDs"
    print(f"{label + ':':<12} {len(entries)}")

    if action == STATUS:
        counts = {}
        for st in states:
            counts[st.state] = counts.get(st.state, 0) + 1
        print()
        for state, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>6}  {STATE_LABEL.get(state, state)}")

        rows = status_rows(entries, states, lock_period, l1)
        ready = [r for r in rows if r[1].state == WITHDRAWABLE]
        if ready:
            total = sum(r[1].bond for r in ready)
            print(f"\n  {format_units(total, 18)} SQD is withdrawable now")
        soonest = [r for r in rows if r[1].state == LOCKED and r[1].unlock_block]
        if soonest:
            nearest = min(r[1].unlock_block for r in soonest)
            days = (nearest - l1) * 12 / 86400
            print(f"  next unlock in ~{days:.1f} days")

        csv_path = f"{base}.{network.name}.status.csv"
        try:
            write_status_csv(csv_path, rows)
            print(f"\nfull report written to {csv_path}")
        except OSError as exc:
            print(f"warning: could not write {csv_path}: {exc}", file=sys.stderr)
        return 0

    try:
        work_entries, skipped_logged, not_ready = select_by_state(
            entries, states, runlog, args.limit, network.name, action
        )
    except RunLogError as exc:
        fail(str(exc))
    except OSError as exc:
        fail(f"cannot read the run log {log_path}: {exc}")

    print(f"log:         {log_path}")
    print(f"skipped:     {len(skipped_logged)} logged, {len(not_ready)} not ready")
    print(f"to {action}: {len(work_entries)}")

    if not work_entries:
        print(f"nothing to {action}")
        blocked = {}
        for _entry, st in not_ready:
            blocked[st.state] = blocked.get(st.state, 0) + 1
        for state, count in sorted(blocked.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>6}  {STATE_LABEL.get(state, state)}")
        return 0

    # No names and no metadata: neither call carries any.
    work = prepare(work_entries, None, frozenset())
    estimate = (
        registry.estimate_deregister_gas
        if action == DEREGISTER
        else registry.estimate_withdraw_gas
    )
    raw_gas, exact = read_rpc(
        estimate, work[0].entry.peer_bytes, what=f"{action} gas estimate"
    )
    gas = raw_gas + raw_gas * GAS_BUFFER_PERCENT // 100
    fees = read_rpc(current_fees, w3, what="fee read")
    eth = check_eth(w3, owner, gas, fees, len(work), needs_approval=False)

    print(
        f"gas:         ~{format_units(eth.required, 18)} ETH max"
        f"{'' if exact else ' (estimate unavailable, using fallback)'}"
    )
    print(f"ETH balance: {format_units(eth.balance, 18)} ETH")
    if action == WITHDRAW:
        returning = sum(
            st.bond for entry, st in zip(entries, states)
            if entry.peer_id in {w.entry.peer_id for w in work}
        )
        print(f"returning:   {format_units(returning, 18)} SQD to {owner}")

    if not eth.sufficient:
        fail(
            f"insufficient ETH for gas: need up to "
            f"{format_units(eth.required, 18)} ETH to cover {len(work)} "
            f"transaction(s), hold {format_units(eth.balance, 18)} ETH"
        )

    if args.dry_run:
        print("\n-- dry run, nothing sent --")
        for item in work:
            print(f"  would {action} {item.entry.peer_id}")
        return 0

    if not confirm(f"\n{action.capitalize()} {len(work)} worker(s) on "
                   f"{network.name}?", args.yes):
        print("aborted")
        return 0

    fees = read_rpc(current_fees, w3, what="fee read")
    result = register_all(
        w3, account, registry, work, runlog,
        fees=fees, gas=gas, network=network.name, action=action,
    )

    csv_path = f"{base}.{network.name}.{CSV_NOUN[action]}.csv"
    try:
        rows = write_registered_csv(csv_path, runlog, network.name, action)
        print(f"\nresults written to {csv_path} ({rows} rows)")
    except OSError as exc:
        print(f"warning: could not write {csv_path}: {exc}", file=sys.stderr)

    print(
        f"\n{action}d {result.registered}, failed {result.failed}, "
        f"pending {result.pending}, gas used {result.gas_used}"
    )
    if result.aborted:
        print(f"run stopped: {result.aborted}", file=sys.stderr)
    return 1 if result.failed or result.pending else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    network = NETWORKS[args.network]
    base = artifact_base(args.peer_id_file)
    log_path = args.log or default_log_path(base, network.name)

    if args.address:
        if args.action != STATUS:
            fail(
                f"--address only applies to --action status; --action "
                f"{args.action} acts as whoever holds the credential. "
                "To act on specific peer IDs use --peer-id."
            )
        if not Web3.is_address(args.address):
            fail(
                f"--address is not an Ethereum address: {args.address!r}. "
                "It takes a wallet address (0x...); for a peer ID use --peer-id."
            )

    # Status is read-only, so an address is enough and no key is needed.
    if args.action == STATUS and args.address:
        account = None
        owner = args.address
    else:
        account = load_signer()
        owner = account.address

    w3 = connect(network, args.rpc_url)
    registry = Registry(w3, network, owner)
    runlog = RunLog(log_path)

    try:
        if args.peer_id_file:
            entries, duplicates = parse_file(args.peer_id_file)
        else:
            entries, duplicates = parse_peer_ids(args.peer_ids)
    except PeerIdError as exc:
        fail(str(exc))
    except OSError as exc:
        fail(f"cannot read {args.peer_id_file}: {exc}")

    for warning in duplicates:
        print(f"warning: {warning}", file=sys.stderr)
    if not entries:
        print(f"{base} contains no peer IDs")
        return 0

    if args.peer_ids and args.peer_id_file:
        entries = restrict_to(entries, args.peer_ids)
        print(f"restricted to {len(entries)} of the file's peer ID(s)")

    if args.action != REGISTER:
        return run_state_action(args, network, w3, registry, runlog, account,
                                owner, entries, log_path)

    # Filter first, then name: numbers are handed out from the first unused
    # value, so naming peers that are about to be skipped would burn them.
    try:
        selected, skipped_logged, skipped_onchain = select_work(
            entries, runlog, registry, args.limit, network.name
        )
        # Names already claimed on this network, plus every explicit name in the
        # file — including on lines this run will not touch, so a generated name
        # can never collide with one waiting further down the list.
        taken = runlog.used_names(network.name) | {e.name for e in entries if e.name}
    except RunLogError as exc:
        fail(str(exc))
    except OSError as exc:
        fail(f"cannot read the run log {log_path}: {exc}")

    try:
        work = prepare(
            selected, args.name_template, taken, batch_size=args.batch
        )
    except NamingError as exc:
        fail(str(exc))

    print(f"network:     {network.name} (chain {network.chain_id})")
    print(f"wallet:      {owner}")
    print(f"log:         {log_path}")
    label = "in file" if args.peer_id_file else "peer IDs"
    print(f"{label + ':':<12} {len(entries)}")
    print(f"skipped:     {len(skipped_logged)} logged, {len(skipped_onchain)} on-chain")
    print(f"to register: {len(work)}")

    if not work:
        print("nothing to register")
        return 0

    funds = check_funds(registry, len(work))
    decimals = read_rpc(registry.token_decimals, what="token decimals read")
    gas, exact = gas_limit_for(registry, work)
    fees = read_rpc(current_fees, w3, what="fee read")
    eth = check_eth(
        w3, account.address, gas, fees, len(work), funds.needs_approval
    )

    print(f"bond:        {format_units(funds.bond, decimals)} SQD each")
    print(f"bond total:  {format_units(funds.required, decimals)} SQD")
    print(f"balance:     {format_units(funds.balance, decimals)} SQD")
    print(
        f"gas:         ~{format_units(eth.required, 18)} ETH max"
        f"{'' if exact else ' (estimate unavailable, using fallback)'}"
    )
    print(f"ETH balance: {format_units(eth.balance, 18)} ETH")
    if funds.needs_approval:
        print(
            f"approval:    needed — allowance is "
            f"{format_units(funds.allowance, decimals)} SQD, "
            f"will approve {format_units(funds.required, decimals)} SQD"
        )

    # After the plan, so the shortfall is read in context. Applies to dry runs
    # too: finding this before sending is the whole point of the pre-check.
    if not eth.sufficient:
        fail(
            f"insufficient ETH for gas: need up to "
            f"{format_units(eth.required, 18)} ETH to cover {len(work)} "
            f"transaction(s), hold {format_units(eth.balance, 18)} ETH"
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

    # Regenerated from the log after every real run, so the operator always has
    # a current record even if the run aborted partway.
    csv_path = default_csv_path(base, network.name)
    try:
        rows = write_registered_csv(csv_path, runlog, network.name)
        print(f"\nregistered nodes written to {csv_path} ({rows} rows)")
    except OSError as exc:
        print(f"warning: could not write {csv_path}: {exc}", file=sys.stderr)

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
