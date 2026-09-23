"""Create the tables and the bucket this backend expects.

    python3 -m backend.tools.provision_aws --dry-run
    python3 -m backend.tools.provision_aws
    python3 -m backend.tools.provision_aws --verify
    python3 -m backend.tools.provision_aws --rebuild     (destructive, asks)

Idempotent: anything that already exists is left alone and reported. Run it
again after changing this file and it will say what differs — it will not
rebuild an index, because DynamoDB cannot add two GSIs in one update and
backfilling one on a live table takes as long as the table is large.

This is a convenience, not infrastructure-as-code. If you already run Terraform
or CDK, read the shapes out of here and put them there instead; the one thing
that matters is that the index names match `dynamo_repository`, because those
are what the queries name.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from ..common.config import settings
from ..common import store

# Only the attributes that key an index are declared. DynamoDB is schemaless
# for everything else, which is why adding a field to CallRow needs no migration.
# Three partition keys and ONE sort key. `ord` is the range key of all three
# indexes -- DynamoDB is happy for one attribute to serve several -- which is
# what replaced gsi1sk/gsi2sk/gsi3sk, three attributes that always held the
# identical string.
CALL_ATTRIBUTES = [
    {"AttributeName": "PK", "AttributeType": "S"},
    {"AttributeName": "SK", "AttributeType": "S"},
    {"AttributeName": "ord", "AttributeType": "S"},
] + [
    {"AttributeName": f"gsi{n}pk", "AttributeType": "S"}
    for n in range(1, 4)
]

# What the list view renders, and everything the dashboard sorts or aggregates
# on. Projecting exactly these makes a 500-call page one index read with no
# base-table fetch; projecting ALL would double the storage and the write cost
# for fields nobody lists on.
#
# `sectionMarks` is in here, and it was not always. The read side used to fill
# it in with a BatchGetItem per hundred calls, on the reasoning that ~140 bytes
# copied into every index item was the more expensive half. That was wrong
# twice: an absent attribute costs nothing, and only a PROCESSED call has marks
# (a quarter of rows), so the projection grows by a quarter of what it looked
# like -- while BatchGetItem is billed on the *whole* base item, 765 bytes of
# it, to read two attributes. Projecting it is fewer requests and fewer billed
# bytes, and it is what makes per-metric averages and metric-wise sorting a
# property of the rows the page already has.
# Shorter than it was, because most of what a list view renders is no longer
# an attribute. PK, SK and the index's own keys are projected automatically, so
# a GSI-3 item already carries callId (in PK), startedAt (in `ord`) and the day;
# `gsi2pk` is projected explicitly because it is the only thing that says whose
# call it is, and `models.hydrate` turns all of that back into the fields a
# caller reads. audioKey, auditKey, scoreBand and dateKey are derived there too,
# and agentDid, team and teamLeaderName are resolved from the cached roster.
LIST_PROJECTION = [
    "durationSec", "customerPhone", "agentName", "callDirection",
    "score", "flagCount", "auditStatus", "disqualified",
    "processingStatus", "sectionMarks",
    "gsi2pk",
]

# What a worker needs to pick a call up. Deliberately not LIST_PROJECTION: the
# work queue is read by the pipeline, not by a list view, and it only has to
# know which call to audit and where its audio is.
WORK_PROJECTION = ["processingStatus", "agentName", "durationSec", "gsi2pk"]


def _index(name, number, projection):
    return {
        "IndexName": name,
        "KeySchema": [
            {"AttributeName": f"gsi{number}pk", "KeyType": "HASH"},
            {"AttributeName": "ord", "KeyType": "RANGE"},
        ],
        "Projection": {
            "ProjectionType": "INCLUDE",
            "NonKeyAttributes": projection,
        },
    }


GLOBAL_SECONDARY_INDEXES = [
    _index(store.INDEX_WORK, 1, WORK_PROJECTION),
    _index(store.INDEX_AGENT, 2, LIST_PROJECTION),
    _index(store.INDEX_DAY, 3, LIST_PROJECTION),
]

# -- the roster ------------------------------------------------------------
# One partition per person or team, plus two lookups. Small enough that the API
# caches it whole, so these indexes exist for correctness at ingest rather than
# for read volume.
DIRECTORY_ATTRIBUTES = [
    {"AttributeName": "PK", "AttributeType": "S"},
    {"AttributeName": "SK", "AttributeType": "S"},
    {"AttributeName": "gsi1pk", "AttributeType": "S"},
    {"AttributeName": "gsi1sk", "AttributeType": "S"},
    {"AttributeName": "gsi2pk", "AttributeType": "S"},
    {"AttributeName": "gsi2sk", "AttributeType": "S"},
]

DIRECTORY_INDEXES = [
    {
        # Cloud Connect hands us an extension, never an account id. Without this
        # every ingested call would need a scan of the roster to find its agent.
        "IndexName": "ext-lookup-index",
        "KeySchema": [
            {"AttributeName": "gsi1pk", "KeyType": "HASH"},
            {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    },
    {
        "IndexName": "team-roster-index",
        "KeySchema": [
            {"AttributeName": "gsi2pk", "KeyType": "HASH"},
            {"AttributeName": "gsi2sk", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    },
]

KEY_SCHEMA = [
    {"AttributeName": "PK", "KeyType": "HASH"},
    {"AttributeName": "SK", "KeyType": "RANGE"},
]


def _table_spec(name, attributes, indexes=None):
    spec = {
        "TableName": name,
        "KeySchema": KEY_SCHEMA,
        "AttributeDefinitions": attributes,
        # On-demand. Call volume follows the working day and a provisioned table
        # sized for the peak is idle most of the night; sized for the average it
        # throttles the morning. Switch to provisioned with autoscaling once
        # there is a month of CloudWatch data to size it from.
        "BillingMode": "PAY_PER_REQUEST",
        "SSESpecification": {"Enabled": True},
        "Tags": [
            {"Key": "app", "Value": "call-auditing"},
            {"Key": "contains", "Value": "customer-pii"},
        ],
    }
    if indexes:
        spec["GlobalSecondaryIndexes"] = indexes
    return spec


def table_spec():
    # No stream. It was here for a Lambda that maintained pre-aggregated daily
    # counters as each call landed. There is no such table any more -- the read
    # side aggregates GSI-3 over the window it is showing -- so there is no
    # consumer, and an enabled stream with nothing reading it is a 24-hour
    # buffer that costs money and hides the fact that nothing is listening.
    return _table_spec(settings.calls_table, CALL_ATTRIBUTES,
                       GLOBAL_SECONDARY_INDEXES)


def directory_spec():
    return _table_spec(settings.directory_table, DIRECTORY_ATTRIBUTES,
                       DIRECTORY_INDEXES)


ALL_SPECS = (table_spec, directory_spec)


def create_table(client, spec, dry_run=False):
    """Create one table from its spec. Idempotent; reports drift if it exists."""
    name = spec["TableName"]
    wanted = spec.get("GlobalSecondaryIndexes", [])
    existing = _describe_table(client, name)
    if existing and existing.get("denied"):
        print(f"table {name}: not authorised — attach "
              f"backend/iam/policy.json")
        return False
    if existing:
        print(f"table {name} exists ({existing['TableStatus']})")
        _report_index_drift(existing, wanted)
        return False
    if dry_run:
        print(f"would create table {name} with {len(wanted)} indexes")
        return True
    client.create_table(**spec)
    print(f"creating table {name}, waiting...")
    client.get_waiter("table_exists").wait(TableName=name)

    # Point-in-time recovery is the difference between a bad deploy costing an
    # afternoon and costing the audit history. It is off by default and cannot
    # be set in create_table.
    client.update_continuous_backups(
        TableName=name,
        PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
    )
    print(f"created {name} with point-in-time recovery")
    return True


def create_tables(client, dry_run=False):
    for spec_fn in ALL_SPECS:
        create_table(client, spec_fn(), dry_run)


def _describe_table(client, name):
    """The live table, None if it is not there, or ("denied", code) if we are
    not allowed to look. Access denied is reported rather than raised: a policy
    scoped to some of the tables is a normal state to be in halfway through a
    rollout, and a stack trace is a poor way to say "attach the policy".
    """
    from botocore.exceptions import ClientError

    try:
        return client.describe_table(TableName=name)["Table"]
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            return None
        if code in ("AccessDeniedException", "UnrecognizedClientException"):
            return {"denied": code}
        raise


def _report_index_drift(described, wanted_indexes):
    live = {i["IndexName"] for i in described.get("GlobalSecondaryIndexes", [])}
    wanted = {i["IndexName"] for i in wanted_indexes}
    for name in sorted(wanted - live):
        print(f"  MISSING index {name} — queries using it will fail")
    for name in sorted(live - wanted):
        print(f"  extra index {name} — unused by this code, still billed")
    if live == wanted:
        print(f"  {len(live) or 'no'} indexes, all as specified")


def lifecycle_rules():
    """The bucket's lifecycle rules.

    A function rather than an inline literal so they can be re-applied to a
    bucket that already exists -- `create_bucket` does nothing at all when the
    bucket is there, which would otherwise mean a rule change never lands.
    """
    intermediates = [
        {
            # The intermediate stages. Regenerable only by re-paying for
            # transcription, so not free to lose, but rarely wanted after a
            # year -- and the audit drawn from them is kept forever.
            "ID": f"expire-{kind}",
            "Status": "Enabled",
            "Filter": {"Prefix": f"{kind}/"},
            "Expiration": {"Days": 365},
        }
        for kind in ("transcript", "clean", "analysis")
    ]
    return [
        {
            # Audio is read once, by the audit worker, within minutes. After
            # that it is kept for disputes and for re-auditing, neither of which
            # needs millisecond access. It is also ~97 GB a day, which is
            # essentially the whole bill.
            #
            # A plain prefix filter, because recordings now have a prefix of
            # their own. Under the old directory-per-call layout no prefix
            # separated audio from the reports beside it, so every object had to
            # be tagged on write and the rule matched the tag -- a lifecycle
            # filter cannot match a file extension. That tagging, and the
            # `s3:PutObjectTagging` it needed, are gone.
            "ID": "audio-cooldown",
            "Status": "Enabled",
            "Filter": {"Prefix": f"{store.AUDIO_PREFIX}/"},
            "Transitions": [
                {"Days": 30, "StorageClass": "STANDARD_IA"},
                {"Days": 90, "StorageClass": "GLACIER_IR"},
            ],
        },
    ] + intermediates + [
        {
            # `audit/` and `report/` have no expiry rule on purpose: they are
            # the product, they are small, and the dashboard reads them
            # indefinitely. Only superseded versions are cleaned up.
            "ID": "expire-old-versions",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
        },
        {
            "ID": "abort-incomplete-uploads",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        },
    ]


def put_lifecycle(client, bucket):
    client.put_bucket_lifecycle_configuration(
        Bucket=bucket, LifecycleConfiguration={"Rules": lifecycle_rules()},
    )


def create_bucket(client, dry_run=False):
    from botocore.exceptions import ClientError

    bucket = settings.audio_bucket
    try:
        client.head_bucket(Bucket=bucket)
        print(f"bucket {bucket} exists")
        return False
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise

    if dry_run:
        print(f"would create bucket {bucket} in {settings.aws_region}")
        return True

    kwargs = {"Bucket": bucket}
    # us-east-1 is the one region that rejects an explicit LocationConstraint.
    if settings.aws_region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {
            "LocationConstraint": settings.aws_region
        }
    client.create_bucket(**kwargs)

    # Against a local S3 stand-in -- MinIO, in docker-compose.sim.yml -- most of
    # what follows is unimplemented and answers 501. None of it is what the
    # rehearsal is testing, and a NotImplemented on `put_public_access_block` is
    # not a reason for a simulated first deploy to fail, so each step is
    # attempted and reported rather than fatal. On real S3 there is no endpoint
    # override and nothing here is skipped.
    simulated = bool(settings.s3_endpoint_url)

    def harden(what, call):
        try:
            call()
        except Exception as exc:  # noqa: BLE001 -- see above
            if not simulated:
                raise
            print(f"  skipped {what}: {type(exc).__name__} "
                  f"(local S3 stand-in does not implement it)")

    # Public access blocked first, before anything is in it. The order matters:
    # a bucket of call recordings that is briefly public is a bucket that was
    # public, and these are named customers on tape.
    harden("public access block", lambda: client.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        },
    ))
    encryption = (
        {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": settings.s3_kms_key_id}
        if settings.s3_kms_key_id else {"SSEAlgorithm": "AES256"}
    )
    harden("default encryption", lambda: client.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [{
                "ApplyServerSideEncryptionByDefault": encryption,
                "BucketKeyEnabled": True,
            }]
        },
    ))
    harden("versioning", lambda: client.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    ))
    # HTTPS only. Without this a presigned URL works over plain HTTP too.
    harden("HTTPS-only policy", lambda: client.put_bucket_policy(Bucket=bucket, Policy=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "DenyInsecureTransport",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        }],
    })))
    harden("lifecycle rules", lambda: put_lifecycle(client, bucket))
    print(f"created bucket {bucket}: private, encrypted, versioned, "
          f"HTTPS-only, with lifecycle rules")
    print("  NOTE: no retention policy for customer PII is set. Decide how long "
          "recordings may be kept and add an Expiration rule.")
    return True


def _scan_all(table_name):
    """Every item in the table, as plain Python. Paginated, no filter.

    Through the resource layer, not the client: the client returns items in
    DynamoDB's own `{"S": "..."}` shape, which would have to be deserialised to
    read a value and re-serialised to write it back. The resource layer hands
    over plain values with `Decimal` for numbers -- which is exactly what
    `batch_writer` wants on the way back in, so nothing is converted twice or
    quietly turned into a float.
    """
    table = store.resource("dynamodb", settings.dynamo_endpoint_url).Table(table_name)
    items, cursor = [], None
    while True:
        page = table.scan(**({"ExclusiveStartKey": cursor} if cursor else {}))
        items.extend(page.get("Items", []))
        cursor = page.get("LastEvaluatedKey")
        if not cursor:
            return items


# Attributes written by builds that no longer exist. Dropped on the way back in
# rather than left to sit on the row: a reader that still knows the old name
# would find a value and believe it, which is worse than finding nothing.
RETIRED_ATTRIBUTES = (
    "reportStatus",     # replaced by `disqualified`
    "reviewedAt",       # the QA sign-off, removed from scope
    "reviewedBy",
    "gsi4pk", "gsi4sk",  # the team index, replaced by a fan-out over agents
    "gsi5pk", "gsi5sk",  # the open-review index
    "claimedAt",         # the audit claim, replaced by taking a job off a queue
    "recoveryCount", "recoveredAt", "revivedAt",
)


def migrate_row(item):
    """One row, in the shape the current code writes.

    The only value that has to be *derived* rather than dropped is
    `disqualified`: a row audited by the old build says so with
    `reportStatus = FATAL`, and nothing else on the row does -- `score` is
    absent on a disqualified call and also on one that was skipped or failed.
    """
    row = {k: v for k, v in item.items() if k not in RETIRED_ATTRIBUTES}
    if "disqualified" not in row and str(item.get("PK", "")).startswith("CALL#"):
        row["disqualified"] = item.get("reportStatus") == "FATAL"
    return row


def rebuild(dynamo, backup_dir, assume_yes=False):
    """Read the calls table out, recreate it to spec, write the rows back.

    A rebuild rather than four `UpdateTable`s because the spec changed in ways
    an update cannot express: a projection is immutable, so `sectionMarks` and
    `disqualified` mean dropping and recreating two indexes, and two more
    indexes have to be deleted -- and DynamoDB allows one index change per
    operation. At a few hundred rows, delete-and-create lands the exact spec in
    one step instead of four sequential backfills.

    **The rows are written to disk before anything is deleted.** The audit
    documents are all in S3 and the call log can be re-fetched, so nothing here
    is irreplaceable in principle -- but 132 of these rows cost real money to
    produce, and "in principle" is not a backup. A crash between the delete and
    the write-back has to be recoverable from a file, not from an argument.
    """
    table_name = settings.calls_table

    print(f"reading {table_name}...")
    items = _scan_all(table_name)
    calls = [i for i in items if str(i.get("PK", "")).startswith("CALL#")]
    others = [i for i in items if not str(i.get("PK", "")).startswith("CALL#")]
    print(f"  {len(items)} items: {len(calls)} calls, {len(others)} control rows")

    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = os.path.join(backup_dir, f"{table_name}-{stamp}.json")
    with open(backup, "w") as handle:
        json.dump(items, handle, indent=2, default=str)
    print(f"  backed up to {backup}")

    migrated = [migrate_row(i) for i in items]
    changed = sum(1 for a, b in zip(items, migrated) if a != b)
    print(f"  {changed} row(s) change shape "
          f"({', '.join(RETIRED_ATTRIBUTES[:3])}, ...)")

    if not assume_yes:
        print(f"\nThis DELETES the {table_name} table and recreates it.")
        answer = input("Type the table name to continue: ").strip()
        if answer != table_name:
            print("aborted — nothing was touched")
            return 1

    print(f"deleting {table_name}...")
    dynamo.delete_table(TableName=table_name)
    dynamo.get_waiter("table_not_exists").wait(TableName=table_name)

    print(f"creating {table_name} to spec...")
    create_table(dynamo, table_spec())
    dynamo.get_waiter("table_exists").wait(TableName=table_name)
    # An index is not queryable the instant the table is ACTIVE.
    for _ in range(60):
        described = _describe_table(dynamo, table_name) or {}
        statuses = [g["IndexStatus"]
                    for g in described.get("GlobalSecondaryIndexes", [])]
        if statuses and all(s == "ACTIVE" for s in statuses):
            break
        time.sleep(2)
    print(f"  indexes: {', '.join(sorted(statuses))}")

    print(f"writing {len(migrated)} row(s) back...")
    table = store.resource("dynamodb", settings.dynamo_endpoint_url).Table(table_name)
    with table.batch_writer() as writer:
        for row in migrated:
            # Written as they came out. `Decimal` is what the resource layer
            # both returns and requires; converting to float here is what
            # boto3 refuses outright, and the one that slips through is the
            # score that happens to be 8.5.
            writer.put_item(Item=row)

    after = len(_scan_all(table_name))
    print(f"  {after} item(s) in the table")
    if after != len(migrated):
        print(f"FAIL wrote {len(migrated)} and read back {after}. The backup "
              f"at {backup} is the source of truth.")
        return 1
    print(f"\nrebuilt. The backup at {backup} can go once the dashboard "
          f"looks right.")
    return 0


def verify(dynamo, s3):
    """Read-only. What a deploy gate should run before sending traffic."""
    ok = True
    for spec_fn in ALL_SPECS:
        spec = spec_fn()
        name = spec["TableName"]
        table = _describe_table(dynamo, name)
        if table is None:
            print(f"FAIL table {name} does not exist")
            ok = False
            continue
        if table.get("denied"):
            print(f"FAIL table {name}: not authorised to describe it — "
                  f"attach backend/iam/policy.json")
            ok = False
            continue
        print(f"ok   table {name} ({table['TableStatus']}, "
              f"{table.get('ItemCount', 0)} items)")
        live = {i["IndexName"]: i["IndexStatus"]
                for i in table.get("GlobalSecondaryIndexes", [])}
        wanted = set()
        for index in spec.get("GlobalSecondaryIndexes", []):
            wanted.add(index["IndexName"])
            status = live.get(index["IndexName"])
            if status != "ACTIVE":
                print(f"FAIL   index {index['IndexName']} is "
                      f"{status or 'MISSING'}")
                ok = False
            else:
                print(f"ok     index {index['IndexName']}")
        # An index on the table that this file no longer describes. Not fatal --
        # it costs storage and a write per mutation, it does not break a read --
        # but it is invisible otherwise, and an index nothing queries is one
        # nobody remembers to delete. Reported as a WARN so a deploy gate does
        # not fail on a cleanup somebody has not got to yet.
        for orphan in sorted(set(live) - wanted):
            print(f"WARN   index {orphan} exists on {name} but is not in the "
                  f"spec. Nothing queries it; it still costs a write on every "
                  f"mutation. Delete it with:\n"
                  f"         aws dynamodb update-table --table-name {name} "
                  f"--global-secondary-index-updates "
                  f"'[{{\"Delete\":{{\"IndexName\":\"{orphan}\"}}}}]'")

    from botocore.exceptions import ClientError
    try:
        s3.head_bucket(Bucket=settings.audio_bucket)
        print(f"ok   bucket {settings.audio_bucket}")
    except ClientError as exc:
        print(f"FAIL bucket {settings.audio_bucket}: "
              f"{exc.response['Error']['Code']}")
        ok = False

    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be created, touch nothing")
    parser.add_argument("--verify", action="store_true",
                        help="check what exists and exit non-zero if it is wrong")
    parser.add_argument("--rebuild", action="store_true",
                        help="DESTRUCTIVE: back the calls table up to disk, "
                             "delete it, recreate it to the current spec, and "
                             "write the rows back in the current shape. Asks "
                             "first unless --yes is given.")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt on --rebuild")
    parser.add_argument("--backup-dir", default="backups",
                        help="where --rebuild writes its backup (default: ./backups)")
    args = parser.parse_args(argv)


    dynamo = store.client("dynamodb", settings.dynamo_endpoint_url)
    s3 = store.client("s3", settings.s3_endpoint_url)

    print(f"region {settings.aws_region}  bucket {settings.audio_bucket}")
    print(f"tables {settings.calls_table}, {settings.directory_table}")
    if settings.dynamo_endpoint_url or settings.s3_endpoint_url:
        print(f"endpoints dynamo={settings.dynamo_endpoint_url} "
              f"s3={settings.s3_endpoint_url}")

    if args.verify:
        return 0 if verify(dynamo, s3) else 1

    if args.rebuild:
        return rebuild(dynamo, args.backup_dir, assume_yes=args.yes)

    create_tables(dynamo, args.dry_run)
    create_bucket(s3, args.dry_run)
    if not args.dry_run:
        print("\nReady. The batch job reads and writes these directly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
