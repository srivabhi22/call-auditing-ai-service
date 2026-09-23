"""Steps 1 to 3 — which day to fetch, fetch it, then sort what came back.

One fixed business day, in tenant time:

    end   = the most recent 02:00 IST (within BATCH_DAY_BOUNDARY_GRACE_MIN)
    start = end - 24 hours

A pod that starts at 02:00 on the 21st asks for 02:00 on the 20th to 02:00 on
the 21st. So does one that starts at 02:30, at 09:00 or at 23:00 that day, and
so does one that fires a few minutes early at 01:58 -- see the grace below.
Nothing is stored between runs and nothing is computed from when the last run
happened.

**Why a fixed day rather than a checkpoint.** A checkpoint is one more piece of
state that has to be correct: it has to be written only when the fetch was
complete, it has to be read before the window can be decided, and when it is
wrong -- or a backfill moves it -- the job silently fetches the wrong period.
Asking for the same calendar day every time cannot drift. Re-fetching a day the
job already has costs nothing, because step 3 asks DynamoDB which call ids it
already holds and does nothing with those, so a day that was fully audited
yesterday produces a run that downloads nothing and audits nothing.

**Why 02:00 and not midnight.** It is past the end of the sales floor's day, so
a day's calls are all in the log by the time the window closes -- Cloud Connect
write the call log row from a different system than the one that ends the call,
and it appears seconds to minutes later. A window ending at midnight would close
while the last calls of the evening were still being written, and because the
window never comes back to them they would never be fetched at all.

**What a missed run costs.** Exactly the day it missed. A checkpoint would have
carried the gap forward and re-fetched it on the next run; this does not, so a
day nothing ran for needs `--window-start`/`--window-end` to pick it up. That is
the trade: one command after an outage somebody already knows about, against a
piece of state that has to be right on every ordinary night.

Everything here is UTC-aware. Cloud Connect's API wants tenant-local times in
the URL path, but that conversion belongs at the edge that talks to them
(`collect.py`); the arithmetic is done against `models.TENANT_TZ` so "02:00" is
02:00 where the calls happened, not 02:00 UTC seven and a half hours out.

── Steps 2 and 3 ────────────────────────────────────────────────────────────

Step 2 asks Cloud Connect for one slice at a time and joins the results. Step 3
puts every row in one of four piles:

    already known    the callId is in DynamoDB -- ignore it
    off roster       the agent extension is not one of ours. Cloud Connect's
                     call log is the whole tenant's, so most of a day's rows
                     belong to other departments, IVR legs and test handsets.
                     Dropped with no row: the test is a dict lookup, so
                     re-checking it tomorrow costs nothing.
    not auditable    never answered, or no recording -- store a SKIPPED row so
                     tomorrow's run does not rediscover and re-check it
    to do            a real recorded call nobody has seen. The work list.

Neither step spends money: no downloads, no transcription, no model calls. That
is why `--dry-run` stops here and why the counts are written to the run summary
*before* step 4 starts -- a window that was wrong is then visible in seconds,
not after a few hundred dollars of transfer.

**Two things about talking to their API.**

Their timestamps carry no offset and are tenant-local (IST). The window is UTC
throughout this package, so the conversion happens here, at the edge, once --
`models.TENANT_TZ` is the single constant it depends on.

Their call log has no pagination and no truncation signal. A slice that returns
suspiciously close to a round number, or more rows than an hour of this tenant's
traffic can plausibly hold, is logged as a warning: it is the only symptom a
silent cut-off has, and section 10 of the architecture doc lists confirming it
as an open question.
"""

import logging
import time
from collections import Counter
from datetime import timedelta

from ..common.agents import Agent, directory as file_directory
from ..common.config import settings
from ..common.models import (
    CallRow, ProcessingStatus, TENANT_TZ, iso, parse_iso, utcnow,
)
from ..common.store import calls as calls_repo, directory_table
from ..common.trace import batch, trace, warn, error
from .cloudconnect import client as cc_client, RetryableError, PermanentError


# ------------------------------------------------------------------------
# step 1 — the day
# ------------------------------------------------------------------------





