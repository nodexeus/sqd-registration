"""Node name resolution and worker metadata encoding.

The contract stores metadata as an opaque string. The network indexer parses it
as JSON and exposes the `name` key as the worker's displayed name, so naming a
node means registering it with {"name":"..."} (compact JSON, no spaces).
"""

import json
import random
from dataclasses import dataclass

from coolname import RandomGenerator
from coolname.data import config as _coolname_config

from sqdreg.peerids import PeerEntry

MAX_METADATA_BYTES = 256


# Two words give ~370,000 combinations. A 1000-node batch collides about five
# times, which the caller's used-name check resolves; three words would make
# that vanishingly rare but reads worse for no practical gain.
AUTO_NAME_WORDS = 2
# Defensive only. The sequence is deterministic and the space is large, so this
# bound is never reached in practice.
_MAX_AUTO_ATTEMPTS = 1000


def auto_name(peer_id: str, attempt: int = 0) -> str:
    """A friendly name derived deterministically from the peer ID.

    Seeded from the peer ID rather than truly random, so a name is stable
    across runs: `--dry-run` previews exactly what will be registered, and a
    retry after a failure reuses the same name instead of inventing another.

    `attempt` walks a different, equally deterministic name for the same peer
    when the first is already taken.
    """
    generator = RandomGenerator(
        _coolname_config, random.Random(f"{peer_id}:{attempt}")
    )
    return generator.generate_slug(AUTO_NAME_WORDS)


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
            # No explicit name and no template: generate one rather than
            # register the node nameless, which is unmanageable at scale.
            for attempt in range(_MAX_AUTO_ATTEMPTS):
                name = auto_name(entry.peer_id, attempt)
                if name not in taken:
                    break
            else:
                raise NamingError(
                    f"could not find an unused generated name for "
                    f"{entry.peer_id} after {_MAX_AUTO_ATTEMPTS} attempts"
                )
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
