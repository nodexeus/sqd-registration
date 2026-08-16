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

from sqdreg.naming import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_DESCRIPTION,
    DEFAULT_WEBSITE,
    NamingError,
    prepare,
)
from sqdreg.networks import NETWORKS
from sqdreg.peerids import PeerIdError, parse_file, parse_peer_ids
from sqdreg.registry import (
    ACTIVE,
    DirectCalls,
    VestingCalls,
    REGISTERED,
    FALLBACK_REGISTER_GAS,
    Treasury,
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
CLAIM = "claim"
STATUS = "status"
ACTIONS = (REGISTER, DEREGISTER, WITHDRAW, CLAIM, STATUS)
# Actions that need no peer IDs at all. Rewards are claimed per wallet: the
# distributor sweeps every worker the wallet owns in one transaction.
WALLET_ACTIONS = (CLAIM,)

LOCAL_SIGNER = "local"
FIREBLOCKS_SIGNER = "fireblocks"
SIGNERS = (LOCAL_SIGNER, FIREBLOCKS_SIGNER)

# Fireblocks queues each transaction for policy evaluation and MPC signing
# before it is broadcast, so the round trip is materially slower than a local
# signature. The receipt wait has to allow for that on top of block time.
FIREBLOCKS_RECEIPT_TIMEOUT = 900

# The worker state each action requires. register() also accepts a slot this
# account previously vacated, which reads as UNREGISTERED.
ACTIONABLE_STATE = {
    REGISTER: UNREGISTERED,
    DEREGISTER: ACTIVE,
    WITHDRAW: WITHDRAWABLE,
}

# What the CSV of confirmed results is called, per action.
CSV_NOUN = {
    REGISTER: "registered",
    DEREGISTER: "deregistered",
    WITHDRAW: "withdrawn",
    CLAIM: "claimed",
}
MAX_CONSECUTIVE_FAILURES = 3
RECEIPT_TIMEOUT = 300
GAS_BUFFER_PERCENT = 25

# Where a gas limit came from, so the plan can say so honestly.
GAS_MEASURED = "measured"   # estimated against the chain
GAS_DEFERRED = "deferred"   # not attempted yet: it would revert pre-approval
GAS_FALLBACK = "fallback"   # attempted and reverted
GAS_BASIS_NOTE = {
    GAS_MEASURED: "",
    GAS_DEFERRED: " (projected; measured once the approval lands)",
    GAS_FALLBACK: " (estimate unavailable, using fallback)",
}
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
            "register (default), deregister, withdraw, claim, or status. "
            "claim sweeps every reward the wallet has earned in one "
            "transaction and needs no peer IDs; status is a read-only report"
        ),
    )
    parser.add_argument(
        "--website",
        default=DEFAULT_WEBSITE,
        help=(
            "website recorded on every worker, shown by the SQD dashboard "
            f"(default: {DEFAULT_WEBSITE}). Pass an empty string to omit it"
        ),
    )
    parser.add_argument(
        "--description",
        default=DEFAULT_DESCRIPTION,
        help=(
            "description recorded on every worker "
            f"(default: {DEFAULT_DESCRIPTION!r}). Pass an empty string to omit it"
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
        "--via-vesting",
        metavar="ADDRESS",
        help=(
            "route every call through this holding contract's execute(). "
            "Required when the workers were registered by a vesting contract: "
            "it is their creator, so register/deregister/withdraw check against "
            "it and not against the signing account. Use tools/owners.py to "
            "find which contract holds which peer ID"
        ),
    )
    parser.add_argument(
        "--signer",
        choices=SIGNERS,
        default=LOCAL_SIGNER,
        help=(
            "local (default) signs with PRIVATE_KEY/MNEMONIC/prompt. "
            "fireblocks sends unsigned transactions over eth_sendTransaction "
            "for the RPC endpoint to sign — point --rpc-url at a running "
            "fireblocks-json-rpc, which holds no exportable key"
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
    if (
        not args.peer_id_file
        and not args.peer_ids
        and args.action not in WALLET_ACTIONS
    ):
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
    # Echoed only when they differ from the default, which includes the case of
    # being deliberately emptied: without that, a resume would silently put the
    # default branding back on the remaining nodes.
    if args.website != DEFAULT_WEBSITE:
        parts += ["--website", shlex.quote(args.website)]
    if args.description != DEFAULT_DESCRIPTION:
        parts += ["--description", shlex.quote(args.description)]
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


def provider_for(endpoint: str):
    """Pick a provider from the endpoint's shape.

    fireblocks-json-rpc listens on a unix socket by default and prints a
    filesystem path rather than a URL, so an HTTP-only client cannot talk to it
    unless it was started with --http.
    """
    if endpoint.startswith(("http://", "https://")):
        return Web3.HTTPProvider(endpoint)
    if endpoint.startswith(("ws://", "wss://")):
        return Web3.LegacyWebSocketProvider(endpoint)
    return Web3.IPCProvider(endpoint)


def resolve_rpc_url(args, network) -> str:
    """The endpoint for this run.

    Under --signer fireblocks, fall back to the address the proxy exports into
    its child's environment, so the wrapped command needs no plumbing of its
    own — and so the shell cannot expand that variable before the proxy has
    even set it.
    """
    if args.rpc_url:
        return args.rpc_url
    if args.signer == FIREBLOCKS_SIGNER:
        exported = os.getenv("FIREBLOCKS_JSON_RPC_ADDRESS")
        if exported:
            return exported
        fail(
            "--signer fireblocks needs the fireblocks-json-rpc endpoint.\n"
            "       Either run this command as a child of the proxy, which "
            "exports FIREBLOCKS_JSON_RPC_ADDRESS:\n"
            "           fireblocks-json-rpc --chainId "
            f"{network.chain_id} -- <this command>\n"
            "       or pass --rpc-url with the address it prints."
        )
    return network.rpc_url


def connect(network, rpc_url: str) -> Web3:
    """Connect to the RPC and refuse to continue on the wrong chain."""
    endpoint = rpc_url
    w3 = Web3(provider_for(endpoint))
    try:
        chain_id = w3.eth.chain_id
    except Exception as exc:
        # An error *response* means something answered, so blaming the endpoint
        # for being unreachable misdirects. A signing proxy in particular
        # relays failures from whatever node it talks to, and those read as if
        # the proxy itself were at fault.
        if is_transport_error(exc):
            fail(f"cannot reach RPC endpoint {endpoint}: {exc}")
        fail(
            f"the RPC endpoint {endpoint} answered with an error: {exc}\n"
            "       Something is listening, so this is the node's complaint "
            "rather than a connection\n"
            "       problem. Behind a signing proxy it is usually the node the "
            "proxy talks to, not the\n"
            "       proxy itself — check what the proxy was told to use "
            "upstream."
        )
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
    # Fetched once. Needed to tell a slot this account vacated (re-registerable)
    # from one somebody else vacated (register() would revert).
    owned = read_rpc(registry.owned_worker_ids, what="owned workers read")

    work = []
    skipped_onchain: list[str] = []
    skipped_foreign: list[str] = []

    for entry in entries:
        if entry.peer_id in already_done:
            continue
        state = read_rpc(
            registry.registration_state,
            entry.peer_bytes,
            owned,
            what=f"registration lookup for {entry.peer_id}",
        )
        if state == REGISTERED:
            skipped_onchain.append(entry.peer_id)
            continue
        if state == FOREIGN:
            skipped_foreign.append(entry.peer_id)
            continue
        work.append(entry)
        if limit is not None and len(work) >= limit:
            break

    return work, skipped_logged, skipped_onchain, skipped_foreign


@dataclass
class FundsCheck:
    """The bond position for a planned run."""

    bond: int
    required: int
    balance: int
    allowance: int
    needs_approval: bool


def check_funds(registry, count: int, dry_run: bool = False) -> FundsCheck:
    """Verify the wallet can bond `count` workers."""
    bond = read_rpc(registry.bond_amount, what="bondAmount() read")
    required = bond * count
    balance = read_rpc(registry.sqd_balance, what="SQD balance read")
    allowance = read_rpc(registry.allowance, what="SQD allowance read")

    if balance < required:
        decimals = read_rpc(registry.token_decimals, what="token decimals read")
        shortfall(
            f"insufficient SQD: need {format_units(required, decimals)} "
            f"to bond {count} workers, hold {format_units(balance, decimals)}",
            dry_run,
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


def shortfall(message: str, dry_run: bool) -> None:
    """Report a funding problem, fatally on a real run.

    A dry run is a report rather than a gate: it should surface every problem
    at once and still exit 0, so it can be looped over many files. A real run
    stops, because sending part of a batch and then running dry is worse than
    not starting.
    """
    if dry_run:
        print(f"SHORTFALL:   {message}", file=sys.stderr)
    else:
        fail(message)


def gas_payer(signer, acting: str, required: str | None) -> str:
    """Whose ETH pays for the transactions.

    Not always the account being acted upon. Through a vesting contract the
    contract owns the workers and holds the SQD, but the transaction is sent by
    its beneficiary, so the beneficiary pays the gas. Checking the contract's
    balance would report a shortfall on an account that never spends anything.
    """
    if signer is not None:
        return signer.address
    return required or acting


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


def gas_limit_for(registry, work, estimate: bool = True) -> tuple[int, str]:
    """Pick one gas limit for every registration in the run.

    Gas scales with metadata length and one limit is reused for the whole run,
    so the measurement is taken against the *longest* metadata — the most
    expensive call. A shorter name can then never exceed it. The result is
    padded to absorb ordinary variation.

    Pass `estimate=False` when the allowance is not yet in place. register()
    calls transferFrom, so estimating before the approval is a call we know will
    revert: it wastes a round trip, and a signing provider reports the revert as
    an error — an alarming thing to show an operator about to move millions,
    for a call that was never going to succeed.

    Returns (gas, basis) where basis explains where the number came from.
    """
    if not work:
        fail("no peers to register")
    longest = max(work, key=lambda candidate: len(candidate.metadata.encode()))
    if not estimate:
        raw, basis = FALLBACK_REGISTER_GAS, GAS_DEFERRED
    else:
        raw, exact = registry.estimate_register_gas(
            longest.entry.peer_bytes, longest.metadata
        )
        basis = GAS_MEASURED if exact else GAS_FALLBACK
    return raw + raw * GAS_BUFFER_PERCENT // 100, basis


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


class LocalSigner:
    """Signs with a key this process holds.

    Knows the hash before broadcasting, which is what lets a failed send still
    be logged with something the operator can look up.
    """

    manages_nonce = False
    receipt_timeout = RECEIPT_TIMEOUT

    def __init__(self, account):
        self.account = account
        self.address = account.address

    def prepare(self, tx):
        """Sign, returning (payload, hash). The hash is known before sending."""
        signed = self.account.sign_transaction(tx)
        return signed, signed.hash

    def dispatch(self, w3, payload):
        w3.eth.send_raw_transaction(payload.raw_transaction)
        return None  # the hash came from prepare()


class RemoteSigner:
    """Hands unsigned transactions to the RPC endpoint to sign.

    For Fireblocks, where the key is split across MPC shares and cannot be
    exported: `fireblocks-json-rpc` accepts eth_sendTransaction, applies the
    workspace's policy, signs, and broadcasts.

    Two consequences the local path does not have:

    - The nonce belongs to the signer. Fireblocks keeps its own sequence per
      vault account, so supplying one would fight it.
    - A failed submit yields no hash, because the hash only exists once the
      remote side has signed. Recovery is the Fireblocks console, which records
      every transaction it was asked to sign.
    """

    manages_nonce = True
    receipt_timeout = FIREBLOCKS_RECEIPT_TIMEOUT

    def __init__(self, address: str):
        self.account = None
        self.address = address

    def prepare(self, tx):
        """Nothing to sign here, so no hash exists yet."""
        return tx, None

    def dispatch(self, w3, payload):
        return w3.eth.send_transaction(payload)


def build_signer(args, w3, required: str | None = None):
    """The signer for this run: a local key, or the RPC endpoint itself."""
    if args.signer != FIREBLOCKS_SIGNER:
        return LocalSigner(load_signer())

    try:
        accounts = list(w3.eth.accounts)
    except Exception as exc:
        # Only a transport failure means nothing is listening. The proxy
        # answers eth_accounts by enumerating the vault's asset wallets, so an
        # error *response* is about the workspace, and repeating install
        # instructions here sends the operator after the wrong thing.
        if is_transport_error(exc):
            fail(
                f"cannot reach the signer at "
                f"{args.rpc_url or 'the RPC endpoint'}: {exc}\n"
                "       --signer fireblocks needs a running "
                "fireblocks-json-rpc\n"
                "       (npm install @fireblocks/fireblocks-json-rpc)"
            )
        fail(
            f"the signer rejected eth_accounts: {exc}\n"
            "       The proxy is running and answered, so this is a Fireblocks "
            "workspace question.\n"
            "       'No <ASSET> asset wallet found' means that vault account "
            "has no wallet for this\n"
            "       chain: add the asset in the Fireblocks console, check "
            "FIREBLOCKS_VAULT_ACCOUNT_IDS,\n"
            "       and check --network — ETH-AETH is Arbitrum One, "
            "ETH-AETH_SEPOLIA is tethys."
        )
    if not accounts:
        fail(
            f"{args.rpc_url or 'the RPC endpoint'} reports no accounts, so it "
            "cannot sign.\n"
            "       An ordinary RPC node answers eth_accounts with an empty "
            "list, so the usual cause is\n"
            "       --rpc-url pointing at a node rather than at a running "
            "fireblocks-json-rpc.\n"
            "       If it is the proxy, check the vault account has this chain's "
            "asset enabled."
        )
    # A job can span accounts in different places: one in Fireblocks, others
    # held directly. Saying which account this run needs, and how to sign for
    # it locally, beats failing on a vault that was never going to hold it.
    if required and required.lower() not in {a.lower() for a in accounts}:
        fail(
            f"this run must be signed by {required}, which the Fireblocks "
            "vault does not hold.\n"
            f"       It offers: {', '.join(accounts)}\n"
            "       If that account's key is held elsewhere, add --signer local "
            "to use it for this\n"
            "       run; fireblocks.env can stay in place for the runs that do "
            "need it."
        )

    by_address = {a.lower(): a for a in accounts}
    if args.address:
        chosen = by_address.get(args.address.lower())
        if chosen is None:
            fail(
                f"--address {args.address} is not offered by the signer. "
                f"Available: {', '.join(accounts)}"
            )
    elif required and required.lower() in by_address:
        # A workspace can expose several vault accounts at once. Picking the
        # one this run actually needs means a job spanning many accounts is
        # configured once, rather than selected by hand per run.
        chosen = by_address[required.lower()]
        if len(accounts) > 1:
            print(f"vault account: {chosen} (of {len(accounts)} offered)")
    else:
        chosen = accounts[0]
        if len(accounts) > 1:
            print(
                f"warning: signer offers {len(accounts)} accounts and this run "
                f"does not name one; using {chosen}. Pass --address to choose.",
                file=sys.stderr,
            )
    return RemoteSigner(w3.to_checksum_address(chosen))


def tx_hash_hex(value) -> str:
    """A transaction hash as an operator can paste it into a block explorer.

    hexbytes 1.x dropped the 0x prefix from .hex(), so hashes were being logged
    and written to the CSV bare — not clickable, and not pasteable without
    editing every row.
    """
    text = value.hex() if hasattr(value, "hex") else str(value)
    return text if text.startswith("0x") else f"0x{text}"


def wait_for(w3, tx_hash, timeout: int = RECEIPT_TIMEOUT) -> dict:
    """Wait for one receipt. Raises TimeExhausted past `timeout`."""
    return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)


def send_and_wait(w3, signer, tx, label: str = "transaction") -> tuple[str, dict]:
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
        payload, raw_hash = signer.prepare(tx)
        if raw_hash is not None:
            tx_hash = tx_hash_hex(raw_hash)
        sent = signer.dispatch(w3, payload)
        if raw_hash is None:
            raw_hash = sent
            tx_hash = tx_hash_hex(raw_hash)
    except Exception as exc:
        raise SendFailed(exc, tx_hash) from exc
    print(f"  {label} sent ({tx_hash}); waiting for the receipt", flush=True)
    try:
        return tx_hash, wait_for(w3, raw_hash, signer.receipt_timeout)
    except Exception as exc:
        raise SendFailed(exc, tx_hash) from exc


def register_all(
    w3, signer, registry, work, runlog, fees, gas, network, action=REGISTER,
    calls=None, bond=0,
) -> RunResult:
    """Act on each peer in turn, logging every attempt as it resolves."""
    result = RunResult()
    # A remote signer keeps its own nonce sequence, so we neither read nor
    # advance one; `None` omits the field from every transaction.
    nonce = (
        None
        if signer.manages_nonce
        else read_rpc(
            w3.eth.get_transaction_count, signer.address, what="nonce read"
        )
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
        if calls is None:
            calls = DirectCalls(signer.address)
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
        # Through a holding contract, execute() approves the bond itself right
        # before forwarding, so registration needs no separate allowance and
        # leaves none standing. The other actions move nothing in.
        tx = calls.wrap(tx, bond if action == REGISTER else 0)
        print(f"[{position}/{total}] {label}", flush=True)

        # A local signer knows the hash before broadcasting, so a failed send
        # is still identifiable. A remote signer does not: the hash exists only
        # once it has signed, so tx_hash stays None on failure and recovery is
        # its own console. Both handlers below allow for None.
        raw_hash = None
        tx_hash = None
        # Whether the transaction left this process. A failure before that is
        # local and definitely sent nothing; a transport failure after it leaves
        # the outcome unknown, whether or not a hash is available.
        dispatched = False

        try:
            payload, raw_hash = signer.prepare(tx)
            if raw_hash is not None:
                tx_hash = tx_hash_hex(raw_hash)
            dispatched = True
            sent = signer.dispatch(w3, payload)
            if raw_hash is None:
                raw_hash = sent
                tx_hash = tx_hash_hex(raw_hash)
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
            if dispatched and is_transport_error(exc):
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

        # Only advance a nonce we own; a remote signer keeps its own sequence.
        if nonce is not None:
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
    """One row per peer ID for the status CSV.

    No thousands separators in any field: a comma inside a value forces the
    writer to quote it, which is valid CSV but trips naive importers and makes
    shell tools like cut and awk split the row in the wrong place.
    """
    rows = []
    for entry, st in zip(entries, states):
        detail = ""
        if st.state == LOCKED and st.unlock_block:
            days = (st.unlock_block - l1_block) * 12 / 86400
            detail = f"unlocks in ~{days:.1f} days at L1 block {st.unlock_block}"
        elif st.state == WITHDRAWABLE:
            detail = f"{format_units(st.bond, 18)} SQD ready to withdraw"
        elif st.state == REGISTERING and st.registered_at:
            hours = (st.registered_at - l1_block) * 12 / 3600
            detail = (
                f"goes live in ~{hours:.1f} h at L1 block {st.registered_at}"
                if st.registered_at > l1_block
                else f"goes live at L1 block {st.registered_at}"
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
    args, network, w3, registry, runlog, signer, owner, entries, log_path, calls,
    payer,
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

        # Rewards are per wallet, not per peer ID, so they belong in the
        # summary rather than a column.
        treasury = Treasury(w3, network, owner)
        claimable = read_rpc(treasury.claimable, what="claimable read")
        if claimable:
            decimals = read_rpc(registry.token_decimals, what="token decimals read")
            print(
                f"  {format_units(claimable, decimals)} SQD in rewards is "
                f"claimable (--action claim)"
            )

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
    eth = check_eth(w3, payer, gas, fees, len(work), needs_approval=False)

    print(
        f"gas:         ~{format_units(eth.required, 18)} ETH max"
        f"{'' if exact else ' (estimate unavailable, using fallback)'}"
    )
    print(f"ETH balance: {format_units(eth.balance, 18)} ETH ({payer})")
    if action == WITHDRAW:
        returning = sum(
            st.bond for entry, st in zip(entries, states)
            if entry.peer_id in {w.entry.peer_id for w in work}
        )
        print(f"returning:   {format_units(returning, 18)} SQD to {owner}")

    if not eth.sufficient:
        shortfall(
            f"insufficient ETH for gas: need up to "
            f"{format_units(eth.required, 18)} ETH to cover {len(work)} "
            f"transaction(s), hold {format_units(eth.balance, 18)} ETH",
            args.dry_run,
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
        w3, signer, registry, work, runlog,
        fees=fees, gas=gas, network=network.name, action=action, calls=calls,
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


def run_claim(args, network, w3, registry, runlog, signer, owner, calls, payer):
    """Sweep every reward this wallet has earned, in one transaction.

    Needs no peer IDs: the distributor loops over getOwnedWorkers(msg.sender)
    internally, zeroing each worker's balance and adding staking rewards, so
    the whole fleet is claimed at once.
    """
    treasury = Treasury(w3, network, owner)
    decimals = read_rpc(registry.token_decimals, what="token decimals read")
    claimable = read_rpc(treasury.claimable, what="claimable read")
    workers = read_rpc(registry.owned_worker_ids, what="owned workers read")

    print(f"network:     {network.name} (chain {network.chain_id})")
    print(f"wallet:      {owner}")
    print(f"workers:     {len(workers)}")
    print(f"claimable:   {format_units(claimable, decimals)} SQD")

    if claimable == 0:
        print("nothing to claim")
        return 0

    raw_gas, exact = read_rpc(
        treasury.estimate_claim_gas, len(workers), what="claim gas estimate"
    )
    gas = raw_gas + raw_gas * GAS_BUFFER_PERCENT // 100
    fees = read_rpc(current_fees, w3, what="fee read")
    eth = check_eth(w3, payer, gas, fees, 1, needs_approval=False)

    print(
        f"gas:         ~{format_units(eth.required, 18)} ETH max"
        f"{'' if exact else ' (estimate unavailable, projected from fleet size)'}"
    )
    print(f"ETH balance: {format_units(eth.balance, 18)} ETH ({payer})")

    if not eth.sufficient:
        shortfall(
            f"insufficient ETH for gas: need up to "
            f"{format_units(eth.required, 18)} ETH, hold "
            f"{format_units(eth.balance, 18)} ETH",
            args.dry_run,
        )

    if args.dry_run:
        print("\n-- dry run, nothing sent --")
        print(f"  would claim {format_units(claimable, decimals)} SQD to {owner}")
        return 0

    if not confirm(
        f"\nClaim {format_units(claimable, decimals)} SQD on {network.name}?",
        args.yes,
    ):
        print("aborted")
        return 0

    fees = read_rpc(current_fees, w3, what="fee read")
    nonce = (
        None
        if signer.manages_nonce
        else read_rpc(w3.eth.get_transaction_count, owner, what="nonce read")
    )
    amount = format_units(claimable, decimals)
    try:
        tx_hash, receipt = send_and_wait(
            w3,
            signer,
            calls.wrap(treasury.build_claim(nonce, fees, gas)),
            label="claim",
        )
    except SendFailed as exc:
        runlog.append(
            Record(
                peer_id=owner, status=PENDING, tx_hash=exc.tx_hash,
                error=str(exc), timestamp=utc_now(),
                network=network.name, action=CLAIM, amount=amount,
            )
        )
        fail(f"claim did not confirm: {exc}")

    status = SUCCESS if receipt["status"] == 1 else FAILED
    runlog.append(
        Record(
            peer_id=owner, status=status, tx_hash=tx_hash,
            block=receipt["blockNumber"], timestamp=utc_now(),
            network=network.name, action=CLAIM, amount=amount,
            error=None if status == SUCCESS else "transaction reverted",
        )
    )
    if status != SUCCESS:
        fail(f"claim reverted ({tx_hash})")

    print(f"\nclaimed {amount} SQD in block {receipt['blockNumber']} ({tx_hash})")
    return 0


def detect_signing_context(w3, network, entries):
    """Work out who must sign, from the workers themselves.

    Reads creators until it finds a registered worker, then classifies that
    creator. Returns (creator, kind, controller) where controller is the
    account that must sign: the creator itself for a wallet, or the holding
    contract's owner() for a vesting contract.

    Returns None when nothing in the file is registered, which is not an error
    here — the caller reports "nothing to do" rather than guessing.
    """
    from tools.owners import classify_owner

    probe = Registry(w3, network, "0x" + "0" * 40)
    for entry in entries:
        worker_id = read_rpc(
            probe.contract.functions.workerIds(entry.peer_bytes).call,
            what=f"creator lookup for {entry.peer_id}",
        )
        if worker_id == 0:
            continue
        creator = read_rpc(
            probe.contract.functions.getWorker(worker_id).call,
            what=f"worker read for {entry.peer_id}",
        )[0]
        if int(creator, 16) == 0:
            continue  # withdrawn: the creator is zeroed
        kind, controller = classify_owner(w3, creator)
        return creator, kind, controller
    return None


def signing_context(args, network, w3, entries):
    """Decide the acting account and how calls reach it.

    For deregister and withdraw the answer is already on chain: the workers
    name their creator. Detecting it means the operator runs one command per
    file rather than looking each contract up by hand, and it lets the required
    signer be stated before a credential is asked for.

    Returns (calls, acting, required_signer).
    """
    if args.via_vesting:
        calls = VestingCalls(w3, network, args.via_vesting, None)
        controller = read_rpc(calls.controller, what="vesting owner() read")
        return calls, calls.address, controller

    # Rewards accrue to whichever account registered the workers, so a peer ID
    # file identifies the account to claim for just as it does for deregister.
    # Registration is the exception: nothing exists yet to read a creator from.
    if args.action not in (DEREGISTER, WITHDRAW, CLAIM):
        return None, None, None
    if not entries:
        return None, None, None

    detected = detect_signing_context(w3, network, entries)
    if detected is None:
        return None, None, None
    creator, kind, controller = detected

    if kind == "vesting":
        label = "rewards held by" if args.action == CLAIM else "held by"
        print(f"{label}: {creator} (a vesting contract)")
        print(f"must be signed by its owner: {controller}")
        return VestingCalls(w3, network, creator, None), creator, controller
    if kind == "contract":
        fail(
            f"these workers were registered by {creator}, which is a contract "
            "but not a recognised\n"
            "       vesting contract. Whatever mechanism it provides has to "
            "drive the call; this tool\n"
            "       cannot infer it. Use --via-vesting if it exposes a "
            "compatible execute()."
        )
    print(f"registered by: {creator}")
    return None, creator, creator



def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    network = NETWORKS[args.network]
    base = artifact_base(args.peer_id_file)
    log_path = args.log or default_log_path(base, network.name)

    # Checked before connecting or asking for a credential: a typo here should
    # not cost a prompt or a round trip.
    if args.via_vesting and not Web3.is_address(args.via_vesting):
        fail(f"--via-vesting is not an address: {args.via_vesting!r}")

    if args.address:
        if (
            args.action != STATUS
            and args.signer != FIREBLOCKS_SIGNER
            and not args.dry_run
        ):
            fail(
                f"--address only applies to --action status, to a --dry-run, "
                f"or to --signer fireblocks where it selects which vault "
                f"account to use; --action {args.action} otherwise acts as "
                "whoever holds the credential. To act on specific peer IDs "
                "use --peer-id."
            )
        if not Web3.is_address(args.address):
            fail(
                f"--address is not an Ethereum address: {args.address!r}. "
                "It takes a wallet address (0x...); for a peer ID use --peer-id."
            )

    w3 = connect(network, resolve_rpc_url(args, network))

    # The file is read before any credential is asked for: for deregister and
    # withdraw the workers themselves say who must sign, so the operator can be
    # told which account is needed rather than guessing and hitting a revert.
    # Parsed whenever one is supplied. claim does not act on peer IDs, but a
    # file still identifies the account whose rewards they are.
    entries, duplicates = [], []
    if args.peer_id_file or args.peer_ids:
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

        if args.peer_ids and args.peer_id_file:
            entries = restrict_to(entries, args.peer_ids)
            print(f"restricted to {len(entries)} of the file's peer ID(s)")

    calls, acting, required = signing_context(args, network, w3, entries)

    # Nothing below sends anything unless a signer exists, so a credential is
    # only worth asking for when one will actually be used. A dry run that
    # already knows whose position to report — from --address, --via-vesting or
    # the workers themselves — needs no key, which makes a whole pre-flight
    # across many files possible without unlocking anything.
    inspect_only = args.action == STATUS or args.dry_run
    known_address = args.address or acting

    if inspect_only and known_address:
        signer = None
        owner = known_address
        if args.dry_run:
            print("(dry run: no credential needed)")
    else:
        signer = build_signer(args, w3, required)
        owner = signer.address

    if signer is not None and required and required.lower() != owner.lower():
        fail(
            f"these workers can only be acted on by {required}, but this run "
            f"signs as {owner}.\n"
            "       Use that account's credential, or --peer-id to select "
            "workers this one owns."
        )
    if signer is None and required:
        print(f"would be signed by: {required}")

    payer = gas_payer(signer, acting or owner, required)

    if calls is None:
        calls = DirectCalls(owner)
    if acting is None:
        acting = owner
    calls.signer_address = owner

    registry = Registry(w3, network, acting)
    runlog = RunLog(log_path)

    if args.action == CLAIM:
        return run_claim(
            args, network, w3, registry, runlog, signer, acting, calls, payer
        )

    if not entries:
        print(f"{base} contains no peer IDs")
        return 0

    if args.action != REGISTER:
        return run_state_action(args, network, w3, registry, runlog, signer,
                                acting, entries, log_path, calls, payer)

    # Filter first, then name: numbers are handed out from the first unused
    # value, so naming peers that are about to be skipped would burn them.
    try:
        selected, skipped_logged, skipped_onchain, skipped_foreign = select_work(
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
            selected,
            args.name_template,
            taken,
            batch_size=args.batch,
            website=args.website,
            description=args.description,
        )
    except NamingError as exc:
        fail(str(exc))

    print(f"network:     {network.name} (chain {network.chain_id})")
    print(f"wallet:      {acting}")
    print(f"log:         {log_path}")
    label = "in file" if args.peer_id_file else "peer IDs"
    print(f"{label + ':':<12} {len(entries)}")
    skipped_note = f"{len(skipped_logged)} logged, {len(skipped_onchain)} on-chain"
    if skipped_foreign:
        skipped_note += f", {len(skipped_foreign)} owned by another account"
    print(f"skipped:     {skipped_note}")
    print(f"to register: {len(work)}")

    if not work:
        print("nothing to register")
        return 0

    funds = check_funds(registry, len(work), dry_run=args.dry_run)
    decimals = read_rpc(registry.token_decimals, what="token decimals read")
    # register() reverts while the allowance is missing, so do not ask.
    gas, gas_basis = gas_limit_for(registry, work, estimate=not funds.needs_approval)
    fees = read_rpc(current_fees, w3, what="fee read")
    eth = check_eth(
        w3, payer, gas, fees, len(work), funds.needs_approval
    )

    print(f"bond:        {format_units(funds.bond, decimals)} SQD each")
    print(f"bond total:  {format_units(funds.required, decimals)} SQD")
    print(f"balance:     {format_units(funds.balance, decimals)} SQD")
    print(
        f"gas:         ~{format_units(eth.required, 18)} ETH max"
        f"{GAS_BASIS_NOTE[gas_basis]}"
    )
    print(f"ETH balance: {format_units(eth.balance, 18)} ETH ({payer})")
    if funds.needs_approval:
        print(
            f"approval:    needed — allowance is "
            f"{format_units(funds.allowance, decimals)} SQD, "
            f"will approve {format_units(funds.required, decimals)} SQD"
        )

    # After the plan, so the shortfall is read in context. Applies to dry runs
    # too: finding this before sending is the whole point of the pre-check.
    if not eth.sufficient:
        shortfall(
            f"insufficient ETH for gas: need up to "
            f"{format_units(eth.required, 18)} ETH to cover {len(work)} "
            f"transaction(s), hold {format_units(eth.balance, 18)} ETH",
            args.dry_run,
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

    if funds.needs_approval and args.via_vesting:
        print(
            "approval:    not needed — execute() approves each bond itself, so "
            "no allowance is left standing"
        )
    elif funds.needs_approval:
        approve_nonce = (
            None
            if signer.manages_nonce
            else read_rpc(w3.eth.get_transaction_count, owner, what="nonce read")
        )
        approve_tx = registry.build_approve(
            amount=funds.required, nonce=approve_nonce, fees=fees
        )
        print("approving bond transfer...")
        try:
            tx_hash, receipt = send_and_wait(
                w3, signer, approve_tx, label="approval"
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
    gas, gas_basis = gas_limit_for(registry, work)
    print(f"gas limit:   {gas} per registration{GAS_BASIS_NOTE[gas_basis]}")

    result = register_all(
        w3,
        signer,
        registry,
        work,
        runlog,
        fees=fees,
        gas=gas,
        network=network.name,
        calls=calls,
        bond=funds.bond,
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
        - len(skipped_foreign)
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
