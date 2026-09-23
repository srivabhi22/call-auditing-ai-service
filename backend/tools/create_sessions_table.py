"""Create the `sessions` table, and nothing else.

    python3 -m backend.tools.create_sessions_table --dry-run
    python3 -m backend.tools.create_sessions_table
    python3 -m backend.tools.create_sessions_table --verify

`provision_aws` creates it too, along with everything else this backend needs.
This exists for the case where everything else is already there and running:
adding one table to a live deployment should not mean running the script that
also touches the calls table, the bucket policy and the lifecycle rules.

Idempotent. A table that is already there is reported and left alone.

    PK                             SK           the row
    SESSION#2026-09-23T02:00:04Z   PENDING      the run is working
    SESSION#2026-09-23T02:00:04Z   COMPLETED    every job has been taken up
                                                and has reached a final state

with `totalCallsProcessed`, `callsFailed`, and the counts that make those two
readable (`callsSkipped`, `callsNotReached`, `callsQueued`, `queueRemaining`).
The status is the sort key, so completing a session *replaces* the row rather
than updating it — see `store.SessionsRepository`. No secondary index: the key
is the whole access pattern, and an index on the status would be a second copy
of the table written on every rewrite.

Needs `dynamodb:CreateTable` and `dynamodb:DescribeTable` on the table:
`backend/iam/policy.json` grants both.
"""

import argparse
import sys

from ..common.config import settings
from ..common import store
from .provision_aws import _describe_table, create_table, sessions_spec


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be created, touch nothing")
    parser.add_argument("--verify", action="store_true",
                        help="check it exists; exit non-zero if not")
    args = parser.parse_args(argv)

    spec = sessions_spec()
    dynamo = store.client("dynamodb", settings.dynamo_endpoint_url)
    print(f"region {settings.aws_region}  table {spec['TableName']}")
    if settings.dynamo_endpoint_url:
        print(f"endpoint {settings.dynamo_endpoint_url}")

    if args.verify:
        table = _describe_table(dynamo, spec["TableName"])
        if table is None:
            print("FAIL the table does not exist")
            return 1
        if table.get("denied"):
            print(f"FAIL not authorised ({table['denied']}) — attach "
                  f"backend/iam/policy.json")
            return 1
        print(f"ok   table {spec['TableName']} ({table['TableStatus']}, "
              f"{table.get('ItemCount', 0)} items)")
        # The table is meant to have no index. One that is there is not fatal,
        # but nothing queries it and it still costs a write on every put and
        # delete, so it is said out loud.
        for orphan in table.get("GlobalSecondaryIndexes", []):
            print(f"WARN   index {orphan['IndexName']} exists but nothing "
                  f"queries it; it still costs a write on every row rewrite")
        return 0

    created = create_table(dynamo, spec, args.dry_run)
    if created and not args.dry_run:
        print("\nReady. The batch run writes a PENDING row when it starts and "
              "replaces it with COMPLETED when every queued job is done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
