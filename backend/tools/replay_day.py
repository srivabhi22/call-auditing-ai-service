"""Replay one business day through the real collection and storage path.

    python3 -m backend.tools.replay_day --day 2026-09-20
    python3 -m backend.tools.replay_day --day 2026-09-20 --dry-run
    python3 -m backend.tools.replay_day --day 2026-09-20 --log-level DEBUG

A deployment rehearsal for phase 1 — fetch, filter, store — and nothing else.
It answers one question: *if the service ran tonight, would the calls land in
DynamoDB and S3 correctly?*

**It runs the service's own code.** This module contains no fetching, no
filtering and no writing of its own. It builds the same `batch.Run` the pod
builds, in the same `--collect-only` mode the collector pod uses, and that run
calls `collect.decide` -> `collect.fetch` -> `collect.sort_records` ->
`ingest.run` -> `queue.fill` exactly as it does at 02:00 every night. What is
added here is a preflight before it and a verification after it; if either of
those disagrees with what the run reported, the run is what is wrong.

The audit step is deliberately not run. `--collect-only` is a real production
mode -- it is how the collector pod is invoked when collection and auditing are
split -- so stopping there is not a special case invented for this script, and
it means the rehearsal spends nothing on Soniox or OpenAI. The rows it leaves
are UNPROCESSED, which is precisely where an auditor pod would pick them up.

**The window.** `--day D` means D 02:00 to D+1 02:00 on the tenant clock, which
is the business day the service defines. It is not typed in as a UTC range:
`collect.day_bounds` computes it, from the same `BATCH_DAY_START_HOUR` and the
same `TENANT_TZ` the nightly run uses, as though a pod had started at 02:00 on
D+1. That way the boundary arithmetic -- the part that is actually easy to get
wrong, and that silently fetches the wrong calls when it is -- is under test
too, rather than being bypassed by a hand-computed timestamp.

Re-running it is safe. Step 3 asks DynamoDB which call ids it already holds and
does nothing with those, so a second run of the same day downloads nothing,
writes nothing and reports every call as already known.
"""

import argparse
import sys
from collections import Counter
from datetime import datetime, timedelta

from ..common.config import settings
from ..common.models import TENANT_TZ, ProcessingStatus, hydrate, iso
from ..common import store
from ..common.store import calls as calls_repo, objects as object_store
from ..common.agents import directory as file_directory
from ..common.trace import configure
from ..batch import collect
from ..batch.cloudconnect import client as cc_client
from ..batch.run import Run, run_rows
from . import provision_aws


RULE = "=" * 78


def heading(text):
    print(f"\n{RULE}\n{text}\n{RULE}")


# ------------------------------------------------------------------------
# the extension alias — TEST SCAFFOLDING, NOT PRODUCTION BEHAVIOUR
# ------------------------------------------------------------------------

# `agents.json` lists 701-705 and 717; the extensions actually carrying traffic
# are 706-708 and 711-712 (Readme, known issue 5 -- the roster drifted, and the
# live DIDs 7971509211..17 are the block agents.py's own docstring quotes). With
# the roster filter in place that mismatch means every call in the window is
# dropped, and a storage rehearsal that stores nothing rehearses nothing.
#
# So for the rehearsal only, the live extensions are presented to the pipeline
# under the roster's numbers. This is a stand-in for the real roster, and it is
# the one thing in this script that does not reflect what the service will do
# in production. Fix `agents.json`, then run with `--no-alias`.
#
# The four 5-digit ids are deliberately absent: 98765 (MPivr), 65432 (UPivr),
# 76543 (RBSEivr) and 87654 (NeetIVR) are IVR endpoints, not handsets, and
# between them they carry 443 calls of which *none* are auditable. They are
# supposed to be dropped.
TEST_EXTENSION_ALIAS = {
    "706": "701",
    "707": "702",
    "708": "703",
    "711": "704",
    "712": "705",
}