class Window:
    """A half-open period [start, end), and the slices it fetches as."""

    def __init__(self, start, end, source):
        self.start = start
        self.end = end
        # How the bounds were arrived at: "day" or "explicit". On the run
        # summary row this is what distinguishes an ordinary night from a run
        # that was told where to look.
        self.source = source

    @property
    def is_empty(self):
        """Nothing to do. Only reachable from `--window-start`/`--window-end`
        given backwards, since the daily window is always 24 hours wide."""
        return self.start >= self.end

    @property
    def hours(self):
        return (self.end - self.start).total_seconds() / 3600

    def slices(self, minutes=None):
        """The window cut into fetch-sized pieces, oldest first.

        Not an optimisation. The call log API has no pagination and no way to
        say a result was cut short: ask for a day with 3,000 calls in it and it
        either returns all of them or returns some, with `status: SUCCESS`
        either way. An hour is ~125 calls at peak, comfortably under anything
        that could truncate, and a slice that fails costs an hour rather than
        the whole day.
        """
        step = timedelta(minutes=minutes or settings.slice_minutes)
        cursor = self.start
        while cursor < self.end:
            yield cursor, min(cursor + step, self.end)
            cursor += step

    def describe(self):
        return (f"{iso(self.start)} -> {iso(self.end)} "
                f"({self.hours:.1f}h, {self.source})")


def day_bounds(now=None, hour=None):
    """The most recent complete `hour`-to-`hour` day.

    Both ends come back tz-aware, on the tenant clock. The boundary is computed
    in tenant time and not in UTC, which is the difference between a window that
    starts at 02:00 where the calls happened and one that starts at 07:30 there.
    """
    hour = settings.day_start_hour if hour is None else hour
    local = (now or utcnow()).astimezone(TENANT_TZ)
    end = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    # The grace is what makes a scheduler set to 02:00 reliable. Firing a
    # fraction early, or a pod clock a second behind, would otherwise move the
    # window a whole day back -- and nothing carries that gap forward, so the
    # day would simply never be audited. Inside the grace the window ends a few
    # minutes in the future, which at this hour is a floor that closed long ago.
    grace = timedelta(minutes=settings.day_boundary_grace_min)
    if end > local + grace:
        # Genuinely before the boundary -- a pod that started at 01:00. The
        # most recent complete day is the one that ended yesterday.
        end -= timedelta(days=1)
    return end - timedelta(days=1), end


def decide(window_start=None, window_end=None, now=None):
    """The period this run fetches.

    `window_start` / `window_end` are the backfill path and override everything:
    a day that was missed, or one whose slices failed, is re-run by naming it.
    Neither is capped -- somebody typing a range has a reason, and a cap that
    silently shortened it would make the run look complete while it was not.
    """
    if window_start or window_end:
        start = parse_iso(window_start)
        end = parse_iso(window_end) or utcnow()
        if start is None:
            # An end with no start is almost always a typo, and guessing a start
            # would fetch a period nobody asked for.
            raise ValueError("--window-end needs --window-start")
        period = Window(start, end, "explicit")
        warn("batch", f"explicit window {period.describe()} — the daily "
                      f"{settings.day_start_hour:02d}:00 boundary is ignored "
                      f"for this run")
        return period

    start, end = day_bounds(now)
    period = Window(start, end, "day")
    batch(f"step 1: window {period.describe()} — "
          f"{settings.day_start_hour:02d}:00 to "
          f"{settings.day_start_hour:02d}:00, tenant time")
    return period


# ------------------------------------------------------------------------
# steps 2 and 3 — fetch it, sort it
# ------------------------------------------------------------------------




# A slice is retried this many times before the run gives up on it. Each failed
# slice is an hour of the day that was never fetched, and nothing comes back for
# it on its own -- so this is deliberately generous: re-reading an hour is free
# and missing one needs somebody to notice and re-run the day.
SLICE_ATTEMPTS = 3
SLICE_BACKOFF_SEC = 5

# Above this many rows in one slice, say so. An hour of this tenant's traffic is
# ~125 calls at peak. Several hundred means either a genuinely exceptional hour
# or a slice wider than intended; either way it is worth a line, because a
# truncated result and a busy hour look identical in the response.
SLICE_SUSPICIOUS_ROWS = 400

# How many call ids to ask DynamoDB about in one go. BatchGetItem's hard limit
# is 100 keys. One GetItem per call also works and costs the same in capacity,
# but 3,000 sequential round trips is half a minute of latency for an answer
# that arrives in two.
BATCH_GET_SIZE = 100


