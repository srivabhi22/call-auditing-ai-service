"""Step 4 — download each recording and put it where the audit step can find it.

Per call, in this order and no other:

    1. write the row as INGESTING
    2. stream the recording from Cloud Connect to a temporary file
    3. upload it to S3 under audioKey
    4. flip the row to UNPROCESSED

*Why the row goes first.* If the pod dies mid-download there is still a record
that this call exists and was half done. Written after the upload instead, a
call that died in between is simply absent -- and step 3 of the next run has no
way to tell it apart from a call nobody has ever seen, so it downloads it again.
A pod killed between the two leaves the row INGESTING with no audio behind it,
which is what `dispatch._fail_stalled_ingests` exists to clean up: nothing else
ever revisits an INGESTING row, and one left alone is a call that is neither
audited nor visible as a problem.

*Why four at a time.* A download is network wait, not CPU, so serial ingestion
spends almost all of its time doing nothing. Four rather than more because at
peak this pulls ~97 GB a day from a partner's server, their download endpoint
historically served one call at a time, and how it behaves under a burst is an
open question (architecture §10). Four keeps the run moving without being the
thing that takes their server down.

*Why one bad recording cannot stop the run.* Three attempts with widening gaps,
then that one call is marked FAILED with the reason on the row and the pool
moves to the next. A 3,000-call run that aborts on call 40 because one wav is
truncated has cost a whole night to save one call.
"""

import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..common.config import settings
from ..common.models import CallRow, ProcessingStatus, iso, utcnow
from ..common.store import calls as calls_repo, objects as object_store
from ..common.trace import (
    batch, call_context, current_run_id, set_run_id, warn, error,
)
from .cloudconnect import client as cc_client, RetryableError, PermanentError


class Ingested:
    def __init__(self):
        self.ingested = 0
        self.failed = 0
        self.claimed_elsewhere = 0
        self.not_reached = 0        # deadline hit before these were started
        self.failures = []          # [(callId, reason)] for the run summary
        self.bytes = 0

    def counts(self):
        return {
            "ingested": self.ingested,
            "ingestFailed": self.failed,
            "ingestClaimedElsewhere": self.claimed_elsewhere,
            "ingestNotReached": self.not_reached,
            "ingestBytes": self.bytes,
        }


def _download_with_retries(client, record, sink_path, attempts, backoff):
    """Stream one recording to disk, retrying what is worth retrying.

    `RetryableError` covers a 404 -- which from their PBX usually means the wav
    is still being written rather than that it is gone -- a 5xx, a timeout and a
    zero-byte body. `PermanentError` does not come back here: a 403 on the
    recording path will be a 403 on the third attempt too, and if the endpoint
    turns out to be IP-allowlisted (§10) every call fails this way, which should
    be visible immediately rather than after three times the wait.
    """
    last = None
    for attempt in range(1, attempts + 1):
        with open(sink_path, "wb") as sink:
            try:
                return client.download_recording(record.recording_url, sink)
            except PermanentError:
                raise
            except RetryableError as exc:
                last = exc
                if attempt < attempts:
                    pause = backoff * (2 ** (attempt - 1))
                    warn("batch", f"download attempt {attempt}/{attempts} "
                                  f"failed ({exc}) — retrying in {pause}s")
                    time.sleep(pause)
    raise last


