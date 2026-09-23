"""Replay the audit half of the pipeline — queue, workers, concurrency, storage.

    python3 -m backend.tools.replay_audit
    python3 -m backend.tools.replay_audit --stage-seconds 2
    python3 -m backend.tools.replay_audit --concurrency 5
    python3 -m backend.tools.replay_audit --dry-run

The other half of the rehearsal. `replay_day` proves calls get *into* DynamoDB
and S3; this proves they get back *out* — that step 5 reads the work list off
GSI-1, that the queue hands each call to exactly one of twenty-five workers,
that each worker stages its audio from S3, writes four artifacts back, and moves
its row to PROCESSED with `gsi1pk` removed so it leaves the work queue.

**It runs the service's own code.** It builds the same `batch.Run` an auditor
pod builds, in the same `--audit-only` mode, and that run calls `queue.fill` and
`audit.run` unmodified. The thread pool, the deadline, the drain margin, the
per-call claim, the S3 round trip, the DynamoDB update and the counters are all
the real ones.

**The one substitution.** `audit.run_pipeline` is replaced by a function that
returns an audit document already on disk under `files/`, instead of spending
five minutes and real money on Soniox and OpenAI. Everything on either side of
it is untouched: the worker still stages the real recording out of S3, the
replayed document still goes through `dashboard_fields`, the intermediates are
still written next to the audio and uploaded by the real `_store_intermediates`,
and a document that does not parse still fails the call the way a bad audit
would.

This is deliberately *not* `STUB_PIPELINE=1`. The built-in stub synthesises a
schema-valid report from a hash of the call id, which exercises the wiring but
tells you nothing about how the real thing behaves -- every stub document is the
same size and the same shape. These are real audits of real calls: full
transcripts, real flag counts, real section marks, documents from a few KB to a
few hundred KB. What the write path does with a 300 KB document is a question
only real ones answer.

**Why there is a delay.** A replayed audit returns in microseconds, and 231 of
those finish before a thread pool has finished starting -- so the concurrency
this is meant to show would not be visible at all. `--stage-seconds` puts a
sleep in each of the four stages, jittered per call so they do not move in
lockstep, which is what makes the pool's behaviour observable. It is the only
thing here that invents a number; set it to 0 for maximum speed.
"""

import argparse
import hashlib
import json
import os
import random
import shutil
import sys
import threading
import time
from collections import Counter

from ..common.config import REPO_ROOT, settings
from ..common.models import ProcessingStatus
from ..common import store
from ..common.store import ARTIFACTS, calls as calls_repo, objects as object_store
from ..common.trace import ai, configure
from ..batch import audit as audit_step, queue as queue_step
from ..batch.run import Run
from . import provision_aws


RULE = "=" * 78

# The four files a completed pipeline leaves beside the audio. `audit` is the
# document; the other three are what `_store_intermediates` uploads.
CORPUS_ROOT = os.path.join(REPO_ROOT, "files")


def heading(text):
    print(f"\n{RULE}\n{text}\n{RULE}")


# ------------------------------------------------------------------------
# the corpus — real pipeline output, keyed by call
# ------------------------------------------------------------------------

def load_corpus(root=CORPUS_ROOT):
    """Every `files/<name>/` that holds a complete set of pipeline output.

    A directory missing any of the four is skipped rather than half-used: a
    replayed call with no clean transcript would fail in `_display_transcript`
    for a reason that has nothing to do with the code under test.
    """
    if not os.path.isdir(root):
        return []
    complete = []
    for name in sorted(os.listdir(root)):
        folder = os.path.join(root, name)
        if not os.path.isdir(folder):
            continue
        paths = {
            kind: os.path.join(folder, f"{name}.{kind}.json")
            for kind in ARTIFACTS
        }
        if all(os.path.isfile(p) for p in paths.values()):
            complete.append((name, paths))
    return complete