class AgentCache:
    """Extension -> Agent, read once per run.

    Step 4 resolves an agent for every call in the window. Against the
    `directory` table that is one GSI-1 query per call -- 3,000 reads of a
    table with eight rows in it. The roster changes on the order of once a
    month, so it is loaded whole, once, and served from a dict.

    `agents.json` is the fallback, not the primary: the table is what a deploy
    updates and the file is what a developer edits, and a run that cannot reach
    the table should still file calls under the right people rather than under
    "Unmapped extension 706".
    """

    def __init__(self, table=None):
        self._table = table if table is not None else directory_table
        self._by_extension = {}
        self._loaded = False
        self.misses = set()

    def load(self):
        if self._loaded:
            return
        self._loaded = True
        if self._table is None:
            batch("roster: no directory table (local backend) — using agents.json")
            return
        try:
            rows = self._table.load_all()
        except Exception as exc:  # noqa: BLE001 -- see the class docstring
            warn("batch", f"could not read the directory table ({exc!r}) — "
                          f"falling back to agents.json for this run")
            return
        for row in rows:
            if row.get("entity") != "AGENT":
                continue
            extension = str(row.get("extension") or "").strip()
            if not extension:
                continue
            self._by_extension[extension] = Agent(
                extension=extension,
                name=row.get("agentName") or "",
                did=row.get("did") or "",
                accountId=row.get("accountId") or "",
                known=True,
                tl=row.get("teamLeaderName") or "",
                state=row.get("team") or "",
            )
        batch(f"roster: {len(self._by_extension)} agents cached from the "
              f"directory table")

    def rostered(self, extension):
        """Whether this extension is one of ours, by extension number alone.

        Deliberately not `resolve(...).known`. `resolve` falls back to the DID,
        and a DID can be shared by a hunt group -- so a call from an extension
        nobody has listed can match one and come back `known`, which is fine for
        naming a call and wrong for deciding whether the call is ours at all.

        The roster is the directory table when it loaded and agents.json when it
        did not, which is the same precedence `resolve` uses: the table is what
        a deploy updates and the file is what a developer edits.
        """
        self.load()
        extension = str(extension or "").strip()
        if not extension:
            return False
        if extension in self._by_extension:
            return True
        # No `did` passed, so this is the extension/accountId lookup and never
        # the DID fallback.
        return file_directory.resolve(extension).known

    def resolve(self, extension, did=""):
        self.load()
        agent = self._by_extension.get(str(extension or "").strip())
        if agent is not None:
            return agent
        # Not in the table. The file directory knows about accountId and DID
        # fallbacks and produces the "Unmapped extension NNN" placeholder, which
        # is what keeps an unknown extension visible on the dashboard instead of
        # dropping the call.
        resolved = file_directory.resolve(extension, did)
        if not resolved.known:
            self.misses.add(str(extension or "?"))
        return resolved


class Collected:
    """What steps 3 and 4 produced."""

    def __init__(self):
        self.fetched = 0            # rows Cloud Connect returned, after dedup
        self.duplicates = 0         # the same callId in two overlapping slices
        self.already_known = 0
        # Calls whose agent extension is not on the roster. Cloud Connect's call
        # log covers every extension on the tenant -- other departments, IVR
        # legs, test handsets -- and a call that is not one of our agents' is
        # not ours to download, store or audit. Dropped outright rather than
        # stored SKIPPED: a SKIPPED row is a permanent answer worth 200 bytes
        # for a call we might otherwise re-check, and this test is a dict lookup
        # that costs nothing to repeat on every run.
        self.off_roster = 0
        self.skipped = 0
        self.skipped_written = 0
        self.todo = []              # [(CallLogRecord, Agent)] -- the work list
        self.failed_slices = []     # [(start, end, reason)]
        self.capped = False

    def counts(self):
        return {
            "fetched": self.fetched,
            "duplicates": self.duplicates,
            "alreadyKnown": self.already_known,
            "offRoster": self.off_roster,
            "skipped": self.skipped,
            "toDo": len(self.todo),
            "failedSlices": len(self.failed_slices),
        }


# -- step 2 ---------------------------------------------------------------

def _to_tenant_local(moment):
    """A UTC instant as the naive local time their URL path wants.

    Their API has no offset anywhere in it and reads every timestamp as tenant
    time. Sending UTC would quietly shift every window by five and a half hours
    -- which does not fail, it just fetches the wrong calls.
    """
    return moment.astimezone(TENANT_TZ).replace(tzinfo=None)


