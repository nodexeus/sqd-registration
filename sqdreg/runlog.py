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
    """One registration attempt."""

    peer_id: str
    status: str
    name: str | None = None
    tx_hash: str | None = None
    block: int | None = None
    error: str | None = None
    timestamp: str | None = None


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

    def succeeded_peer_ids(self) -> set[str]:
        """Peer IDs a previous run confirmed on-chain."""
        return {r.peer_id for r in self.records() if r.status == SUCCESS}