class AliasingClient:
    """The Cloud Connect client, with the extension alias applied on the way in.

    A wrapper rather than a hook inside `collect`, because the point of this
    rehearsal is that the pipeline runs unmodified. `Run` already takes a
    `client`, so substituting one is a seam the production code provides; every
    step after the fetch -- the roster filter, `CallRow.from_call_log`, the
    direction handling, the ingest -- sees ordinary records and behaves exactly
    as it will on the night.

    `caller` and `callee` are both rewritten, rather than the direction-aware
    `agent_extension`, so that `CallLogRecord`'s own inbound/outbound logic is
    what decides which side is the agent -- the same code, on the same shape of
    payload. A number that is not an alias key is left alone, which is why the
    IVR ids and every customer number pass through untouched.
    """

    def __init__(self, inner, alias):
        self._inner = inner
        self._alias = alias
        self.rewritten = Counter()

    def fetch_call_logs_between(self, start, end):
        records = self._inner.fetch_call_logs_between(start, end)
        for record in records:
            for field in ("caller", "callee"):
                raw = str(record.payload.get(field) or "").strip()
                if raw in self._alias:
                    record.payload[field] = self._alias[raw]
                    self.rewritten[f"{raw} -> {self._alias[raw]}"] += 1
        return records

    def download_recording(self, url, sink, chunk_size=64 * 1024):
        return self._inner.download_recording(url, sink, chunk_size)

    def fetch_call_log(self, *args, **kwargs):
        return self._inner.fetch_call_log(*args, **kwargs)


# ------------------------------------------------------------------------
# phase 0 — preflight
# ------------------------------------------------------------------------

def preflight():
    """What the run is about to write to, and whether it is there.

    Read-only. `provision_aws.verify` is the same check the deploy gate runs,
    so a failure here is the same failure a deploy would have reported -- and
    it happens before a single call is fetched.
    """
    heading("PHASE 0 — preflight")

    print(f"region            {settings.aws_region}")
    print(f"calls table       {settings.calls_table}")
    print(f"directory table   {settings.directory_table}")
    print(f"media bucket      {settings.audio_bucket}")
    print(f"call log API      {settings.cc_base_url}")
    print(f"credentials       CC_TOKEN_ID {'set' if settings.cc_token_id else 'MISSING'}")
    print(f"day boundary      {settings.day_start_hour:02d}:00 tenant time "
          f"(UTC{TENANT_TZ.utcoffset(None)})")
    print(f"slice size        {settings.slice_minutes} min")
    print(f"download workers  {settings.download_concurrency}")
    print()

    dynamo = store.client("dynamodb", settings.dynamo_endpoint_url)
    s3 = store.client("s3", settings.s3_endpoint_url)
    if not provision_aws.verify(dynamo, s3):
        print("\npreflight FAILED — run `python3 -m backend.tools.provision_aws` first")
        return False

    # The roster this run will filter on. Printed in full because it is the one
    # input that decides which calls are kept, and a run that quietly filtered
    # against a stale roster would look like a quiet day.
    agents = collect.AgentCache()
    agents.load()
    source = "directory table"
    roster = dict(agents._by_extension)
    if not roster:
        source = "agents.json (directory table unreadable or empty)"
        roster = {a.extension: a for a in file_directory.all_agents()}

    print(f"\nroster ({len(roster)} extensions, from the {source}) — only calls "
          f"made by or to these are kept:")
    for extension in sorted(roster):
        agent = roster[extension]
        print(f"  ext {extension:<6} {agent.name:<22} "
              f"tl={agent.tl or '-':<16} team={agent.state or '-'}")

    existing = len(calls_repo.query(status=ProcessingStatus.UNPROCESSED)) \
        + len(calls_repo.query(status=ProcessingStatus.FAILED)) \
        + len(calls_repo.query(status=ProcessingStatus.INGESTING))
    print(f"\ncalls table currently holds {existing} row(s) in a work status")
    return True


# ------------------------------------------------------------------------
# phase 1 — the window
# ------------------------------------------------------------------------

