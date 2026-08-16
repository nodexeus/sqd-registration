"""Node name resolution and worker metadata encoding.

The contract stores metadata as an opaque string. The network indexer parses it
as JSON and exposes the `name` key as the worker's displayed name, so naming a
node means registering it with {"name":"..."} (compact JSON, no spaces).
"""

import json
import random
from dataclasses import dataclass

from coolname import generate_slug
from coolname.data import config as _coolname_config

from sqdreg.peerids import PeerEntry

MAX_METADATA_BYTES = 256


# Default nodes per batch. Each batch gets one random word, so a 1000-node run
# draws 20 words and produces 20 visibly distinct groups.
DEFAULT_BATCH_SIZE = 50
# Characters of the peer ID used as the per-node suffix. Base58, and peer IDs
# share the leading "12D3KooW", so the tail is the random part: 58**6 is about
# 38 billion, making a duplicate name effectively impossible.
PEER_SUFFIX_LEN = 6


# Words unsuitable as a base name: they must survive being split back off at
# the first hyphen, so anything non-alphabetic is out, and very short or very
# long words read badly in a dashboard.
_MIN_WORD, _MAX_WORD = 3, 12


def _word_pool() -> list[str]:
    """Every usable single word coolname ships, not just one category.

    Drawing from all of its lists — adjectives, animals, colours, planets,
    abstract nouns — keeps batch words varied instead of all of one kind.
    """
    pool = set()
    for value in _coolname_config.values():
        if not isinstance(value, dict) or value.get("type") != "words":
            continue
        for word in value.get("words", ()):
            if _MIN_WORD <= len(word) <= _MAX_WORD and word.isalpha():
                pool.add(word)
    if len(pool) < 100:  # pragma: no cover - guards a library restructure
        # Fall back to the noun half of generated slugs.
        while len(pool) < 200:
            word = generate_slug(2).split("-", 1)[1]
            if word.isalpha():
                pool.add(word)
    return sorted(pool)


def peer_suffix(peer_id: str, length: int = PEER_SUFFIX_LEN) -> str:
    return peer_id[-length:]


def pick_batch_words(count: int, exclude=frozenset(), rng=None) -> list[str]:
    """Choose `count` distinct batch words, avoiding any already in use.

    Excluding words the log has already seen keeps batches visually distinct:
    redrawing a word would make two separate groups look like one.
    """
    rng = rng or random.Random()
    pool = [w for w in _word_pool() if w not in exclude]
    if len(pool) < count:
        raise NamingError(
            f"only {len(pool)} unused batch words available but {count} "
            f"batches are needed; raise --batch to use fewer"
        )
    return rng.sample(pool, count)


def base_words(names) -> set[str]:
    """The batch word of each existing name, i.e. everything before the tail."""
    return {name.split("-", 1)[0] for name in names if "-" in name}


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


def encode_metadata(
    name: str | None, website: str | None = None, description: str | None = None
) -> str:
    """Encode worker metadata as the string the contract stores.

    The indexer parses this JSON and exposes `name`, `website` and
    `description` as fields on the worker — confirmed against a live
    registration. Empty values are omitted rather than written as "", so a
    worker with no website has a null website rather than a blank one.
    """
    fields = {"name": name, "website": website, "description": description}
    present = {k: v for k, v in fields.items() if v}
    if not present:
        return ""
    metadata = json.dumps(present, separators=(",", ":"), ensure_ascii=False)
    size = len(metadata.encode())
    if size > MAX_METADATA_BYTES:
        raise NamingError(
            f"metadata for name {name!r} is {size} bytes, over the "
            f"{MAX_METADATA_BYTES}-byte cap. Shorten the website or "
            f"description, which apply to every node in the run."
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
    batch_size: int = DEFAULT_BATCH_SIZE,
    rng=None,
    website: str | None = None,
    description: str | None = None,
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

    if batch_size < 1:
        raise NamingError("batch size must be at least 1")

    taken = set(used_names)

    # One word per batch of entries that need a generated name. Drawn fresh
    # every run, so an interrupted batch simply ends and the next run starts a
    # new word rather than trying to reconstruct the old one.
    auto_count = 0 if template else sum(1 for e in entries if not e.name)
    words = (
        pick_batch_words(
            -(-auto_count // batch_size), exclude=base_words(taken), rng=rng
        )
        if auto_count
        else []
    )
    auto_index = 0

    prepared = []
    # A template with no {n} renders the same string forever; searching for an
    # unused value would never terminate.
    constant = bool(template) and template_is_constant(template)
    next_n = 1

    for entry in entries:
        if entry.name:
            name = entry.name
        elif not template:
            # No explicit name and no template: name it from its batch rather
            # than register nameless, which is unmanageable at scale.
            word = words[auto_index // batch_size]
            name = f"{word}-{peer_suffix(entry.peer_id)}"
            auto_index += 1
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
            NamedPeer(
                entry=entry,
                name=name,
                metadata=encode_metadata(name, website, description),
            )
        )
    return prepared
