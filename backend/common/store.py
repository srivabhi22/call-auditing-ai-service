"""Persistence. DynamoDB, S3, and the keys that address them.

One module, because it is one concern: everything in this backend that talks to
AWS is here, and nothing above it builds a client, converts a number or spells a
key. Four things live in it, in the order they depend on each other:

    clients         one cached boto3 client per service, one retry policy
    conversions     DynamoDB has no float; this is the only place that crosses
    keys            where a call's audio and artifacts live in S3
    S3ObjectStore   put/get/presign against the media bucket
    DynamoCallsRepository   the `calls` table and its three indexes
    publish_roster          agents.json -> S3, for the dashboard
    RollupsRepository       the `rollups` table -- a day's totals per scope
    SessionsRepository      the `sessions` table -- did the night finish?

It used to be a package of eight modules, half of which were a second
implementation backed by JSON files on disk. That backend existed so the AI
pipeline could run without credentials, and the batch job refused to start on it
anyway -- the work list is a sparse index and there is no honest file-based
equivalent that is not a scan of every call ever processed. Two implementations
of an interface with one real caller is a layer that costs more to keep true
than it ever paid back, so there is one now, and `STORAGE_BACKEND` is gone with
it.

Clients are built lazily and cached. Building them at import would make every
test need credentials on the machine, and would fail at import time -- the worst
moment for it, because nothing is running yet to report the failure.
"""

import decimal
import json
import logging
import os
import re
import threading
import unicodedata
from datetime import datetime, timezone

from .config import settings
from .models import ProcessingStatus, hydrate
from .trace import boot, db, s3

log = logging.getLogger(__name__)



# ------------------------------------------------------------------------
# clients and type conversions
# ------------------------------------------------------------------------

_lock = threading.Lock()
_clients = {}

# DynamoDB has no float. boto3's resource layer serialises Decimal and refuses
# float outright, so every score, ratio and probability has to cross this line
# in both directions. Doing it anywhere other than here means finding out in
# production, on the first call that happens to score 8.5.
_DECIMAL_CONTEXT = decimal.Context(
    prec=38,
    Emin=-128,
    Emax=126,
    rounding=decimal.ROUND_HALF_EVEN,
    traps=[decimal.Inexact, decimal.Rounded],
)


def _boto_config():
    from botocore.config import Config

    return Config(
        region_name=settings.aws_region,
        retries={
            # Adaptive adds client-side rate limiting on top of the retries,
            # which is what keeps a burst of audits from turning a throttle
            # into a thundering retry storm against the same partition.
            "max_attempts": settings.aws_max_attempts,
            "mode": "adaptive",
        },
        connect_timeout=settings.aws_connect_timeout_sec,
        read_timeout=settings.aws_read_timeout_sec,
        # One pool sized for the audit workers plus the API threads. The default
        # of 10 silently serialises everything past the tenth concurrent call.
        max_pool_connections=settings.aws_max_pool_connections,
    )


def client(service, endpoint_url=None):
    """A cached boto3 client.

    Credentials come from the default chain — the task role on ECS, the
    execution role on Lambda, the environment or ~/.aws locally. Nothing here
    reads a key or a secret, and nothing should: a credential in configuration
    is a credential in a log line eventually.
    """
    key = (service, endpoint_url)
    with _lock:
        if key not in _clients:
            import boto3

            _clients[key] = boto3.client(
                service, endpoint_url=endpoint_url, config=_boto_config()
            )
            log.info("built boto3 %s client region=%s endpoint=%s",
                     service, settings.aws_region, endpoint_url or "default")
        return _clients[key]


def resource(service, endpoint_url=None):
    """The resource layer, used only for DynamoDB.

    Table.put_item and friends take plain Python types and handle the attribute
    typing, which is the difference between a readable repository and one that
    is half serialisation code.
    """
    key = ("resource:" + service, endpoint_url)
    with _lock:
        if key not in _clients:
            import boto3

            _clients[key] = boto3.resource(
                service, endpoint_url=endpoint_url, config=_boto_config()
            )
        return _clients[key]


def reset_clients():
    """Drop the cache. For tests that change region or endpoint."""
    with _lock:
        _clients.clear()


# -- type conversion ------------------------------------------------------

def to_dynamo(value):
    """Python out, DynamoDB-safe in.

    Three rules, each learned from a specific failure:

      float -> Decimal    boto3 raises TypeError on float, always
      None  -> dropped    a null attribute is not the same as an absent one to
                          a sparse index, and absent is what the row means
      ""    -> kept       empty strings are legal since 2020 for non-key
                          attributes; the key attributes are filtered separately
                          by the repository, which knows which ones they are
    """
    if isinstance(value, float):
        # str() first: Decimal(0.1) is 0.1000000000000000055511151231257827,
        # which DynamoDB then rejects for exceeding 38 digits of precision.
        return _DECIMAL_CONTEXT.create_decimal(str(value))
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(v) for v in value]
    return value