def pick_for(call_id, corpus):
    """Which recorded audit this call replays. Stable, so a rerun is comparable.

    Hashed rather than assigned in order because the queue is drained by
    twenty-five threads and the order calls come off it is not the order they
    went on -- an ordinal would make the pairing depend on scheduling, and two
    runs of the same window would not be comparable.
    """
    seed = int(hashlib.sha256(str(call_id).encode()).hexdigest()[:8], 16)
    return corpus[seed % len(corpus)]


# ------------------------------------------------------------------------
# the replay pipeline, and what it records about itself
# ------------------------------------------------------------------------

class Timeline:
    """When each call was in the pipeline, and on which thread.

    The whole point of the exercise: the counters the run keeps say how many
    calls were audited, and say nothing about whether twenty-five of them were
    ever in flight at once. This is what answers that, and it is recorded from
    inside the replaced function so it measures the real pool and the real
    queue rather than anything this module arranges.
    """

    def __init__(self):
        self.spans = []          # (call_id, thread, start, end, bytes)
        self._lock = threading.Lock()

    def record(self, call_id, thread, start, end, size):
        with self._lock:
            self.spans.append((call_id, thread, start, end, size))

    def report(self, run_started, run_finished):
        if not self.spans:
            print("no calls went through the pipeline")
            return

        threads = Counter(s[1] for s in self.spans)
        durations = sorted(s[3] - s[2] for s in self.spans)
        busy = sum(durations)
        wall = run_finished - run_started

        # Peak concurrency, by sweeping the start/end events in time order. A
        # count of distinct threads is not the same thing: twenty-five threads
        # that each ran once, one after another, is a pool that never had two
        # calls in flight.
        events = sorted(
            [(s[2], 1) for s in self.spans] + [(s[3], -1) for s in self.spans]
        )
        peak, current = 0, 0
        for _, delta in events:
            current += delta
            peak = max(peak, current)

        print(f"calls through the pipeline   {len(self.spans)}")
        print(f"worker threads used          {len(threads)}")
        print(f"peak calls in flight         {peak}")
        print(f"configured concurrency       {settings.audit_concurrency}")
        print()
        print(f"wall clock                   {wall:.1f}s")
        print(f"summed pipeline time         {busy:.1f}s")
        print(f"effective speed-up           {busy / wall:.1f}x"
              if wall > 0 else "")
        print()
        print(f"per-call pipeline time       "
              f"min {durations[0]:.2f}s  "
              f"median {durations[len(durations) // 2]:.2f}s  "
              f"max {durations[-1]:.2f}s")

        print("\nper-thread share:")
        for thread, count in threads.most_common():
            bar = "#" * max(1, round(count * 40 / max(threads.values())))
            print(f"  {thread:<22} {count:>4}  {bar}")

        # In-flight over time, so an uneven drain or a long tail is visible
        # rather than averaged away.
        print("\ncalls in flight over the run:")
        buckets = 40
        span = max(run_finished - run_started, 1e-6)
        counts = []
        for b in range(buckets):
            at = run_started + span * (b + 0.5) / buckets
            counts.append(sum(1 for s in self.spans if s[2] <= at < s[3]))
        top = max(counts) or 1
        for level in range(top, 0, -max(1, top // 12)):
            row = "".join("#" if c >= level else " " for c in counts)
            print(f"  {level:>3} |{row}")
        print(f"      +{'-' * buckets}")
        print(f"       0s{' ' * (buckets - 8)}{span:.0f}s")

        print(f"\ndocuments replayed           "
              f"{sum(s[4] for s in self.spans) / 1024 / 1024:.1f} MB of audit "
              f"JSON, largest {max(s[4] for s in self.spans) / 1024:.0f} KB")


def build_replay_pipeline(corpus, timeline, stage_seconds):
    """The stand-in for `audit.run_pipeline`.

    Same signature, same return shape -- `(document, mode)` -- and the same
    obligations: it leaves `<work_dir>/<call_id>.{transcript,clean,analysis}.json`
    behind, because the real `_store_intermediates` runs straight afterwards and
    uploads exactly those, and it attaches the display transcript to the
    document through `audit._display_transcript`, which is the real function
    reading the real files.
    """
    def replay_pipeline(audio_path, call_id, work_dir):  # noqa: ARG001 --
        # work_dir is part of run_pipeline's signature; the real one needs it
        # to run subprocesses in, and this one works from `audio_path` alone.
        thread = threading.current_thread().name
        started = time.monotonic()

        name, paths = pick_for(call_id, corpus)
        base = os.path.splitext(audio_path)[0]
        ai(f"replaying {name} (no model called)")

        # The jitter is per call and deterministic, so two runs are comparable
        # while the calls still do not move in lockstep -- a pool where every
        # job takes exactly the same time drains in visible waves that say more
        # about the sleep than about the pool.
        jitter = random.Random(call_id).uniform(0.7, 1.3)

        for kind in ARTIFACTS:
            if stage_seconds:
                time.sleep(stage_seconds * jitter)
            if kind == "audit":
                continue
            # Copied, not symlinked: the real pipeline writes real files here
            # and `_store_intermediates` opens them by path.
            shutil.copyfile(paths[kind], f"{base}.{kind}.json")

        with open(paths["audit"], encoding="utf-8") as handle:
            document = json.load(handle)
        size = os.path.getsize(paths["audit"])

        # The real function, over the files just written.
        if transcript := audit_step._display_transcript(base):
            document["transcript"] = transcript

        timeline.record(call_id, thread, started, time.monotonic(), size)
        return document, f"replay:{name}"

    return replay_pipeline


# ------------------------------------------------------------------------
# phase 0 — preflight
# ------------------------------------------------------------------------

def preflight(corpus):
    heading("PHASE 0 — preflight")

    print(f"region            {settings.aws_region}")
    print(f"calls table       {settings.calls_table}")
    print(f"media bucket      {settings.audio_bucket}")
    print(f"audit concurrency {settings.audit_concurrency}")
    print(f"deadline          {settings.deadline_min} min, "
          f"drain margin {settings.drain_margin_min} min")
    print(f"work dir          {settings.work_dir}")
    print()

    dynamo = store.client("dynamodb", settings.dynamo_endpoint_url)
    s3 = store.client("s3", settings.s3_endpoint_url)
    if not provision_aws.verify(dynamo, s3):
        print("\npreflight FAILED")
        return None

    print(f"\ncorpus: {len(corpus)} complete pipeline output(s) under "
          f"{os.path.relpath(CORPUS_ROOT, REPO_ROOT)}/")
    if not corpus:
        print("FAIL nothing to replay — each files/<name>/ needs all four of "
              + ", ".join(f"{k}.json" for k in ARTIFACTS))
        return None

    # The work list, read exactly as step 5 reads it.
    pending = queue_step.pending(calls_repo)
    print(f"\nGSI-1 work-queue-index holds {len(pending)} UNPROCESSED call(s)")
    if not pending:
        print("nothing is UNPROCESSED — run replay_day first, or re-run this "
              "after putting rows back")
        return None

    counts = calls_repo.count_by_status()
    for status, count in sorted(counts.items()):
        print(f"  {status:<14} {count}")
    return pending


# ------------------------------------------------------------------------
# phase 3 — what the workers left behind
# ------------------------------------------------------------------------

def verify_storage(call_ids):
    """Every audited call must have four artifacts in S3 and a PROCESSED row.

    Read back rather than counted: the run's own tally says what the workers
    believed, and the only thing that proves the audit half works is finding the
    documents afterwards and finding the rows out of the work queue.
    """
    heading("PHASE 3 — what is in DynamoDB and S3 now")

    rows = {}
    for call_id in call_ids:
        row = calls_repo.get(call_id)
        if row:
            rows[call_id] = row

    by_status = Counter(r.get("processingStatus") for r in rows.values())
    print(f"{len(rows)} of the {len(call_ids)} queued call(s) read back\n")
    for status, count in by_status.most_common():
        print(f"  {status:<14} {count}")

    ok = True
    processed = [c for c, r in rows.items()
                 if r.get("processingStatus") == ProcessingStatus.PROCESSED]

    # The dashboard scalars. A PROCESSED row with no score is one where
    # `dashboard_fields` silently produced nothing, which a status alone hides.
    scored = [c for c in processed if rows[c].get("score") is not None]
    disqualified = [c for c in processed if rows[c].get("disqualified")]
    print(f"\nPROCESSED rows carrying a score: {len(scored)} "
          f"({len(disqualified)} disqualified, which correctly have none)")
    unexplained = [c for c in processed
                   if rows[c].get("score") is None and c not in disqualified]
    if unexplained:
        ok = False
        print(f"FAIL {len(unexplained)} PROCESSED row(s) have neither a score "
              f"nor `disqualified`: {unexplained[:10]}")

    bands = Counter(rows[c].get("scoreBand") for c in processed)
    print("\nscore bands (derived on read, not stored):")
    for band, count in bands.most_common():
        print(f"  {band or '(none)':<20} {count}")

    flags = Counter(rows[c].get("auditStatus") or "(no flags)" for c in processed)
    print("\nhighest flag severity:")
    for severity, count in flags.most_common():
        print(f"  {severity:<20} {count}")

    # The four artifacts, per call.
    print(f"\nchecking S3 artifacts for {len(processed)} PROCESSED call(s)...")
    missing = Counter()
    for call_id in processed:
        row = rows[call_id]
        started = row.get("startedAt")
        for kind in ARTIFACTS:
            if not object_store.exists(store.artifact_key(call_id, started, kind)):
                missing[kind] += 1
    if missing:
        ok = False
        for kind, count in missing.most_common():
            print(f"FAIL {count} call(s) have no {kind}/ document in S3")
    else:
        print(f"ok   all four artifacts present for every PROCESSED call "
              f"({len(processed) * len(ARTIFACTS)} objects)")

    # The whole reason `gsi1pk` is removed at PROCESSED. If it is still there
    # the call is audited, paid for, and will be audited again on every run.
    remaining = calls_repo.count_by_status()
    print("\nGSI-1 work-queue-index after the run:")
    if remaining:
        for status, count in sorted(remaining.items()):
            print(f"  {status:<14} {count}")
    else:
        print("  empty — every call left the work queue")
    still_queued = remaining.get(ProcessingStatus.UNPROCESSED, 0)
    audited_but_indexed = [
        c for c in processed
        if calls_repo.get(c) and "gsi1pk" in (calls_repo.table.get_item(
            Key={"PK": f"CALL#{c}", "SK": "META"},
            ProjectionExpression="gsi1pk").get("Item") or {})
    ]
    if audited_but_indexed:
        ok = False
        print(f"FAIL {len(audited_but_indexed)} PROCESSED row(s) still carry "
              f"gsi1pk — they are still in the work queue and will be audited "
              f"and paid for again: {audited_but_indexed[:10]}")
    else:
        print(f"ok   no PROCESSED row is still in the work queue "
              f"({still_queued} genuinely unprocessed left)")

    # Attribute count, which is the other thing this schema is meant to hold to.
    if processed:
        sample = calls_repo.table.get_item(
            Key={"PK": f"CALL#{processed[0]}", "SK": "META"}
        ).get("Item") or {}
        print(f"\naudited row shape — {len(sample)} stored attribute(s):")
        for key in sorted(sample):
            value = store.from_dynamo(sample[key])
            rendered = str(value)
            if len(rendered) > 44:
                rendered = rendered[:41] + "..."
            print(f"  {key:<18} {rendered}")

    return ok


# ------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m backend.tools.replay_audit",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--stage-seconds", type=float, default=1.0, metavar="S",
        help="simulated time per pipeline stage, jittered per call (four "
             "stages, so ~4S per call). The real thing is 60-90s a stage. "
             "0 runs flat out. Default 1.0.",
    )
    parser.add_argument(
        "--concurrency", type=int, metavar="N",
        help=f"workers, overriding AUDIT_CONCURRENCY "
             f"(currently {settings.audit_concurrency}). Lower it to see the "
             f"queue serialise.",
    )
    parser.add_argument(
        "--max-calls", type=int, metavar="N",
        help="cap how many UNPROCESSED calls are queued this run.",
    )
    parser.add_argument(
        "--deadline", type=int, metavar="MINUTES",
        help=f"how long the run may take (default {settings.deadline_min}). "
             f"Set it low to watch the drain margin stop the workers.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="fill the queue and stop. Nothing is audited, downloaded or "
             "written.",
    )
    parser.add_argument(
        "--log-level", metavar="LEVEL", default="INFO",
        help="DEBUG adds a line per DynamoDB write and per S3 object.",
    )
    args = parser.parse_args(argv)

    configure(level=args.log_level, force=True)

    if args.concurrency:
        settings.audit_concurrency = args.concurrency

    corpus = load_corpus()
    pending = preflight(corpus)
    if pending is None:
        return 2

    heading("PHASE 1 — the replay pipeline (THE ONE SUBSTITUTION)")
    print("audit.run_pipeline is replaced. Everything else is the real thing:\n")
    print("  step 5  read GSI-1, fill the queue     queue.fill        REAL")
    print("  step 6  drain it with N workers        audit.run         REAL")
    print("    - claim the job                      Queue.get_nowait  REAL")
    print("    - read the row, skip if terminal     repository.get    REAL")
    print("    - stage the audio from S3            store.download_to REAL")
    print("    - transcribe -> clean -> analyse -> audit              REPLAYED")
    print("    - upload the four artifacts          store.put_json    REAL")
    print("    - PROCESSED + dashboard scalars      repository.update REAL")
    print()
    print(f"corpus            {len(corpus)} recorded audits, assigned to calls "
          f"by a hash of the callId")
    print(f"stage delay       {args.stage_seconds}s per stage, x0.7-1.3 jitter "
          f"per call (~{args.stage_seconds * 4:.1f}s per call)")
    print(f"workers           {settings.audit_concurrency}")
    print(f"queued            {len(pending)} call(s)")
    if not args.stage_seconds:
        print("\nNOTE --stage-seconds 0: calls will finish faster than the pool "
              "starts,\n     so the concurrency figures below will understate "
              "the real thing.")

    timeline = Timeline()
    if not args.dry_run:
        audit_step.run_pipeline = build_replay_pipeline(
            corpus, timeline, args.stage_seconds
        )

    heading("PHASE 2 — the run (auditor pod, --audit-only)")
    run = Run(
        mode="audit",
        dry_run=args.dry_run,
        max_calls=args.max_calls,
        deadline_min=args.deadline,
    )
    started = time.monotonic()
    exit_code = run.execute()
    finished = time.monotonic()

    heading("PHASE 2 RESULT")
    print(f"run id    {run.run_id}")
    print(f"outcome   {run.outcome}  (exit {exit_code})")
    print("counts:")
    for key in sorted(run.counts):
        print(f"  {key:<24} {run.counts[key]}")
    if run.failures:
        print(f"\nfailures ({len(run.failures)}):")
        for failure in run.failures[:20]:
            print(f"  {failure}")

    if args.dry_run:
        heading("dry run — the queue was filled and nothing was audited")
        return exit_code

    heading("PHASE 2b — concurrency")
    timeline.report(started, finished)

    stored_ok = verify_storage(pending)

    heading("VERDICT")
    if exit_code == 0 and stored_ok:
        print("PASS — the work list drained through the real queue and the "
              "real worker pool,\nevery audited call has four artifacts in S3, "
              "and every one of them left GSI-1.")
        return 0
    if stored_ok:
        print(f"PARTIAL — storage is consistent, but the run reported "
              f"{len(run.failures)} failure(s) above.")
        return exit_code
    print("FAIL — what is in DynamoDB and S3 does not agree. See PHASE 3.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
