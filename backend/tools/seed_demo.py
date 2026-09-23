"""Wipe the test data and re-seed it from the audits already on disk.

    python3 -m backend.tools.seed_demo             write
    python3 -m backend.tools.seed_demo --check     read back
    python3 -m backend.tools.seed_demo --directory roster only
    python3 -m backend.tools.seed_demo --wipe      delete everything

Demo data, for seeing the dashboard work end to end before real calls arrive.
The audited floor — every agent in agents.json carrying a `tl` — with ten to
twenty calls each, one directory per call:

    2026-09-16/naman-sharma/call_naman-sharma_2026-09-16T11-20-05/
        audio_4.wav  audio_4.transcript.json  audio_4.clean.json
        audio_4.analysis.json  audio_4.audit.json

Real data throughout. The five JSON files are the ones the pipeline produced, and
the row's score, flag count, severity and fatal bit are projected off the audit by
the same `dashboard_fields` the audit worker uses. Three things are invented,
because nothing on disk supplies them:

  - which agent took the call  (the recordings are not attributed)
  - the customer phone number  (there is no customer in an audit document)
  - startedAt                  (a wav has no clock; spread over recent working
                                days so the by-day charts have a shape to draw)

`--wipe` deletes objects and rows, so it needs `backend/iam/policy.json`
attached to the caller.
"""

import argparse
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

from ..common.agents import directory
from ..batch.audit import dashboard_fields
from ..common.config import REPO_ROOT, settings
from ..common.models import CallRow, ProcessingStatus, score_band, to_ten_digits
from ..common.store import calls, directory_table, objects
from ..common import store as layout

FILES = os.path.join(REPO_ROOT, "files")

# Ten to fifteen calls each, drawn per agent from the seeded RNG. A fixed count
# per agent makes every agent's numbers identical and hides anything that
# depends on one having more to say than another; a range gives the floor shape.
MIN_CALLS_PER_AGENT = 10
MAX_CALLS_PER_AGENT = 15

# Calls land across this many recent weekdays. Wider than the counts above, so
# an agent's calls are spread rather than one-a-day in lockstep.
SPREAD_WEEKDAYS = 20

# Working hours, so the timestamps look like a call centre rather than a cron job.
FIRST_HOUR, LAST_HOUR = 9, 18
IST = timezone(timedelta(hours=5, minutes=30))

# Fixed, so a re-run reproduces the same floor. A demo that shuffles its own
# numbers every time is one nobody can compare against what they saw yesterday.
SEED = 20260916


def complete_calls():
    """Calls under files/ that have all five files, sorted for a stable order."""
    found = []
    if not os.path.isdir(FILES):
        return found
    for name in sorted(os.listdir(FILES)):
        folder = os.path.join(FILES, name)
        if not os.path.isdir(folder):
            continue
        needed = [os.path.join(folder, f"{name}.wav")] + [
            os.path.join(folder, f"{name}.{kind}.json") for kind in layout.ARTIFACTS
        ]
        if all(os.path.isfile(p) for p in needed):
            found.append(name)
    return found


def pick_agents():
    """The audited floor, from agents.json.

    Whoever carries a `tl` is seeded and nobody else, so the roster sheet is the
    single place the floor is defined — adding an agent here is adding a `tl` and
    a `state` to their row, not editing this file.
    """
    return directory.audited_agents()