def from_dynamo(value):
    """DynamoDB out, plain Python in.

    Decimal back to int where it is integral and float otherwise, so a row read
    back is `json.dumps`-able and compares equal to the one that was written.
    Leaving Decimal in place means FastAPI cannot serialise the response.
    """
    if isinstance(value, decimal.Decimal):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value


def clean_item(item, index_keys=()):
    """An item ready for put_item.

    Drops None, converts floats, and drops the empty-string values of attributes
    that are index keys. DynamoDB will not accept an empty string in a key, and
    the right meaning for "this row has no agent extension" is that it does not
    appear in that index at all — which is exactly what omitting it does.
    """
    cleaned = {}
    for name, value in item.items():
        if value is None:
            continue
        if name in index_keys and value == "":
            continue
        cleaned[name] = to_dynamo(value)
    return cleaned


# ------------------------------------------------------------------------
# where a call's files live in S3
# ------------------------------------------------------------------------

# The stages, in the order the pipeline writes them. Each is also its own
# top-level S3 prefix.
ARTIFACTS = ("transcript", "clean", "analysis", "audit")

# The recording's prefix. Kept separate from ARTIFACTS because it is the one
# lifecycle rules act on, and because its extension varies.
AUDIO_PREFIX = "audio"

# The rendered report. Written by the report tooling rather than the pipeline,
# so it is not in ARTIFACTS, but it is filed the same way.
REPORT_PREFIX = "report"

# Every prefix this module writes under, which is what a wipe and an IAM policy
# both need to enumerate.
PREFIXES = (AUDIO_PREFIX,) + ARTIFACTS + (REPORT_PREFIX,)

# What counts as the recording. Soniox takes more than this, but these are the
# containers the dialer actually produces.
AUDIO_EXTENSIONS = (
    ".wav", ".mp3", ".m4a", ".mp4", ".ogg", ".flac", ".aac", ".amr", ".webm",
)


def slug(value, fallback="unknown"):
    """A name safe to put in a key: lowercase, ASCII, hyphen-separated.

    No longer used for S3 keys -- see the module docstring on why names are not
    in keys any more -- but still the right normaliser for anywhere a
    human-typed name has to become an identifier.
    """
    if not value:
        return fallback
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or fallback


def parse_started_at(started_at_iso):
    """The call's start, as a datetime. Falls back to now, in UTC.

    Accepts what the row actually carries: ISO-8601 with an offset, with a `Z`,
    or naive. A row whose timestamp cannot be read still gets a key rather than
    an exception -- losing the recording over a malformed clock would be worse
    than filing it under today.
    """
    if isinstance(started_at_iso, datetime):
        return started_at_iso
    try:
        text = str(started_at_iso).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except (ValueError, TypeError, AttributeError):
        return datetime.now(timezone.utc)


def date_path(started_at_iso):
    """`2026/09/16` -- the partition every key carries.

    Built from the call's own start, never today's date: a retry or a re-audit
    crossing midnight would otherwise write where the stored key does not point.
    """
    return f"{parse_started_at(started_at_iso):%Y/%m/%d}"


def date_key(started_at_iso):
    """`2026-09-16` -- the same day, in the form the DynamoDB row stores."""
    return f"{parse_started_at(started_at_iso):%Y-%m-%d}"


def audio_key(call_id, started_at_iso, extension=".wav"):
    """The recording."""
    if not extension.startswith("."):
        extension = f".{extension}"
    return f"{AUDIO_PREFIX}/{date_path(started_at_iso)}/{call_id}{extension}"


def artifact_key(call_id, started_at_iso, kind):
    """One of the pipeline's JSON outputs. `kind` is one of ARTIFACTS."""
    if kind not in ARTIFACTS:
        raise ValueError(f"unknown artifact {kind!r}; expected one of {ARTIFACTS}")
    return f"{kind}/{date_path(started_at_iso)}/{call_id}.json"


def report_key(call_id, started_at_iso):
    """The rendered PDF report."""
    return f"{REPORT_PREFIX}/{date_path(started_at_iso)}/{call_id}.pdf"


def all_keys(call_id, started_at_iso, audio_extension=".wav"):
    """Every key this call could own, for deleting one call without a listing."""
    return (
        [audio_key(call_id, started_at_iso, audio_extension)]
        + [artifact_key(call_id, started_at_iso, k) for k in ARTIFACTS]
        + [report_key(call_id, started_at_iso)]
    )


def is_recording(key):
    """Whether writing this key should start phase 2.

    A plain prefix test again, now that the recordings share one. The extension
    is checked too so a stray non-audio object under `audio/` cannot queue an
    audit of something Soniox will reject.
    """
    head, _, _ = str(key or "").partition("/")
    return (
        head == AUDIO_PREFIX
        and os.path.splitext(key)[1].lower() in AUDIO_EXTENSIONS
    )


def call_id_from_key(key):
    """`audit/2026/09/16/audio_4.json` -> `audio_4`.

    The file name is the call id and nothing else, which is the point of putting
    the artifact type in the prefix rather than in the suffix.
    """
    return os.path.splitext(os.path.basename(key))[0]


# ------------------------------------------------------------------------
# the media bucket
# ------------------------------------------------------------------------

