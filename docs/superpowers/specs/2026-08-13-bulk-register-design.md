# Bulk SQD Worker Registration — Design

Date: 2026-08-13
Status: Approved

## Purpose

Register many SQD worker nodes on-chain from a file of peer IDs, using a local
wallet, in one command:

```bash
bulk_register.py peer_ids.txt
```

This replaces an earlier throwaway JS script (`bulk-register.js`) that generated
fresh peer IDs with `sqd-keygen` and registered 300 of them with no pre-checks,
no idempotency, no naming, and the wallet mnemonic hardcoded in the source. The
peer IDs now come from a caller-supplied file; the script never generates keys.

## Non-goals

- Key generation. Peer IDs are produced elsewhere and handed to this script.
- Deregistration and bond withdrawal.
- `updateMetadata`. Renaming an existing worker is a separate, cheap operation
  the contract already supports; this script only sets a name at registration.
- Metadata fields other than `name`. The indexer also surfaces `website`,
  `email`, and `description`, but none are needed here.
- Any live-network automated test.

## On-chain facts

`WorkerRegistration` exposes:

| Member | Purpose here |
| --- | --- |
| `register(bytes peerId, string metadata) public` | The write call. |
| `register(bytes peerId) external` | Sugar — its body is exactly `register(peerId, "")`. |
| `workerIds(bytes) → uint256` | First half of the registration check. `0` means the peer ID has never been seen. |
| `getWorker(uint256) → Worker` | Second half. `Worker.registeredAt == 0` means the slot is vacant. |
| `bondAmount() → uint256` | SQD bonded per worker. |
| `SQD() → address` | The bond token, so it need not be hardcoded. |

Because `register(peerId)` is defined as `register(peerId, "")`, the script uses
only the two-argument overload and passes `""` when no name is given. One ABI
entry, one code path.

Registration performs `SQD.transferFrom(msg.sender, address(this), bondAmount)`,
so the wallet must hold the bond **and** have granted the registry an ERC-20
allowance covering it. The contract also enforces `peerId.length <= 64`.

### The registration check is two reads, not one

`withdraw()` runs `delete workers[workerId]` but leaves `workerIds[peerId]`
pointing at the now-vacant slot. `register()` accounts for this explicitly:

```solidity
if (workerIds[peerId] != 0) {
  require(workers[workerIds[peerId]].registeredAt == 0, "Worker already exists");
  require(ownedWorkers[msg.sender].contains(workerIds[peerId]), "Worker already registered by different account");
  workerId = workerIds[peerId];
}
```

So a peer ID that was registered, deregistered, then withdrawn has a non-zero
`workerIds` entry while being perfectly re-registerable. Treating `workerIds != 0`
as "already registered" would silently and permanently skip any peer ID that had
been cycled out. The check is therefore:

> registered ⟺ `workerIds(peerId) != 0` **and** `getWorker(that id).registeredAt != 0`

The second read only happens when the first is non-zero, which is the minority
case, so this costs little.

Note the third `require`: re-registering a vacated slot only works for the
account that originally created it. Someone else's withdrawn peer ID reverts,
and is handled by the normal failure path.

Deployments (from `subsquid-network-contracts`):

| Network | Chain ID | WorkerRegistration | SQD |
| --- | --- | --- | --- |
| `mainnet` (Arbitrum One) | 42161 | `0x36e2b147db67e76ab67a4d07c293670ebefcae4e` | `0x1337420dED5ADb9980CFc35f8f2B054ea86f8aB1` |
| `tethys` (Arbitrum Sepolia) | 421614 | `0xCD8e983F8c4202B0085825Cf21833927D1e2b6Dc` | `0x24f9C46d86c064a6FA2a568F918fe62fC6917B3c` |

The SQD addresses are recorded for reference only; at runtime the token address
is read from `SQD()` on the registry.

## Node names

The contract stores metadata as an opaque string. The network indexer
`JSON.parse`s it and exposes the result as GraphQL fields on `Worker` —
confirmed by introspection: `name`, `website`, `email`, `description`. A node's
displayed name is therefore the `name` key of a JSON object:

```json
{"name":"prod-worker-01"}
```

The script emits compact JSON with no spaces, and `""` when there is no name.
Metadata is capped at 256 bytes; anything longer is rejected before sending,
since the only limit the chain imposes is gas.

Names come from two places, explicit winning over generated:

1. **An optional second column** in the input file: `peer_id,name`.
2. **`--name-template`**, applied to any line lacking a name.

The template is a Python format string supporting `{n}` and `{peer_id}`. `{n}`
is the peer ID's 1-based position in the file after duplicates are collapsed —
*not* its position in the work list — so a given peer ID gets the same name no
matter which subset a run happens to register. Format specs work, so
`--name-template 'nodexeus-{n:03d}'` yields `nodexeus-001`, `nodexeus-002`, and
so on. The template is validated against a probe value at startup, so a bad
placeholder fails immediately rather than mid-run.

If a line has no name and no template is given, the node registers with empty
metadata and shows up unnamed. `updateMetadata` can name it later.

## Layout

```
sqd-registration/
├── bulk_register.py          # CLI entry point: arg parsing + orchestration
├── sqdreg/
│   ├── __init__.py
│   ├── networks.py           # network table: RPC, registry address, chain ID
│   ├── peerids.py            # file parsing, base58 decode, multihash validation
│   ├── naming.py             # name resolution and metadata encoding
│   ├── registry.py           # WorkerRegistration + ERC-20 wrapper
│   └── runlog.py             # resumable JSONL result log
├── tests/
├── requirements.txt          # web3, base58, python-dotenv, pytest
├── .env.example
├── .gitignore                # .env, *.jsonl
└── peer_ids.txt.example
```

Small modules rather than one file, so the on-chain calls can be mocked and the
parsing, naming, and skip logic tested without a wallet or an RPC endpoint.

### Module responsibilities

**`networks.py`** — a static table mapping network name to default RPC URL,
registry address, and expected chain ID. No I/O. Depends on nothing.

**`peerids.py`** — reads the input file and returns validated `PeerEntry`
records carrying the base58 string, the raw bytes, the optional explicit name,
and the 1-based file index. Owns all parsing rules and the multihash sanity
check. Pure functions over text; no web3 import.

**`naming.py`** — resolves a `PeerEntry` plus an optional template into a final
name, and encodes a name as the metadata string. Validates the template and the
encoded size. Pure; no web3 import.

**`registry.py`** — wraps a `web3.eth.Contract`. Exposes the reads, the
two-part `is_registered` check, and builders returning unsigned transactions.
Signing and sending live in the entry point so the wrapper stays trivially
mockable.

**`runlog.py`** — append-only JSONL. One record per attempt:
`{peer_id, name, status, tx_hash, block, timestamp, error}` where `status` is
`success`, `failed`, or `pending`. Exposes `succeeded_peer_ids()` for the skip
filter. Flushes on every write.

**`bulk_register.py`** — argument parsing, the run sequence below, all console
output, and the confirmation prompt.

## Interface

```bash
bulk_register.py peer_ids.txt                              # mainnet, confirms before sending
bulk_register.py peer_ids.txt --network tethys
bulk_register.py peer_ids.txt --limit 10                   # register only 10 new nodes
bulk_register.py peer_ids.txt --name-template 'sqd-{n:03d}'
bulk_register.py peer_ids.txt --dry-run                    # validate + print plan, send nothing
bulk_register.py peer_ids.txt --yes                        # skip the confirmation prompt
bulk_register.py peer_ids.txt --rpc-url https://...        # override the default RPC
bulk_register.py peer_ids.txt --log run.jsonl              # override the default log path
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--network` | `mainnet` | `mainnet` or `tethys`. |
| `--limit`, `-n` | unset | Cap on new registrations. Must be a positive integer. |
| `--name-template` | unset | Name for lines without an explicit name. Supports `{n}` and `{peer_id}`. |
| `--dry-run` | off | Run every check, print the plan, send nothing. |
| `--yes` | off | Skip the interactive confirmation. |
| `--rpc-url` | per network | Override the RPC endpoint. |
| `--log` | `<input>.run.jsonl` | Result log path. |

