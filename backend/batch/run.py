"""The run: one full pass of the pipeline, and the row it writes about itself.

There is no CLI. A run is started by the HTTP trigger in `backend.api`, which
owns the process; this module owns what a run *is*. `Run(mode=...).execute()`
does the whole thing and returns an exit code, and the two are deliberately
separate so that the thing that decides *when* to run and the thing that knows
*how* can be changed independently.

    mode="full"     steps 1-6: fetch the day, ingest it, queue it, audit it
    mode="collect"  steps 1-5: fetch and store, but run no model
    mode="audit"    steps 5-6: queue whatever is already UNPROCESSED, audit it

One run, in order: take the most recent complete 02:00-to-02:00 day (tenant
time, no checkpoint and nothing carried between runs), fetch it from the call
log, download the recordings to S3 as UNPROCESSED rows, put every UNPROCESSED
call id on an in-process queue, and let AUDIT_CONCURRENCY workers drain it
through the AI pipeline -- four JSON artifacts into S3 and the row marked
PROCESSED. Then the summary row is written.

    0   all good
    1   the run finished, some individual calls failed
    2   could not run at all -- the config is wrong, the queue is missing,
        or their API is unreachable

These are still called exit codes because that is what they mean to whoever
reads them; the API turns them into an `outcome` on the run row. The
distinction that matters is the middle one: forty broken recordings out of
three thousand is a normal night and must not page anyone, while "the call log
API returned nothing for twenty-four hours" must.

Two ways to spend nothing while testing:

    dry_run=True    fetch and sort, then stop. Reads the call log, reports the
                    split, writes nothing and queues nothing.
    STUB_PIPELINE=1 the four AI stages are replaced by a schema-valid stub, so
                    the whole path runs end to end with no spend and no keys.

── The run ──────────────────────────────────────────────────────────────────

    1  window    the most recent complete 02:00-to-02:00 day, tenant time
    2  collect   ask Cloud Connect for that day, one hour at a time
    3  collect   sort the result: known / off roster / not auditable / to do
    4  ingest    download each recording to S3, row goes UNPROCESSED
    5  dispatch  put every UNPROCESSED call id on the queue
    6  audit     workers drain the queue: pipeline -> four JSONs in S3 -> PROCESSED
    6b rollup    re-sum every day this run touched into the `rollups` table
    7  control   write the run summary

**Nothing is carried between runs.** The window is computed from the clock, not
from a stored marker, so there is no checkpoint to be wrong, none to move, and
no branch for "the first run". Asking for the same day twice costs nothing --
step 3 asks the table which call ids it already holds and does nothing with
those -- so a re-run of a finished day downloads nothing and audits nothing.

The queue is in this process and in memory (`queue.py`). It is not a source of
truth and is never read to find out what exists: step 5 fills it from
`gsi1pk = UNPROCESSED` on every run, so a pod that dies with three hundred jobs
on it loses nothing -- those rows are still UNPROCESSED, and the next run
enqueues them again from the same query.

It is also a process-wide singleton, which is the reason `backend.api` allows
only one run at a time per pod: two runs sharing one queue would hand the same
call to both.

**Why step 5 reads the table rather than step 4's output.** Yesterday's calls
that the previous pod ran out of time for, and calls a pipeline error put back,
are UNPROCESSED rows exactly like the ones this run just downloaded. One query
picks up all three, so leftovers need no special handling anywhere.

**The deadline.** The pod is alive for a few hours and then stops, whether or
not the work is done. The run therefore stops *starting* work
`BATCH_DRAIN_MARGIN_MIN` before its deadline and lets what is in flight finish.
Anything unreached stays UNPROCESSED, which is exactly where the next run's step
5 looks.

── The control row ──────────────────────────────────────────────────────────

It lives in the `calls` table as a singleton partition (architecture §4.6).
It is tiny, written once per run, and a second table for it would be a second
thing to provision, grant and verify.

    PK  RUN#<runId>   SK  <startedAt>   the counts and the outcome

**There is no checkpoint row and no lock row**, and both absences are the
point. The window is computed from the clock rather than from a stored marker,
so there is nothing that can be wrong about where the last run got to. And two
pods running at once is not worth preventing: they build the same work list
from the table, and the duplicate is dropped by the row read at the top of
`audit_one` for the cost of one GetItem.

Two things about these rows living in the calls table matter, and both are
failure modes rather than preferences:

- **They carry none of `gsi1pk`..`gsi3pk`.** An index key on a control row puts
  it in a dashboard query as a call with no agent, no score and no start time.
  Absent key, absent from the index -- which is why nothing here goes near
  `CallRow.to_item()`.
- **Their sort keys are not `META`,** so `DynamoCallsRepository._key()` cannot
  address them. This module gets its own small accessor onto the same table
  rather than widening the calls repository, because widening it would mean
  every caller of `get()` has to start asking whether what came back is a call.

Anything that walks the whole table -- any aggregation the read side does --
has to filter on `PK` beginning `CALL#` for the same reason. A control row counted as a call is a wrong number on a dashboard that
nobody can explain.
"""