class S3ObjectStore:
    def __init__(self, bucket=None, endpoint_url=None):
        self.bucket = bucket or settings.audio_bucket
        self.endpoint_url = endpoint_url or settings.s3_endpoint_url

    @property
    def client(self):
        return client("s3", self.endpoint_url)

    def _encryption_args(self):
        if settings.s3_kms_key_id:
            return {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": settings.s3_kms_key_id,
            }
        return {"ServerSideEncryption": "AES256"}

    # -- writes -----------------------------------------------------------

    def put_file(self, key, source_path, metadata=None):
        """s3.upload_file — a managed multipart upload for anything large.

        The source is deleted afterwards, matching the local store's move
        semantics. The caller streamed a recording to a temporary file and does
        not want it twice.
        """
        size = os.path.getsize(source_path)
        extra = {"Metadata": _string_metadata(metadata), **self._encryption_args()}
        self.client.upload_file(source_path, self.bucket, key, ExtraArgs=extra)
        os.unlink(source_path)
        s3(f"put s3://{self.bucket}/{key} ({size / 1024:.0f} KB)")
        return size

    def put_json(self, key, document, metadata=None):
        """s3.put_object with the report as the body."""
        body = json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8")
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json; charset=utf-8",
            Metadata=_string_metadata(metadata),
            **self._encryption_args(),
        )
        s3(f"put s3://{self.bucket}/{key} ({len(body) / 1024:.0f} KB)")
        return len(body)

    # -- reads ------------------------------------------------------------

    def download_to(self, key, dest_path):
        """s3.download_file into the work directory. False if it is not there.

        The audit worker stages audio through this rather than reading in place:
        the AI pipeline's stages shell out and want a real path.
        """
        from botocore.exceptions import ClientError

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        try:
            self.client.download_file(self.bucket, key, dest_path)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                s3(f"missing s3://{self.bucket}/{key}")
                return False
            raise
        s3(f"staged s3://{self.bucket}/{key} -> {dest_path}")
        return True

    def read_json(self, key):
        from botocore.exceptions import ClientError

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return None
            raise
        return json.loads(response["Body"].read().decode("utf-8"))

    def exists(self, key):
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise
        return True

    def get_metadata(self, key):
        """head_object's Metadata. S3 lowercases every key on the way back,
        which is why the writers use lowercase names to begin with."""
        from botocore.exceptions import ClientError

        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return {}
            raise
        return response.get("Metadata", {})

    def presigned_url(self, key, expires_sec=None):
        """A time-limited GET, for playing a recording in the dashboard.

        The bucket blocks public access, so this is the only way audio reaches
        a browser. Short-lived on purpose: the URL is a bearer credential for
        one object and it will end up in a browser history.
        """
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_sec or settings.s3_presign_expiry_sec,
        )

    def local_path(self, key):
        """Not meaningful for S3. Present so a caller that reaches for it gets
        a clear failure rather than a path that silently does not exist."""
        raise NotImplementedError(
            "S3 objects have no local path — use download_to(key, dest)"
        )


def _string_metadata(metadata):
    """S3 metadata values must be strings, and the keys become lowercase."""
    if not metadata:
        return {}
    return {str(k).lower(): str(v) for k, v in metadata.items() if v is not None}


# ------------------------------------------------------------------------
# the calls table
# ------------------------------------------------------------------------

INDEX_WORK = "work-queue-index"
INDEX_AGENT = "agent-time-index"
INDEX_DAY = "day-time-index"

# (index, partition attribute, sort attribute) for each.
# One shared sort key. `ord` is `<startedAt>#<callId>` and is the range key of
# all three indexes: DynamoDB allows that, and the three separate copies this
# replaced held byte-for-byte the same string.
INDEX_KEYS = {
    INDEX_WORK: ("gsi1pk", "ord"),
    INDEX_AGENT: ("gsi2pk", "ord"),
    INDEX_DAY: ("gsi3pk", "ord"),
}

# Every attribute that keys a GSI, which is what `clean_item` and `_update`
# need to treat differently from ordinary data: DynamoDB rejects an empty
# string in an index key outright, and the right meaning for "this row has no
# agent extension" is that it is absent from that index rather than in it under
# a blank key. Dropping on write and REMOVEing on update is also what makes
# GSI-1 sparse, so a call that reaches PROCESSED leaves the work queue.
INDEX_KEY_ATTRIBUTES = frozenset(
    attribute for pair in INDEX_KEYS.values() for attribute in pair
)

ALL_STATUSES = (
    ProcessingStatus.INGESTING, ProcessingStatus.UNPROCESSED,
    ProcessingStatus.AUDITING, ProcessingStatus.PROCESSED,
    ProcessingStatus.FAILED,
    ProcessingStatus.DISCARDED,
)


def _months(start, end):
    """The `yyyy-mm` buckets a window touches, oldest first.

    GSI-2 partitions by month, so a query spanning a month boundary is two
    queries. Enumerating them here keeps that fact out of every caller.
    """
    first, last = (start or "")[:7], (end or "")[:7]
    if not first or not last:
        return []
    out, year, month = [], int(first[:4]), int(first[5:7])
    while f"{year:04d}-{month:02d}" <= last:
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
        if len(out) > 120:       # a decade; a window wider than that is a bug
            break
    return out


