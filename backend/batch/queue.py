"""The job queue, and the step that fills it.

The run has two halves: a producer that decides what needs auditing, and workers
that do it. This is the seam between them, and the only thing that crosses it is
a call id -- everything else about the call is already a row in DynamoDB, and a
job carrying a copy of it would be a second source of truth that goes stale the
moment anything updates the row.

**Why in memory is enough.** The queue is not where the work is recorded; the
table is. Step 5 fills this from `gsi1pk = UNPROCESSED` on every run, so a pod
that dies with three hundred jobs still in here loses nothing -- those rows are
still UNPROCESSED, and the next run enqueues them again from the same query.
Making the queue durable would mean keeping two records of the same fact in step
with each other, which is work in exchange for nothing: the durable one already
exists.

That is also why nothing here has acknowledgements, visibility timeouts or a
redrive policy. A job is taken by exactly one worker because `Queue.get` is
atomic, and a worker that dies mid-audit leaves its row UNPROCESSED, which is
the same state every other kind of failure leaves it in.

**The producer finishes before the consumers start.** Step 5 fills the queue,
step 6 drains it, and they do not overlap -- so a worker that finds the queue
empty is finished, with no need to distinguish "empty" from "nothing has been
put on it yet". `get_nowait` is the whole of the protocol.

**Two pods are still safe**, if not coordinated. They each build their own queue
from the same index and will overlap, so the duplicate lands on `audit_one`'s
first read, which drops anything already PROCESSED. That costs one DynamoDB read
where a broker would have cost nothing -- the price of not running a broker
for a job whose two halves live in the same process.

── Filling it ───────────────────────────────────────────────────────────────

GSI-1: gsi1pk = UNPROCESSED     the work list, read from the table

It asks the table rather than pushing "the calls the ingest step just
downloaded". That is the whole reason leftovers need no special handling:
yesterday's calls that the previous pod ran out of time for, calls a pipeline
error put back, and the ones this run just downloaded are all UNPROCESSED rows
and are all read by the same query.

**Why the table and not the queue is the source of truth.** The queue is in
this process and lives as long as the run does; the row is the record. A pod
killed with three hundred jobs still queued loses nothing, because the next run
reads them back out of this same query. The opposite arrangement, where the
queue holds work the table does not know about, would lose those calls the
moment the process ended -- which for a job that is *designed* to end is every
single run.

**Re-enqueuing a call another pod is auditing is harmless.** Two pods build
their work list from the same index and will overlap. The duplicate finds a
PROCESSED row and is dropped in one read -- a few milliseconds against the
alternative, which is tracking which UNPROCESSED rows somebody else already has
and is a second source of truth by another name.
"""

import queue as _queue
from collections import namedtuple
from datetime import timedelta

from ..common.config import settings
from ..common.models import ProcessingStatus, iso, parse_iso, utcnow
from ..common.store import calls as calls_repo
from ..common.trace import batch, warn

# ------------------------------------------------------------------------
# the queue
# ------------------------------------------------------------------------




# What a worker gets back. `enqueued_at` is not read by the worker; it is there
# because the first question asked about a run that ended early is how long its
# last jobs sat before anybody reached them.
Job = namedtuple("Job", "call_id run_id enqueued_at")


class JobQueue:
    """One FIFO, filled by step 5 and drained by step 6's worker threads."""

    def __init__(self):
        self._q = _queue.Queue()
        # What was put on it this run, which `depth()` cannot say once the
        # workers have started taking things off.
        self._sent = 0

    # -- producer ---------------------------------------------------------

    def send(self, call_ids, run_id="", enqueued_at=""):
        """Put call ids on. Returns `(queued, failures)`.

        The failure list is always empty and the signature keeps it anyway: the
        caller counts and reports per-call failures, and a queue that cannot fail
        is a property of this implementation rather than of the step.
        """
        count = 0
        for call_id in call_ids:
            self._q.put(Job(call_id=call_id, run_id=run_id, enqueued_at=enqueued_at))
            count += 1
        self._sent += count
        return count, []

    # -- workers ----------------------------------------------------------

    def take(self):
        """The next job, or None when the queue is empty.

        `Queue.get_nowait` is atomic, which is the entire reason two workers
        cannot be handed the same call. Empty means finished, because nothing is
        enqueued after the workers start.
        """
        try:
            return self._q.get_nowait()
        except _queue.Empty:
            return None

    # -- operations -------------------------------------------------------

    def depth(self):
        """How many jobs are still waiting. Exact, unlike a broker's estimate."""
        return self._q.qsize()

    def sent(self):
        return self._sent

    def clear(self):
        """Throw away what is left. Called when the run stops at its deadline.

        The jobs are dropped rather than handed anywhere: their rows are still
        UNPROCESSED, so the next run finds them on GSI-1 exactly as it found
        them this time.
        """
        dropped = 0
        while self.take() is not None:
            dropped += 1
        if dropped:
            batch(f"{dropped} job(s) left on the queue were dropped — their rows "
                  f"are still UNPROCESSED, so the next run queues them again")
        return dropped

    def describe(self):
        return f"in-process queue ({self.depth()} waiting)"


# One per process. The producer and the workers are threads in the same run, so
# module state is the shared object -- there is nothing to connect to and nothing
# to configure.
job_queue = JobQueue()


# ------------------------------------------------------------------------
# step 5 — everything UNPROCESSED goes on it
# ------------------------------------------------------------------------