def ingest_one(record, agent, repository=None, store=None, client=None):
    """One call, start to finish. Returns ("ingested"|"failed"|"taken", detail).

    Never raises for an ordinary failure. The pool calls this 3,000 times and
    an exception escaping would cancel the batch it is in; the outcome is
    returned instead so the caller can count it and carry on.
    """
    repository = repository or calls_repo
    store = store or object_store
    client = client or cc_client

    call_id = record.call_id
    row = CallRow.from_call_log(
        record, agent, ProcessingStatus.INGESTING
    )

    # The conditional write is the whole of dedup and the whole of the
    # two-pods-one-call guard. Losing it means another pod already has this
    # call, which is the normal outcome of running a second collector and is
    # not worth a warning.
    if not repository.put_if_absent(row):
        return "taken", "already in the table"

    os.makedirs(settings.work_dir, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(suffix=".wav", dir=settings.work_dir)
    os.close(handle)

    try:
        size = _download_with_retries(
            client, record, temp_path,
            settings.ingest_max_attempts, settings.ingest_backoff_sec,
        )
        # put_file has move semantics: it uploads and unlinks the source, which
        # is what should happen to a staged temporary copy.
        store.put_file(row.audioKey, temp_path, metadata={
            "callid": call_id,
            "unique-token": record.unique_token or "",
        })
        repository.set_status(call_id, ProcessingStatus.UNPROCESSED,
                              statusAt=iso(utcnow()))
        batch(f"ingested {size / 1024:.0f} KB -> {row.audioKey}, now UNPROCESSED")
        return "ingested", size

    except Exception as exc:  # noqa: BLE001 -- see the docstring
        # The row already exists, so it is left visible as FAILED rather than
        # deleted. A deleted row is one step 3 rediscovers and re-downloads on
        # every run forever; a FAILED one is a call somebody can look at, with
        # the reason on it.
        reason = f"{type(exc).__name__}: {exc}"
        repository.update(
            call_id,
            processingStatus=ProcessingStatus.FAILED,
            failureReason=reason[:400],
            statusAt=iso(utcnow()),
        )
        error("batch", f"ingest failed: {reason}")
        return "failed", reason
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def run(todo, repository=None, store=None, client=None, deadline=None,
        concurrency=None):
    """Step 4 over the whole work list, `DOWNLOAD_CONCURRENCY` at a time.

    `deadline` is a datetime past which no new download is started. The ones in
    flight finish; anything not reached keeps no row at all and is simply
    re-collected next run, because nothing was written for it.
    """
    result = Ingested()
    if not todo:
        batch("step 4: nothing to ingest")
        return result

    workers = concurrency or settings.download_concurrency
    batch(f"step 4: ingesting {len(todo)} call(s), {workers} at a time")

    # The run id is re-set inside each worker rather than carried by a copied
    # Context. `copy_context()` returns one object and `Context.run` refuses to
    # be entered twice concurrently -- with a pool of four it raises "context
    # is already entered" on the second call, which is how this was found.
    run_tag = current_run_id()

    def work(record, agent):
        set_run_id(run_tag)
        with call_context(record.call_id):
            return ingest_one(record, agent, repository, store, client)

    started = 0
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="ingest") as pool:
        futures = {}
        for record, agent in todo:
            if deadline and utcnow() >= deadline:
                # Submitting the rest and cancelling them later would still
                # write their INGESTING rows. Not starting them leaves nothing
                # behind at all, which is the state the next run handles best.
                result.not_reached = len(todo) - started
                warn("batch", f"deadline reached — {result.not_reached} call(s) "
                              f"not started. They have no row, so the next run "
                              f"collects them again from the same window.")
                break
            futures[pool.submit(work, record, agent)] = record.call_id
            started += 1

        for future in as_completed(futures):
            call_id = futures[future]
            try:
                outcome, detail = future.result()
            except Exception as exc:  # noqa: BLE001 -- a worker must not be
                # able to take the run down; ingest_one already catches its own.
                result.failed += 1
                result.failures.append((call_id, f"worker crashed: {exc!r}"))
                error("batch", f"{call_id}: ingest worker crashed: {exc!r}")
                continue
            if outcome == "ingested":
                result.ingested += 1
                result.bytes += int(detail or 0)
            elif outcome == "taken":
                result.claimed_elsewhere += 1
            else:
                result.failed += 1
                result.failures.append((call_id, detail))

    batch(f"step 4 done: {result.ingested} ingested "
          f"({result.bytes / 1024 / 1024:.0f} MB), {result.failed} failed, "
          f"{result.claimed_elsewhere} already taken, "
          f"{result.not_reached} not reached")
    return result