def _days(start, end):
    """The `yyyy-mm-dd` buckets a window touches, newest first.

    Newest first because every list view reads that way and stops as soon as it
    has a page, so the oldest days are usually never queried at all.
    """
    from datetime import date, timedelta

    try:
        first = date.fromisoformat((start or "")[:10])
        last = date.fromisoformat((end or "")[:10])
    except ValueError:
        return []
    out, cursor = [], last
    while cursor >= first and len(out) <= 400:
        out.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return out


class DynamoCallsRepository:
    def __init__(self, table_name=None, endpoint_url=None):
        self.table_name = table_name or settings.calls_table
        self.endpoint_url = endpoint_url or settings.dynamo_endpoint_url
        self._table = None

    @property
    def table(self):
        if self._table is None:
            self._table = resource(
                "dynamodb", self.endpoint_url
            ).Table(self.table_name)
        return self._table

    @staticmethod
    def _key(call_id):
        """The whole primary key, from the id alone.

        This is what the constant sort key buys. Under `SK = <startedAt>#<id>`
        every update first had to read the row back to discover its own sort
        key — a second round trip on the hot path of every status change, to
        recover something the caller was never given. Now there is nothing to
        recover.
        """
        return {"PK": f"CALL#{call_id}", "SK": "META"}

    # -- writes -----------------------------------------------------------

    def put_if_absent(self, row):
        """Write the row, unless it is already there.

            ConditionExpression="attribute_not_exists(PK)"

        The condition is what makes a duplicate delivery harmless: two workers
        racing on one call cannot both write, and the loser stops rather than
        overwriting a row that may already have an audit on it. It is also what
        replaced the uniqueToken index — see the module docstring.
        """
        from botocore.exceptions import ClientError

        item = clean_item(row.to_item(), INDEX_KEY_ATTRIBUTES)
        try:
            self.table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                db(f"put {row.pk} REJECTED — already exists")
                return False
            raise
        db(f"put {row.pk} status={row.processingStatus} "
           f"agent={row.agentExtension} ({row.agentName or '-'}) "
           f"{row.callDirection or '-'} {row.durationSec}s")
        return True

    @staticmethod
    def _index_maintenance(changes):
        """The index keys a set of changes implies.

        The sparse indexes are only sparse if something keeps their key
        attributes in step with the column they shadow. Doing it here, once,
        rather than at each call site is what stops a status change somewhere
        leaving a finished call sitting in the work queue forever.

        `None` means REMOVE, which is how a row leaves a sparse index.
        """
        from .models import ProcessingStatus, WORK_STATUSES

        extra = {}
        if "processingStatus" in changes:
            status = changes["processingStatus"]
            extra["gsi1pk"] = status if status in WORK_STATUSES else None
        return extra

    def _update(self, call_id, changes, condition=None, condition_values=None):
        """One update_item, built from a dict of changes.

        Every attribute goes through a #name placeholder. Guessing which of
        DynamoDB's several hundred reserved words a field collides with is not
        a thing anyone should have to do twice, and `status` and `name` are both
        on that list.
        """
        from botocore.exceptions import ClientError

        sets, removes = [], []
        names, values = {}, {}
        for index, (field, value) in enumerate(changes.items()):
            placeholder = f"#f{index}"
            names[placeholder] = field
            if value is None or (value == "" and field in INDEX_KEY_ATTRIBUTES):
                # An explicit None means "clear it", which is REMOVE. Setting
                # NULL instead leaves the attribute present, and a sparse index
                # keyed on it would keep the row. An empty string in an index
                # key is the same mistake by another route — DynamoDB rejects
                # it outright.
                removes.append(placeholder)
            else:
                sets.append(f"{placeholder} = :v{index}")
                values[f":v{index}"] = to_dynamo(value)

        expression = " ".join(
            part for part in (
                f"SET {', '.join(sets)}" if sets else "",
                f"REMOVE {', '.join(removes)}" if removes else "",
            ) if part
        )
        if not expression:
            return True

        kwargs = {
            "Key": self._key(call_id),
            "UpdateExpression": expression,
            "ExpressionAttributeNames": names,
            # The row must exist. Without this, update_item on a missing key
            # creates a stub item with only the changed attributes — a row with
            # a score and no call.
            "ConditionExpression": "attribute_exists(PK)",
        }
        if condition:
            kwargs["ConditionExpression"] += f" AND {condition}"
            values.update(condition_values or {})
        if values:
            kwargs["ExpressionAttributeValues"] = values

        try:
            self.table.update_item(**kwargs)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def update(self, call_id, **changes):
        changes = {**changes, **self._index_maintenance(changes)}
        ok = self._update(call_id, changes)
        shown = " ".join(f"{k}={v}" for k, v in changes.items()
                         if v not in (None, "") and not k.startswith("gsi"))
        db(f"update CALL#{call_id} {shown}" + ("" if ok else " FAILED"))
        return ok

    def set_status(self, call_id, status, **changes):
        return self.update(call_id, processingStatus=status, **changes)

    def claim(self, call_id, expected_status, new_status):
        """Compare-and-set on processingStatus. True if this caller won.

            ConditionExpression="processingStatus = :expected"

        The condition is the whole point. Reading the status and then writing it
        as two operations lets two workers both see UNPROCESSED and both start
        auditing; the queue is at-least-once, so that race is reached by an
        ordinary redelivery rather than by a bug. An audit that runs twice costs
        real money twice.
        """
        changes = {"processingStatus": new_status}
        changes.update(self._index_maintenance(changes))
        won = self._update(
            call_id, changes,
            condition="processingStatus = :expected",
            condition_values={":expected": expected_status},
        )
        db(f"claim CALL#{call_id} {expected_status} -> {new_status}"
           if won else f"claim CALL#{call_id} LOST — not {expected_status}")
        return won

    def discard(self, call_id):
        """Reduce a disqualified call to a tombstone. Not used by default.

        Kept because the alternative — deleting the row — makes the ingestion
        sweep rediscover the call on every run and pay for the audit again,
        every time. Whether disqualified calls are discarded at all is a product
        decision; this is what it costs to do it safely if they are.
        """
        return self.update(
            call_id,
            processingStatus=ProcessingStatus.DISCARDED,
            score=None, flagCount=None, criterionMarks=None,
            auditStatus=None, disqualified=None,
            agentName=None, customerPhone=None,
            gsi2pk=None, gsi3pk=None,
        )

    # -- reads ------------------------------------------------------------

    def get(self, call_id):
        response = self.table.get_item(Key=self._key(call_id))
        item = response.get("Item")
        # `hydrate` puts back what the row does not store: callId, startedAt,
        # agentExtension, audioKey, auditKey, scoreBand and dateKey are all
        # recomputed from the keys, so no caller has to know the difference.
        return hydrate(from_dynamo(item)) if item else None

    def exists(self, call_id):
        """Whether we already know this call. One projected GetItem."""
        response = self.table.get_item(
            Key=self._key(call_id), ProjectionExpression="PK"
        )
        return bool(response.get("Item"))

    def _query_index(self, index, partition, start=None, end=None,
                     limit=None, descending=True, count_only=False):
        """One index partition, as a time range on its sort key."""
        pk_attr, sk_attr = INDEX_KEYS[index]
        condition = f"{pk_attr} = :k"
        values = {":k": partition}
        # The sort key is `<startedAt>#<callId>`, so the bounds are a prefix
        # comparison: "\uf8ff" sorts after any id that can follow the timestamp.
        if start and end:
            condition += f" AND {sk_attr} BETWEEN :start AND :end"
            values[":start"], values[":end"] = start, end + "#\uf8ff"
        elif start:
            condition += f" AND {sk_attr} >= :start"
            values[":start"] = start
        elif end:
            condition += f" AND {sk_attr} <= :end"
            values[":end"] = end + "#\uf8ff"

        kwargs = {
            "IndexName": index,
            "KeyConditionExpression": condition,
            "ExpressionAttributeValues": values,
            "ScanIndexForward": not descending,
        }
        if count_only:
            kwargs["Select"] = "COUNT"

        rows, total = [], 0
        while True:
            response = self.table.query(**kwargs)
            total += response.get("Count", 0)
            if not count_only:
                rows.extend(
                    hydrate(from_dynamo(i)) for i in response.get("Items", [])
                )
            cursor = response.get("LastEvaluatedKey")
            if not cursor or (limit and len(rows) >= limit):
                break
            kwargs["ExclusiveStartKey"] = cursor
        return total if count_only else rows

    def query(self, agent_extension=None, status=None,
              start=None, end=None, limit=None):
        """The dashboard read. One or more index queries, never a scan.

        Which index depends on what was asked for:

            agent given     GSI-2, one partition per month in the window
            status given    GSI-1, one partition — outstanding work only
            neither         GSI-3, one partition per day

        There is no team index. A team is its agents, and the roster that says
        which is already cached whole by every caller that asks — so a team
        window is a fan-out over five AGENT partitions rather than a fourth
        copy of the table. The team index it replaced was also only ever 23%
        populated, because an extension the roster does not list has no team,
        which made any sum over teams disagree with the org total.

        The bucketed indexes fan out over the window. That sounds worse than a
        single partition and is not: the list is time-descending and limited, so
        the walk stops at the first day that fills the page, and the buckets are
        what keep any one partition from becoming the whole table.
        """
        if agent_extension:
            rows = self._fan_out(
                INDEX_AGENT,
                [f"AGENT#{agent_extension}#{m}"
                 for m in reversed(_months(start, end) or [""])] or None,
                start, end, limit,
            )
        elif status:
            rows = self._query_index(INDEX_WORK, status, start, end, limit)
        else:
            rows = self._fan_out(
                INDEX_DAY,
                [f"DAY#{d}" for d in _days(start, end)],
                start, end, limit,
            )

        if status and agent_extension:
            rows = [r for r in rows if r.get("processingStatus") == status]
        rows.sort(key=lambda r: r.get("startedAt") or "", reverse=True)
        db(f"query -> {len(rows)} rows")
        return rows[:limit] if limit else rows

    def _fan_out(self, index, partitions, start, end, limit):
        """Walk bucketed partitions newest-first, stopping once the page is full.

        Without a window there is nothing to enumerate, so this falls back to a
        scan of the index — which is what an unbounded "everything, ever" read
        is, and is why every dashboard call passes a window.
        """
        if not partitions:
            return self._scan_index(index, limit)
        rows = []
        for partition in partitions:
            rows.extend(
                self._query_index(index, partition, start, end,
                                  None if limit is None else limit - len(rows))
            )
            if limit and len(rows) >= limit:
                break
        return rows

    def _scan_index(self, index, limit=None):
        kwargs = {"IndexName": index}
        rows = []
        while True:
            response = self.table.scan(**kwargs)
            rows.extend(hydrate(from_dynamo(i))
                        for i in response.get("Items", []))
            cursor = response.get("LastEvaluatedKey")
            if not cursor or (limit and len(rows) >= limit):
                break
            kwargs["ExclusiveStartKey"] = cursor
        return rows

    def count_by_status(self):
        """One COUNT query per outstanding status against GSI-1.

        Only the work statuses are countable this way, because only they are in
        the index. PROCESSED is deliberately not: counting finished calls by
        scanning an index of every call ever finished is the read this schema
        exists to avoid. The read side gets that number by aggregating GSI-3
        over the window it is actually showing.
        """
        from .models import WORK_STATUSES

        counts = {}
        for status in ALL_STATUSES:
            if status not in WORK_STATUSES:
                continue
            total = self._query_index(INDEX_WORK, status, count_only=True)
            if total:
                counts[status] = total
        return counts