### Credentials

The wallet key is read from the environment, or from a `.env` file in the
working directory, in this order:

1. `PRIVATE_KEY` — hex, with or without `0x`.
2. `MNEMONIC` — BIP-39 phrase, using the default derivation path
   `m/44'/60'/0'/0/0`.

If both are set, `PRIVATE_KEY` wins and the script warns. If neither is set it
exits with code 2. The key is never logged, echoed, or written to the run log;
only the derived address is displayed.

### Input file format

One entry per line, either `peer_id` or `peer_id,name`. Blank lines and lines
whose first non-whitespace character is `#` are ignored. Whitespace around both
fields is stripped. A trailing comma with nothing after it is an error, not an
empty name — it is far more likely a typo than an intent. Duplicate peer IDs are
collapsed to their first occurrence with a warning naming the repeated ID;
whether the duplicate carries a different name makes no difference, the first
wins.

## Run sequence

1. **Load credentials.** Derive the signing address.
2. **Connect.** Instantiate web3 against the RPC URL. Read `eth_chainId` and
   assert it equals the selected network's expected chain ID. A mismatch exits
   immediately — this is the guard against firing mainnet bonds at a list
   intended for tethys, or vice versa.
3. **Validate the template,** if one was given, by formatting it against a probe
   value. A bad placeholder exits here, before any file or chain access.
4. **Parse and validate the whole file.** Base58-decode every peer ID and check
   the result looks like a libp2p multihash: first byte `0x00` (identity) or
   `0x12` (sha2-256), second byte equal to the remaining length, total length
   between 32 and 64 bytes — the upper bound matching the contract's own
   `peerId.length <= 64`. **Any malformed line aborts the run before a single
   transaction is sent.** Discovering a bad line 40 registrations in is the
   failure mode this prevents.
5. **Resolve names and encode metadata** for every entry, and check each encoded
   result against the 256-byte cap. Also done before any transaction.
6. **Apply the skip filters,** in order:
   a. Drop peer IDs recorded as `success` in the run log.
   b. For each survivor, apply the two-read registration check; drop it if the
      registry holds a live registration.
   Both counts are reported.
7. **Apply `--limit`.** Truncate the remaining work list to the first `N` in
   original file order. Applying the limit *after* the skip filters is
   deliberate: `--limit 10` means ten new registrations, not "look at the first
   ten lines and skip whatever is already done among them." File order makes it
   deterministic, so a second run continues down the file instead of
   reshuffling. A limit larger than the actionable set clamps silently.
8. **Check funds.** Read `bondAmount()`; required bond is `bond × len(work)`.
   Exit if the wallet's SQD balance is below that. Read the current allowance;
   if it is below the requirement, display required versus current and prompt.
   On confirmation, send a single `approve(required)` and wait for its receipt
   before continuing. `--dry-run` reports what the approval would be and sends
   nothing. Under `--yes` the approval proceeds without prompting.
9. **Estimate cost.** Gas varies with metadata length, and one gas limit is
   reused for every registration in the run, so the estimate is taken against
   **the work item with the longest metadata** — the most expensive one — and
   then padded 25%. A shorter name can never exceed a limit set by the longest.
   Estimation reverts when the allowance is not yet in place, the common case
   during a `--dry-run` on a wallet that has never approved, so a failed
   estimate falls back to a documented constant (`FALLBACK_REGISTER_GAS`) and
   the output labels the figure as approximate rather than aborting the run.
10. **Confirm.** `--dry-run` prints the full plan — network, address, counts,
    skips, bond, estimated gas, and each peer ID with the name it would get —
    then exits 0. `--dry-run` takes precedence over `--yes`; combining them
    still sends nothing. Otherwise prompt y/n unless `--yes`.
11. **Register sequentially.** Fetch the nonce once, then for each entry build,
    sign, and send the transaction with an explicit incrementing nonce, and wait
    for its receipt before starting the next. Append to the run log immediately
    after each receipt so a crash or Ctrl-C loses nothing.
12. **Summarize.** Registered, skipped, and failed counts; total gas spent; the
    number of peer IDs still unregistered in the file; and the command to resume.

