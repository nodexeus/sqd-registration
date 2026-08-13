# Bulk SQD Worker Registration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `bulk_register.py` CLI that reads a file of libp2p peer IDs, optionally names each node, and registers each as an SQD worker on Arbitrum — skipping any already registered, capped by an optional `--limit`.

**Architecture:** A thin entry script owns argument parsing, orchestration, and console output. Five small modules underneath it own one concern each: the network table, peer ID parsing, name resolution, the contract wrapper, and the resumable result log. The contract wrapper builds unsigned transactions and returns them; signing and sending stay in the entry script, so every on-chain interaction can be mocked in tests.

**Tech Stack:** Python 3.10+, `web3` v7, `base58`, `python-dotenv`, `pytest`.

## Global Constraints

- Python 3.10 or newer (the code uses `X | None` type syntax).
- `web3>=7.0,<8` — v7 renamed `SignedTransaction.rawTransaction` to `raw_transaction`. Use the v7 name.
- Secrets come only from `PRIVATE_KEY` or `MNEMONIC` in the environment or a `.env` file. No key material in source, output, or the run log — only the derived address may be displayed.
- No automated test may contact a real RPC endpoint or require a key. `web3` is mocked throughout.
- All work is local: `git init` with no remote, no issue tracker. Commit after every task.
- Test peer IDs are constructed programmatically from raw multihash bytes, never hardcoded as base58 strings, so fixtures are guaranteed valid.
- Only the two-argument `register(bytes, string)` overload is used. The one-argument form's body is exactly `register(peerId, "")`, so passing `""` is equivalent and keeps a single code path.
- The registration check is **two** reads: `workerIds(peerId) != 0` **and** `getWorker(id).registeredAt != 0`. `withdraw()` leaves `workerIds` populated while vacating the worker slot, so `workerIds` alone would permanently skip re-registerable peer IDs.
- Network parameters, verbatim:
  - `mainnet`: chain ID `42161`, RPC `https://arb1.arbitrum.io/rpc`, WorkerRegistration `0x36e2b147db67e76ab67a4d07c293670ebefcae4e`
  - `tethys`: chain ID `421614`, RPC `https://sepolia-rollup.arbitrum.io/rpc`, WorkerRegistration `0xCD8e983F8c4202B0085825Cf21833927D1e2b6Dc`
- Constants: `MAX_CONSECUTIVE_FAILURES = 3`, `RECEIPT_TIMEOUT = 300` seconds, `FALLBACK_REGISTER_GAS = 350_000`, `GAS_BUFFER_PERCENT = 25`, `MAX_METADATA_BYTES = 256`.

---

### Task 1: Scaffolding and the network table

**Files:**
- Create: `requirements.txt`, `.gitignore`, `sqdreg/__init__.py`, `sqdreg/networks.py`
- Test: `tests/test_networks.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `sqdreg.networks.Network` — a frozen dataclass with fields `name: str`, `chain_id: int`, `rpc_url: str`, `worker_registration: str`. `sqdreg.networks.NETWORKS: dict[str, Network]` keyed by `"mainnet"` and `"tethys"`.

- [ ] **Step 1: Initialise the repo and dependencies**

Run from `sqd-registration/`:

```bash
git init
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
```

Create `requirements.txt`:

```
web3>=7.0,<8
base58>=2.1
python-dotenv>=1.0
pytest>=8.0
```

Create `.gitignore`:

```
.venv/
__pycache__/
*.pyc
.env
*.run.jsonl
.pytest_cache/
```

Install:

```bash
.venv/bin/pip install -r requirements.txt
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_networks.py`:

```python
import dataclasses

import pytest
from eth_utils import to_checksum_address

from sqdreg.networks import NETWORKS


def test_both_networks_are_defined():
    assert set(NETWORKS) == {"mainnet", "tethys"}


def test_mainnet_parameters():
    mainnet = NETWORKS["mainnet"]
    assert mainnet.name == "mainnet"
    assert mainnet.chain_id == 42161
    assert mainnet.rpc_url == "https://arb1.arbitrum.io/rpc"
    assert to_checksum_address(mainnet.worker_registration) == to_checksum_address(
        "0x36e2b147db67e76ab67a4d07c293670ebefcae4e"
    )


def test_tethys_parameters():
    tethys = NETWORKS["tethys"]
    assert tethys.name == "tethys"
    assert tethys.chain_id == 421614
    assert tethys.rpc_url == "https://sepolia-rollup.arbitrum.io/rpc"
    assert to_checksum_address(tethys.worker_registration) == to_checksum_address(
        "0xCD8e983F8c4202B0085825Cf21833927D1e2b6Dc"
    )


def test_key_matches_name_field():
    for key, network in NETWORKS.items():
        assert key == network.name


def test_network_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        NETWORKS["mainnet"].chain_id = 1
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_networks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqdreg'`

- [ ] **Step 4: Write minimal implementation**

Create empty `sqdreg/__init__.py`.

Create `sqdreg/networks.py`:

```python
"""Static network parameters for SQD worker registration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Network:
    """One deployment of the SQD network."""

    name: str
    chain_id: int
    rpc_url: str
    worker_registration: str


NETWORKS: dict[str, Network] = {
    "mainnet": Network(
        name="mainnet",
        chain_id=42161,
        rpc_url="https://arb1.arbitrum.io/rpc",
        worker_registration="0x36e2b147db67e76ab67a4d07c293670ebefcae4e",
    ),
    "tethys": Network(
        name="tethys",
        chain_id=421614,
        rpc_url="https://sepolia-rollup.arbitrum.io/rpc",
        worker_registration="0xCD8e983F8c4202B0085825Cf21833927D1e2b6Dc",
    ),
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_networks.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add requirements.txt .gitignore sqdreg/__init__.py sqdreg/networks.py tests/test_networks.py
git commit -m "feat: add network table for mainnet and tethys"
```

---

### Task 2: Peer ID parsing and validation

**Files:**
- Create: `sqdreg/peerids.py`
- Test: `tests/test_peerids.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `sqdreg.peerids.PeerIdError(ValueError)`
  - `sqdreg.peerids.PeerEntry` — frozen dataclass with `peer_id: str`, `peer_bytes: bytes`, `name: str | None`, `index: int` (1-based file position, assigned after duplicates collapse)
  - `sqdreg.peerids.decode_peer_id(peer_id: str) -> bytes` — raises `PeerIdError`
  - `sqdreg.peerids.parse_file(path) -> tuple[list[PeerEntry], list[str]]` — returns `(entries, duplicate_warnings)` in file order

Validation rules: the base58 decode must succeed; total length 32–64 bytes, the upper bound matching the contract's own `peerId.length <= 64`; first byte is the multihash code, either `0x00` (identity) or `0x12` (sha2-256); second byte is the digest length and must equal `len(raw) - 2`.

Line format: `peer_id` or `peer_id,name`. Split on the first comma only, so a name may itself contain commas. A line ending in a bare comma is an error.

- [ ] **Step 1: Write the failing test**

Create `tests/test_peerids.py`:

```python
import base58
import pytest

from sqdreg.peerids import PeerIdError, decode_peer_id, parse_file


def make_peer_id(code: int = 0x00, digest_len: int = 36, seed: int = 0):
    """Build a structurally valid peer ID and its raw bytes."""
    digest = bytes((seed + i) % 256 for i in range(digest_len))
    raw = bytes([code, digest_len]) + digest
    return base58.b58encode(raw).decode(), raw


def test_decodes_identity_multihash():
    peer_id, raw = make_peer_id(code=0x00, digest_len=36)
    assert decode_peer_id(peer_id) == raw


def test_decodes_sha256_multihash():
    peer_id, raw = make_peer_id(code=0x12, digest_len=32)
    assert decode_peer_id(peer_id) == raw


def test_rejects_non_base58():
    with pytest.raises(PeerIdError, match="not valid base58"):
        decode_peer_id("not-a-peer-id-0OIl")


def test_rejects_too_short():
    raw = bytes([0x00, 4]) + b"abcd"
    with pytest.raises(PeerIdError, match="expected 32-64"):
        decode_peer_id(base58.b58encode(raw).decode())


def test_rejects_over_64_bytes():
    raw = bytes([0x00, 70]) + bytes(70)
    with pytest.raises(PeerIdError, match="expected 32-64"):
        decode_peer_id(base58.b58encode(raw).decode())


def test_rejects_unknown_multihash_code():
    raw = bytes([0x99, 36]) + bytes(36)
    with pytest.raises(PeerIdError, match="unsupported multihash code 0x99"):
        decode_peer_id(base58.b58encode(raw).decode())


def test_rejects_declared_length_mismatch():
    raw = bytes([0x00, 40]) + bytes(36)
    with pytest.raises(PeerIdError, match="declares digest length 40"):
        decode_peer_id(base58.b58encode(raw).decode())


def test_parse_file_reads_entries_in_order(tmp_path):
    first, first_raw = make_peer_id(seed=1)
    second, second_raw = make_peer_id(seed=2)
    path = tmp_path / "peers.txt"
    path.write_text(f"{first}\n{second}\n")

    entries, duplicates = parse_file(path)

    assert [e.peer_id for e in entries] == [first, second]
    assert [e.peer_bytes for e in entries] == [first_raw, second_raw]
    assert [e.name for e in entries] == [None, None]
    assert [e.index for e in entries] == [1, 2]
    assert duplicates == []


def test_parse_file_reads_the_optional_name_column(tmp_path):
    peer_id, _ = make_peer_id(seed=3)
    path = tmp_path / "peers.txt"
    path.write_text(f"{peer_id},prod-worker-01\n")

    entries, _ = parse_file(path)

    assert entries[0].name == "prod-worker-01"


def test_parse_file_strips_whitespace_around_both_fields(tmp_path):
    peer_id, _ = make_peer_id(seed=4)
    path = tmp_path / "peers.txt"
    path.write_text(f"  {peer_id} ,  spaced name  \n")

    entries, _ = parse_file(path)

    assert entries[0].peer_id == peer_id
    assert entries[0].name == "spaced name"


