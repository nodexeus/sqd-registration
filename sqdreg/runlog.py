"""Append-only JSONL record of registration attempts."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

SUCCESS = "success"
FAILED = "failed"
PENDING = "pending"


class RunLogError(Exception):
    """The run log exists but could not be parsed."""


def utc_now() -> str:
    """Current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Record:
    """One registration attempt.

    The field names are the on-disk schema — `records()` reconstructs each row
    with `Record(**json.loads(line))` — so new fields go on the end with a
    default and existing fields never move.
    """

    peer_id: str
    status: str
    name: str | None = None
    tx_hash: str | None = None
    block: int | None = None
    error: str | None = None
    timestamp: str | None = None
    network: str | None = None
    # Which action produced this record. Defaults to "register" so logs written
    # before the other actions existed still read correctly.
    action: str = "register"


class RunLog:
    """A resumable log of attempts, one JSON object per line."""

    def __init__(self, path):
        self.path = Path(path)

    def append(self, record: Record) -> None:
        """Write one record and flush, so a crash cannot lose it."""
        with self.path.open("a") as handle:
            handle.write(json.dumps(asdict(record)) + "\n")
            handle.flush()

    def records(self) -> list[Record]:
        """Every record in the log, in write order.

        A crash mid-`append` can leave a truncated final line, and that is
        exactly the log a resume reads. Raising a typed error instead of a
        JSONDecodeError lets the caller name the bad line, so the operator
        repairs one line rather than deleting the record of what was bonded.
        """
        if not self.path.exists():
            return []
        records = []
        for number, line in enumerate(self.path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append(Record(**json.loads(line)))
            except (ValueError, TypeError) as exc:
                raise RunLogError(
                    f"{self.path} line {number} is not a valid record: {exc}. "
                    "A crash while writing can truncate the last line; repair or "
                    "remove that one line — do not delete the log, it is the "
                    "record of what has already been bonded."
                ) from exc
        return records

    def succeeded_peer_ids(self, network: str, action: str = "register") -> set[str]:
        """Peer IDs a previous run confirmed on-chain *for this network*.

        A registration on tethys says nothing about mainnet, and the log is
        keyed by input file, so an unscoped filter would let a tethys rehearsal
        make a later mainnet run report "nothing to register" and exit 0 with
        zero nodes actually registered.

        Back-compatibility: a record with no `network` was written before the
        field existed, when a log could only describe whichever network the
        operator happened to run; those still match so existing logs keep
        resuming. A record naming a *different* network never matches.
        """
        return {
            r.peer_id
            for r in self.records()
            if r.status == SUCCESS
            and r.network in (None, network)
            and (r.action or "register") == action
        }

    def used_names(self, network: str) -> set[str]:
        """Names already claimed on this network, so numbering can skip them.

        `pending` counts as claimed: that transaction may well have landed, and
        reusing its number would put two workers under one name. A `failed`
        registration never landed, so its number is genuinely free.
        """
        return {
            r.name
            for r in self.records()
            if r.name
            and r.status in (SUCCESS, PENDING)
            and r.network in (None, network)
            and (r.action or "register") == "register"
        }

    def completed(self, network: str, action: str = "register") -> list[Record]:
        """Confirmed results for one action on this network, in order."""
        return [
            r
            for r in self.records()
            if r.status == SUCCESS
            and r.network in (None, network)
            and (r.action or "register") == action
        ]
