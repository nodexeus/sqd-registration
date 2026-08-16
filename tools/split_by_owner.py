#!/usr/bin/env python3
"""Split a peer ID list into one file per registering account.

    .venv/bin/python tools/split_by_owner.py owners.csv --out-dir batches

Each output file holds workers from a single creator, which is what lets the
operator run one command per file with no flags: bulk_register.py reads the
creator off the chain and asks for that account's credential.

Every file carries a header comment naming the creator, the account that must
sign, and how many workers it holds. Comments are ignored by the parser, so the
file stays directly runnable while documenting itself.
"""

import argparse
import collections
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ordered so the operator meets the simple cases first: wallets sign for
# themselves, holding contracts need their beneficiary.
KIND_ORDER = {"eoa": 0, "vesting": 1, "contract": 2, "withdrawn": 3, "unregistered": 4}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("owners_csv", help="output of tools/owners.py --csv")
    parser.add_argument("--out-dir", default="batches")
    parser.add_argument(
        "--prefix", default="peers", help="filename prefix (default: peers)"
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PEER_ID",
        help=(
            "leave this peer ID out; repeat for several. Use for workers "
            "already migrated, which would otherwise be deregistered again"
        ),
    )
    args = parser.parse_args(argv)

    rows = list(csv.DictReader(open(args.owners_csv)))

    excluded = set(args.exclude)
    missing = excluded - {r["peer_id"] for r in rows}
    if missing:
        sys.exit(
            "error: --exclude peer ID not present in "
            f"{args.owners_csv}: {', '.join(sorted(missing))}"
        )
    if excluded:
        rows = [r for r in rows if r["peer_id"] not in excluded]
        print(f"excluding {len(excluded)} peer ID(s)\n")

    groups = collections.defaultdict(list)
    for row in rows:
        groups[(row["owner_kind"], row["creator"], row["controller"])].append(
            row["peer_id"]
        )

    ordered = sorted(
        groups.items(),
        key=lambda kv: (KIND_ORDER.get(kv[0][0], 9), -len(kv[1]), kv[0][1]),
    )
    os.makedirs(args.out_dir, exist_ok=True)

    manifest = []
    for index, ((kind, creator, controller), peers) in enumerate(ordered, start=1):
        signer = controller or creator
        name = f"{args.prefix}-{index:02d}-{kind}-{signer[2:10]}-{len(peers)}.txt"
        path = os.path.join(args.out_dir, name)
        with open(path, "w") as handle:
            handle.write(f"# {len(peers)} workers registered by {creator}\n")
            handle.write(f"# owner kind: {kind}\n")
            if kind == "vesting":
                handle.write(
                    f"# calls go through this contract's execute(),\n"
                    f"# so the run must be signed by its owner: {signer}\n"
                )
            else:
                handle.write(f"# must be signed by: {signer}\n")
            handle.write("#\n")
            handle.write(f"#   ./sqd {path} --action deregister\n")
            handle.write("\n")
            handle.write("\n".join(peers) + "\n")
        manifest.append((index, name, kind, creator, signer, len(peers)))

    index_path = os.path.join(args.out_dir, "MANIFEST.csv")
    with open(index_path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("order", "file", "owner_kind", "creator", "signer", "workers"))
        writer.writerows(manifest)

    print(f"{len(rows)} peer IDs -> {len(manifest)} files in {args.out_dir}/\n")
    print(f"{'#':>3}  {'file':<44}{'kind':<10}{'signer':<44}workers")
    for order, name, kind, _creator, signer, count in manifest:
        print(f"{order:>3}  {name:<44}{kind:<10}{signer:<44}{count}")
    print(f"\ntotal {sum(m[5] for m in manifest)} workers; index at {index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