def test_parse_file_splits_on_the_first_comma_only(tmp_path):
    peer_id, _ = make_peer_id(seed=5)
    path = tmp_path / "peers.txt"
    path.write_text(f"{peer_id},name,with,commas\n")

    entries, _ = parse_file(path)

    assert entries[0].name == "name,with,commas"


def test_parse_file_rejects_a_bare_trailing_comma(tmp_path):
    peer_id, _ = make_peer_id(seed=6)
    path = tmp_path / "peers.txt"
    path.write_text(f"{peer_id},\n")

    with pytest.raises(PeerIdError, match="line 1"):
        parse_file(path)


def test_parse_file_ignores_blanks_and_comments(tmp_path):
    peer_id, raw = make_peer_id(seed=7)
    path = tmp_path / "peers.txt"
    path.write_text(f"# a comment\n\n   \n{peer_id}\n   # indented comment\n")

    entries, duplicates = parse_file(path)

    assert [e.peer_id for e in entries] == [peer_id]
    assert duplicates == []


def test_parse_file_collapses_duplicates_and_warns(tmp_path):
    peer_id, _ = make_peer_id(seed=8)
    path = tmp_path / "peers.txt"
    path.write_text(f"{peer_id},first-name\n{peer_id},second-name\n")

    entries, duplicates = parse_file(path)

    assert len(entries) == 1
    assert entries[0].name == "first-name"
    assert len(duplicates) == 1
    assert "line 2" in duplicates[0]
    assert peer_id in duplicates[0]


def test_index_is_assigned_after_duplicates_collapse(tmp_path):
    first, _ = make_peer_id(seed=9)
    second, _ = make_peer_id(seed=10)
    path = tmp_path / "peers.txt"
    path.write_text(f"{first}\n{first}\n{second}\n")

    entries, _ = parse_file(path)

    assert [(e.peer_id, e.index) for e in entries] == [(first, 1), (second, 2)]


def test_parse_file_reports_line_number_of_bad_entry(tmp_path):
    good, _ = make_peer_id(seed=11)
    path = tmp_path / "peers.txt"
    path.write_text(f"{good}\ngarbage-0OIl\n")

    with pytest.raises(PeerIdError, match="line 2"):
        parse_file(path)