def business_day(day):
    """`--day D` as the instants the service would have used for it.

    Computed by `collect.day_bounds`, not assembled here: it is the function
    the nightly run calls, and running it against a pinned `now` is what proves
    a pod starting at 02:00 on D+1 would ask for exactly this period.
    """
    heading(f"PHASE 1 — the window for {day}")

    # A pod starting on the boundary at the end of the day in question.
    as_if = datetime.combine(
        day + timedelta(days=1),
        datetime.min.time(),
        tzinfo=TENANT_TZ,
    ).replace(hour=settings.day_start_hour)

    start, end = collect.day_bounds(now=as_if)

    print(f"as if a pod started   {as_if:%Y-%m-%d %H:%M:%S %Z} "
          f"({iso(as_if)})")
    print(f"day_bounds() gives    {start:%Y-%m-%d %H:%M %Z} -> "
          f"{end:%Y-%m-%d %H:%M %Z}   (tenant time)")
    print(f"                      {iso(start)} -> {iso(end)}   (UTC, what the "
          f"table stores)")
    print(f"width                 {(end - start).total_seconds() / 3600:.0f}h, "
          f"fetched as {int((end - start).total_seconds() / 60 / settings.slice_minutes)} "
          f"slice(s) of {settings.slice_minutes} min")

    # The assertion is the point of computing it this way rather than typing it.
    expected_start = datetime.combine(
        day, datetime.min.time(), tzinfo=TENANT_TZ
    ).replace(hour=settings.day_start_hour)
    if start != expected_start or end != expected_start + timedelta(days=1):
        print(f"\nFAIL day_bounds returned {iso(start)}..{iso(end)}, but "
              f"{day} {settings.day_start_hour:02d}:00 tenant time is "
              f"{iso(expected_start)}. The boundary arithmetic is wrong.")
        return None
    print(f"\nok    this is {day} {settings.day_start_hour:02d}:00 to "
          f"{day + timedelta(days=1)} {settings.day_start_hour:02d}:00 tenant "
          f"time, which is what the nightly run would have asked for")
    return start, end


# ------------------------------------------------------------------------
# phase 3 — what actually landed
# ------------------------------------------------------------------------

def base_items(call_ids):
    """The full base-table item for each call id, by BatchGetItem.

    Necessary because the index query above cannot answer for the whole row.
    GSI-2 and GSI-3 are INCLUDE projections carrying `LIST_PROJECTION` -- what a
    list view renders -- and `audioKey`, `callDirection` and the hangup fields
    are deliberately not in it. Reading them off an index row returns nothing
    and means nothing; checking that an attribute the projection never carried
    is missing would fail every time, on a table that is completely correct.
    """
    table = calls_repo.table
    dynamo = table.meta.client
    items, ordered = {}, list(call_ids)
    for index in range(0, len(ordered), 100):
        chunk = ordered[index:index + 100]
        request = {table.name: {
            "Keys": [{"PK": f"CALL#{c}", "SK": "META"} for c in chunk]
        }}
        for _ in range(5):
            response = dynamo.batch_get_item(RequestItems=request)
            for item in response.get("Responses", {}).get(table.name, []):
                # Through `hydrate`, exactly as the repository's own reads are.
                # The table stores seventeen attributes; callId, startedAt,
                # agentExtension, audioKey, auditKey, scoreBand and dateKey are
                # all recomputed from the keys, and a reader that skips this
                # step sees a row with no agent and no audio on it.
                plain = hydrate(store.from_dynamo(item))
                items[plain["callId"]] = plain
            request = response.get("UnprocessedKeys") or {}
            if not request:
                break
    return items