# ------------------------------------------------------------------------
# the roster -- agents.json, published to S3
# ------------------------------------------------------------------------

# There was a `directory` DynamoDB table here, one row per agent and team,
# written by a deploy and scanned whole by both this service and the dashboard.
# It is gone. The roster it held was a transcription of `agents.json`, which is
# the file a human actually edits -- so the table was a second copy of the
# truth, kept in step by remembering to run a seeding command, and the two
# silently disagreeing was a class of bug nobody could see.
#
# Now the file *is* the roster. This service reads it from disk (see
# `common/agents.py`, which reloads it when the mtime changes, so a correction
# needs no restart), and publishes it to S3 for the dashboard -- which lives in
# another repo, on another deployment, and would otherwise need its own copy.
#
# One object, rewritten at the start of every run. It is ~2 KB, the dashboard
# caches it for ten minutes, and the alternative -- a roster the dashboard
# groups by that is not the roster this service filters on -- puts calls and
# their agents on different sides of a disagreement nobody notices until a
# month of reports is wrong.
ROSTER_KEY = "roster/agents.json"


def publish_roster(agents, store=None, key=ROSTER_KEY):
    """Write the roster to S3, as the shape the dashboard reads.

    Not the raw file: the on-disk JSON carries `_comment` keys and the roster's
    own spelling of a DID, and pushing the file verbatim would make every
    reader re-implement `common.agents`'s normalisation. What goes up is the
    resolved `Agent` list -- DIDs normalised to ten digits, teams and leaders
    named -- so a reader needs no knowledge of the file format at all.

    `audited` is carried rather than filtered on. Who is reported on is the
    reader's decision: this service still ingests and names a call from a live
    extension that has no team leader, and the dashboard still has to render it
    somewhere rather than have it vanish between the two.
    """
    store = store or objects
    document = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agents": [
            {
                "extension": agent.extension,
                "name": agent.name,
                "did": agent.did_key,
                "accountId": agent.accountId,
                "team": agent.state or "",
                "teamLeaderName": agent.tl or "",
                "audited": agent.audited,
            }
            for agent in sorted(agents, key=lambda a: a.extension)
        ],
    }
    store.put_json(key, document)
    db(f"roster published: {len(document['agents'])} agent(s) -> {key}")
    return document


