"""Node name resolution and worker metadata encoding.

The contract stores metadata as an opaque string. The network indexer parses it
as JSON and exposes the `name` key as the worker's displayed name, so naming a
node means registering it with `{"name": "..."}`.
"""

import json
from dataclasses import dataclass

from sqdreg.peerids import PeerEntry

MAX_METADATA_BYTES = 256


class NamingError(ValueError):
    """A name or name template was unusable."""


@dataclass(frozen=True)
class NamedPeer:
    """A peer entry with its resolved name and encoded metadata."""

    entry: PeerEntry
    name: str | None
    metadata: str


def validate_template(template: str) -> None:
    """Reject a template before any file or chain access."""
    try:
        template.format(n=1, peer_id="probe")
    except (KeyError, IndexError) as exc:
        raise NamingError(
            f"unknown placeholder {exc} in --name-template; "
            "only {n} and {peer_id} are available"
        ) from exc
    except ValueError as exc:
        raise NamingError(f"invalid --name-template: {exc}") from exc


def resolve_name(entry: PeerEntry, template: str | None) -> str | None:
    """An explicit name if the file gave one, else the template, else nothing."""
    if entry.name:
        return entry.name
    if template:
        return template.format(n=entry.index, peer_id=entry.peer_id)
    return None


def encode_metadata(name: str | None) -> str:
    """Encode a name as the metadata string the contract stores."""
    if not name:
        return ""
    metadata = json.dumps({"name": name}, separators=(",", ":"))
    size = len(metadata.encode())
    if size > MAX_METADATA_BYTES:
        raise NamingError(
            f"metadata for name {name!r} is {size} bytes, "
            f"over the {MAX_METADATA_BYTES}-byte cap"
        )
    return metadata


def prepare(entries: list[PeerEntry], template: str | None) -> list[NamedPeer]:
    """Resolve and encode names for every entry, failing fast on any problem."""
    if template:
        validate_template(template)
    prepared = []
    for entry in entries:
        name = resolve_name(entry, template)
        prepared.append(
            NamedPeer(entry=entry, name=name, metadata=encode_metadata(name))
        )
    return prepared