def verify_storage(start, end, roster_extensions):
    """Read back what the run wrote, from DynamoDB and from S3.

    Deliberately a *separate* read rather than a report of the counters the run
    kept: a counter says what the code believed it did, and the only thing that
    proves storage works is finding the rows and the objects afterwards. The
    reads go through the repository's own query path -- GSI-3 for the window,
    GSI-1 for the work list -- so the dashboard's read side is exercised too.
    """
    heading("PHASE 3 — what is actually in DynamoDB and S3")

    indexed = calls_repo.query(start=iso(start), end=iso(end))
    print(f"GSI-3 day-time-index over the window: {len(indexed)} call row(s)")
    print("  (an INCLUDE projection — what a dashboard list view renders)")

    if not indexed:
        print("\nnothing was stored for this window")
        return True

    rows = sorted(
        base_items(r.get("callId") for r in indexed).values(),
        key=lambda r: r.get("startedAt") or "", reverse=True,
    )
    print(f"base table, by BatchGetItem:         {len(rows)} full item(s)")
    if len(rows) != len(indexed):
        print(f"FAIL the index holds {len(indexed)} row(s) and the base table "
              f"{len(rows)} — they disagree")
        return False
    print()

    by_status = Counter(r.get("processingStatus") for r in rows)
    by_agent = Counter(r.get("agentExtension") for r in rows)
    by_direction = Counter(r.get("callDirection") for r in rows)

    print("by processingStatus:")
    for status, count in by_status.most_common():
        print(f"  {status:<14} {count}")
    print("\nby agent extension:")
    for extension, count in by_agent.most_common():
        name = next((r.get("agentName") for r in rows
                     if r.get("agentExtension") == extension), "")
        print(f"  ext {extension:<6} {count:>4}  {name}")
    print("\nby direction:")
    for direction, count in by_direction.most_common():
        print(f"  {direction or '-':<14} {count}")

    ok = True

    # The index and the base table must agree on the attributes the projection
    # does carry. A GSI is maintained asynchronously, so a row updated and then
    # read through an index can be briefly stale -- but minutes later it is not
    # eventual consistency any more, it is a projection that never caught up.
    by_id = {r.get("callId"): r for r in rows}
    drift = [
        r.get("callId") for r in indexed
        if by_id.get(r.get("callId"), {}).get("processingStatus")
        != r.get("processingStatus")
    ]
    if drift:
        ok = False
        print(f"FAIL {len(drift)} row(s) have a different processingStatus in "
              f"GSI-3 than in the base table: {drift[:10]}")
    else:
        print(f"ok   GSI-3 and the base table agree on all {len(rows)} row(s)")

    # The filter's own assertion. Every row in the table must belong to an
    # extension on the roster; one that does not means a call was stored that
    # this run was supposed to drop.
    strays = sorted(set(by_agent) - roster_extensions)
    if strays:
        ok = False
        print(f"\nFAIL {len(strays)} stored extension(s) are not on the "
              f"roster: {', '.join(str(s) for s in strays)}")
    else:
        print(f"\nok   every stored row belongs to one of the "
              f"{len(roster_extensions)} roster extensions")

    # Every row's audio must be where its audioKey says it is, and a row with
    # no audioKey must have no object. A row pointing at a missing object is
    # the failure that only shows up later, when the audit worker cannot stage
    # the file -- which is exactly what this rehearsal exists to catch early.
    print("\nchecking each row's audio against S3...")
    missing, unexpected, checked = [], [], 0
    for row in rows:
        key = row.get("audioKey") or ""
        status = row.get("processingStatus")
        if key:
            checked += 1
            if not object_store.exists(key):
                missing.append((row.get("callId"), key, status))
        elif status not in (ProcessingStatus.SKIPPED, ProcessingStatus.FAILED):
            unexpected.append((row.get("callId"), status))

    if missing:
        ok = False
        print(f"FAIL {len(missing)} row(s) point at audio that is not in S3:")
        for call_id, key, status in missing[:10]:
            print(f"       {call_id} ({status}) -> {key}")
    else:
        print(f"ok   all {checked} row(s) with an audioKey have their object "
              f"in s3://{settings.audio_bucket}")

    if unexpected:
        ok = False
        print(f"FAIL {len(unexpected)} row(s) have no audioKey but are not "
              f"SKIPPED/FAILED: {unexpected[:10]}")

    # The other direction: objects in the bucket that no row points at. An
    # orphan is a recording that was downloaded and paid for and that nothing
    # will ever audit or delete.
    referenced = {r.get("audioKey") for r in rows if r.get("audioKey")}
    listed = set()
    s3 = object_store.client
    paginator = s3.get_paginator("list_objects_v2")
    total_bytes = 0
    for page in paginator.paginate(Bucket=settings.audio_bucket,
                                   Prefix=f"{store.AUDIO_PREFIX}/"):
        for obj in page.get("Contents", []):
            listed.add(obj["Key"])
            total_bytes += obj["Size"]
    print(f"\ns3://{settings.audio_bucket}/{store.AUDIO_PREFIX}/ holds "
          f"{len(listed)} object(s), {total_bytes / 1024 / 1024:.1f} MB")

    orphans = sorted(listed - referenced)
    if orphans:
        print(f"WARN {len(orphans)} object(s) under audio/ are not referenced "
              f"by any row in this window:")
        for key in orphans[:5]:
            print(f"       {key}")
        print("       (expected if the bucket holds other days; a problem if "
              "it does not)")

    # GSI-1 is the work list the auditor pod reads. If the sparse index did not
    # get its key attributes, the rows are stored and invisible -- which looks
    # like success everywhere except the one place that matters.
    print("\nGSI-1 work-queue-index (what an auditor pod would pick up):")
    remaining = calls_repo.count_by_status()
    if remaining:
        for status, count in sorted(remaining.items()):
            print(f"  {status:<14} {count}")
    else:
        print("  empty")

    unprocessed_rows = sum(1 for r in rows
                           if r.get("processingStatus") == ProcessingStatus.UNPROCESSED)
    indexed = remaining.get(ProcessingStatus.UNPROCESSED, 0)
    if indexed < unprocessed_rows:
        ok = False
        print(f"FAIL {unprocessed_rows} row(s) are UNPROCESSED in the window "
              f"but GSI-1 holds only {indexed} — the sparse index is out of "
              f"step with processingStatus and those calls will never be "
              f"audited")
    else:
        print(f"ok   all {unprocessed_rows} UNPROCESSED row(s) from this "
              f"window are in the work queue")

    # A sample, so there is something concrete to eyeball against the portal.
    print("\nsample of stored rows (newest first):")
    header = (f"  {'callId':<22} {'startedAt':<21} {'dir':<9} {'ext':<5} "
              f"{'agent':<18} {'sec':>5}  {'status':<12} audioKey")
    print(header)
    for row in rows[:12]:
        print(f"  {str(row.get('callId'))[:22]:<22} "
              f"{str(row.get('startedAt')):<21} "
              f"{str(row.get('callDirection') or '-'):<9} "
              f"{str(row.get('agentExtension') or '-'):<5} "
              f"{str(row.get('agentName') or '-')[:18]:<18} "
              f"{int(row.get('durationSec') or 0):>5}  "
              f"{str(row.get('processingStatus')):<12} "
              f"{row.get('audioKey') or '-'}")
    if len(rows) > 12:
        print(f"  ... and {len(rows) - 12} more")

    return ok