def plan():
    """Which call goes to which agent, when, and with what customer number."""
    rng = random.Random(SEED)
    available = complete_calls()
    agents = pick_agents()
    if not agents:
        return []

    counts = {
        a.extension: rng.randint(MIN_CALLS_PER_AGENT, MAX_CALLS_PER_AGENT)
        for a in agents
    }
    needed = sum(counts.values())

    if len(available) < needed:
        # Reuse rather than refuse: a distinct recording per row is a
        # nice-to-have, rows that render is the point. call_id_for() keys on the
        # agent and ordinal, so a reused recording still gets its own row.
        pool = (available * (needed // max(len(available), 1) + 1))[:needed]
        rng.shuffle(pool)
    else:
        pool = rng.sample(available, needed)

    days = recent_weekdays(SPREAD_WEEKDAYS)
    rows, index = [], 0
    for agent in agents:
        # Distinct days where there are enough, so one agent is not stacked onto
        # a single afternoon; more calls than days means some days carry two.
        chosen = rng.sample(days, min(counts[agent.extension], len(days)))
        while len(chosen) < counts[agent.extension]:
            chosen.append(rng.choice(days))
        for day in chosen:
            source = pool[index]
            index += 1
            when = day.replace(
                hour=rng.randint(FIRST_HOUR, LAST_HOUR),
                minute=rng.randint(0, 59),
                second=rng.randint(0, 59),
                microsecond=0,
            )
            rows.append({
                "source": source,
                "agent": agent,
                "startedAt": when.isoformat(),
                "customerPhone": str(rng.randint(6000000000, 9999999999)),
            })
    rows.sort(key=lambda r: r["startedAt"])
    return rows


def recent_weekdays(count):
    """The last `count` weekdays, oldest first, at midnight IST."""
    days, cursor = [], datetime.now(IST).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    while len(days) < count:
        cursor -= timedelta(days=1)
        if cursor.weekday() < 5:
            days.append(cursor)
    return list(reversed(days))


def call_id_for(entry, ordinal):
    """A per-row id, since one recording can back more than one row.

    The source name alone would collide the moment a recording is reused, and two
    rows sharing a partition key is one row with the other's audit on it.
    """
    return f"{entry['source']}-{entry['agent'].extension}-{ordinal:02d}"


# -- writing ---------------------------------------------------------------

def seed_one(entry, ordinal):
    call_id = call_id_for(entry, ordinal)
    agent = entry["agent"]
    started = entry["startedAt"]
    folder = os.path.join(FILES, entry["source"])

    prefix = f"<artifact>/{layout.date_path(started)}/{call_id}"
    audio_key = layout.audio_key(call_id, started)

    with open(os.path.join(folder, f"{entry['source']}.audit.json"),
              encoding="utf-8") as handle:
        document = json.load(handle)

    # The conversation travels with the audit: a flag's turn_index plus a turn's
    # start_ms are what put a clock and a play button on each flag in the report.
    transcript = display_transcript(
        os.path.join(folder, f"{entry['source']}.clean.json"),
        os.path.join(folder, f"{entry['source']}.transcript.json"),
    )
    if transcript:
        document["transcript"] = transcript
    # The row's id, not the recording's, so the report names the call it is on.
    document["call_id"] = call_id

    # --- S3: the recording, then everything written about it -----------------
    wav = os.path.join(folder, f"{entry['source']}.wav")
    if not objects.exists(audio_key):
        # NOT objects.put_file: that has move semantics and unlinks the source,
        # which is right for a staged temporary copy and catastrophic for files/,
        # where these recordings are the only copy.
        with open(wav, "rb") as handle:
            objects.client.put_object(
                Bucket=objects.bucket, Key=audio_key, Body=handle,
                Metadata={"callid": call_id},
            )

    for kind in layout.ARTIFACTS:
        key = layout.artifact_key(call_id, started, kind)
        if kind == "audit":
            objects.put_json(key, document, metadata={"callid": call_id})
            continue
        path = os.path.join(folder, f"{entry['source']}.{kind}.json")
        with open(path, encoding="utf-8") as handle:
            objects.put_json(key, json.load(handle), metadata={"callid": call_id})

    # --- DynamoDB ------------------------------------------------------------
    audit_key = layout.artifact_key(call_id, started, "audit")
    row = CallRow(
        callId=call_id,
        customerPhone=to_ten_digits(entry["customerPhone"]),
        startedAt=started,
        durationSec=round((document.get("metadata") or {}).get("duration_ms", 0) / 1000),
        agentExtension=agent.extension,
        agentName=agent.name,
        processingStatus=ProcessingStatus.PROCESSED,
        callDirection="OUTBOUND",
        statusAt=started,
    )
    calls.put_if_absent(row)
    fields = dashboard_fields(document)
    calls.update(
        call_id,
        processingStatus=ProcessingStatus.PROCESSED,
        # The identity fields too, not just the audit's. `put_if_absent` leaves
        # an existing row alone, so without these a re-seed cannot correct a row
        # written under an older format -- which is exactly what a re-seed is
        # usually being run to do.
        #
        # agentDid, team, teamLeaderName, agentAccountId, audioKey and auditKey
        # are no longer among them: the row does not store any of those. The
        # first four are resolved from the roster on read and the last two are
        # derived from the call id and its start.
        customerPhone=row.customerPhone,
        agentName=row.agentName,
        durationSec=row.durationSec,
        statusAt=started,
        **fields,
    )
    return call_id, prefix, fields


def display_transcript(clean_path, raw_path):
    """report_data's own transcript builder — see backend/audit/pipeline.py."""
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    try:
        import report_data

        return report_data.display_transcript(clean_path, raw_path)
    except Exception as exc:  # noqa: BLE001
        print(f"  transcript unavailable: {exc!r}")
        return None


# -- wiping ----------------------------------------------------------------

def wipe():
    """Empty the bucket and the tables. Needs backend/iam/policy.json attached."""
    from botocore.exceptions import ClientError

    removed_objects = 0
    try:
        paginator = objects.client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=objects.bucket):
            # Versioning is on, so deleting the current version only writes a
            # delete marker. Every version has to go or the bucket still bills.
            batch = [
                {"Key": v["Key"], "VersionId": v["VersionId"]}
                for kind in ("Versions", "DeleteMarkers")
                for v in page.get(kind, [])
            ]
            for chunk in (batch[i:i + 1000] for i in range(0, len(batch), 1000)):
                objects.client.delete_objects(
                    Bucket=objects.bucket, Delete={"Objects": chunk, "Quiet": True}
                )
                removed_objects += len(chunk)
    except ClientError as exc:
        print(f"S3 wipe failed ({exc.response['Error']['Code']}). "
              f"Is backend/iam/policy.json attached?")
        return 1
    print(f"deleted {removed_objects} S3 object versions")

    for label, repo in (("calls", calls), ("directory", directory_table)):
        if repo is None:
            continue
        try:
            removed = _empty_table(repo.table)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                print(f"{label}: table does not exist yet, nothing to wipe")
                continue
            # Only `calls` is load-bearing. The roster is regenerated from
            # agents.json, so a seed that cannot reach it is worth a warning
            # rather than a refusal to seed the thing that holds the data.
            if label == "calls":
                print(f"{label} wipe failed ({code}). Is "
                      f"backend/iam/policy.json attached?")
                return 1
            print(f"{label}: cannot wipe ({code}) — see "
                  f"backend/iam/policy.json; continuing")
        print(f"deleted {removed} rows from {label}")
    return 0


def _empty_table(table):
    """Delete every item. Scans for keys only, then batches the deletes."""
    removed = 0
    scan = table.scan(ProjectionExpression="PK, SK")
    while True:
        with table.batch_writer() as batch:
            for item in scan.get("Items", []):
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
                removed += 1
        if "LastEvaluatedKey" not in scan:
            break
        scan = table.scan(
            ProjectionExpression="PK, SK",
            ExclusiveStartKey=scan["LastEvaluatedKey"],
        )
    return removed


# -- reading back ----------------------------------------------------------

def check():
    rows = calls.query(limit=500)
    by_agent = {}
    for row in rows:
        by_agent.setdefault(
            f"{row.get('agentExtension','?')} {row.get('agentName','')}", []
        ).append(row)

    print(f"{len(rows)} rows across {len(by_agent)} agents\n")
    for who, group in sorted(by_agent.items()):
        scored = [r for r in group if r.get("score") is not None]
        average = sum(r["score"] for r in scored) / len(scored) if scored else 0
        flagged = sum(1 for r in group if (r.get("flagCount") or 0) > 0)
        print(f"{who:<24} {len(group):>3} calls  avg {average:5.1f}  "
              f"{flagged} flagged")

    if rows:
        # Read the sample from the base table. `query` answers from a GSI, and
        # that index projects only what the list view renders — audioKey is not
        # in it, so reading the sample from the query result would print None for
        # a key that is perfectly well stored.
        sample = calls.get(
            sorted(rows, key=lambda r: r.get("startedAt", ""))[0]["callId"]
        ) or {}
        print(f"\nearliest: {sample.get('callId')}")
        print(f"  audioKey {sample.get('audioKey')}")
        print(f"  auditKey {sample.get('auditKey')}")
        print(f"  recordingSourceUrl present: "
              f"{'recordingSourceUrl' in sample}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="read back, write nothing")
    parser.add_argument("--wipe", action="store_true",
                        help="delete every object and row first")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan, touch nothing")
    parser.add_argument("--directory", action="store_true",
                        help="push agents.json into the directory table and "
                             "stop, writing no S3 objects and no call rows")
    args = parser.parse_args(argv)

    if args.check:
        return check()

    if args.directory:
        seed_directory()
        return 0

    entries = plan()
    if args.dry_run:
        from collections import Counter

        per = Counter(e["agent"].name for e in entries)
        print(f"{len(entries)} calls across {len(per)} agents\n")
        for who, n in sorted(per.items()):
            print(f"  {who:<20} {n:>3}")
        print()
        for ordinal, entry in enumerate(entries[:6]):
            call_id = call_id_for(entry, ordinal)
            print(f"  {call_id}")
            print(f"    {layout.audio_key(call_id, entry['startedAt'])}")
            print(f"    {layout.artifact_key(call_id, entry['startedAt'], 'audit')}")
        print(f"  … and {len(entries) - 6} more")
        return 0

    if args.wipe and (code := wipe()):
        return code

    for ordinal, entry in enumerate(entries):
        call_id, prefix, fields = seed_one(entry, ordinal)
        print(f"{call_id:<34} {prefix}")
        print(f"{'':<34}   score={fields['score']} band={score_band(fields['score'])} "
              f"flags={fields['flagCount']} "
              + (" DISQUALIFIED" if fields["disqualified"] else ""))

    print(f"\n{len(entries)} calls seeded.")
    seed_directory()
    return 0


def seed_directory():
    """Push agents.json into the directory table."""
    if directory_table is None:
        print("directory: no AWS backend, skipped")
        return
    from botocore.exceptions import ClientError

    try:
        counts = directory_table.sync_from(pick_agents())
    except ClientError as exc:
        print(f"directory: NOT written ({exc.response['Error']['Code']}). "
              f"The table or the IAM policy is missing — see "
              f"backend/iam/policy.json.")
        return
    print(f"directory: {counts['agents']} agents, {counts['teams']} teams")


if __name__ == "__main__":
    sys.exit(main())