import os
import socket
import uuid
from datetime import timedelta

from ..common.config import settings
from ..common.models import ProcessingStatus, iso, utcnow
from ..common import store
from ..common.store import calls as calls_repo, sessions as sessions_repo
from ..common.trace import batch, set_run_id, warn, error
from . import (audit as audit_step, collect, ingest, queue as queue_step,
               rollup as rollup_step)
from .audit import ModelGuardError
from .queue import job_queue

# ------------------------------------------------------------------------
# the run-summary row
# ------------------------------------------------------------------------




RUN_PREFIX = "RUN#"

# The outcomes a run can end on, and what each means to Jenkins via the exit
# code. Kept as strings on the row so "did last night's run work?" is one read
# and not an exercise in reading a number back into a meaning.
OUTCOME_OK = "OK"                  # exit 0
OUTCOME_PARTIAL = "PARTIAL"        # exit 1 -- some calls failed, the run did not
OUTCOME_FAILED = "FAILED"          # exit 2 -- could not run at all
OUTCOME_RUNNING = "RUNNING"        # written at the start; a row still saying
                                   # this is a pod that was killed


def new_run_id():
    """Time-sortable, so `RUN#` partitions list in the order they happened, with
    enough randomness that two pods starting in the same second do not collide.
    """
    return f"{utcnow():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def holder_name():
    """Which pod this is. The hostname is the pod name on Kubernetes, which is
    the one string that ties a run row back to the logs that produced it."""
    return f"{socket.gethostname()}/{os.getpid()}"


class ControlRows:
    """The accessor. One table handle, no CallRow anywhere near it."""

    def __init__(self, table_name=None, endpoint_url=None):
        self.table_name = table_name or settings.calls_table
        self.endpoint_url = endpoint_url or settings.dynamo_endpoint_url
        self._table = None

    @property
    def table(self):
        if self._table is None:
            self._table = store.resource(
                "dynamodb", self.endpoint_url
            ).Table(self.table_name)
        return self._table

    # -- the run summary --------------------------------------------------

    def start_run(self, run_id, started_at, fields=None):
        """Write the row at the start, marked RUNNING.

        Written before any work rather than at the end, so a pod that is killed
        leaves a row saying what it was doing and when it began. A summary only
        written on success cannot describe a failure, which is the case it is
        most needed for.
        """
        item = {
            "PK": f"{RUN_PREFIX}{run_id}",
            "SK": iso(started_at),
            "runId": run_id,
            "startedAt": iso(started_at),
            "outcome": OUTCOME_RUNNING,
            "host": holder_name(),
            "pipelineMode": settings.pipeline_mode,
            # Epoch seconds, for the table's TTL if it is ever enabled. Run rows
            # accumulate one per day forever otherwise -- harmless at this size,
            # but there is no reason to keep 2029's view of 2026.
            "expiresAtEpoch": int(
                (started_at + timedelta(days=settings.run_summary_ttl_days))
                .timestamp()
            ),
            **(fields or {}),
        }
        self.table.put_item(Item=store.clean_item(item, frozenset({"PK", "SK"})))
        batch(f"run row written: {RUN_PREFIX}{run_id}")
        return item

    def update_run(self, run_id, started_at, **fields):
        """Merge counts into the run row as the steps finish.

        Step 4's counts are written here *before* step 5 downloads anything, so
        a window that was wrong is visible in seconds rather than after a few
        hundred dollars of transfer and transcription.
        """
        if not fields:
            return
        names, values, sets = {}, {}, []
        for index, (field, value) in enumerate(fields.items()):
            names[f"#f{index}"] = field
            values[f":v{index}"] = store.to_dynamo(value)
            sets.append(f"#f{index} = :v{index}")
        self.table.update_item(
            Key={"PK": f"{RUN_PREFIX}{run_id}", "SK": iso(started_at)},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def read_run(self, run_id):
        response = self.table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": f"{RUN_PREFIX}{run_id}"},
            Limit=1,
        )
        items = response.get("Items") or []
        return store.from_dynamo(items[0]) if items else None


run_rows = ControlRows()


# ------------------------------------------------------------------------
# the run
# ------------------------------------------------------------------------




EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CANNOT_RUN = 2


class Run:
    """One invocation. Holds the counts so step 7 has something to write."""

    def __init__(self, mode="full", dry_run=False, window_start=None,
                 window_end=None, max_calls=None, deadline_min=None,
                 repository=None, store=None, control=None, client=None,
                 queue=None, roster=None):
        self.mode = mode                  # full | collect | audit
        self.dry_run = dry_run
        self.window_start = window_start
        self.window_end = window_end
        self.max_calls = max_calls or settings.max_calls_per_run
        self.deadline_min = deadline_min or settings.deadline_min

        self.repository = repository or calls_repo
        self.store = store
        self.control = control or run_rows
        self.client = client
        self.queue = queue or job_queue
        # `agents.json`, read from disk. One per run, shared by the roster
        # filter in step 3 and the publish in step 0 so they cannot differ.
        self.roster = roster or collect.AgentCache()

        self.run_id = new_run_id()
        self.started_at = utcnow()
        self.deadline = self.started_at + timedelta(minutes=self.deadline_min)
        # Nothing new is started after this. The margin is sized to the longest
        # single audit measured (~20 min), so the workers finish what they hold
        # before the pod goes rather than leaving calls invisible on the queue
        # for the length of the visibility timeout.
        self.drain_deadline = self.deadline - timedelta(
            minutes=settings.drain_margin_min
        )

        self.counts = {}
        self.failures = []
        self.outcome = OUTCOME_OK
        self.holder = holder_name()

    # -- helpers ----------------------------------------------------------

    def _record(self, **counts):
        """Merge counts into the run row as soon as they are known.

        Written as the run goes rather than at the end, because the question the
        row answers -- "what is it doing / what did it do?" -- is asked most
        often about a run that did not reach the end.
        """
        self.counts.update(counts)
        if self.dry_run:
            return
        try:
            self.control.update_run(self.run_id, self.started_at, **counts)
        except Exception as exc:  # noqa: BLE001 -- bookkeeping must not be able
            # to fail a run that is otherwise working.
            warn("batch", f"could not update the run row: {exc!r}")

    def _fail(self, message):
        self.outcome = OUTCOME_FAILED
        self.failures.append(message)
        error("batch", message)
        return EXIT_CANNOT_RUN

    def _partial(self, message):
        if self.outcome == OUTCOME_OK:
            self.outcome = OUTCOME_PARTIAL
        self.failures.append(message)

    # -- the run ----------------------------------------------------------

    def execute(self):
        set_run_id(self.run_id)
        batch(f"run {self.run_id} starting — mode={self.mode} "
              f"dry_run={self.dry_run} deadline={iso(self.deadline)} "
              f"(stops starting work at {iso(self.drain_deadline)}) "
              f"pipeline={settings.pipeline_mode}")

        # Checked before the window is read and before a single row is touched.
        # A run that is going to refuse should refuse while the table is
        # untouched, not after marking forty calls as work in progress.
        if self.mode in ("full", "audit"):
            try:
                audit_step.check_models()
            except ModelGuardError as exc:
                return self._fail(str(exc))

        if self.dry_run:
            # Deliberately no run row and nothing queued. A dry run is a
            # read-only inspection: it answers "is the window right" before
            # anything is downloaded, transcribed or audited.
            batch("dry run: no row written, nothing downloaded, nothing queued")
            return self._body()

        try:
            self.control.start_run(self.run_id, self.started_at, {
                "mode": self.mode,
                "deadline": iso(self.deadline),
            })
            self._session_start()
            return self._body()
        except Exception as exc:  # noqa: BLE001 -- the summary is written in
            # the finally below whatever happened here.
            error("batch", f"run failed: {exc!r}", exc_info=True)
            return self._fail(f"unhandled: {type(exc).__name__}: {exc}")
        finally:
            self._finish()

    def _body(self):
        """Steps 1 to 6. The run summary is around it."""
        period = None
        collected = None

        # Step 0: the roster, to S3. The dashboard is a separate deployment and
        # cannot read `agents.json` off this disk, so every run republishes it.
        # It is ~2 KB and idempotent, and doing it here rather than on a
        # schedule means the roster the dashboard groups by is always the one
        # this run filtered on -- the two disagreeing is a whole month of
        # reports quietly attributed to the wrong people.
        #
        # Never fatal. A run that audits calls correctly but could not rewrite a
        # roster that has not changed is not a failed run.
        try:
            published = store.publish_roster(self.roster.all_agents(), self.store)
            batch(f"roster: {len(published['agents'])} agent(s) published to "
                  f"{store.ROSTER_KEY}")
        except Exception as exc:  # noqa: BLE001 -- see above
            self._partial(f"roster not published: {exc!r}")

        if self.mode in ("full", "collect"):
            # --- step 1 --------------------------------------------------
            period = collect.decide(self.window_start, self.window_end)
            self._record(
                windowStart=iso(period.start),
                windowEnd=iso(period.end),
                windowSource=period.source,
            )

            if period.is_empty:
                batch("nothing to collect — the window is empty")
                collected = collect.Collected()
            else:
                # --- steps 2 and 3 ---------------------------------------
                records, failed_slices = collect.fetch(period, self.client)
                collected = collect.sort_records(
                    records, self.repository, dry_run=self.dry_run,
                    max_calls=self.max_calls,
                )
                collected.failed_slices = failed_slices
                for start, end, reason in failed_slices:
                    self._partial(f"call log slice {start}..{end}: {reason}")

            # Written before step 4 downloads anything. A window that was wrong
            # is then visible in seconds rather than after a few hundred dollars
            # of transfer and transcription.
            self._record(**collected.counts())

            if self.dry_run:
                batch("dry run: stopping after step 3 — nothing downloaded "
                      "and nothing queued")
                self._record(outcome=self.outcome)
                return self._exit_code()

            # --- step 4 --------------------------------------------------
            ingested = ingest.run(
                collected.todo, self.repository, self.store, self.client,
                deadline=self.drain_deadline,
            )
            self._record(**ingested.counts())
            for call_id, reason in ingested.failures:
                self._partial(f"ingest {call_id}: {reason}")

            # A slice that never returned is a hole in the day. There is no
            # marker to hold back any more, so it is said out loud and
            # reported as PARTIAL: the calls in that hour were not fetched, and
            # tomorrow's run asks for tomorrow -- re-running this day is a
            # decision for whoever reads the exit code.
            if collected.failed_slices:
                warn("batch", f"{len(collected.failed_slices)} slice(s) of this "
                              f"day failed. Those calls were not fetched; re-run "
                              f"with --window-start/--window-end to pick them up.")

        # --- step 5 ----------------------------------------------------------
        # Runs in every mode, including `--audit-only`: an auditor pod's whole
        # job is to put the outstanding work on the queue and then do it.
        dispatched = queue_step.fill(
            self.repository, self.queue, limit=self.max_calls,
            run_id=self.run_id, dry_run=self.dry_run,
        )
        self._record(**dispatched.counts())
        for call_id, reason in dispatched.failures:
            self._partial(f"queue {call_id}: {reason}")

        # --- step 6 ----------------------------------------------------------
        if self.mode in ("full", "audit") and not self.dry_run:
            audited = audit_step.run(
                self.repository, self.store, self.queue,
                deadline=self.drain_deadline, run_id=self.run_id,
            )
            self._record(**audited.counts(),
                         datesTouched=sorted(audited.dates))
            for call_id, reason in audited.failures:
                self._partial(f"audit {call_id}: {reason}")

            # --- step 6b -------------------------------------------------
            # The days this run changed are re-summed into the `rollups`
            # table, so the dashboard adds up a span of small rows instead of
            # re-aggregating every call in it on every read.
            #
            # Here rather than in the audit worker because a roll-up is a
            # whole day and the day is only finished once the workers are:
            # summing it per call would be one full-day rebuild per call, and
            # incrementing it per call would drift the first time a call is
            # re-audited. Failures are absorbed -- these rows are a read-side
            # convenience, and a day that could not be summed is fixed with
            # `backend.tools.build_rollups`, not by failing a run whose calls
            # are all safely audited.
            self._rollup(audited.dates)
        else:
            batch(f"mode={self.mode}: the work is queued, this pod is not "
                  f"auditing it")

        return self._exit_code()

    def _rollup(self, dates):
        """Step 6b. Re-sum the days this run touched."""
        if not dates:
            return
        try:
            counts = rollup_step.rebuild_days(sorted(dates))
        except Exception as exc:  # noqa: BLE001 -- see the call site
            warn("batch", f"could not build the day roll-ups: {exc!r}")
            self._record(rollupDaysFailed=len(dates))
            return
        self._record(**counts)
        if counts["rollupDaysFailed"]:
            warn("batch", f"{counts['rollupDaysFailed']} day(s) were not "
                          f"rolled up — the dashboard will be short those "
                          f"days until a run touches them again")

    # -- the session marker -----------------------------------------------
    #
    # `RUN#` says what the run did, in detail, in the calls table. The session
    # row says one thing, in a table of its own: is this session still working,
    # or has every job it took on reached a final state? That is the question
    # anything downstream asks -- an export, a report mailer, a dashboard
    # banner -- and it should not have to read and interpret a run summary to
    # answer it.

    @property
    def session_id(self):
        """`SESSION#<this>`. The run's own start, so both rows share a
        partition however many hours apart they are written."""
        return iso(self.started_at)

    def _session_start(self):
        try:
            sessions_repo.start(
                self.session_id,
                runId=self.run_id,
                mode=self.mode,
                host=self.holder,
                deadline=iso(self.deadline),
                pipelineMode=settings.pipeline_mode,
            )
        except Exception as exc:  # noqa: BLE001 -- bookkeeping must not be
            # able to fail a run that is otherwise working.
            warn("batch", f"could not write the PENDING session row: {exc!r}")

    def _session_complete(self, summary):
        """Replace PENDING with COMPLETED, once every job has been taken up.

        Called from `_finish`, which runs whatever happened -- so "completed"
        here means *the session is over*, not that every call in it succeeded.
        The counts say how it went, and `outcome` carries OK / PARTIAL /
        FAILED. A session that never gets here keeps its PENDING row, which is
        exactly what a killed pod should leave behind.
        """
        processed = int(self.counts.get("audited", 0) or 0)
        failed = (int(self.counts.get("auditFailed", 0) or 0)
                  + int(self.counts.get("auditRetryable", 0) or 0)
                  + int(self.counts.get("ingestFailed", 0) or 0))
        try:
            sessions_repo.complete(
                self.session_id,
                runId=self.run_id,
                mode=self.mode,
                host=self.holder,
                outcome=self.outcome,
                finishedAt=summary["finishedAt"],
                durationSec=summary["durationSec"],
                # The two numbers asked for, and the three that make them
                # readable: a session can end with jobs it never reached
                # (the deadline cut it short) or ones it deliberately skipped
                # (already PROCESSED, or no longer auditable), and lumping
                # either into "failed" would be a wrong number nobody can
                # explain.
                totalCallsProcessed=processed,
                callsFailed=failed,
                callsSkipped=int(self.counts.get("auditSkipped", 0) or 0),
                callsNotReached=int(self.counts.get("auditNotReached", 0) or 0),
                callsQueued=int(self.counts.get("queued", 0) or 0),
                queueRemaining=summary["queueRemaining"],
                failureCount=summary["failureCount"],
            )
            batch(f"session {self.session_id} COMPLETED — "
                  f"{processed} processed, {failed} failed")
        except Exception as exc:  # noqa: BLE001 -- see `_session_start`
            warn("batch", f"could not complete the session row: {exc!r} — it "
                          f"stays PENDING")

    # -- step 7 -----------------------------------------------------------

    def _finish(self):
        """The summary row. The last thing the run does."""
        finished = utcnow()
        try:
            remaining = self.repository.count_by_status()
        except Exception as exc:  # noqa: BLE001
            warn("batch", f"could not count the work queue: {exc!r}")
            remaining = {}

        summary = {
            "finishedAt": iso(finished),
            "durationSec": int((finished - self.started_at).total_seconds()),
            "outcome": self.outcome,
            "remainingUnprocessed": remaining.get(
                ProcessingStatus.UNPROCESSED, 0
            ),
            "remainingByStatus": remaining,
            # Jobs still on the queue when the run stopped. Nonzero means the
            # deadline cut it short, which is the one line that makes such a run
            # readable at a glance.
            "queueRemaining": self.queue.depth(),
            # Bounded. A run where three thousand calls failed the same way does
            # not need three thousand copies of the reason on one row, and
            # DynamoDB's 400 KB item limit would refuse it anyway.
            "failures": self.failures[:50],
            "failureCount": len(self.failures),
        }
        self._record(**summary)
        self._session_complete(summary)

        batch(f"run {self.run_id} {self.outcome} in {summary['durationSec']}s — "
              + ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items())
                          if isinstance(v, int)))
        if remaining:
            batch("work list still holds: "
                  + ", ".join(f"{k}={v}" for k, v in sorted(remaining.items())))
        if self.queue.depth():
            batch(f"{self.queue.depth()} job(s) were still queued when the run "
                  f"stopped — their rows are UNPROCESSED for the next run")
        if self.failures:
            warn("batch", f"{len(self.failures)} failure(s) this run; the first "
                          f"few: " + " | ".join(self.failures[:3]))

    def _exit_code(self):
        if self.outcome == OUTCOME_FAILED:
            return EXIT_CANNOT_RUN
        if self.outcome == OUTCOME_PARTIAL:
            return EXIT_PARTIAL
        return EXIT_OK