## Error handling

| Condition | Behaviour |
| --- | --- |
| No `PRIVATE_KEY` or `MNEMONIC` | Exit 2 before connecting. |
| Chain ID mismatch | Exit 2. No transactions. |
| Bad `--name-template` placeholder | Exit 2 before reading the file. |
| Malformed or undecodable peer ID | Exit 2, naming the line number. No transactions. |
| Line ends in a bare comma | Exit 2, naming the line number. |
| Encoded metadata over 256 bytes | Exit 2, naming the peer ID. No transactions. |
| `--limit` not a positive integer | Argparse rejects it, exit 2. |
| SQD balance below required bond | Exit 2, showing required versus held. |
| Allowance short, prompt declined | Exit 0. Nothing sent. |
| Approval transaction reverts | Exit 2 before any registration. |
| Single registration reverts | Log `failed` with the revert reason and continue. |
| 3 *consecutive* failures | Abort the run. Prevents burning gas down a 300-line file when the cause is systemic. |
| Receipt wait times out | Log `pending` with the tx hash — never `success` — and **abort the run**. Because nonces are sequential, a stuck transaction blocks every one queued behind it, so continuing would only produce a cascade of `pending` records. The next run's registration check settles whether it landed. |
| Ctrl-C | Flush the log, print the resume command, exit 130. |

A `pending` record does not satisfy the log skip filter, so a re-run re-examines
that peer ID on-chain rather than assuming either outcome.

## Testing

pytest, with `web3` mocked throughout. No test touches a real RPC endpoint or
requires a key. Test peer IDs are built programmatically from raw multihash
bytes rather than hardcoded as base58 strings, so fixtures are valid by
construction.

**`test_peerids.py`** — valid `12D3KooW…` (identity multihash) and `Qm…`
(sha2-256) IDs decode correctly; non-base58 text, truncated IDs, over-64-byte
IDs, and wrong multihash prefixes are rejected; comments and blank lines are
ignored; the optional name column parses, with whitespace stripped; a bare
trailing comma is an error; duplicates collapse and warn, keeping the first
line's name; file indices are 1-based and assigned after duplicates collapse;
errors name the offending line number.

**`test_naming.py`** — an explicit name beats a template; a template fills in
where no explicit name exists; `{n}` and `{peer_id}` substitute correctly;
format specs such as `{n:03d}` work; `{n}` reflects file position rather than
work-list position; an unknown placeholder is rejected at validation time;
metadata encodes as compact JSON; a missing name encodes as `""`; over-cap
metadata is rejected.

**`test_registry.py`** — the wrapper builds `approve` and `register`
transactions with the expected calldata, metadata, and nonce; `is_registered`
returns `False` when `workerIds` is 0 *without* a second read, `True` when
`workerIds` is non-zero and `registeredAt` is non-zero, and `False` for a
withdrawn slot where `workerIds` is non-zero but `registeredAt` is 0; a failed
gas estimate yields the fallback.

**`test_runlog.py`** — round-trips records including the name;
`succeeded_peer_ids()` returns only `success` entries and excludes `failed` and
`pending`; appending to an existing log preserves prior records.

**`test_select_work.py` / `test_register_all.py` / `test_main.py`** — with a
mocked registry and signer:

- Peer IDs already registered on-chain are skipped and never sent.
- A withdrawn peer ID is *not* skipped.
- Peer IDs logged as `success` are skipped without an on-chain read.
- `--limit` applies after the skip filters, clamps when larger than the
  actionable set, and preserves file order across two sequential runs.
- Bond and allowance arithmetic use the limited count, not the file count.
- Gas is estimated against the longest metadata in the work list.
- Nonces increment by exactly one per sent transaction.
- Three consecutive failures abort; two failures separated by a success do not.
- A receipt timeout records `pending`, not `success`, and stops the run.
- Chain ID mismatch, a bad template, malformed input, and over-cap metadata all
  exit before any transaction is built.
- `--dry-run` sends nothing on any path, including the approval, and does so
  even when combined with `--yes`.
- The resolved name is written to the run log alongside the peer ID.