def read_roster(store=None, key=ROSTER_KEY):
    """The published roster, or None when it has never been written."""
    return (store or objects).read_json(key)


# ------------------------------------------------------------------------
# the rollups table -- a day's totals, at three scopes
# ------------------------------------------------------------------------

# One row per scope per day:
#
#     PK                        SK
#     OVERALL                   2026-09-20      the whole floor
#     TEAM#MP                   2026-09-20      one team
#     AGENT#MP#pankaj-kourav    2026-09-20      one agent, under their team
#
# The scope is in the partition key and the day is the sort key, so "this team,
# last 30 days" is one query of 30 small rows rather than 30 day-partitions of
# GSI-3 and a few thousand call rows aggregated in the API process. Every
# number on the row is a *sum* or an extreme, never an average: averages do not
# add up, so the row carries `scoreSum` and `scoredCalls` and the caller
# divides once, at the end of the span it is actually showing.
#
# The agent key is `AGENT#<team>#<name>` rather than the extension the calls
# table keys by, because these rows are read by a dashboard and written by one
# batch job -- readability wins where nothing joins on it. The extension is on
# the row as an attribute for anything that has to get back to the call rows.
SCOPE_OVERALL = "OVERALL"
SCOPE_TEAM = "TEAM"
SCOPE_AGENT = "AGENT"

