"""Parsing and validation of libp2p peer ID files."""

from dataclasses import dataclass
from pathlib import Path

import base58

MULTIHASH_IDENTITY = 0x00
MULTIHASH_SHA2_256 = 0x12
_ALLOWED_CODES = (MULTIHASH_IDENTITY, MULTIHASH_SHA2_256)
_MIN_LEN = 32
# The contract enforces `peerId.length <= 64`, so anything longer is
# guaranteed to revert.
_MAX_LEN = 64


class PeerIdError(ValueError):
    """A peer ID line failed to decode or validate."""


@dataclass(frozen=True)
class PeerEntry:
    """One validated line of the input file."""

    peer_id: str
    peer_bytes: bytes
    name: str | None
    index: int


def decode_peer_id(peer_id: str) -> bytes:
    """Decode a base58 peer ID to the raw multihash bytes `register` expects."""
    try:
        raw = base58.b58decode(peer_id)
    except ValueError as exc:
        raise PeerIdError(f"{peer_id!r} is not valid base58") from exc

    if not _MIN_LEN <= len(raw) <= _MAX_LEN:
        raise PeerIdError(
            f"{peer_id!r} decodes to {len(raw)} bytes, expected {_MIN_LEN}-{_MAX_LEN}"
        )

    code, declared_len = raw[0], raw[1]
    if code not in _ALLOWED_CODES:
        raise PeerIdError(f"{peer_id!r} has unsupported multihash code 0x{code:02x}")
    if declared_len != len(raw) - 2:
        raise PeerIdError(
            f"{peer_id!r} declares digest length {declared_len} "
            f"but carries {len(raw) - 2} bytes"
        )
    return raw


def _split_line(text: str) -> tuple[str, str | None]:
    """Split `peer_id` or `peer_id,name`, on the first comma only."""
    if "," not in text:
        return text.strip(), None
    peer_id, name = text.split(",", 1)
    return peer_id.strip(), name.strip()


def parse_lines(lines, origin: str = "line") -> tuple[list[PeerEntry], list[str]]:
    """Parse peer ID lines from any source.

    `origin` names the unit in error messages, so a file says "line 4" and a
    repeated command-line flag says "--peer-id 4".

    Blank lines and `#` comments are ignored. Duplicates collapse to their
    first occurrence, keeping that line's name. Indices are 1-based positions
    among the surviving entries, so a peer ID's name is stable no matter which
    subset of the file a run registers. Any invalid line raises `PeerIdError`
    naming its line number.
    """
    entries: list[PeerEntry] = []
    first_seen: dict[str, int] = {}
    duplicates: list[str] = []

    for lineno, line in enumerate(lines, start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue

        peer_id, name = _split_line(text)
        if name is not None and not name:
            raise PeerIdError(
                f"{origin} {lineno}: trailing comma with no name "
                f"(drop the comma to register {peer_id} unnamed)"
            )
        try:
            raw = decode_peer_id(peer_id)
        except PeerIdError as exc:
            raise PeerIdError(f"{origin} {lineno}: {exc}") from exc

        if peer_id in first_seen:
            duplicates.append(
                f"{origin} {lineno}: duplicate of {origin} "
                f"{first_seen[peer_id]}: {peer_id}"
            )
            continue

        first_seen[peer_id] = lineno
        entries.append(
            PeerEntry(
                peer_id=peer_id,
                peer_bytes=raw,
                name=name,
                index=len(entries) + 1,
            )
        )

    return entries, duplicates


def parse_file(path) -> tuple[list[PeerEntry], list[str]]:
    """Read a peer ID file.

    Blank lines and `#` comments are ignored. Duplicates collapse to their
    first occurrence, keeping that line's name. Any invalid line raises
    `PeerIdError` naming its line number.
    """
    return parse_lines(Path(path).read_text().splitlines())


def parse_peer_ids(peer_ids) -> tuple[list[PeerEntry], list[str]]:
    """Build entries straight from repeated --peer-id values.

    Same validation as a file, so a malformed ID is rejected before any chain
    access; only the wording of the error changes.
    """
    return parse_lines(peer_ids, origin="--peer-id")
