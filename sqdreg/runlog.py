"""Append-only JSONL record of registration attempts."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

SUCCESS = "success"
FAILED = "failed"
PENDING = "pending"


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
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                records.append(Record(**json.loads(line)))
        return records

    def succeeded_peer_ids(self, network: str) -> set[str]:
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
            if r.status == SUCCESS and r.network in (None, network)
        }