# Where an agent the roster gives no team to is filed. Such an agent still has
# calls, and dropping them would make the teams stop summing to the floor --
# the exact disagreement that retired the old team index (see `query`).
UNASSIGNED_TEAM = "UNASSIGNED"

# GSI-1 on this table: every row of one scope, by day. What "all teams on
# 2026-09-20", or a leaderboard over a span, reads -- the base table can only
# answer for one team or one agent at a time. The partition is a scope name, so
# there are exactly three of them; at one row per agent per day that is tens of
# thousands of small rows a year, which is a partition DynamoDB does not notice.
INDEX_SCOPE = "scope-date-index"


def rollup_pk(scope, team="", agent_key=""):
    """The partition key for a scope. The one place the key shape is spelled."""
    if scope == SCOPE_OVERALL:
        return SCOPE_OVERALL
    team = team or UNASSIGNED_TEAM
    if scope == SCOPE_TEAM:
        return f"{SCOPE_TEAM}#{team}"
    if scope == SCOPE_AGENT:
        if not agent_key:
            raise ValueError("an agent rollup needs an agent key")
        return f"{SCOPE_AGENT}#{team}#{agent_key}"
    raise ValueError(f"unknown rollup scope {scope!r}")


def rollup_scope(pk):
    """(scope, team, agent_key) back out of a partition key."""
    parts = str(pk or "").split("#")
    if parts[0] == SCOPE_OVERALL:
        return SCOPE_OVERALL, "", ""
    if parts[0] == SCOPE_TEAM and len(parts) == 2:
        return SCOPE_TEAM, parts[1], ""
    if parts[0] == SCOPE_AGENT and len(parts) == 3:
        return SCOPE_AGENT, parts[1], parts[2]
    return "", "", ""


class RollupsRepository:
    def __init__(self, table_name=None, endpoint_url=None):
        self.table_name = table_name or settings.rollups_table
        self.endpoint_url = endpoint_url or settings.dynamo_endpoint_url
        self._table = None

    @property
    def table(self):
        if self._table is None:
            self._table = resource(
                "dynamodb", self.endpoint_url
            ).Table(self.table_name)
        return self._table

    # -- writes -----------------------------------------------------------

    def put_many(self, items):
        """Write a day's rows. Whole-row puts, not counter updates.

        A roll-up is rebuilt from the call rows rather than incremented as
        calls land, so writing is idempotent by construction: run it twice on
        the same day and the second put lands the same numbers. Incrementing
        would be cheaper and would drift the first time a call is re-audited,
        a run is retried, or a row is corrected by hand -- and a counter that
        has drifted cannot be told apart from one that has not.
        """
        written = 0
        with self.table.batch_writer() as writer:
            for item in items:
                writer.put_item(Item=clean_item(item, ("PK", "SK", "scope")))
                written += 1
        db(f"rollups put {written} row(s)")
        return written

    def delete_many(self, keys):
        """Remove rows by (PK, SK). What a rebuild does with a scope that no
        longer has any calls on that day -- an agent who left, or a day whose
        rows were wiped and re-seeded. Left alone, such a row is a number the
        dashboard keeps adding into a span forever.
        """
        removed = 0
        with self.table.batch_writer() as writer:
            for pk, sk in keys:
                writer.delete_item(Key={"PK": pk, "SK": sk})
                removed += 1
        if removed:
            db(f"rollups deleted {removed} stale row(s)")
        return removed

    # -- reads ------------------------------------------------------------

    def get(self, pk, date):
        response = self.table.get_item(Key={"PK": pk, "SK": date})
        item = response.get("Item")
        return from_dynamo(item) if item else None

    def span(self, pk, start=None, end=None):
        """One scope over a date range, oldest day first.

        `start` and `end` are `yyyy-mm-dd` and both inclusive.
        """
        condition = "PK = :k"
        values = {":k": pk}
        if start and end:
            condition += " AND SK BETWEEN :s AND :e"
            values[":s"], values[":e"] = start[:10], end[:10]
        elif start:
            condition += " AND SK >= :s"
            values[":s"] = start[:10]
        elif end:
            condition += " AND SK <= :e"
            values[":e"] = end[:10]
        return self._query({
            "KeyConditionExpression": condition,
            "ExpressionAttributeValues": values,
        })

    def scope_span(self, scope, start=None, end=None):
        """Every row of one scope across a range — all teams, or all agents.

        GSI-1, because the base table partitions by the individual team or
        agent and so cannot answer "all of them" without one query each.
        """
        # `#s`, because `scope` is a DynamoDB reserved word and a bare one in a
        # key condition is a ValidationException, not a wrong answer.
        condition = "#s = :k"
        values = {":k": scope}
        if start and end:
            condition += " AND SK BETWEEN :s AND :e"
            values[":s"], values[":e"] = start[:10], end[:10]
        elif start:
            condition += " AND SK >= :s"
            values[":s"] = start[:10]
        elif end:
            condition += " AND SK <= :e"
            values[":e"] = end[:10]
        return self._query({
            "IndexName": INDEX_SCOPE,
            "KeyConditionExpression": condition,
            "ExpressionAttributeNames": {"#s": "scope"},
            "ExpressionAttributeValues": values,
        })

    def day(self, scope, date):
        """Every row of one scope on one day. What a rebuild diffs against."""
        return self.scope_span(scope, date, date)

    def _query(self, kwargs):
        rows = []
        while True:
            response = self.table.query(**kwargs)
            rows.extend(from_dynamo(i) for i in response.get("Items", []))
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                return rows
            kwargs["ExclusiveStartKey"] = cursor