def _fetch_slice(client, start, end):
    """One slice, with retries. Raises the last error if all attempts fail."""
    last = None
    for attempt in range(1, SLICE_ATTEMPTS + 1):
        try:
            rows = client.fetch_call_logs_between(
                _to_tenant_local(start), _to_tenant_local(end)
            )
            if len(rows) >= SLICE_SUSPICIOUS_ROWS:
                warn("batch", f"slice {iso(start)}..{iso(end)} returned "
                              f"{len(rows)} rows. Their call log has no "
                              f"pagination and no truncation signal, so a "
                              f"result this large may be incomplete — consider "
                              f"lowering BATCH_SLICE_MINUTES.")
            return rows
        except PermanentError:
            # Bad credentials or a malformed request. Retrying spends attempts
            # on something that cannot start working, so it goes straight up.
            raise
        except RetryableError as exc:
            last = exc
            if attempt < SLICE_ATTEMPTS:
                pause = SLICE_BACKOFF_SEC * (2 ** (attempt - 1))
                warn("batch", f"slice {iso(start)}..{iso(end)} attempt "
                              f"{attempt}/{SLICE_ATTEMPTS} failed ({exc}) — "
                              f"retrying in {pause}s")
                time.sleep(pause)
    raise last


def fetch(window, client=None):
    """Step 2. Every call in the window, one slice at a time.

    Returns `(records, failed_slices)`. A slice that could not be fetched after
    its retries does not abort the run -- the other twenty-three hours are still
    worth ingesting -- but those calls are simply not fetched, and nothing goes
    back for them unless the day is re-run.
    """
    client = client or cc_client
    slices = list(window.slices())
    batch(f"step 2: fetching {len(slices)} slice(s) of "
          f"{settings.slice_minutes} min across {window.describe()}")

    records, failed = [], []
    for index, (start, end) in enumerate(slices, 1):
        try:
            rows = _fetch_slice(client, start, end)
        except PermanentError as exc:
            # Every subsequent slice will fail the same way. Stopping here
            # rather than grinding through 24 of them makes the cause the first
            # thing in the log instead of the twenty-fourth.
            error("batch", f"slice {index}/{len(slices)} failed permanently: "
                           f"{exc}. Stopping collection — the remaining slices "
                           f"would fail identically.")
            failed.append((iso(start), iso(end), str(exc)))
            break
        except RetryableError as exc:
            error("batch", f"slice {index}/{len(slices)} {iso(start)}..{iso(end)} "
                           f"gave up after {SLICE_ATTEMPTS} attempts: {exc}")
            failed.append((iso(start), iso(end), str(exc)))
            continue

        batch(f"  slice {index}/{len(slices)} {iso(start)}..{iso(end)}: "
              f"{len(rows)} rows")
        records.extend(rows)

    batch(f"step 2 done: {len(records)} rows, {len(failed)} slice(s) failed")
    return records, failed


# -- step 3 ---------------------------------------------------------------

def _known_call_ids(repository, call_ids):
    """Which of these are already in the table, as a set.

    BatchGetItem projecting only `PK`, so nothing is read that is then thrown
    away. Unprocessed keys are retried with backoff rather than dropped: an
    unprocessed key silently treated as "not known" re-ingests a call that is
    already stored, which `put_if_absent` would reject anyway -- but only after
    the recording had been downloaded again.
    """
    if not call_ids:
        return set()

    table = repository.table
    client = table.meta.client
    known = set()
    ordered = list(call_ids)

    for index in range(0, len(ordered), BATCH_GET_SIZE):
        chunk = ordered[index:index + BATCH_GET_SIZE]
        request = {
            table.name: {
                "Keys": [{"PK": f"CALL#{c}", "SK": "META"} for c in chunk],
                "ProjectionExpression": "PK",
            }
        }
        for attempt in range(5):
            response = client.batch_get_item(RequestItems=request)
            for item in response.get("Responses", {}).get(table.name, []):
                known.add(item["PK"].split("#", 1)[1])
            request = response.get("UnprocessedKeys") or {}
            if not request:
                break
            # DynamoDB returns unprocessed keys under throttling. Backing off is
            # the documented response; hammering the same keys makes it worse.
            time.sleep(0.2 * (2 ** attempt))
        else:
            warn("batch", f"{len(request.get(table.name, {}).get('Keys', []))} "
                          f"key(s) still unprocessed after 5 attempts — those "
                          f"calls will be treated as new and rejected by "
                          f"put_if_absent if they are not")
    return known