def test_entry_is_immutable(tmp_path):
    import dataclasses

    peer_id, _ = make_peer_id(seed=12)
    path = tmp_path / "peers.txt"
    path.write_text(f"{peer_id}\n")

    entries, _ = parse_file(path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        entries[0].name = "nope"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_peerids.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqdreg.peerids'`

- [ ] **Step 3: Write minimal implementation**

Create `sqdreg/peerids.py`:

```python
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


def parse_file(path) -> tuple[list[PeerEntry], list[str]]:
    """Read a peer ID file.

    Blank lines and `#` comments are ignored. Duplicates collapse to their
    first occurrence, keeping that line's name. Indices are 1-based positions
    among the surviving entries, so a peer ID's name is stable no matter which
    subset of the file a run registers. Any invalid line raises `PeerIdError`
    naming its line number.
    """
    entries: list[PeerEntry] = []
    first_seen: dict[str, int] = {}
    duplicates: list[str] = []

    for lineno, line in enumerate(Path(path).read_text().splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue

        peer_id, name = _split_line(text)
        if name is not None and not name:
            raise PeerIdError(
                f"line {lineno}: trailing comma with no name "
                f"(drop the comma to register {peer_id} unnamed)"
            )
        try:
            raw = decode_peer_id(peer_id)
        except PeerIdError as exc:
            raise PeerIdError(f"line {lineno}: {exc}") from exc

        if peer_id in first_seen:
            duplicates.append(
                f"line {lineno}: duplicate of line {first_seen[peer_id]}: {peer_id}"
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_peerids.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add sqdreg/peerids.py tests/test_peerids.py
git commit -m "feat: add peer ID parsing with optional name column"
```

---

### Task 3: Name resolution and metadata encoding

**Files:**
- Create: `sqdreg/naming.py`
- Test: `tests/test_naming.py`

**Interfaces:**
- Consumes: `sqdreg.peerids.PeerEntry`
- Produces:
  - `sqdreg.naming.MAX_METADATA_BYTES = 256`
  - `sqdreg.naming.NamingError(ValueError)`
  - `sqdreg.naming.NamedPeer` — frozen dataclass with `entry: PeerEntry`, `name: str | None`, `metadata: str`
  - `sqdreg.naming.validate_template(template: str) -> None` — raises `NamingError`
  - `sqdreg.naming.resolve_name(entry: PeerEntry, template: str | None) -> str | None`
  - `sqdreg.naming.encode_metadata(name: str | None) -> str`
  - `sqdreg.naming.prepare(entries: list[PeerEntry], template: str | None) -> list[NamedPeer]`

The name a node displays is the `name` key of the JSON metadata string the
contract stores verbatim. Explicit names beat the template. `{n}` is the entry's
file index, not its work-list position.

- [ ] **Step 1: Write the failing test**

Create `tests/test_naming.py`:

```python
import json

import pytest

from sqdreg.naming import (
    MAX_METADATA_BYTES,
    NamingError,
    encode_metadata,
    prepare,
    resolve_name,
    validate_template,
)
from sqdreg.peerids import PeerEntry


def entry(peer_id="peer-a", name=None, index=1):
    return PeerEntry(peer_id=peer_id, peer_bytes=peer_id.encode(), name=name, index=index)


def test_explicit_name_wins_over_template():
    assert resolve_name(entry(name="explicit"), "template-{n}") == "explicit"


def test_template_fills_in_when_no_explicit_name():
    assert resolve_name(entry(index=7), "sqd-{n}") == "sqd-7"


def test_no_name_and_no_template_yields_none():
    assert resolve_name(entry(), None) is None


def test_template_substitutes_peer_id():
    assert resolve_name(entry(peer_id="abc"), "w-{peer_id}") == "w-abc"


def test_template_supports_format_specs():
    assert resolve_name(entry(index=3), "nodexeus-{n:03d}") == "nodexeus-003"


def test_template_without_placeholders_is_allowed():
    assert resolve_name(entry(), "static") == "static"


def test_validate_template_rejects_unknown_placeholder():
    with pytest.raises(NamingError, match="unknown placeholder"):
        validate_template("sqd-{nope}")


def test_validate_template_rejects_positional_placeholder():
    with pytest.raises(NamingError, match="unknown placeholder"):
        validate_template("sqd-{0}")


def test_validate_template_rejects_malformed_braces():
    with pytest.raises(NamingError, match="invalid"):
        validate_template("sqd-{n")


def test_validate_template_accepts_valid_templates():
    validate_template("sqd-{n:04d}-{peer_id}")


def test_encode_metadata_is_compact_json():
    assert encode_metadata("worker-1") == '{"name":"worker-1"}'
    assert json.loads(encode_metadata("worker-1")) == {"name": "worker-1"}


def test_encode_metadata_is_empty_string_when_unnamed():
    assert encode_metadata(None) == ""
    assert encode_metadata("") == ""


def test_encode_metadata_escapes_json_correctly():
    assert json.loads(encode_metadata('quote " and \\ backslash')) == {
        "name": 'quote " and \\ backslash'
    }


def test_encode_metadata_rejects_oversized_names():
    with pytest.raises(NamingError, match=str(MAX_METADATA_BYTES)):
        encode_metadata("x" * MAX_METADATA_BYTES)


def test_encode_metadata_measures_bytes_not_characters():
    # Each of these is 3 bytes in UTF-8, so 100 of them exceeds nothing, but
    # 90 plus the JSON wrapper does.
    name = "あ" * 90
    with pytest.raises(NamingError):
        encode_metadata(name)


def test_prepare_resolves_every_entry():
    entries = [entry(peer_id="a", index=1), entry(peer_id="b", name="named", index=2)]

    prepared = prepare(entries, "sqd-{n:02d}")

    assert [p.name for p in prepared] == ["sqd-01", "named"]
    assert [p.metadata for p in prepared] == ['{"name":"sqd-01"}', '{"name":"named"}']
    assert [p.entry for p in prepared] == entries


def test_prepare_validates_the_template_once_up_front():
    with pytest.raises(NamingError, match="unknown placeholder"):
        prepare([entry()], "sqd-{bad}")


def test_prepare_with_no_template_leaves_unnamed_entries_empty():
    prepared = prepare([entry(peer_id="a"), entry(peer_id="b", name="named")], None)

    assert [p.name for p in prepared] == [None, "named"]
    assert [p.metadata for p in prepared] == ["", '{"name":"named"}']


def test_prepare_uses_file_index_not_list_position():
    entries = [entry(peer_id="c", index=3), entry(peer_id="d", index=4)]

    prepared = prepare(entries, "sqd-{n}")

    assert [p.name for p in prepared] == ["sqd-3", "sqd-4"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_naming.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqdreg.naming'`

- [ ] **Step 3: Write minimal implementation**

Create `sqdreg/naming.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_naming.py -v`
Expected: 19 passed

- [ ] **Step 5: Commit**

```bash
git add sqdreg/naming.py tests/test_naming.py
git commit -m "feat: add name resolution and metadata encoding"
```

---

### Task 4: Resumable run log

**Files:**
- Create: `sqdreg/runlog.py`
- Test: `tests/test_runlog.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - Constants `sqdreg.runlog.SUCCESS = "success"`, `FAILED = "failed"`, `PENDING = "pending"`
  - `sqdreg.runlog.Record` — dataclass with `peer_id: str`, `status: str`, `name: str | None = None`, `tx_hash: str | None = None`, `block: int | None = None`, `error: str | None = None`, `timestamp: str | None = None`
  - `sqdreg.runlog.RunLog(path)` with `.append(record: Record) -> None`, `.records() -> list[Record]`, `.succeeded_peer_ids() -> set[str]`
  - `sqdreg.runlog.utc_now() -> str` — ISO-8601 UTC timestamp

- [ ] **Step 1: Write the failing test**

Create `tests/test_runlog.py`:

```python
import json

from sqdreg.runlog import FAILED, PENDING, SUCCESS, Record, RunLog, utc_now


def test_append_then_read_round_trips(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    record = Record(
        peer_id="peer-a", status=SUCCESS, name="worker-1", tx_hash="0xabc", block=42
    )

    log.append(record)

    assert log.records() == [record]


def test_records_is_empty_when_file_absent(tmp_path):
    assert RunLog(tmp_path / "missing.jsonl").records() == []
    assert RunLog(tmp_path / "missing.jsonl").succeeded_peer_ids() == set()


def test_append_preserves_existing_records(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="peer-a", status=SUCCESS))
    log.append(Record(peer_id="peer-b", status=FAILED, error="reverted"))

    assert [r.peer_id for r in log.records()] == ["peer-a", "peer-b"]


def test_succeeded_excludes_failed_and_pending(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="ok", status=SUCCESS))
    log.append(Record(peer_id="bad", status=FAILED, error="reverted"))
    log.append(Record(peer_id="slow", status=PENDING, tx_hash="0xdef"))

    assert log.succeeded_peer_ids() == {"ok"}


def test_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(json.dumps({"peer_id": "ok", "status": SUCCESS}) + "\n\n")

    assert [r.peer_id for r in RunLog(path).records()] == ["ok"]


def test_the_name_is_persisted(tmp_path):
    path = tmp_path / "run.jsonl"
    log = RunLog(path)
    log.append(Record(peer_id="peer-a", status=SUCCESS, name="worker-1"))

    assert json.loads(path.read_text().splitlines()[0])["name"] == "worker-1"


def test_utc_now_is_iso_with_timezone():
    stamp = utc_now()
    assert "T" in stamp
    assert stamp.endswith("+00:00")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_runlog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqdreg.runlog'`

- [ ] **Step 3: Write minimal implementation**

Create `sqdreg/runlog.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_runlog.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add sqdreg/runlog.py tests/test_runlog.py
git commit -m "feat: add resumable JSONL run log"
```

---

### Task 5: Contract wrapper

**Files:**
- Create: `sqdreg/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: `sqdreg.networks.Network`
- Produces:
  - `sqdreg.registry.FALLBACK_REGISTER_GAS = 350_000`
  - `sqdreg.registry.WORKER_REGISTRATION_ABI`, `sqdreg.registry.ERC20_ABI`
  - `sqdreg.registry.Registry(w3, network: Network, address: str)` with:
    - `.contract` — the WorkerRegistration contract object
    - `.is_registered(peer_bytes: bytes) -> bool`
    - `.bond_amount() -> int`
    - `.token()` — the SQD ERC-20 contract object, cached
    - `.sqd_balance() -> int`
    - `.allowance() -> int`
    - `.token_decimals() -> int`
    - `.build_approve(amount: int, nonce: int, fees: dict) -> dict`
    - `.build_register(peer_bytes: bytes, metadata: str, nonce: int, fees: dict, gas: int) -> dict`
    - `.estimate_register_gas(peer_bytes: bytes, metadata: str) -> tuple[int, bool]` — `(gas, exact)`; `exact` is `False` when estimation failed and the fallback was used

`fees` is a dict carrying `maxFeePerGas` and `maxPriorityFeePerGas`.

`is_registered` is deliberately two reads. `withdraw()` runs
`delete workers[workerId]` but leaves `workerIds[peerId]` populated, and
`register()` explicitly permits re-registering such a slot. Testing `workerIds`
alone would permanently skip any peer ID that had been cycled out.

- [ ] **Step 1: Write the failing test**

Create `tests/test_registry.py`:

```python
from unittest.mock import MagicMock

from sqdreg.networks import NETWORKS
from sqdreg.registry import FALLBACK_REGISTER_GAS, Registry

ADDRESS = "0x0000000000000000000000000000000000000001"
TOKEN = "0x0000000000000000000000000000000000000002"
PEER = b"\x00$peer"
METADATA = '{"name":"worker-1"}'


def make_registry(network_name="mainnet"):
    w3 = MagicMock()
    w3.to_checksum_address.side_effect = lambda value: value
    registration = MagicMock()
    token = MagicMock()
    w3.eth.contract.side_effect = [registration, token]
    return Registry(w3, NETWORKS[network_name], ADDRESS), registration, token


def worker_tuple(registered_at):
    """Worker struct: creator, peerId, bond, registeredAt, deregisteredAt, metadata."""
    return ["0x0", PEER, 100, registered_at, 0, ""]


def test_unseen_peer_id_is_not_registered_and_needs_only_one_read():
    registry, registration, _ = make_registry()
    registration.functions.workerIds.return_value.call.return_value = 0

    assert registry.is_registered(PEER) is False
    registration.functions.getWorker.assert_not_called()


def test_live_worker_is_registered():
    registry, registration, _ = make_registry()
    registration.functions.workerIds.return_value.call.return_value = 5
    registration.functions.getWorker.return_value.call.return_value = worker_tuple(900)

    assert registry.is_registered(PEER) is True
    registration.functions.getWorker.assert_called_once_with(5)


def test_withdrawn_worker_is_not_registered():
    """withdraw() vacates the slot but leaves workerIds populated."""
    registry, registration, _ = make_registry()
    registration.functions.workerIds.return_value.call.return_value = 5
    registration.functions.getWorker.return_value.call.return_value = worker_tuple(0)

    assert registry.is_registered(PEER) is False


def test_bond_amount_reads_the_contract():
    registry, registration, _ = make_registry()
    registration.functions.bondAmount.return_value.call.return_value = 10**23

    assert registry.bond_amount() == 10**23


def test_token_address_comes_from_the_registry_and_is_cached():
    registry, registration, token = make_registry()
    registration.functions.SQD.return_value.call.return_value = TOKEN

    assert registry.token() is token
    assert registry.token() is token
    registration.functions.SQD.return_value.call.assert_called_once()


def test_balance_and_allowance_use_the_signing_address():
    registry, registration, token = make_registry()
    registration.functions.SQD.return_value.call.return_value = TOKEN
    token.functions.balanceOf.return_value.call.return_value = 500
    token.functions.allowance.return_value.call.return_value = 100

    assert registry.sqd_balance() == 500
    assert registry.allowance() == 100
    token.functions.balanceOf.assert_called_once_with(ADDRESS)
    token.functions.allowance.assert_called_once_with(ADDRESS, registry.contract.address)


def test_build_register_passes_metadata_nonce_chain_id_and_gas():
    registry, registration, _ = make_registry()
    registration.functions.register.return_value.build_transaction.side_effect = (
        lambda params: dict(params)
    )

    tx = registry.build_register(
        peer_bytes=PEER,
        metadata=METADATA,
        nonce=5,
        fees={"maxFeePerGas": 200, "maxPriorityFeePerGas": 10},
        gas=123456,
    )

    assert tx["nonce"] == 5
    assert tx["gas"] == 123456
    assert tx["chainId"] == 42161
    assert tx["from"] == ADDRESS
    assert tx["maxFeePerGas"] == 200
    registration.functions.register.assert_called_once_with(PEER, METADATA)


def test_build_register_uses_empty_metadata_for_unnamed_workers():
    registry, registration, _ = make_registry()
    registration.functions.register.return_value.build_transaction.side_effect = (
        lambda params: dict(params)
    )

    registry.build_register(
        peer_bytes=PEER, metadata="", nonce=1, fees={}, gas=1
    )

    registration.functions.register.assert_called_once_with(PEER, "")


def test_build_approve_targets_the_registry_as_spender():
    registry, registration, token = make_registry()
    registration.functions.SQD.return_value.call.return_value = TOKEN
    token.functions.approve.return_value.build_transaction.side_effect = (
        lambda params: dict(params)
    )

    tx = registry.build_approve(
        999, nonce=3, fees={"maxFeePerGas": 200, "maxPriorityFeePerGas": 10}
    )

    assert tx["nonce"] == 3
    token.functions.approve.assert_called_once_with(registry.contract.address, 999)


def test_estimate_register_gas_returns_exact_estimate():
    registry, registration, _ = make_registry()
    registration.functions.register.return_value.estimate_gas.return_value = 210000

    assert registry.estimate_register_gas(PEER, METADATA) == (210000, True)


def test_estimate_register_gas_falls_back_when_estimation_reverts():
    registry, registration, _ = make_registry()
    registration.functions.register.return_value.estimate_gas.side_effect = Exception(
        "execution reverted: ERC20: insufficient allowance"
    )

    assert registry.estimate_register_gas(PEER, METADATA) == (
        FALLBACK_REGISTER_GAS,
        False,
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqdreg.registry'`

- [ ] **Step 3: Write minimal implementation**

Create `sqdreg/registry.py`:

```python
"""Wrapper around the SQD WorkerRegistration contract and its bond token."""

from sqdreg.networks import Network

FALLBACK_REGISTER_GAS = 350_000

# Worker struct field order: creator, peerId, bond, registeredAt,
# deregisteredAt, metadata.
_REGISTERED_AT_INDEX = 3

_WORKER_COMPONENTS = [
    {"name": "creator", "type": "address"},
    {"name": "peerId", "type": "bytes"},
    {"name": "bond", "type": "uint256"},
    {"name": "registeredAt", "type": "uint128"},
    {"name": "deregisteredAt", "type": "uint128"},
    {"name": "metadata", "type": "string"},
]

WORKER_REGISTRATION_ABI = [
    {
        "inputs": [
            {"name": "peerId", "type": "bytes"},
            {"name": "metadata", "type": "string"},
        ],
        "name": "register",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "peerId", "type": "bytes"}],
        "name": "workerIds",
        "outputs": [{"name": "id", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "workerId", "type": "uint256"}],
        "name": "getWorker",
        "outputs": [
            {"name": "", "type": "tuple", "components": _WORKER_COMPONENTS}
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "bondAmount",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "SQD",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class Registry:
    """Reads and unsigned-transaction builders for WorkerRegistration.

    Transactions are returned unsigned; signing and sending belong to the
    caller so this class stays trivially mockable in tests.
    """

    def __init__(self, w3, network: Network, address: str):
        self.w3 = w3
        self.network = network
        self.address = address
        self.contract = w3.eth.contract(
            address=w3.to_checksum_address(network.worker_registration),
            abi=WORKER_REGISTRATION_ABI,
        )
        self._token = None

    # --- reads ---

    def is_registered(self, peer_bytes: bytes) -> bool:
        """Whether the registry holds a *live* registration for this peer ID.

        Two reads, not one: `withdraw()` deletes the worker but leaves
        `workerIds[peerId]` pointing at the vacated slot, and `register()`
        explicitly allows re-registering it. Trusting `workerIds` alone would
        permanently skip any peer ID that had been cycled out.
        """
        worker_id = self.contract.functions.workerIds(peer_bytes).call()
        if worker_id == 0:
            return False
        worker = self.contract.functions.getWorker(worker_id).call()
        return worker[_REGISTERED_AT_INDEX] != 0

    def bond_amount(self) -> int:
        return self.contract.functions.bondAmount().call()

    def token(self):
        """The bond token, read from the registry rather than hardcoded."""
        if self._token is None:
            address = self.contract.functions.SQD().call()
            self._token = self.w3.eth.contract(
                address=self.w3.to_checksum_address(address), abi=ERC20_ABI
            )
        return self._token

    def sqd_balance(self) -> int:
        return self.token().functions.balanceOf(self.address).call()

    def allowance(self) -> int:
        return (
            self.token().functions.allowance(self.address, self.contract.address).call()
        )

    def token_decimals(self) -> int:
        return self.token().functions.decimals().call()

    # --- writes ---

    def _base_tx(self, nonce: int, fees: dict) -> dict:
        return {
            "from": self.address,
            "nonce": nonce,
            "chainId": self.network.chain_id,
            **fees,
        }

    def build_approve(self, amount: int, nonce: int, fees: dict) -> dict:
        return self.token().functions.approve(
            self.contract.address, amount
        ).build_transaction(self._base_tx(nonce, fees))

    def build_register(
        self, peer_bytes: bytes, metadata: str, nonce: int, fees: dict, gas: int
    ) -> dict:
        """Build a register() transaction with gas supplied explicitly.

        Gas is never auto-estimated here: estimation reverts while the bond
        allowance is missing, which would abort an otherwise valid run.
        """
        return self.contract.functions.register(
            peer_bytes, metadata
        ).build_transaction({**self._base_tx(nonce, fees), "gas": gas})

    def estimate_register_gas(
        self, peer_bytes: bytes, metadata: str
    ) -> tuple[int, bool]:
        """Estimate register() gas, falling back when estimation reverts.

        Returns (gas, exact). Estimation fails whenever the allowance is not
        yet in place — the normal case for a dry run on a fresh wallet — so
        any failure yields the documented fallback instead of an error.
        """
        try:
            gas = self.contract.functions.register(peer_bytes, metadata).estimate_gas(
                {"from": self.address}
            )
        except Exception:
            return FALLBACK_REGISTER_GAS, False
        return gas, True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_registry.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add sqdreg/registry.py tests/test_registry.py
git commit -m "feat: add WorkerRegistration wrapper with two-read registration check"
```

---

### Task 6: CLI arguments, credentials, and the chain-ID guard

**Files:**
- Create: `bulk_register.py`, `.env.example`, `peer_ids.txt.example`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `sqdreg.networks.NETWORKS`
- Produces:
  - `bulk_register.MAX_CONSECUTIVE_FAILURES = 3`, `bulk_register.RECEIPT_TIMEOUT = 300`, `bulk_register.GAS_BUFFER_PERCENT = 25`
  - `bulk_register.fail(message: str) -> NoReturn` — prints to stderr, exits 2
  - `bulk_register.positive_int(value: str) -> int`
  - `bulk_register.parse_args(argv: list[str] | None = None) -> argparse.Namespace` with attributes `peer_id_file`, `network`, `limit`, `name_template`, `dry_run`, `yes`, `rpc_url`, `log`
  - `bulk_register.default_log_path(peer_id_file: str) -> str`
  - `bulk_register.load_signer() -> LocalAccount`
  - `bulk_register.connect(network, rpc_url: str | None) -> Web3` — exits 2 on chain-ID mismatch

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli.py`:

```python
from unittest.mock import MagicMock, patch

import pytest

import bulk_register
from sqdreg.networks import NETWORKS

KEY = "0x" + "11" * 32
# The standard public Hardhat/Foundry test phrase. Holds nothing.
TEST_MNEMONIC = "test " * 11 + "junk"


def test_defaults():
    args = bulk_register.parse_args(["peers.txt"])
    assert args.peer_id_file == "peers.txt"
    assert args.network == "mainnet"
    assert args.limit is None
    assert args.name_template is None
    assert args.dry_run is False
    assert args.yes is False
    assert args.rpc_url is None


def test_all_flags_parse():
    args = bulk_register.parse_args(
        [
            "peers.txt",
            "--network",
            "tethys",
            "--limit",
            "10",
            "--name-template",
            "sqd-{n:03d}",
            "--dry-run",
            "--yes",
            "--rpc-url",
            "http://localhost:8545",
            "--log",
            "custom.jsonl",
        ]
    )
    assert args.network == "tethys"
    assert args.limit == 10
    assert args.name_template == "sqd-{n:03d}"
    assert args.dry_run is True
    assert args.yes is True
    assert args.rpc_url == "http://localhost:8545"
    assert args.log == "custom.jsonl"


def test_short_limit_flag():
    assert bulk_register.parse_args(["peers.txt", "-n", "5"]).limit == 5


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_limit_is_rejected(value):
    with pytest.raises(SystemExit) as exc:
        bulk_register.parse_args(["peers.txt", "--limit", value])
    assert exc.value.code == 2


def test_unknown_network_is_rejected():
    with pytest.raises(SystemExit):
        bulk_register.parse_args(["peers.txt", "--network", "nope"])


def test_default_log_path_derives_from_input():
    assert bulk_register.default_log_path("peers.txt") == "peers.txt.run.jsonl"


def test_load_signer_prefers_private_key(monkeypatch, capsys):
    monkeypatch.setenv("PRIVATE_KEY", KEY)
    monkeypatch.setenv("MNEMONIC", TEST_MNEMONIC)
    monkeypatch.setattr(bulk_register, "load_dotenv", lambda: None)

    account = bulk_register.load_signer()

    assert account.address.startswith("0x")
    assert "both PRIVATE_KEY and MNEMONIC" in capsys.readouterr().err


def test_load_signer_accepts_mnemonic(monkeypatch):
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    monkeypatch.setenv("MNEMONIC", TEST_MNEMONIC)
    monkeypatch.setattr(bulk_register, "load_dotenv", lambda: None)

    assert bulk_register.load_signer().address.startswith("0x")


def test_load_signer_exits_when_no_credentials(monkeypatch):
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    monkeypatch.delenv("MNEMONIC", raising=False)
    monkeypatch.setattr(bulk_register, "load_dotenv", lambda: None)

    with pytest.raises(SystemExit) as exc:
        bulk_register.load_signer()
    assert exc.value.code == 2


def test_connect_accepts_matching_chain_id():
    w3 = MagicMock()
    w3.eth.chain_id = 42161
    with patch.object(bulk_register, "Web3", return_value=w3):
        assert bulk_register.connect(NETWORKS["mainnet"], None) is w3


def test_connect_exits_on_chain_id_mismatch(capsys):
    w3 = MagicMock()
    w3.eth.chain_id = 421614
    with patch.object(bulk_register, "Web3", return_value=w3):
        with pytest.raises(SystemExit) as exc:
            bulk_register.connect(NETWORKS["mainnet"], None)
    assert exc.value.code == 2
    assert "42161" in capsys.readouterr().err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bulk_register'`

- [ ] **Step 3: Write minimal implementation**

Create `bulk_register.py`:

```python
#!/usr/bin/env python3
"""Bulk-register SQD worker nodes from a file of peer IDs."""

import argparse
import os
import sys
from typing import NoReturn

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3

from sqdreg.networks import NETWORKS

MAX_CONSECUTIVE_FAILURES = 3
RECEIPT_TIMEOUT = 300
GAS_BUFFER_PERCENT = 25


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


def default_log_path(peer_id_file: str) -> str:
    return f"{peer_id_file}.run.jsonl"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register SQD worker nodes in bulk from a file of peer IDs."
    )
    parser.add_argument(
        "peer_id_file", help="file with one 'peer_id' or 'peer_id,name' per line"
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
            "name for lines without an explicit name; supports {n} "
            "(file position) and {peer_id}, e.g. 'nodexeus-{n:03d}'"
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
    parser.add_argument("--log", help="result log path (default: <input>.run.jsonl)")
    return parser.parse_args(argv)


def load_signer():
    """Build the signing account from PRIVATE_KEY or MNEMONIC."""
    load_dotenv()
    private_key = os.getenv("PRIVATE_KEY")
    mnemonic = os.getenv("MNEMONIC")

    if private_key and mnemonic:
        print(
            "warning: both PRIVATE_KEY and MNEMONIC are set; using PRIVATE_KEY",
            file=sys.stderr,
        )
    if private_key:
        return Account.from_key(private_key.strip())
    if mnemonic:
        Account.enable_unaudited_hdwallet_features()
        return Account.from_mnemonic(mnemonic.strip())
    fail(
        "neither PRIVATE_KEY nor MNEMONIC is set "
        "(put one in the environment or a .env file)"
    )


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
```

Create `.env.example`:

```
# Provide exactly one of the following. PRIVATE_KEY wins if both are set.
PRIVATE_KEY=0x...
# MNEMONIC=word word word ... word
```

Create `peer_ids.txt.example`:

```
# One entry per line: either "peer_id" or "peer_id,name".
# Blank lines and # comments are ignored.
#
# Lines with no name fall back to --name-template, if given.
12D3KooWExamplePeerIdReplaceMe1111111111111111111111,prod-worker-01
12D3KooWExamplePeerIdReplaceMe2222222222222222222222,prod-worker-02
12D3KooWExamplePeerIdReplaceMe3333333333333333333333
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add bulk_register.py .env.example peer_ids.txt.example tests/test_cli.py
git commit -m "feat: add CLI arguments, credential loading, and chain-ID guard"
```

---

### Task 7: Work selection and `--limit`

**Files:**
- Modify: `bulk_register.py` — append `select_work` after `connect`
- Test: `tests/test_select_work.py`

**Interfaces:**
- Consumes: `sqdreg.runlog.RunLog`, `sqdreg.registry.Registry`, `sqdreg.naming.NamedPeer`
- Produces: `bulk_register.select_work(prepared, runlog, registry, limit) -> tuple[list[NamedPeer], list[str], list[str]]` returning `(work, skipped_logged, skipped_onchain)`

The limit applies **after** both skip filters, and the on-chain scan stops as
soon as the limit is met, so a `--limit 10` run against a 500-line file makes
about 10 registration checks rather than 500.

- [ ] **Step 1: Write the failing test**

Create `tests/test_select_work.py`:

```python
from unittest.mock import MagicMock

import bulk_register
from sqdreg.naming import NamedPeer
from sqdreg.peerids import PeerEntry
from sqdreg.runlog import FAILED, PENDING, SUCCESS, Record, RunLog


def prepared(*names):
    items = []
    for index, name in enumerate(names, start=1):
        entry = PeerEntry(
            peer_id=name, peer_bytes=name.encode(), name=None, index=index
        )
        items.append(NamedPeer(entry=entry, name=None, metadata=""))
    return items


def registry_with(registered=()):
    registry = MagicMock()
    registered = set(registered)
    registry.is_registered.side_effect = lambda raw: raw.decode() in registered
    return registry


def test_returns_everything_when_nothing_is_registered(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")

    work, logged, onchain = bulk_register.select_work(
        prepared("a", "b"), log, registry_with(), None
    )

    assert [w.entry.peer_id for w in work] == ["a", "b"]
    assert logged == []
    assert onchain == []


def test_skips_peers_already_registered_onchain(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")

    work, logged, onchain = bulk_register.select_work(
        prepared("a", "b", "c"), log, registry_with(registered=["b"]), None
    )

    assert [w.entry.peer_id for w in work] == ["a", "c"]
    assert onchain == ["b"]
    assert logged == []


def test_skips_peers_logged_as_successful_without_an_onchain_read(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="a", status=SUCCESS))
    registry = registry_with()

    work, logged, _ = bulk_register.select_work(
        prepared("a", "b"), log, registry, None
    )

    assert [w.entry.peer_id for w in work] == ["b"]
    assert logged == ["a"]
    registry.is_registered.assert_called_once_with(b"b")


def test_failed_and_pending_log_entries_do_not_skip(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="a", status=FAILED, error="reverted"))
    log.append(Record(peer_id="b", status=PENDING, tx_hash="0xabc"))

    work, logged, _ = bulk_register.select_work(
        prepared("a", "b"), log, registry_with(), None
    )

    assert [w.entry.peer_id for w in work] == ["a", "b"]
    assert logged == []


def test_limit_applies_after_the_skip_filters(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")

    work, _, onchain = bulk_register.select_work(
        prepared("a", "b", "c", "d"), log, registry_with(registered=["a", "b"]), 2
    )

    assert [w.entry.peer_id for w in work] == ["c", "d"]
    assert onchain == ["a", "b"]


def test_limit_stops_the_onchain_scan_early(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    registry = registry_with()

    work, _, _ = bulk_register.select_work(
        prepared("a", "b", "c", "d", "e"), log, registry, 2
    )

    assert [w.entry.peer_id for w in work] == ["a", "b"]
    assert registry.is_registered.call_count == 2


def test_limit_larger_than_the_actionable_set_clamps(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")

    work, _, _ = bulk_register.select_work(prepared("a", "b"), log, registry_with(), 99)

    assert [w.entry.peer_id for w in work] == ["a", "b"]


def test_limit_preserves_file_order_across_sequential_runs(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    everything = prepared("a", "b", "c", "d")

    first, _, _ = bulk_register.select_work(everything, log, registry_with(), 2)
    for item in first:
        log.append(Record(peer_id=item.entry.peer_id, status=SUCCESS))
    second, _, _ = bulk_register.select_work(everything, log, registry_with(), 2)

    assert [w.entry.peer_id for w in first] == ["a", "b"]
    assert [w.entry.peer_id for w in second] == ["c", "d"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_select_work.py -v`
Expected: FAIL — `AttributeError: module 'bulk_register' has no attribute 'select_work'`

- [ ] **Step 3: Write minimal implementation**

Append to `bulk_register.py`:

```python
def select_work(prepared, runlog, registry, limit):
    """Choose which prepared peers to register.

    Drops peers a previous run logged as successful, then drops peers the
    registry already holds a live registration for. `limit` caps the result
    *after* both filters, so `--limit 10` always means ten new registrations.
    The on-chain scan stops once the limit is met to avoid needless RPC calls.

    Returns (work, skipped_logged, skipped_onchain).
    """
    already_done = runlog.succeeded_peer_ids()
    skipped_logged = [
        item.entry.peer_id for item in prepared if item.entry.peer_id in already_done
    ]

    work = []
    skipped_onchain: list[str] = []

    for item in prepared:
        if item.entry.peer_id in already_done:
            continue
        if registry.is_registered(item.entry.peer_bytes):
            skipped_onchain.append(item.entry.peer_id)
            continue
        work.append(item)
        if limit is not None and len(work) >= limit:
            break

    return work, skipped_logged, skipped_onchain
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_select_work.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add bulk_register.py tests/test_select_work.py
git commit -m "feat: add work selection with skip filters and --limit"
```

---

### Task 8: Funds check, fees, and confirmation

**Files:**
- Modify: `bulk_register.py` — append after `select_work`
- Test: `tests/test_funds.py`

**Interfaces:**
- Consumes: `sqdreg.registry.Registry`
- Produces:
  - `bulk_register.FundsCheck` — dataclass with `bond: int`, `required: int`, `balance: int`, `allowance: int`, `needs_approval: bool`
  - `bulk_register.check_funds(registry, count: int) -> FundsCheck` — exits 2 when the balance is short
  - `bulk_register.current_fees(w3) -> dict` — `{"maxFeePerGas": int, "maxPriorityFeePerGas": int}`
  - `bulk_register.gas_limit_for(registry, work) -> tuple[int, bool]` — estimates against the longest metadata in `work`, pads by `GAS_BUFFER_PERCENT`, returns `(gas, exact)`
  - `bulk_register.format_units(amount: int, decimals: int) -> str`
  - `bulk_register.confirm(prompt: str, assume_yes: bool) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_funds.py`:

```python
from unittest.mock import MagicMock

import pytest

import bulk_register
from sqdreg.naming import NamedPeer
from sqdreg.peerids import PeerEntry

BOND = 10**23  # 100,000 SQD at 18 decimals


def registry_with(bond=BOND, balance=0, allowance=0):
    registry = MagicMock()
    registry.bond_amount.return_value = bond
    registry.sqd_balance.return_value = balance
    registry.allowance.return_value = allowance
    registry.token_decimals.return_value = 18
    return registry


def item(peer_id, metadata):
    entry = PeerEntry(
        peer_id=peer_id, peer_bytes=peer_id.encode(), name=None, index=1
    )
    return NamedPeer(entry=entry, name=None, metadata=metadata)


def test_sufficient_balance_and_allowance_needs_no_approval():
    check = bulk_register.check_funds(
        registry_with(balance=BOND * 5, allowance=BOND * 5), count=3
    )

    assert check.required == BOND * 3
    assert check.needs_approval is False


def test_short_allowance_flags_approval():
    check = bulk_register.check_funds(
        registry_with(balance=BOND * 5, allowance=BOND), count=3
    )

    assert check.needs_approval is True
    assert check.required == BOND * 3


def test_exact_allowance_needs_no_approval():
    check = bulk_register.check_funds(
        registry_with(balance=BOND * 3, allowance=BOND * 3), count=3
    )

    assert check.needs_approval is False


def test_short_balance_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        bulk_register.check_funds(
            registry_with(balance=BOND, allowance=BOND * 9), count=3
        )

    assert exc.value.code == 2
    assert "insufficient SQD" in capsys.readouterr().err


def test_required_uses_the_limited_count_not_the_file_count():
    check = bulk_register.check_funds(
        registry_with(balance=BOND * 500, allowance=0), count=10
    )

    assert check.required == BOND * 10


def test_current_fees_doubles_base_fee_and_adds_priority():
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"baseFeePerGas": 100}
    w3.eth.max_priority_fee = 10

    assert bulk_register.current_fees(w3) == {
        "maxFeePerGas": 210,
        "maxPriorityFeePerGas": 10,
    }


def test_current_fees_tolerates_a_chain_without_base_fee():
    w3 = MagicMock()
    w3.eth.get_block.return_value = {}
    w3.eth.max_priority_fee = 10

    assert bulk_register.current_fees(w3)["maxFeePerGas"] == 10


def test_gas_limit_estimates_against_the_longest_metadata():
    registry = MagicMock()
    registry.estimate_register_gas.return_value = (200000, True)
    work = [
        item("a", '{"name":"short"}'),
        item("b", '{"name":"a-much-longer-worker-name"}'),
        item("c", ""),
    ]

    gas, exact = bulk_register.gas_limit_for(registry, work)

    assert exact is True
    assert gas == 250000  # 200000 + 25%
    registry.estimate_register_gas.assert_called_once_with(
        b"b", '{"name":"a-much-longer-worker-name"}'
    )


def test_gas_limit_reports_an_inexact_estimate():
    registry = MagicMock()
    registry.estimate_register_gas.return_value = (400000, False)

    gas, exact = bulk_register.gas_limit_for(registry, [item("a", "")])

    assert exact is False
    assert gas == 500000


def test_format_units_renders_whole_and_fractional_amounts():
    assert bulk_register.format_units(10**18, 18) == "1"
    assert bulk_register.format_units(BOND, 18) == "100000"
    assert bulk_register.format_units(15 * 10**17, 18) == "1.5"


def test_confirm_returns_true_immediately_when_assume_yes():
    assert bulk_register.confirm("go?", assume_yes=True) is True


@pytest.mark.parametrize(
    "reply,expected",
    [("y", True), ("Y", True), ("yes", True), ("n", False), ("", False)],
)
def test_confirm_reads_stdin(monkeypatch, reply, expected):
    monkeypatch.setattr("builtins.input", lambda _: reply)
    assert bulk_register.confirm("go?", assume_yes=False) is expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_funds.py -v`
Expected: FAIL — `AttributeError: module 'bulk_register' has no attribute 'check_funds'`

- [ ] **Step 3: Write minimal implementation**

Add `from dataclasses import dataclass` and `from decimal import Decimal` to the imports at the top of `bulk_register.py`, then append:

```python
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
    bond = registry.bond_amount()
    required = bond * count
    balance = registry.sqd_balance()
    allowance = registry.allowance()

    if balance < required:
        decimals = registry.token_decimals()
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


def current_fees(w3) -> dict:
    """EIP-1559 fees with headroom for a base-fee rise mid-run."""
    base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 0)
    priority = w3.eth.max_priority_fee
    return {
        "maxFeePerGas": base_fee * 2 + priority,
        "maxPriorityFeePerGas": priority,
    }


def gas_limit_for(registry, work) -> tuple[int, bool]:
    """Pick one gas limit for every registration in the run.

    Gas scales with metadata length and one limit is reused for the whole run,
    so the estimate is taken against the *longest* metadata — the most
    expensive call. A shorter name can then never exceed it. The result is
    padded to absorb ordinary variation.
    """
    longest = max(work, key=lambda candidate: len(candidate.metadata))
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
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_funds.py -v`
Expected: 16 passed

- [ ] **Step 5: Commit**

```bash
git add bulk_register.py tests/test_funds.py
git commit -m "feat: add bond funds check, gas limit selection, and confirmation"
```

---

### Task 9: Registration loop

**Files:**
- Modify: `bulk_register.py` — append after `confirm`
- Test: `tests/test_register_all.py`

**Interfaces:**
- Consumes: `sqdreg.registry.Registry`, `sqdreg.runlog.RunLog`, `sqdreg.naming.NamedPeer`
- Produces:
  - `bulk_register.RunResult` — dataclass with `registered: int = 0`, `failed: int = 0`, `pending: int = 0`, `gas_used: int = 0`, `aborted: str | None = None`
  - `bulk_register.send_tx(w3, account, tx) -> HexBytes` — signs and sends, returning the hash
  - `bulk_register.wait_for(w3, tx_hash) -> dict` — waits for the receipt, `RECEIPT_TIMEOUT` seconds
  - `bulk_register.send_and_wait(w3, account, tx) -> tuple[str, dict]` — the two composed, returning `(tx_hash_hex, receipt)`; used for the one-off approval
  - `bulk_register.register_all(w3, account, registry, work, runlog, fees, gas) -> RunResult`

Send and wait are separate functions because the loop needs the hash *inside*
the timeout handler: a `pending` record whose `tx_hash` is missing is useless,
since looking the transaction up is the only way to resolve it.

Loop rules: fetch the nonce once and increment locally; append to the log after
every attempt, including the resolved name; reset the consecutive-failure
counter on success; abort on `MAX_CONSECUTIVE_FAILURES` consecutive failures; on
a receipt timeout record `pending` and abort, because a stuck nonce blocks
everything queued behind it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_register_all.py`:

```python
from unittest.mock import MagicMock

from web3.exceptions import TimeExhausted

import bulk_register
from sqdreg.naming import NamedPeer
from sqdreg.peerids import PeerEntry
from sqdreg.runlog import FAILED, PENDING, SUCCESS, RunLog

FEES = {"maxFeePerGas": 200, "maxPriorityFeePerGas": 10}


def work(*names):
    items = []
    for index, name in enumerate(names, start=1):
        entry = PeerEntry(
            peer_id=name, peer_bytes=name.encode(), name=None, index=index
        )
        items.append(
            NamedPeer(entry=entry, name=f"worker-{name}", metadata=f'{{"name":"{name}"}}')
        )
    return items


def make_env(receipts, start_nonce=5):
    """Build (w3, account, registry) whose receipts follow `receipts`.

    Each entry is either a status int (1 success, 0 revert) or an exception
    instance to raise from wait_for_transaction_receipt.
    """
    w3 = MagicMock()
    w3.eth.get_transaction_count.return_value = start_nonce
    w3.eth.send_raw_transaction.side_effect = [
        MagicMock(hex=lambda i=i: f"0x{i:02x}") for i in range(len(receipts))
    ]

    def receipt(_tx_hash, timeout=None):
        entry = receipts.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return {"status": entry, "gasUsed": 100, "blockNumber": 1000}

    w3.eth.wait_for_transaction_receipt.side_effect = receipt

    account = MagicMock()
    account.address = "0x0000000000000000000000000000000000000001"
    registry = MagicMock()
    registry.build_register.side_effect = lambda **kwargs: {"nonce": kwargs["nonce"]}
    return w3, account, registry


def test_all_successes_are_logged(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([1, 1])

    result = bulk_register.register_all(
        w3, account, registry, work("a", "b"), log, FEES, gas=300000
    )

    assert (result.registered, result.failed, result.pending) == (2, 0, 0)
    assert result.gas_used == 200
    assert result.aborted is None
    assert [r.status for r in log.records()] == [SUCCESS, SUCCESS]


def test_the_resolved_name_is_logged(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([1])

    bulk_register.register_all(
        w3, account, registry, work("a"), log, FEES, gas=300000
    )

    assert log.records()[0].name == "worker-a"


def test_metadata_is_passed_to_the_builder(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([1])

    bulk_register.register_all(
        w3, account, registry, work("a"), log, FEES, gas=300000
    )

    assert registry.build_register.call_args.kwargs["metadata"] == '{"name":"a"}'


def test_nonces_increment_by_one_per_transaction(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([1, 1, 1], start_nonce=7)

    bulk_register.register_all(
        w3, account, registry, work("a", "b", "c"), log, FEES, gas=300000
    )

    nonces = [c.kwargs["nonce"] for c in registry.build_register.call_args_list]
    assert nonces == [7, 8, 9]


def test_a_revert_is_logged_and_the_run_continues(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([0, 1])

    result = bulk_register.register_all(
        w3, account, registry, work("a", "b"), log, FEES, gas=300000
    )

    assert (result.registered, result.failed) == (1, 1)
    assert [r.status for r in log.records()] == [FAILED, SUCCESS]
    assert result.aborted is None


def test_three_consecutive_failures_abort(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([0, 0, 0, 1])

    result = bulk_register.register_all(
        w3, account, registry, work("a", "b", "c", "d"), log, FEES, gas=300000
    )

    assert (result.registered, result.failed) == (0, 3)
    assert "consecutive" in result.aborted
    assert len(log.records()) == 3


def test_a_success_resets_the_failure_counter(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([0, 0, 1, 0, 0, 1])

    result = bulk_register.register_all(
        w3, account, registry, work("a", "b", "c", "d", "e", "f"), log, FEES, gas=300000
    )

    assert (result.registered, result.failed) == (2, 4)
    assert result.aborted is None


def test_receipt_timeout_records_pending_and_aborts(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([TimeExhausted("too slow"), 1])

    result = bulk_register.register_all(
        w3, account, registry, work("a", "b"), log, FEES, gas=300000
    )

    assert (result.pending, result.registered) == (1, 0)
    assert "timed out" in result.aborted
    records = log.records()
    assert len(records) == 1
    assert records[0].status == PENDING
    # The hash must survive the timeout — it is the only way to resolve the
    # transaction later.
    assert records[0].tx_hash == "0x00"


def test_send_failure_is_logged_as_failed(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([1])
    w3.eth.send_raw_transaction.side_effect = ValueError("nonce too low")

    result = bulk_register.register_all(
        w3, account, registry, work("a"), log, FEES, gas=300000
    )

    assert result.failed == 1
    assert log.records()[0].status == FAILED
    assert "nonce too low" in log.records()[0].error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_register_all.py -v`
Expected: FAIL — `AttributeError: module 'bulk_register' has no attribute 'register_all'`

- [ ] **Step 3: Write minimal implementation**

Add `from web3.exceptions import TimeExhausted` and
`from sqdreg.runlog import FAILED, PENDING, SUCCESS, Record, RunLog, utc_now`
to the imports, then append:

```python
@dataclass
class RunResult:
    """Outcome of a registration loop."""

    registered: int = 0
    failed: int = 0
    pending: int = 0
    gas_used: int = 0
    aborted: str | None = None


def send_tx(w3, account, tx):
    """Sign and send one transaction, returning its hash."""
    signed = account.sign_transaction(tx)
    return w3.eth.send_raw_transaction(signed.raw_transaction)


def wait_for(w3, tx_hash) -> dict:
    """Wait for one receipt. Raises TimeExhausted past RECEIPT_TIMEOUT."""
    return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT)


def send_and_wait(w3, account, tx) -> tuple[str, dict]:
    """Send and wait as one step. For standalone transactions like the approval.

    The registration loop calls send_tx and wait_for separately, because it
    needs the hash inside its timeout handler.
    """
    tx_hash = send_tx(w3, account, tx)
    return tx_hash.hex(), wait_for(w3, tx_hash)


def register_all(w3, account, registry, work, runlog, fees, gas) -> RunResult:
    """Register each peer in turn, logging every attempt as it resolves."""
    result = RunResult()
    nonce = w3.eth.get_transaction_count(account.address)
    consecutive_failures = 0
    total = len(work)

    for position, item in enumerate(work, start=1):
        peer_id = item.entry.peer_id
        label = f"{peer_id} as {item.name}" if item.name else peer_id
        tx = registry.build_register(
            peer_bytes=item.entry.peer_bytes,
            metadata=item.metadata,
            nonce=nonce,
            fees=fees,
            gas=gas,
        )
        print(f"[{position}/{total}] {label}", flush=True)

        try:
            raw_hash = send_tx(w3, account, tx)
        except Exception as exc:
            # Nothing reached the mempool, so the nonce stays free for the
            # next attempt.
            runlog.append(
                Record(
                    peer_id=peer_id,
                    status=FAILED,
                    name=item.name,
                    error=str(exc),
                    timestamp=utc_now(),
                )
            )
            result.failed += 1
            consecutive_failures += 1
            print(f"  send failed: {exc}", file=sys.stderr)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                result.aborted = (
                    f"stopped after {consecutive_failures} consecutive failures"
                )
                break
            continue

        tx_hash = raw_hash.hex()

        try:
            receipt = wait_for(w3, raw_hash)
        except TimeExhausted as exc:
            runlog.append(
                Record(
                    peer_id=peer_id,
                    status=PENDING,
                    name=item.name,
                    tx_hash=tx_hash,
                    timestamp=utc_now(),
                )
            )
            result.pending += 1
            result.aborted = (
                "receipt timed out; later transactions would queue behind a "
                "stuck nonce, so the run stopped"
            )
            print(f"  timed out waiting for receipt: {exc}", file=sys.stderr)
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
```

Note on the nonce: it advances only after a receipt is seen, so a send that
never reached the mempool leaves the nonce free for the next attempt.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_register_all.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add bulk_register.py tests/test_register_all.py
git commit -m "feat: add sequential registration loop with abort conditions"
```

---

### Task 10: `main()` — wiring, plan output, and dry run

**Files:**
- Modify: `bulk_register.py` — append `main` and the `__main__` guard
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `bulk_register.main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing test**

Create `tests/test_main.py`:

```python
from unittest.mock import MagicMock, patch

import base58
import pytest

import bulk_register

BOND = 10**23


def peer_id_for(seed):
    raw = bytes([0x00, 36]) + bytes((seed + i) % 256 for i in range(36))
    return base58.b58encode(raw).decode()


def make_peer_file(tmp_path, count, names=False):
    lines = []
    for seed in range(count):
        peer_id = peer_id_for(seed)
        lines.append(f"{peer_id},named-{seed}" if names else peer_id)
    path = tmp_path / "peers.txt"
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def wired(monkeypatch):
    """Patch every boundary main() touches; yield the mocks."""
    account = MagicMock()
    account.address = "0x0000000000000000000000000000000000000001"
    w3 = MagicMock()
    registry = MagicMock()
    registry.bond_amount.return_value = BOND
    registry.sqd_balance.return_value = BOND * 1000
    registry.allowance.return_value = BOND * 1000
    registry.token_decimals.return_value = 18
    registry.is_registered.return_value = False
    registry.estimate_register_gas.return_value = (300000, True)

    monkeypatch.setattr(bulk_register, "load_signer", lambda: account)
    monkeypatch.setattr(bulk_register, "connect", lambda network, rpc: w3)
    monkeypatch.setattr(bulk_register, "Registry", lambda *a, **k: registry)
    monkeypatch.setattr(
        bulk_register,
        "current_fees",
        lambda _w3: {"maxFeePerGas": 200, "maxPriorityFeePerGas": 10},
    )
    yield account, w3, registry


def test_dry_run_sends_nothing(wired, tmp_path, capsys):
    _, w3, _ = wired
    path = make_peer_file(tmp_path, 3)

    code = bulk_register.main(
        [str(path), "--dry-run", "--log", str(tmp_path / "l.jsonl")]
    )

    assert code == 0
    w3.eth.send_raw_transaction.assert_not_called()
    assert "dry run" in capsys.readouterr().out.lower()


def test_dry_run_wins_over_yes(wired, tmp_path):
    _, w3, _ = wired
    path = make_peer_file(tmp_path, 3)

    bulk_register.main(
        [str(path), "--dry-run", "--yes", "--log", str(tmp_path / "l.jsonl")]
    )

    w3.eth.send_raw_transaction.assert_not_called()


def test_dry_run_shows_the_name_each_node_would_get(wired, tmp_path, capsys):
    path = make_peer_file(tmp_path, 3)

    bulk_register.main(
        [
            str(path),
            "--dry-run",
            "--name-template",
            "sqd-{n:03d}",
            "--log",
            str(tmp_path / "l.jsonl"),
        ]
    )

    out = capsys.readouterr().out
    assert "sqd-001" in out
    assert "sqd-003" in out


def test_explicit_names_beat_the_template(wired, tmp_path, capsys):
    path = make_peer_file(tmp_path, 2, names=True)

    bulk_register.main(
        [
            str(path),
            "--dry-run",
            "--name-template",
            "sqd-{n:03d}",
            "--log",
            str(tmp_path / "l.jsonl"),
        ]
    )

    out = capsys.readouterr().out
    assert "named-0" in out
    assert "sqd-001" not in out


def test_bad_template_exits_before_any_transaction(wired, tmp_path):
    _, w3, _ = wired
    path = make_peer_file(tmp_path, 2)

    with pytest.raises(SystemExit) as exc:
        bulk_register.main(
            [
                str(path),
                "--yes",
                "--name-template",
                "sqd-{bogus}",
                "--log",
                str(tmp_path / "l.jsonl"),
            ]
        )

    assert exc.value.code == 2
    w3.eth.send_raw_transaction.assert_not_called()


def test_oversized_name_exits_before_any_transaction(wired, tmp_path):
    _, w3, _ = wired
    path = tmp_path / "peers.txt"
    path.write_text(f"{peer_id_for(0)},{'x' * 300}\n")

    with pytest.raises(SystemExit) as exc:
        bulk_register.main([str(path), "--yes", "--log", str(tmp_path / "l.jsonl")])

    assert exc.value.code == 2
    w3.eth.send_raw_transaction.assert_not_called()


def test_declining_the_prompt_sends_nothing(wired, tmp_path, monkeypatch):
    _, w3, _ = wired
    path = make_peer_file(tmp_path, 2)
    monkeypatch.setattr("builtins.input", lambda _: "n")

    code = bulk_register.main([str(path), "--log", str(tmp_path / "l.jsonl")])

    assert code == 0
    w3.eth.send_raw_transaction.assert_not_called()


def test_confirmed_run_registers(wired, tmp_path):
    path = make_peer_file(tmp_path, 2)
    with patch.object(bulk_register, "register_all") as register_all:
        register_all.return_value = bulk_register.RunResult(registered=2)
        code = bulk_register.main(
            [str(path), "--yes", "--log", str(tmp_path / "l.jsonl")]
        )

    assert code == 0
    register_all.assert_called_once()


def test_approval_is_sent_when_the_allowance_is_short(wired, tmp_path):
    _, _, registry = wired
    registry.allowance.return_value = 0
    path = make_peer_file(tmp_path, 2)
    with patch.object(bulk_register, "send_and_wait") as send_and_wait, patch.object(
        bulk_register, "register_all"
    ) as register_all:
        send_and_wait.return_value = (
            "0xapproval",
            {"status": 1, "gasUsed": 1, "blockNumber": 1},
        )
        register_all.return_value = bulk_register.RunResult(registered=2)
        bulk_register.main([str(path), "--yes", "--log", str(tmp_path / "l.jsonl")])

    registry.build_approve.assert_called_once()
    assert registry.build_approve.call_args.kwargs["amount"] == BOND * 2


def test_dry_run_never_sends_the_approval(wired, tmp_path):
    _, w3, registry = wired
    registry.allowance.return_value = 0
    path = make_peer_file(tmp_path, 2)

    bulk_register.main([str(path), "--dry-run", "--log", str(tmp_path / "l.jsonl")])

    w3.eth.send_raw_transaction.assert_not_called()


def test_malformed_input_exits_before_any_transaction(wired, tmp_path):
    _, w3, _ = wired
    path = tmp_path / "peers.txt"
    path.write_text("garbage-0OIl\n")

    with pytest.raises(SystemExit) as exc:
        bulk_register.main([str(path), "--yes", "--log", str(tmp_path / "l.jsonl")])

    assert exc.value.code == 2
    w3.eth.send_raw_transaction.assert_not_called()


def test_nothing_to_do_exits_cleanly(wired, tmp_path, capsys):
    _, w3, registry = wired
    registry.is_registered.return_value = True
    path = make_peer_file(tmp_path, 2)

    code = bulk_register.main([str(path), "--yes", "--log", str(tmp_path / "l.jsonl")])

    assert code == 0
    w3.eth.send_raw_transaction.assert_not_called()
    assert "nothing to register" in capsys.readouterr().out.lower()


def test_duplicate_warnings_are_printed(wired, tmp_path, capsys):
    peer_id = peer_id_for(0)
    path = tmp_path / "peers.txt"
    path.write_text(f"{peer_id}\n{peer_id}\n")

    bulk_register.main([str(path), "--dry-run", "--log", str(tmp_path / "l.jsonl")])

    assert "duplicate" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_main.py -v`
Expected: FAIL — `AttributeError: module 'bulk_register' has no attribute 'main'`

- [ ] **Step 3: Write minimal implementation**

Add these to the imports:

```python
from sqdreg.naming import NamingError, prepare
from sqdreg.peerids import PeerIdError, parse_file
from sqdreg.registry import Registry
```

Then append:

```python
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    network = NETWORKS[args.network]
    log_path = args.log or default_log_path(args.peer_id_file)

    account = load_signer()
    w3 = connect(network, args.rpc_url)
    registry = Registry(w3, network, account.address)
    runlog = RunLog(log_path)

    try:
        entries, duplicates = parse_file(args.peer_id_file)
    except PeerIdError as exc:
        fail(str(exc))
    except OSError as exc:
        fail(f"cannot read {args.peer_id_file}: {exc}")

    for warning in duplicates:
        print(f"warning: {warning}", file=sys.stderr)
    if not entries:
        print(f"{args.peer_id_file} contains no peer IDs")
        return 0

    try:
        prepared = prepare(entries, args.name_template)
    except NamingError as exc:
        fail(str(exc))

    work, skipped_logged, skipped_onchain = select_work(
        prepared, runlog, registry, args.limit
    )

    print(f"network:     {network.name} (chain {network.chain_id})")
    print(f"wallet:      {account.address}")
    print(f"log:         {log_path}")
    print(f"in file:     {len(entries)}")
    print(f"skipped:     {len(skipped_logged)} logged, {len(skipped_onchain)} on-chain")
    print(f"to register: {len(work)}")

    if not work:
        print("nothing to register")
        return 0

    funds = check_funds(registry, len(work))
    decimals = registry.token_decimals()
    gas, exact = gas_limit_for(registry, work)
    fees = current_fees(w3)
    gas_cost_wei = gas * fees["maxFeePerGas"] * len(work)

    print(f"bond:        {format_units(funds.bond, decimals)} SQD each")
    print(f"bond total:  {format_units(funds.required, decimals)} SQD")
    print(f"balance:     {format_units(funds.balance, decimals)} SQD")
    print(
        f"gas:         ~{format_units(gas_cost_wei, 18)} ETH max"
        f"{'' if exact else ' (estimate unavailable, using fallback)'}"
    )
    if funds.needs_approval:
        print(
            f"approval:    needed — allowance is "
            f"{format_units(funds.allowance, decimals)} SQD, "
            f"will approve {format_units(funds.required, decimals)} SQD"
        )

    if args.dry_run:
        print("\n-- dry run, nothing sent --")
        for item in work:
            print(f"  {item.entry.peer_id} -> {item.name or '(unnamed)'}")
        return 0

    if not confirm(f"\nRegister {len(work)} worker(s) on {network.name}?", args.yes):
        print("aborted")
        return 0

    if funds.needs_approval:
        approve_tx = registry.build_approve(
            amount=funds.required,
            nonce=w3.eth.get_transaction_count(account.address),
            fees=fees,
        )
        print("approving bond transfer...")
        tx_hash, receipt = send_and_wait(w3, account, approve_tx)
        if receipt["status"] != 1:
            fail(f"approval reverted ({tx_hash})")
        print(f"  approved ({tx_hash})")

    result = register_all(w3, account, registry, work, runlog, fees, gas)

    remaining = (
        len(entries)
        - len(skipped_logged)
        - len(skipped_onchain)
        - result.registered
    )
    print(
        f"\nregistered {result.registered}, failed {result.failed}, "
        f"pending {result.pending}, gas used {result.gas_used}"
    )
    if result.aborted:
        print(f"run stopped: {result.aborted}", file=sys.stderr)
    if remaining > 0:
        print(f"{remaining} peer ID(s) still unregistered; resume with:")
        print(f"  {sys.argv[0]} {args.peer_id_file} --network {network.name}")

    return 1 if result.failed or result.pending else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted; progress is in the run log", file=sys.stderr)
        raise SystemExit(130) from None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_main.py -v`
Expected: 13 passed

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/pytest -v`
Expected: all pass — 118 tests

- [ ] **Step 6: Verify the CLI is wired end to end**

Run: `.venv/bin/python bulk_register.py --help`
Expected: usage text listing `--network`, `-n/--limit`, `--name-template`, `--dry-run`, `--yes`, `--rpc-url`, `--log`

- [ ] **Step 7: Commit**

```bash
git add bulk_register.py tests/test_main.py
git commit -m "feat: wire main() with plan output, approval, and dry run"
```

---

### Task 11: README

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: the finished CLI.
- Produces: nothing consumed by other tasks.

- [ ] **Step 1: Write the README**

Create `README.md`:

```markdown
# bulk-register

Register SQD worker nodes in bulk from a file of libp2p peer IDs, optionally
naming each one.

## Setup

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    cp .env.example .env      # then put your key in it

Provide exactly one credential in `.env` or the environment:

- `PRIVATE_KEY` — hex, with or without `0x`
- `MNEMONIC` — BIP-39 phrase, derived at `m/44'/60'/0'/0/0`

`.env` is gitignored. Nothing but the derived address is ever printed.

## Usage

    .venv/bin/python bulk_register.py peer_ids.txt --dry-run
    .venv/bin/python bulk_register.py peer_ids.txt --limit 10
    .venv/bin/python bulk_register.py peer_ids.txt --name-template 'nodexeus-{n:03d}'
    .venv/bin/python bulk_register.py peer_ids.txt --network tethys

| Flag | Meaning |
| --- | --- |
| `--network` | `mainnet` (default) or `tethys` |
| `-n`, `--limit` | Register at most this many *new* nodes |
| `--name-template` | Name for lines without an explicit name |
| `--dry-run` | Run every check, print the plan, send nothing |
| `--yes` | Skip the confirmation prompt |
| `--rpc-url` | Override the network's default RPC |
| `--log` | Result log path (default `<input>.run.jsonl`) |

Always `--dry-run` first. It reports the bond total, the estimated gas, whether
an approval is needed, and exactly which peer IDs would be registered under
which names.

## Input file

One entry per line, either `peer_id` or `peer_id,name`:

    12D3KooW...aaa,prod-worker-01
    12D3KooW...bbb,prod-worker-02
    12D3KooW...ccc

Blank lines and `#` comments are ignored. Duplicates are collapsed with a
warning, keeping the first line's name. A name may contain commas; only the
first comma separates the fields. A line ending in a bare comma is an error.

## Naming

A node's displayed name is the `name` key of the JSON metadata the contract
stores, which the network indexer parses and exposes over GraphQL. Names come
from two places, explicit beating generated:

1. The optional second column in the input file.
2. `--name-template`, applied to any line without a name.

The template supports `{n}` and `{peer_id}`, including format specs, so
`--name-template 'nodexeus-{n:03d}'` yields `nodexeus-001`, `nodexeus-002`, and
so on. `{n}` is the peer ID's position in the *file*, not in the work list, so a
given peer ID gets the same name regardless of which subset a run registers.

Lines with neither register unnamed. The contract's `updateMetadata` can name
them later without re-bonding, so a missing or wrong name is not permanent.

## How `--limit` works

The limit applies to the *actionable* set, after peer IDs already registered
have been filtered out. `--limit 10` means ten new registrations, so running it
twice against the same file registers ten, then the next ten.

## Safety

- The RPC's chain ID must match the chosen network, or the run aborts. This is
  the guard against firing mainnet bonds at a tethys-intended list.
- The whole input file — peer IDs, names, and metadata sizes — is validated
  before any transaction is sent.
- Registration bonds SQD per worker and needs an ERC-20 allowance. The script
  approves exactly `bond × count`, never an unlimited amount.
- Already-registered peer IDs are skipped, so re-running a partly finished file
  wastes no gas.
- Every attempt is appended to a JSONL log immediately, so an interrupted run
  resumes cleanly.
- Three consecutive failures abort the run rather than burning gas down a long
  file.
- A receipt timeout stops the run: nonces are sequential, so a stuck
  transaction would block everything behind it.

## Note on re-registering withdrawn peer IDs

The registry keeps `workerIds[peerId]` populated after a worker is withdrawn,
even though the worker slot itself is vacated and the peer ID can be registered
again. The skip check therefore reads both `workerIds` and the worker's
`registeredAt`; a peer ID you previously cycled out is correctly offered for
re-registration rather than skipped forever.

One caveat from the contract: re-registering a vacated slot only works for the
account that originally created it. Someone else's withdrawn peer ID reverts,
and shows up as a normal failure.

## Networks

| Network | Chain | WorkerRegistration |
| --- | --- | --- |
| `mainnet` | Arbitrum One (42161) | `0x36e2b147db67e76ab67a4d07c293670ebefcae4e` |
| `tethys` | Arbitrum Sepolia (421614) | `0xCD8e983F8c4202B0085825Cf21833927D1e2b6Dc` |

The bond token address is read from the registry's `SQD()` getter, not
hardcoded.

## Tests

    .venv/bin/pytest

`web3` is mocked throughout. No test contacts an RPC endpoint or needs a key.
```

- [ ] **Step 2: Verify the documented commands match the implementation**

Run: `.venv/bin/python bulk_register.py --help`
Expected: every flag in the README table appears in the usage output, with the same defaults.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add README"
```

---

## Notes for the implementer

- **Import placement.** Tasks 6–10 all append to `bulk_register.py` and each adds imports. Collect them at the top of the file rather than mid-file: `argparse`, `os`, `sys`, `dataclass`, `Decimal`, `NoReturn`, `load_dotenv`, `Account`, `Web3`, `TimeExhausted`, then the `sqdreg` imports.
- **`web3` v7 naming.** Signed transactions expose `raw_transaction`, not `rawTransaction`. If `send_raw_transaction` raises `AttributeError`, a v6 install is the cause — check `requirements.txt` was honoured.
- **The registration check must stay two reads.** `withdraw()` leaves `workerIds[peerId]` populated while `delete workers[workerId]` vacates the slot, and `register()` explicitly permits re-registering it. Collapsing `is_registered` to `workerIds != 0` would permanently skip any peer ID that had been cycled out. `test_withdrawn_worker_is_not_registered` guards this.
- **`select_work` scanning.** The early `break` on the limit is deliberate and tested. Do not hoist the registration checks into a list comprehension over all entries; that would make a `--limit 10` run against 500 lines do 500 checks.
- **Never auto-estimate gas inside `build_register`.** `build_transaction` estimates gas whenever the dict lacks a `gas` key, and estimation reverts while the allowance is missing. Gas is always passed in explicitly.
- **Gas is estimated against the longest metadata.** One limit is reused for the whole run, and gas scales with metadata length, so estimating against a short name would under-fund a later long one. `test_gas_limit_estimates_against_the_longest_metadata` guards this.
- **`{n}` is the file index.** It comes from `PeerEntry.index`, assigned in `parse_file` after duplicates collapse — not from the work list's position. Otherwise names would shift depending on how many peers a run happened to skip.
- **Don't hardcode base58 peer IDs in tests.** Build them from raw multihash bytes as the existing helpers do, so fixtures are valid by construction.