# ------------------------------------------------------------------------
# the sessions table -- did the night finish?
# ------------------------------------------------------------------------

# One item per processing session:
#
#     PK                          SK
#     SESSION#2026-09-23T02:00:04Z   PENDING      while the run is working
#     SESSION#2026-09-23T02:00:04Z   COMPLETED    every queued job is done
#
# **The status is the sort key, so it is not updated -- it is replaced.**
# DynamoDB cannot change a key attribute on an existing item, so finishing a
# session is a put of the COMPLETED item followed by a delete of the PENDING
# one. In that order: a reader that lands between the two sees both and takes
# COMPLETED, which is the right answer, where the other order would show a
# session that had briefly never existed.
#
# A session left saying PENDING is therefore a real signal and not a bug -- it
# is a pod that was killed, or a run that could not reach the end. That is the
# whole reason the row is written at the start rather than only at the end: a
# marker that only ever appears on success cannot describe the case anyone
# actually needs it for.
#
# No index. Every access is by a session whose start is already known -- the
# run writing its own two rows, or a reader asking after one -- which the
# primary key answers on its own. An index on the status would be a second
# copy of the table written on every put and delete, to answer a question
# ("which sessions are still PENDING?") that a scan of a few hundred tiny rows
# answers well enough on the rare occasion anybody asks it.
SESSION_PREFIX = "SESSION#"
SESSION_PENDING = "PENDING"
SESSION_COMPLETED = "COMPLETED"


def session_pk(started_at_iso):
    """`SESSION#<iso started>`. The run's own start, never `now`: the two rows
    of one session have to share a partition, and the second is written hours
    after the first."""
    return f"{SESSION_PREFIX}{started_at_iso}"


class SessionsRepository:
    def __init__(self, table_name=None, endpoint_url=None):
        self.table_name = table_name or settings.sessions_table
        self.endpoint_url = endpoint_url or settings.dynamo_endpoint_url
        self._table = None

    @property
    def table(self):
        if self._table is None:
            self._table = resource(
                "dynamodb", self.endpoint_url
            ).Table(self.table_name)
        return self._table

    # -- writes -----------------------------------------------------------

    def start(self, started_at_iso, **fields):
        """The PENDING row, written before any work is taken up."""
        item = {
            "PK": session_pk(started_at_iso),
            "SK": SESSION_PENDING,
            "status": SESSION_PENDING,
            "sessionId": started_at_iso,
            "startedAt": started_at_iso,
            "totalCallsProcessed": 0,
            "callsFailed": 0,
            **fields,
        }
        self.table.put_item(Item=clean_item(item, ("PK", "SK")))
        db(f"session {item['PK']} PENDING")
        return item

    def complete(self, started_at_iso, **fields):
        """Replace the PENDING row with a COMPLETED one.

        Put first, delete second -- see the note above the class. The delete is
        unconditional: a session with no PENDING row (a re-run of `complete`,
        or a row someone removed by hand) still ends up with exactly the
        COMPLETED row it should have.
        """
        item = {
            "PK": session_pk(started_at_iso),
            "SK": SESSION_COMPLETED,
            "status": SESSION_COMPLETED,
            "sessionId": started_at_iso,
            "startedAt": started_at_iso,
            **fields,
        }
        self.table.put_item(Item=clean_item(item, ("PK", "SK")))
        self.table.delete_item(
            Key={"PK": session_pk(started_at_iso), "SK": SESSION_PENDING}
        )
        db(f"session {item['PK']} COMPLETED")
        return item

    # -- reads ------------------------------------------------------------

    def get(self, started_at_iso):
        """The session, whichever state it is in. Both rows if it is mid-swap."""
        response = self.table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": session_pk(started_at_iso)},
        )
        rows = [from_dynamo(i) for i in response.get("Items", [])]
        # COMPLETED sorts before PENDING, and is the truth when both are there.
        return rows[0] if rows else None




# ---------------------------------------------------------------------------
# the instances everything else imports
# ---------------------------------------------------------------------------

calls = DynamoCallsRepository()
objects = S3ObjectStore()
rollups = RollupsRepository()
sessions = SessionsRepository()

boot(f"storage  dynamodb tables={settings.calls_table},{settings.rollups_table},"
     f"{settings.sessions_table} "
     f"s3 bucket={settings.audio_bucket} region={settings.aws_region}")
