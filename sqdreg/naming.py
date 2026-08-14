"""Node name resolution and worker metadata encoding.

The contract stores metadata as an opaque string. The network indexer parses it
as JSON and exposes the `name` key as the worker's displayed name, so naming a
node means registering it with {"name":"..."} (compact JSON, no spaces).
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
    metadata = json.dumps({"name": name}, separators=(",", ":"), ensure_ascii=False)
    size = len(metadata.encode())
    if size > MAX_METADATA_BYTES:
        raise NamingError(
            f"metadata for name {name!r} is {size} bytes, "
            f"over the {MAX_METADATA_BYTES}-byte cap"
        )
    return metadata


def template_is_constant(template: str) -> bool:
    """Whether the template ignores `{n}`, so numbering cannot advance it."""
    return template.format(n=1, peer_id="probe") == template.format(
        n=2, peer_id="probe"
    )


def prepare(
    entries: list[PeerEntry],
    template: str | None,
    used_names: frozenset[str] | set[str] = frozenset(),
) -> list[NamedPeer]:
    """Resolve and encode names for the entries about to be registered.

    A template's `{n}` is the lowest number whose rendered name is not already
    in `used_names` — names taken by earlier runs, plus explicit names anywhere
    in the input file. That is what lets each template start its own sequence:
    a second group registered under a different prefix begins at 1 rather than
    continuing the file's line numbering, while resuming an interrupted group
    picks up where it stopped instead of colliding from 1 again.

    Because numbers are handed out as entries are consumed, callers must pass
    only the entries they intend to register. Naming the whole file would burn
    numbers on peers that are skipped.
    """
    if template:
        validate_template(template)

    taken = set(used_names)
    prepared = []
    # A template with no {n} renders the same string forever; searching for an
    # unused value would never terminate.
    constant = bool(template) and template_is_constant(template)
    next_n = 1

    for entry in entries:
        if entry.name:
            name = entry.name
        elif not template:
            name = None
        elif constant:
            name = template.format(n=next_n, peer_id=entry.peer_id)
        else:
            while True:
                name = template.format(n=next_n, peer_id=entry.peer_id)
                next_n += 1
                if name not in taken:
                    break

        if name:
            taken.add(name)
        prepared.append(
            NamedPeer(entry=entry, name=name, metadata=encode_metadata(name))
        )
    return prepared
