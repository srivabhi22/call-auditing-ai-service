"""Step 6 — workers take call ids off the queue and run the AI pipeline.

Per job:

    1. read the row; skip anything already PROCESSED (another pod got there)
    2. download the audio from S3 into a directory of its own
    3. transcribe -> clean -> analyse -> audit
    4. write the four JSON artifacts to S3
    5. PROCESSED, with the dashboard scalars, and `gsi1pk` removed so the row
       leaves the work list
    6. delete the temporary files

**Taking a job is the claim.** `Queue.get_nowait` is atomic, so exactly one
worker gets any given call and two workers cannot audit the same one. There is
no compare-and-set on the row and no AUDITING state: within a run the queue
already answers "who has this", and a second answer in DynamoDB would be a
second thing to keep in step with it.

What that does not cover is a *second pod* running at the same time, since each
builds its own queue from the same index. Step 1 is the whole defence and it is
cheap: one read, and a call that is already PROCESSED is dropped before anything
is downloaded or any model is called.

**Twenty-five at a time.** Almost all of the 5-6 minutes an audit takes is spent
waiting on Soniox and OpenAI. Serially, 3,000 calls is 300 hours of wall clock a
day, which is not a tuning problem but an impossible one. The ceiling is not the
CPU but OpenAI's tokens-per-minute: one call in flight measures ~60,000 tpm, so
Tier 2 (2M) allows ~33 and Tier 3 (4M) ~66. 25 per pod is what two or three pods
share safely on Tier 3.

**The drain margin.** The pod is alive for a few hours and then stops. Twenty
minutes before the deadline the workers stop *taking* jobs and finish what they
hold, because 20 minutes is about the longest single audit measured. Whatever is
left on the queue is dropped -- those rows are still UNPROCESSED, and the next
run enqueues them from the table exactly as this one did.

── The pipeline itself ──────────────────────────────────────────────────────

The repo's four stages — transcribe, clean, analyse, audit — run as subprocesses
against a staged copy of the recording. They need SONIOX_API_KEY and
OPENAI_API_KEY; without both, `stub_audit` produces a schema-valid report instead
so the wiring can be exercised end to end with no spend and no keys. Which one
ran is recorded on the report itself, so a stubbed audit is never mistaken for a
real one downstream.

`dashboard_fields` is the contract between the audit document and the calls
table. Every scalar the dashboard reads is projected onto the row so a list view
never has to open a report from S3; the full document stays in S3 and is fetched
only when someone opens one call.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from ..common.config import REPO_ROOT, settings
from ..common.models import ProcessingStatus, audit_key_for, iso, score_band, utcnow
from ..common.store import (
    ARTIFACTS, artifact_key, calls as calls_repo, objects as object_store,
)
from ..common.trace import (
    ai, audit as audit_log, batch, call_context, current_run_id, set_run_id,
    warn, error,
)
from .queue import job_queue

# ------------------------------------------------------------------------
# the four stages, as subprocesses
# ------------------------------------------------------------------------




# The four stages, in order. Each writes a file next to the audio that the next
# one reads, which is why they run in one directory.
STAGES = ("transcribe", "clean", "analyze", "audit")

# Exactly audit_schema.Severity, lowest first. "critical" and "none" used to be
# here; nothing could ever emit "critical", so a filter for it would have
# silently matched nothing. If the enum gains a level, add it here too.
SEVERITY_ORDER = ("low", "medium", "high")


def run_pipeline(audio_path, call_id, work_dir):
    """Audio in, audit document out.

    Returns (document, mode) where mode is "real" or "stub".
    """
    if settings.force_stub_pipeline or not settings.pipeline_available:
        reason = (
            "STUB_PIPELINE is set" if settings.force_stub_pipeline
            else "SONIOX_API_KEY or OPENAI_API_KEY is missing"
        )
        ai(f"stub pipeline ({reason})")
        return stub_audit(audio_path, call_id), "stub"

    ai(f"running pipeline over {os.path.basename(audio_path)}")
    return _run_real_pipeline(audio_path, call_id, work_dir), "real"


def _run_real_pipeline(audio_path, call_id, work_dir):
    """The repo's four scripts, as subprocesses.

    Subprocesses rather than imports because each script owns its own argument
    parsing and exit behaviour, and a stage that dies takes only its own process
    down. The audit worker treats a non-zero exit as retryable.
    """
    base = os.path.splitext(audio_path)[0]
    steps = [
        ([sys.executable, os.path.join(REPO_ROOT, "transcribe.py"), audio_path],
         f"{base}.transcript.json"),
        ([sys.executable, os.path.join(REPO_ROOT, "clean_transcript.py"),
          f"{base}.transcript.json"], f"{base}.clean.json"),
        ([sys.executable, os.path.join(REPO_ROOT, "analyze_call.py"),
          f"{base}.clean.json"], f"{base}.analysis.json"),
        ([sys.executable, os.path.join(REPO_ROOT, "audit_call.py"),
          f"{base}.clean.json"], f"{base}.audit.json"),
    ]

    for stage, (command, output) in zip(STAGES, steps):
        if os.path.exists(output):
            ai(f"  {stage}: already done, skipping")
            continue
        ai(f"  {stage}...")
        result = subprocess.run(
            command, cwd=work_dir, capture_output=True, text=True, timeout=1800,
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "")[-400:]
            raise PipelineError(f"{stage} exited {result.returncode}: {tail}")
        if not os.path.exists(output):
            raise PipelineError(f"{stage} wrote no {os.path.basename(output)}")

    with open(f"{base}.audit.json", encoding="utf-8") as fh:
        document = json.load(fh)

    # The conversation, stored with the audit rather than beside it.
    #
    # Every flag carries a `turn_index` into this list, and every turn carries a
    # `start_ms`. That pair is what lets the report put a clock on a flag and a
    # play button next to it. Without the transcript the flags still render — as
    # quotes with nowhere to go, which is most of their value gone.
    #
    # One object, because the report is read far more often than it is written
    # and a second fetch to join them would be on every open.
    if transcript := _display_transcript(base):
        document["transcript"] = transcript
    return document


def _display_transcript(base):
    """The turns the report renders, from the two files the pipeline left behind.

    `report_data` owns this: the cleaned transcript is what the audit was scored
    against and what `turn_index` indexes into, while the timings live in the raw
    one, and it only lifts them across when the two align exactly. A partial
    alignment would put the wrong clock against a quote, which is worse than no
    clock at all — so this is imported rather than approximated here.
    """
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    try:
        import report_data
    except ImportError:
        ai("report_data not importable — report will have no transcript")
        return None
    try:
        return report_data.display_transcript(
            f"{base}.clean.json", f"{base}.transcript.json"
        )
    except Exception as exc:  # noqa: BLE001 — a transcript is not worth failing an audit
        ai(f"transcript unavailable: {exc!r}")
        return None


class PipelineError(Exception):
    """A stage failed. Retryable — most causes are transient API errors."""


def stub_audit(audio_path, call_id):
    """A schema-valid audit, derived from the callId so it is stable per call.

    Not random: the same call stubs to the same numbers every time, so a rerun is
    comparable and a test can assert on them. The shape matches what audit_call.py
    writes, because the field mapping below reads both.
    """
    seed = int(hashlib.sha256(str(call_id).encode()).hexdigest()[:8], 16)
    score = round(55 + (seed % 400) / 10, 1)
    flag_count = seed % 4
    severities = ["none", "low", "medium", "high"]
    outcomes = [
        "committed", "interested_no_commitment", "callback_requested",
        "not_interested", "demo_agreed",
    ]

    return {
        "call_id": call_id,
        "schema_version": "2.1.0",
        "generated_by": "backend.audit.pipeline:stub_audit",
        "stub": True,
        "metadata": {
            "call_id": call_id,
            "talk_ratio": round(0.3 + (seed % 50) / 100, 4),
            "total_turns": 20 + (seed % 40),
        },
        "scores": {
            "overall_score_100": score,
            "flag_count": flag_count,
            "disqualified": False,
        },
        "report": {
            "outcome_block": {
                "outcome": outcomes[seed % len(outcomes)],
                "conversion_probability": round((seed % 90) / 100, 2),
                "next_step": "stubbed — no real analysis was run",
            },
            "compliance_flags": [
                {"flag_type": "stub_flag", "severity": severities[min(index + 1, 3)]}
                for index in range(flag_count)
            ],
            "coaching": {"summary": "Stubbed audit. Set the API keys to run for real."},
        },
        "warnings": ["This audit was stubbed; no transcription or scoring ran."],
    }


def dashboard_fields(document):
    """The scalars the calls table carries, projected off the audit document.

    Kept in one function because it is the single place the audit's shape and the
    table's shape have to agree. If audit_call.py changes its schema, this is what
    breaks, and it breaks visibly rather than silently writing nulls.
    """
    scores = document.get("scores") or {}
    report = document.get("report") or {}
    flags = report.get("compliance_flags") or []

    score = scores.get("overall_score_100")
    # audit_call.py sets scores.disqualified when a disqualifying flag is
    # present. Defaulting to False rather than None: an audit that ran and found
    # no misconduct is a definite "not fatal", not an unknown.
    disqualified = bool(scores.get("disqualified", False))

    if disqualified:
        # audit_call.py reports overall_score_100 = 0 for a disqualified call,
        # which is not a score of zero but the absence of one -- the flags void
        # the scorecard before it is totalled. Stored as 0 it is indistinguishable
        # from a genuinely terrible call, and it drags every average, median and
        # section mean it lands in. `disqualified` already carries the meaning,
        # so the number is dropped.
        score = None

    return {
        "score": score,
        "flagCount": scores.get("flag_count", len(flags)),
        # None, not "", so `_update` REMOVEs it. `clean_item` drops an empty
        # string only for index keys, so the update path was writing "" onto
        # every clean call -- present, falsy, and in any severity filter that
        # tests for presence. Absent is what "no flags" is supposed to mean.
        "auditStatus": _audit_status(flags) or None,
        # The only attribute that tells a disqualified call apart from one that
        # was never audited -- both carry no score.
        "disqualified": disqualified,
        # Every metric the scorecard awards, at the finest level the audit
        # produces: the eight criteria, not the six sections. Sections are the
        # sum of their criteria -- verified against every document in the
        # corpus -- so storing the criteria stores both, and `models.hydrate`
        # adds `sectionMarks` back on read. Storing the sections instead threw
        # away the breakdown for the same number of bytes.
        "criterionMarks": scores.get("criterion_marks") or None,
    }


def _audit_status(flags):
    """The highest flag severity on the call: LOW, MEDIUM, HIGH, or "".

    Empty when there are no flags, which `clean_item` turns into an absent
    attribute rather than a "NONE" sentinel. An unrecognised severity is skipped
    rather than raising — a new severity name should not fail an audit that has
    already been paid for.
    """
    worst = -1
    for flag in flags:
        severity = str(flag.get("severity", "")).strip().lower()
        if severity in SEVERITY_ORDER:
            worst = max(worst, SEVERITY_ORDER.index(severity))
    return SEVERITY_ORDER[worst].upper() if worst >= 0 else ""


# ------------------------------------------------------------------------
# the worker
# ------------------------------------------------------------------------


# The only model this job may spend money on. Measured over 100 real audits:
# luna is $0.117 for a 25-minute call and gpt-5.4-mini is $0.381 for identical
# output -- 3.25x for nothing. A typo in AUDIT_MODEL is otherwise invisible
# until the invoice, which is why this is a refusal to start and not a warning.
REQUIRED_MODEL = "gpt-5.6-luna"

# Statuses a message can legitimately find and should be dropped on rather than
# audited. PROCESSED is the duplicate-delivery case; the others are calls
# somebody or something took out of the work list after the message was sent.
TERMINAL = frozenset({
    ProcessingStatus.PROCESSED,
    ProcessingStatus.DISCARDED,
})


class ModelGuardError(Exception):
    """The configured model is not the one this job is costed for."""


def check_models():
    """Refuse to run on anything but `REQUIRED_MODEL`.

    Checked before the first message is taken, not per call: a run that is going
    to refuse should refuse while the queue is untouched, rather than after
    making twenty-five messages invisible for half an hour.
    """
    if settings.allow_any_model:
        warn("batch", f"ALLOW_ANY_MODEL is set — the cost guard is off. "
                      f"audit={settings.audit_model} "
                      f"analysis={settings.analysis_model}")
        return
    wrong = {
        name: value
        for name, value in (("AUDIT_MODEL", settings.audit_model),
                            ("ANALYSIS_MODEL", settings.analysis_model))
        if value != REQUIRED_MODEL
    }
    if wrong:
        raise ModelGuardError(
            "refusing to start: "
            + ", ".join(f"{k}={v!r}" for k, v in wrong.items())
            + f" but this job is costed for {REQUIRED_MODEL!r} only "
              f"(gpt-5.4-mini is 3.25x the price for identical work). "
              f"Set them correctly, or set ALLOW_ANY_MODEL=1 deliberately."
        )
    batch(f"model guard ok: {REQUIRED_MODEL}, pipeline is "
          f"{settings.pipeline_mode}")


class Audited:
    """The counts, written to the run summary. Mutated from every worker."""

    def __init__(self):
        self.audited = 0
        self.failed = 0
        self.skipped = 0        # already PROCESSED, or no longer auditable
        self.retryable = 0      # pipeline error; left UNPROCESSED for next run
        self.not_reached = 0    # still on the queue when the deadline came
        self.failures = []
        # The yyyy-mm-dd days this run changed a score on. Recorded on the run
        # summary: it is what an incremental export -- the ClickHouse sync, when
        # it lands -- needs in order to know which partitions to re-read.
        self.dates = set()
        self._lock = threading.Lock()

    def record(self, outcome, call_id, detail):
        with self._lock:
            if outcome == "audited":
                self.audited += 1
                if detail:
                    self.dates.add(detail)
            elif outcome == "skipped":
                self.skipped += 1
            elif outcome == "retry":
                self.retryable += 1
                self.failures.append((call_id, detail))
            else:
                self.failed += 1
                self.failures.append((call_id, detail))

    def counts(self):
        return {
            "audited": self.audited,
            "auditFailed": self.failed,
            "auditSkipped": self.skipped,
            "auditRetryable": self.retryable,
            "auditNotReached": self.not_reached,
        }


def _store_intermediates(store, call_id, started_at, work_dir):
    """Upload the transcript, clean and analysis files beside the audit.

    Best effort. The audit is already written and the row is about to be marked
    PROCESSED; failing the call because a supporting file did not upload would
    throw away a scored audit -- already paid for -- over a copy of its own
    working notes. Each miss is logged, so a bucket quietly rejecting writes is
    still visible.
    """
    base = os.path.join(work_dir, call_id)
    for kind in ARTIFACTS:
        if kind == "audit":
            continue  # already uploaded, and it is the one that matters
        path = f"{base}.{kind}.json"
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                store.put_json(
                    artifact_key(call_id, started_at, kind),
                    json.load(handle),
                    metadata={"callid": call_id},
                )
        except Exception as exc:  # noqa: BLE001 -- see the docstring
            warn("batch", f"could not store the {kind} artifact: {exc!r}")


def audit_one(call_id, repository=None, store=None, run_id=""):
    """Audit one call. Returns ("audited"|"skipped"|"retry"|"failed", detail).

    Never raises for an ordinary failure. A worker thread calls this in a loop
    and an exception escaping would take the worker out for the rest of the run,
    so the outcome is returned and counted instead.

    The four outcomes differ only in what the row is left saying, which is what
    the next run reads:

        audited   PROCESSED, artifacts in S3, out of the work list
        skipped   nothing written -- the call was not ours to audit
        retry     back to UNPROCESSED, so the *next* run queues it again, after
                  the upstream API has had time to recover. Not retried inside
                  this run: an immediate retry is the worst moment to ask again.
        failed    FAILED with the reason on it, which is where a person looks
    """
    repository = repository or calls_repo
    store = store or object_store

    row = repository.get(call_id)
    if row is None:
        # The index is eventually consistent, so it can name a row that has
        # since been deleted. Nothing to do and nothing wrong.
        return "skipped", "no row"

    status = row.get("processingStatus")
    if status in TERMINAL:
        # A second pod audited it between that pod's enqueue and this one's.
        # This read is the whole reason that is harmless, and it happens before
        # anything is downloaded or any model is called.
        return "skipped", f"already {status}"

    audio_key = row.get("audioKey")
    if not audio_key:
        repository.update(
            call_id,
            processingStatus=ProcessingStatus.FAILED,
            failureReason="queued for audit but the row has no audioKey",
            statusAt=iso(utcnow()),
        )
        return "failed", "no audioKey"

    # A directory per call, so two audits in flight cannot collide over the
    # intermediate files the pipeline stages write next to the audio.
    work_dir = os.path.join(settings.work_dir, call_id)
    os.makedirs(work_dir, exist_ok=True)
    staged = os.path.join(work_dir, f"{call_id}.wav")

    try:
        if not store.download_to(audio_key, staged):
            repository.update(
                call_id,
                processingStatus=ProcessingStatus.FAILED,
                failureReason=f"audio missing at {audio_key}",
                statusAt=iso(utcnow()),
            )
            return "failed", f"audio missing at {audio_key}"

        document, mode = run_pipeline(staged, call_id, work_dir)

        started = row.get("startedAt", "")
        key = audit_key_for(call_id, started)
        store.put_json(key, document, metadata={"callid": call_id})
        _store_intermediates(store, call_id, started, work_dir)

        fields = dashboard_fields(document)
        repository.update(
            call_id,
            processingStatus=ProcessingStatus.PROCESSED,
            statusAt=iso(utcnow()),
            # Cleared on success. A call that failed and then succeeded should
            # not carry the old reason forever -- it reads as a broken call on
            # every dashboard that shows the field.
            failureReason=None,
            **fields,
        )
        audit_log(f"PROCESSED ({mode}) score={fields['score']} "
                  f"flags={fields['flagCount']} "
                  f"band={score_band(fields['score']) or '-'}"
                  + (" DISQUALIFIED" if fields["disqualified"] else ""))
        return "audited", (started or "")[:10]

    except PipelineError as exc:
        # A stage exited non-zero. Usually a transient API error, so the call is
        # left where the next run will find it: UNPROCESSED, which is exactly
        # what the enqueue step reads. Not retried inside this run, because the
        # thing that failed is almost always an upstream API having a bad
        # minute, and an immediate retry is the worst time to ask it again.
        repository.update(
            call_id,
            processingStatus=ProcessingStatus.UNPROCESSED,
            failureReason=f"pipeline: {exc}"[:400],
        )
        error("batch", f"pipeline failed, left UNPROCESSED for the next run: "
                       f"{exc}")
        return "retry", f"pipeline: {exc}"
    except Exception as exc:  # noqa: BLE001 -- one call must not end the run
        repository.update(
            call_id,
            processingStatus=ProcessingStatus.FAILED,
            failureReason=f"{type(exc).__name__}: {exc}"[:400],
            statusAt=iso(utcnow()),
        )
        error("batch", f"audit failed: {exc!r}", exc_info=True)
        return "failed", f"{type(exc).__name__}: {exc}"
    finally:
        if not settings.keep_workdir:
            shutil.rmtree(work_dir, ignore_errors=True)


def _worker(queue, repository, store, result, deadline, run_id, run_tag):
    """One worker thread: take a job, audit it, repeat until the queue is empty.

    Each worker takes from the queue for itself rather than being handed a slice
    of a list. That keeps a pod whose calls happen to be short from sitting idle
    while another worker grinds through twenty-five long ones, and it means the
    number of workers is a tuning knob rather than a partitioning scheme.
    """
    set_run_id(run_tag or run_id)

    while True:
        if deadline and utcnow() >= deadline:
            # The remaining jobs are counted and dropped once by the caller, not
            # once per worker -- twenty-five threads each reporting the same
            # backlog would be twenty-five identical warnings.
            return

        job = queue.take()
        if job is None:
            return

        with call_context(job.call_id):
            try:
                outcome, detail = audit_one(job.call_id, repository, store, run_id)
            except Exception as exc:  # noqa: BLE001 -- audit_one catches its
                # own; this is the thread itself failing, e.g. out of memory.
                outcome, detail = "failed", f"worker crashed: {exc!r}"
                error("batch", f"audit worker crashed: {exc!r}")
            result.record(outcome, job.call_id, detail)


def run(repository=None, store=None, queue=None, deadline=None,
        concurrency=None, run_id=""):
    """Drain the queue with `AUDIT_CONCURRENCY` workers, or until the deadline."""
    repository = repository or calls_repo
    store = store or object_store
    queue = queue or job_queue
    result = Audited()
    check_models()

    waiting = queue.depth()
    if not waiting:
        batch("audit: the queue is empty — nothing to do")
        return result

    workers = min(concurrency or settings.audit_concurrency, waiting)
    batch(f"audit: {waiting} job(s) queued, {workers} worker(s)"
          + (f", nothing new started after {iso(deadline)}" if deadline else ""))

    run_tag = current_run_id()
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="audit") as pool:
        for _ in range(workers):
            pool.submit(_worker, queue, repository, store, result,
                        deadline, run_id, run_tag)

    # Anything still queued means the deadline stopped the workers. Dropped
    # here, once: the rows are UNPROCESSED, so the next run enqueues them from
    # the table exactly as this one did.
    result.not_reached = queue.depth()
    if result.not_reached:
        warn("batch", f"drain margin reached — {result.not_reached} call(s) were "
                      f"never started. They stay UNPROCESSED and are the first "
                      f"thing the next run queues.")
        queue.clear()

    batch(f"audit done: {result.audited} audited, {result.failed} failed, "
          f"{result.retryable} left for the next run, "
          f"{result.skipped} skipped, {result.not_reached} not reached")
    return result