def sort_records(records, repository=None, agents=None, dry_run=False,
                 max_calls=None):
    """Step 3. The three-way split.

    `dry_run` does every read and no write, so the counts are exactly what a
    real run would produce -- including the SKIPPED pile, which it reports but
    does not store.
    """
    repository = repository or calls_repo
    agents = agents or AgentCache()
    max_calls = max_calls or settings.max_calls_per_run
    result = Collected()
    # Why each skipped call was skipped, counted rather than listed. At 3,000
    # calls a day the per-call line is ~1,000 lines of "Unanswered" around the
    # handful an operator actually needs; the breakdown says the same thing in
    # one line and the detail is still there at DEBUG.
    skip_reasons = Counter()
    # Which extensions the roster filter dropped, and how many each. Counted
    # rather than listed for the same reason: it is the one line that says
    # whether the filter is doing what it should or eating the floor's calls.
    off_roster_ext = Counter()

    # Overlapping slices and the 5-minute rewind both re-deliver calls, so the
    # same callId legitimately arrives more than once. Deduped here rather than
    # left to `put_if_absent`, because a duplicate that reaches step 4 has its
    # recording downloaded before the write that rejects it.
    unique = {}
    for record in records:
        call_id = record.call_id
        if not call_id:
            warn("batch", f"call log row with no callid "
                          f"(token={record.unique_token or '-'}) — ignoring it")
            continue
        if call_id in unique:
            result.duplicates += 1
            continue
        unique[call_id] = record
    result.fetched = len(unique)

    batch(f"step 3: sorting {result.fetched} distinct call(s) "
          f"({result.duplicates} duplicate row(s) dropped)")

    known = _known_call_ids(repository, list(unique))
    batch(f"  {len(known)} of them are already in the table")

    for call_id, record in unique.items():
        if call_id in known:
            result.already_known += 1
            continue

        # The roster filter, before anything else looks at the call. Tested on
        # `agent_extension`, which is direction-aware -- `caller` on an outbound
        # call and `callee` on an inbound one -- so this is "made by or made to
        # one of our agents" and not "made by".
        #
        # Ahead of `resolve` on purpose: resolving first would file every one of
        # these under an "Unmapped extension NNN" placeholder and put it in
        # `agents.misses`, which drives a warning telling somebody to add it to
        # the roster. These are not missing from the roster; they are not ours.
        if not agents.rostered(record.agent_extension):
            result.off_roster += 1
            off_roster_ext[record.agent_extension or "?"] += 1
            trace("batch", f"  NOT OURS {call_id}: ext="
                           f"{record.agent_extension or '-'} is not on the "
                           f"roster ({record.direction or '-'}, "
                           f"{record.call_sec}s)",
                  level=logging.DEBUG)
            continue

        agent = agents.resolve(record.agent_extension, record.did)

        if not record.is_auditable:
            reason = record.call_status or "unknown status"
            if not record.recording_url:
                reason += ", no recording"
            result.skipped += 1
            skip_reasons[reason] += 1
            if not dry_run:
                row = CallRow.from_call_log(
                    record, agent, ProcessingStatus.SKIPPED
                )
                # Stored rather than dropped. A call that is simply absent is
                # one this step rediscovers and re-checks on every run, forever;
                # a SKIPPED row is a permanent answer that costs 200 bytes.
                if repository.put_if_absent(row):
                    result.skipped_written += 1
            trace("batch", f"  SKIP {call_id}: {reason} ({record.call_sec}s, "
                           f"ext={record.agent_extension})",
                  level=logging.DEBUG)
            continue

        result.todo.append((record, agent))

    if len(result.todo) > max_calls:
        result.capped = True
        warn("batch",
             f"the window holds {len(result.todo)} calls to ingest, over the "
             f"BATCH_MAX_CALLS_PER_RUN cap of {max_calls}. Taking the oldest "
             f"{max_calls} and leaving the rest, which have no row and are "
             f"so the next run starts from the same place and takes the next "
             f"batch. If this is not a backfill, the window is wrong.")
        result.todo.sort(key=lambda pair: pair[0].started_at_iso)
        result.todo = result.todo[:max_calls]

    if agents.misses:
        warn("batch", f"{len(agents.misses)} extension(s) are not in the "
                      f"roster and their calls are filed under a placeholder: "
                      f"{', '.join(sorted(agents.misses)[:10])}. Add them to "
                      f"agents.json and re-sync the directory table.")

    if result.off_roster:
        batch(f"  off roster — {result.off_roster} call(s) across "
              f"{len(off_roster_ext)} extension(s), dropped: "
              + ", ".join(f"{ext}x{n}"
                          for ext, n in off_roster_ext.most_common(15))
              + (", ..." if len(off_roster_ext) > 15 else ""))

    for reason, count in skip_reasons.most_common():
        batch(f"  not auditable — {reason}: {count}")

    batch(f"step 3 done: {result.already_known} already known, "
          f"{result.off_roster} off roster, "
          f"{result.skipped} not auditable"
          f"{'' if dry_run else f' ({result.skipped_written} rows written)'}, "
          f"{len(result.todo)} to ingest")
    return result