# Statuses that are stranded rather than outstanding: nothing in this design
# writes AUDITING any more, so a row still carrying it was left there by an
# older build and nothing else will ever pick it up. Swept
# back into the work list here, which costs one index query per run and saves a
# call that has already been paid to download.
LEGACY_IN_FLIGHT = ("AUDITING",)


class Dispatched:
    def __init__(self):
        self.queued = 0
        self.failed = 0
        self.revived = 0        # legacy AUDITING rows put back to UNPROCESSED
        self.stalled = 0        # INGESTING rows a killed pod left behind
        self.failures = []      # [(callId, reason)] for the run summary

    def counts(self):
        return {
            "queued": self.queued,
            "queueFailed": self.failed,
            "queueRevived": self.revived,
            "ingestStalled": self.stalled,
        }


def _revive_legacy(repository, dry_run=False):
    """Put any pre-queue AUDITING row back to UNPROCESSED. Returns the count.

    Deliberately unconditional -- no "has it been stuck for an hour" check --
    because nothing writes AUDITING any longer, so every such row is stale by
    definition. This can be deleted once no AUDITING rows remain.
    """
    revived = 0
    for status in LEGACY_IN_FLIGHT:
        rows = repository.query(status=status)
        for row in rows:
            call_id = row.get("callId")
            if not call_id:
                continue
            revived += 1
            if dry_run:
                continue
            repository.update(
                call_id,
                processingStatus=ProcessingStatus.UNPROCESSED,
                statusAt=iso(utcnow()),
            )
    if revived:
        warn("batch", f"{revived} row(s) were left in AUDITING by an older "
                      f"build and nothing would ever have picked them up — "
                      f"back to UNPROCESSED and onto the queue")
    return revived


def _fail_stalled_ingests(repository, now, dry_run=False):
    """Mark INGESTING rows that nobody is going to finish. Returns the count.

    A row goes INGESTING before the download starts and moves to UNPROCESSED or
    FAILED when it ends, both inside the same `ingest_one` call. The only way
    one survives in between is a pod killed outright -- and nothing else in the
    system ever looks at an INGESTING row, so left alone it is a call that is
    neither audited nor visible as a problem.

    FAILED rather than back to UNPROCESSED, because the audio never reached S3:
    queued, it would be picked up by a worker, find nothing at `audioKey`, and be
    marked FAILED anyway -- after a download from S3 and a round trip through the
    queue. The reason on the row says what happened, which is what somebody
    looking at it needs.

    The cutoff is what keeps this from touching a download that is still going
    in another pod. A download takes seconds and gives up after a couple of
    minutes; an hour is not a close call.
    """
    cutoff = now - timedelta(minutes=settings.stuck_after_min)
    stalled = 0
    for row in repository.query(status=ProcessingStatus.INGESTING,
                                end=iso(cutoff)):
        call_id = row.get("callId")
        if not call_id:
            continue
        # The index sort key is the time of the *call*, not of the download, so
        # the range condition above is a way of reading fewer rows and not the
        # decision. The decision is made on when the row itself was written.
        full = repository.get(call_id) or {}
        if full.get("processingStatus") != ProcessingStatus.INGESTING:
            continue
        since = parse_iso(full.get("statusAt")) or parse_iso(full.get("startedAt"))
        if since and since > cutoff:
            continue
        stalled += 1
        if dry_run:
            continue
        repository.update(
            call_id,
            processingStatus=ProcessingStatus.FAILED,
            failureReason=("the download never finished — the pod writing this "
                           "row was killed, and no audio reached S3"),
            statusAt=iso(now),
        )
    if stalled:
        warn("batch", f"{stalled} row(s) were left mid-download by a pod that "
                      f"was killed — marked FAILED, since their audio never "
                      f"reached S3")
    return stalled


def pending(repository=None, limit=None):
    """Every UNPROCESSED call id, oldest first.

    Oldest first because a call that has been waiting since yesterday is the one
    closest to a retention boundary, and because a queue drained newest-first
    can starve its own tail indefinitely. The order is only a hint once the
    messages are on a standard queue, which does not promise to return them in
    the order they went on -- but it decides which calls make it onto the queue
    at all when the cap truncates the list, and that is the part that matters.
    """
    repository = repository or calls_repo
    rows = repository.query(status=ProcessingStatus.UNPROCESSED, limit=limit)
    rows.sort(key=lambda row: row.get("startedAt") or "")
    return [row["callId"] for row in rows if row.get("callId")]


def fill(repository=None, queue=None, limit=None, run_id="", dry_run=False):
    """Step 5. Put every UNPROCESSED call on the queue. Returns what it did."""
    repository = repository or calls_repo
    queue = queue or job_queue
    result = Dispatched()

    result.revived = _revive_legacy(repository, dry_run=dry_run)
    result.stalled = _fail_stalled_ingests(repository, utcnow(), dry_run=dry_run)

    call_ids = pending(repository, limit)
    if not call_ids:
        batch("enqueue: nothing is UNPROCESSED — the queue gets nothing")
        return result

    if dry_run:
        batch(f"enqueue: would queue {len(call_ids)} call(s)")
        result.queued = len(call_ids)
        return result

    batch(f"enqueue: putting {len(call_ids)} UNPROCESSED call(s) on the queue")
    sent, failed = queue.send(call_ids, run_id=run_id, enqueued_at=iso(utcnow()))
    result.queued = sent
    result.failed = len(failed)
    result.failures = failed
    for call_id, reason in failed[:10]:
        warn("batch", f"{call_id} could not be queued: {reason}")

    batch(f"enqueue done: {result.queued} queued, {result.failed} failed"
          + (f", {result.revived} revived" if result.revived else ""))
    return result