def show_run_row(run_id):
    """The run summary the pod wrote, read back from the table.

    The same row Jenkins and the on-call dashboard read the morning after, so
    seeing it here is the last part of the write path under test.
    """
    heading("PHASE 4 — the run summary row")
    row = run_rows.read_run(run_id)
    if not row:
        print(f"no RUN#{run_id} row found")
        return
    print(f"PK  RUN#{run_id}   SK  {row.get('startedAt')}\n")
    for key in sorted(row):
        if key in ("PK", "SK"):
            continue
        print(f"  {key:<24} {row[key]}")


# ------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m backend.tools.replay_day",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--day", required=True, metavar="YYYY-MM-DD",
        help="the business day to replay: this date at "
             f"{settings.day_start_hour:02d}:00 tenant time, through the next "
             f"date at {settings.day_start_hour:02d}:00.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="fetch and filter, write nothing and download nothing. Use it to "
             "see the split before committing to the transfer.",
    )
    parser.add_argument(
        "--max-calls", type=int, metavar="N",
        help=f"cap this run (default {settings.max_calls_per_run}).",
    )
    parser.add_argument(
        "--deadline", type=int, metavar="MINUTES",
        help=f"how long the run may take (default {settings.deadline_min}).",
    )
    parser.add_argument(
        "--no-alias", action="store_true",
        help="do not apply the test extension alias. This is what the service "
             "will actually do; with today's agents.json it keeps no calls at "
             "all. Use it once the roster is fixed.",
    )
    parser.add_argument(
        "--log-level", metavar="LEVEL", default="INFO",
        help="DEBUG adds a line per DynamoDB write, per S3 object and per "
             "call the roster filter dropped. Default INFO.",
    )
    args = parser.parse_args(argv)

    configure(level=args.log_level, force=True)

    try:
        day = datetime.strptime(args.day, "%Y-%m-%d").date()
    except ValueError:
        print(f"--day must be YYYY-MM-DD, got {args.day!r}")
        return 2

    if not preflight():
        return 2

    window = business_day(day)
    if window is None:
        return 2
    start, end = window

    roster_extensions = {a.extension for a in file_directory.all_agents()}
    agents = collect.AgentCache()
    agents.load()
    roster_extensions |= set(agents._by_extension)

    client = None
    if not args.no_alias:
        client = AliasingClient(cc_client, TEST_EXTENSION_ALIAS)
        heading("PHASE 1b — extension alias (TEST SCAFFOLDING)")
        print("agents.json does not list the extensions that are actually live,")
        print("so for this rehearsal the live ones are presented under the")
        print("roster's numbers. Nothing else about the run is changed, and")
        print("this is the only part of it that is not production behaviour.\n")
        for live, rostered in sorted(TEST_EXTENSION_ALIAS.items()):
            agent = file_directory.lookup(rostered)
            print(f"  live ext {live}  ->  roster ext {rostered}  "
                  f"({agent.name})")
        print("\n  not aliased, and so still dropped: 98765 (MPivr), "
              "65432 (UPivr),")
        print("                                    76543 (RBSEivr), "
              "87654 (NeetIVR)")
        print("\nfix agents.json and re-run with --no-alias to test what the "
              "service\nwill really do.")

    heading("PHASE 2 — the run (collector pod, --collect-only)")
    print("this is backend.batch.Run in the mode the collector pod uses:")
    print("  step 1  decide the window          collect.decide")
    print("  step 2  fetch the call log         collect.fetch")
    print("  step 3  split it four ways         collect.sort_records  <- roster filter")
    print("  step 4  download and store audio   ingest.run            -> S3 + DynamoDB")
    print("  step 5  fill the work queue        queue.fill")
    print("  step 6  audit                      SKIPPED (--collect-only)")
    print()

    run = Run(
        mode="collect",
        dry_run=args.dry_run,
        window_start=iso(start),
        window_end=iso(end),
        max_calls=args.max_calls,
        deadline_min=args.deadline,
        client=client,
    )
    exit_code = run.execute()

    heading("PHASE 2 RESULT")
    print(f"run id    {run.run_id}")
    print(f"outcome   {run.outcome}  (exit {exit_code})")
    print("counts:")
    for key in sorted(run.counts):
        print(f"  {key:<24} {run.counts[key]}")
    if client is not None and client.rewritten:
        print("\nextension alias applied to:")
        for mapping, count in sorted(client.rewritten.items()):
            print(f"  {mapping:<16} {count} call leg(s)")
    if run.failures:
        print(f"\nfailures ({len(run.failures)}):")
        for failure in run.failures[:20]:
            print(f"  {failure}")

    if args.dry_run:
        heading("dry run — nothing was written, so there is nothing to verify")
        return exit_code

    stored_ok = verify_storage(start, end, roster_extensions)
    show_run_row(run.run_id)

    heading("VERDICT")
    if exit_code == 0 and stored_ok:
        print("PASS — the day was fetched, filtered to the roster, and every "
              "kept call has a DynamoDB row and its audio in S3.")
        print("The rows are UNPROCESSED: an auditor pod would pick them up "
              "from GSI-1 on its next run.")
        return 0
    if stored_ok:
        print(f"PARTIAL — storage is consistent, but the run reported "
              f"{len(run.failures)} failure(s) above. Retrieval is what to "
              f"look at, not storage.")
        return exit_code
    print("FAIL — what is in DynamoDB and S3 does not agree. See PHASE 3.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
