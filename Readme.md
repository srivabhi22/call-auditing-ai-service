# call_auditing

Scores sales calls. A batch job: the scheduler starts a pod, the job runs once
from start to finish, the pod exits. No webhooks and nothing up between runs —
the job queue lives in the process and is filled and drained inside the run.

```
python -m backend.batch run
```

It fetches yesterday's calls from Cloud Connect, downloads the recordings,
transcribes and audits each one with an AI pipeline, and writes a scored row to
DynamoDB and the audit document to S3.

**Nothing here reads the data back.** The dashboard that renders it is the
`/call-auditing` screen in the **ml-dashboard** repo, which reads the same two
tables and the same bucket directly. This side holds the Cloud Connect token and
the model keys; the side people actually open holds neither.

The split is deliberate and the coupling between them is narrow, but it is real
and mostly invisible — see [What the read side depends on](#what-the-read-side-depends-on)
before changing the row, an index projection or an S3 key.

---

## The flow

```
                          ┌───────────────────────────┐
                          │       Cloud Connect       │
                          │  call log API · recordings│
                          └─────────────┬─────────────┘
                                        │
 ┌──────────────────────────────────────┼───────────────────────────────────────┐
 │  ONE POD — starts on a schedule, does all of this, exits                      │
 │                                      │                                       │
 │  PRODUCE                             ▼                                       │
 │   ① window     the last complete 02:00→02:00 day, tenant time (no state)      │
 │   ② fetch      the call log for that day, one hour per request                │
 │   ③ sort       already known │ not auditable │ to do                          │
 │   ④ ingest     recording ────────────────────────────▶  S3   audio/…          │
 │                row ──────────────────────────────────▶  DynamoDB              │
 │                                                          UNPROCESSED          │
 │   ⑤ dispatch   ask DynamoDB for every UNPROCESSED row                         │
 │                   ├─ leftovers from previous runs                             │
 │                   └─ the calls ④ just wrote                                   │
 │                one job per callId ───────────────────▶  in-process queue      │
 │                                                                               │
 │  ─────────────────────────────────────────────────────────────────────────    │
 │  CONSUME                                                                      │
 │   ⑥ audit      25 worker threads, each taking from the queue for itself       │
 │                                                                               │
 │      queue ─▶ worker ─▶ read the row  (already PROCESSED? drop it)            │
 │                      ─▶ pull the audio from S3                                │
 │                      ─▶ transcribe ▸ clean ▸ analyse ▸ audit                  │
 │                      ─▶ transcript/clean/analysis/audit.json ──▶  S3          │
 │                      ─▶ row PROCESSED + scores ───────────────▶  DynamoDB     │
 │                                                                               │
 │   ⑦ finish     write the run summary row, exit 0 / 1 / 2                      │
 └───────────────────────────────────────────────────────────────────────────────┘

        anything still UNPROCESSED when the pod goes ──▶ ⑤ of the next run

                    DynamoDB + S3 ──▶ ml-dashboard /call-auditing
```

And where each of those lives:

```
  python -m backend.batch run
      │
      ├─ 1. the day to fetch: last complete 02:00→02:00     collect.py
      │        computed from the clock, nothing stored
      ├─ 2. ask Cloud Connect for that day, hour by hour    collect.py
      ├─ 3. sort: already have it / not auditable / to do   collect.py
      ├─ 4. download each recording to S3, row UNPROCESSED  ingest.py
      ├─ 5. queue every UNPROCESSED call id                 queue.py
      │        yesterday's leftovers and today's, one query
      ├─ 6. workers drain the queue, 25 at a time           audit.py
      │        pipeline → 4 JSONs in S3 → row PROCESSED
      └─ 7. write the run summary                           __main__.py
```

**DynamoDB holds the to-do list; the queue only hands it out.** The queue is a
`queue.Queue` in the same process, filled by step 5 and drained by step 6, and
it is never read to find out what exists. A pod killed with three hundred jobs
still on it loses nothing: those rows are still `UNPROCESSED`, which is exactly
what step 5 of the next run asks the table for. "Just run it again" is the
correct response to almost every failure, and that is a property of the design
rather than a hope.

**Nothing is carried between runs.** No checkpoint, no lock, no marker of where
the last run got to — the window is the last complete 02:00-to-02:00 day, worked
out from the clock. Asking for a day twice is free, because step 3 asks the table
which call ids it already holds and does nothing with those.

**Why leftovers need no special handling.** Step 5 asks the table for everything
`UNPROCESSED`. Calls the previous pod ran out of time for, calls a pipeline error
put back, and the ones step 4 just downloaded are all the same rows to it.

---

## Running it

```bash
uv venv && uv pip install -r requirements.txt

python -m backend.batch run                  # the whole run
python -m backend.batch run --dry-run        # fetch and sort only, writes nothing
python -m backend.batch run --collect-only   # collect, download, queue; no AI
python -m backend.batch run --audit-only     # queue and audit what is outstanding
python -m backend.batch run --window-start 2026-09-19T00:00:00Z \
                           --window-end   2026-09-20T00:00:00Z   # a backfill
```

Settings come from `.env`, which is commented line by line. On a pod the task
definition supplies them instead and the file is absent.

### Exit codes — what Jenkins reads

| Code | Meaning | What to do |
|---|---|---|
| `0` | all good | nothing |
| `1` | the run finished; some individual calls failed | a warning |
| `2` | could not run at all — bad config, wrong model, their API is unreachable | a failure worth paging |

The middle one matters. Forty broken recordings out of three thousand is a
normal night and must not page anyone; "the call log API returned nothing for
twenty-four hours" must. One exit code covering both means either being paged
every morning or never being paged at all.

### Spending nothing while testing

```bash
STUB_PIPELINE=1 python -m backend.batch run     # Soniox and OpenAI replaced by
                                                # a schema-valid stub
python -m backend.batch run --dry-run           # no model, no download
```

`--dry-run` writes no row and sends no message, so it stays usable while a real
run is going — which is exactly when somebody wants to check what the window
looks like.

---

## Did last night's run work?

One read, not a hunt through pod logs that are already gone:

```python
from backend.batch.control import control
control.read_run("20260920T175544Z-a50b0975")
```

```json
{ "outcome": "OK", "durationSec": 14, "windowSource": "day",
  "windowStart": "2026-09-20T16:26:56Z", "windowEnd": "2026-09-20T17:40:46Z",
  "fetched": 5, "alreadyKnown": 4, "skipped": 1, "toDo": 0,
  "ingested": 0, "ingestFailed": 0, "audited": 1, "auditFailed": 0,
  "datesTouched": ["2026-08-24"],
  "remainingUnprocessed": 0, "remainingByStatus": {},
  "failures": [], "failureCount": 0, "pipelineMode": "real" }
```

Step 3's counts are written **before** step 4 downloads anything, so a window
that was wrong is visible in seconds rather than after a few hundred dollars of
transfer and transcription.

`datesTouched` is the days this run changed a score on. Nothing in this repo
reads it — it is there because it is the first thing an incremental export will
ask for, and it is free to record while the run already knows the answer. When
the numbers move to ClickHouse, that is the list of partitions to re-read.

---

## The AI pipeline

Four stages at the repo root, run as **subprocesses** — which is why they are
not inside the package. Each writes a file the next one reads.

```
transcribe.py        audio → .transcript.json   (Soniox)
clean_transcript.py        → .clean.json
analyze_call.py            → .analysis.json     (tone)
audit_call.py              → .audit.json        (marked against the scheme)
```

Supporting them: `audit_schema.py` (the pydantic schema the audit is validated
against), `prompts/audit_system_prompt.md` (the marking scheme, loaded at
runtime), `cost.py` (what a run cost), `report_data.py` (attaches the transcript
to an audit so a flag can carry a clock and a play button).

The call is marked out of 70 — seven sections of ten. A disqualifying conduct
flag makes the call **fatal**: it is not scored at all, because there is no
partial credit for a call that should never have happened.

### The cost guard

The run **refuses to start** unless `AUDIT_MODEL` and `ANALYSIS_MODEL` are both
`gpt-5.6-luna`. Measured over 100 real audits: luna is $0.117 for a 25-minute
call and `gpt-5.4-mini` is $0.381 for identical output — 3.25× for nothing. A
typo there is otherwise invisible until the invoice. `ALLOW_ANY_MODEL=1` turns
it off deliberately.

It is checked before the first call is claimed, so a run that is going to refuse
refuses while the work queue is untouched.

---

## Storage

Two DynamoDB tables and one S3 bucket. `python -m backend.tools.provision_aws`
creates them; `--verify` is the deploy gate.

### `calls` — the to-do list *and* the dashboard's record

```
PK  CALL#<callId>      SK  META
```

The sort key is a constant on purpose: it makes each call an item collection, so
sibling rows (a coaching note, an appeal, a re-audit) can be added later and read
with the call in one query, with no migration.

| Index | Partition key | Answers |
|---|---|---|
| GSI-1 `work-queue` | `gsi1pk` = the work status | **which calls still need work** — the job's queue |
| GSI-2 `agent-time` | `AGENT#<ext>#<yyyy-mm>` | one agent over a month |
| GSI-3 `day-time` | `DAY#<yyyy-mm-dd>` | recent calls, all teams |

There is no team index. A team is its agents, and the roster that says which is
cached whole by the read side — so a team window is a fan-out over that team's
five `AGENT#` partitions rather than a fourth copy of the table. The
`TEAM#<team>#<day>` index it replaced needed 30 partitions for the same 30-day
window and was only ever 23% populated: an extension the roster does not list
carries no team, so any sum over teams disagreed with the org total.

Every index sorts on `<startedAt>#<callId>`, so a time window is a range
condition rather than a filter that reads rows and throws them away.

**GSI-1 is sparse, and that is load-bearing.** `gsi1pk` is written
only while a call is `INGESTING`, `UNPROCESSED` or `FAILED`, and is
*deleted* at `PROCESSED` or `SKIPPED` — which drops the row out of the index.
Indexing every status instead would pile every call ever finished into one
`PROCESSED` partition forever: a hot partition and a full extra copy of the
table nothing reads. Sparse, the index holds only outstanding work, so it stays
in the hundreds and *is* the queue.

> **The one rule when touching this code.** `processingStatus` and `gsi1pk` move
> together, and only `repository.update()` does that correctly. Never `put_item` a call row outside `put_if_absent`. A call written
> to `PROCESSED` with `gsi1pk` still on it sits in the work queue forever and is
> audited, and paid for, on every run.

### How a call moves

```
new call ──► INGESTING ──► UNPROCESSED ──► PROCESSED
                 │              ▲
                 └──► FAILED    └── a pipeline error puts it back, and the
                                    next run queues it again
call with no recording ──► SKIPPED
```

There is no `AUDITING` state any more. A call being worked on is a job one
worker took off the queue, and `Queue.get_nowait` is atomic, so no second worker
can have it; a second answer to "who has this" in DynamoDB would be a second
thing to keep in step with the queue. The constant is kept in `ProcessingStatus` only because rows
written by the old build still carry it, and `dispatch._revive_legacy` reads
those back into the work list.

`SKIPPED` is stored rather than dropped. A call that is simply absent is one
step 3 rediscovers and re-checks on every run, forever; a `SKIPPED` row is a
permanent answer that costs 200 bytes.

### The control row

One singleton in the same table — tiny, written once a run, and a second table
for it would be a second thing to provision and grant.

```
PK  RUN#<runId>   SK  <startedAt>   the counts and the outcome
```

**There is no checkpoint row and no lock row**, and both absences are the point.
The window is worked out from the clock, so there is no stored marker that can
be wrong about where the last run got to and none to move. And two pods at once
is not worth preventing: they build the same work list, and the duplicate is
dropped by one `GetItem` at the top of the audit.

The cost is that a *missed* day stays missed. A checkpoint would have carried
the gap forward; this does not, so a night nothing ran for is picked up with
`--window-start` / `--window-end` — one command after an outage somebody already
knows about, against a piece of state that has to be right every other night.

It carries **none** of `gsi1pk`..`gsi3pk`, or it would appear in dashboard
queries as a malformed call. Its sort key is not `META`, so the calls repository
cannot reach it — `__main__.py` has its own small accessor onto the same table.

### `directory` — the roster

`AGENT#<accountId>`, `TEAM#<teamId>`, `USER#<accountId>`, each `SK = PROFILE`,
with an `EXT#<extension>` lookup index. Cloud Connect give an extension, not an
account id. Loaded **whole, once per run** into an in-process cache: 3,000 calls
against a table of eight rows is 3,000 reads to answer the same question.

`agents.json` is the editable copy and the fallback;
`python -m backend.tools.seed_demo --directory` pushes it into the table.

### There is no third table

Daily counters used to be pre-aggregated into a `rollups` table — one row per
scope per day, so a dashboard tile was a range query returning at most one row
per day in the window rather than adding up raw calls.

**That table is gone.** The read side aggregates GSI-3 over the window it is
actually showing, and the longer-term home for analytics is ClickHouse, not a
third DynamoDB table. Two things are worth being explicit about, because they
are what you pay for that decision:

- **Read cost is now O(calls in window), not O(days in window).** At ~230
  auditable calls a day a 30-day window is ~7,000 rows and a few hundred
  milliseconds. At the 3,000/day the system is specified for it is ~90,000 rows
  and ~31 MB, which is ~32 paginated responses. That is the number to watch, and
  it is the trigger for moving to ClickHouse rather than for re-adding a table.
- **`sectionMarks` is not in any GSI projection.** The strengths/weaknesses
  panel needs it, so aggregating it live means a `BatchGetItem` against the base
  table for the calls in the window. DynamoDB cannot change an existing index's
  projection in place — an index has to be dropped and recreated, which
  backfills — so this is a deliberate decision to make once, not something to
  drift into.

The batch job writes everything those aggregations read. It does not aggregate
anything itself, and the last step no longer rebuilds anything: it writes the
run summary. What it does record is `datesTouched` on the run
row — the days this run changed a score on, which is what an incremental export
into ClickHouse will want first.

### S3

```
s3://<bucket>/audio/2026/09/16/<callId>.wav
              transcript/2026/09/16/<callId>.json
              clean/ · analysis/ · audit/ · report/
```

Artifact type first, then the date, then the call id — every key derivable from
three values the job already has, so no stage needs a `LIST` to find the
previous stage's output.

The prefix shape is what makes the lifecycle rules one line each. S3 lifecycle
filters on prefix, tag and age — **not on file extension** — so with audio and
JSON in one folder, expiring recordings while keeping audits needed every object
tagged on write. With `audio/` as its own prefix that tagging disappears.

| Prefix | Rule | Why |
|---|---|---|
| `audio/` | IA at 30 days, Glacier IR at 90 | ~2.9 TB/month, and nothing re-reads it after the transcript |
| `transcript/`, `clean/`, `analysis/` | expire at 1 year | regenerable, but only by re-paying for transcription |
| `audit/`, `report/` | kept | this is the product; a few GB a year |

No agent name in the key. Names are hand-typed, not unique and not stable; a key
embedding one is wrong the moment an agent changes team. `callId` is immutable,
and agent and team live on the row, which is what anything filters on anyway.

---

## Why the awkward-looking bits are that way

**Step 3 fetches an hour at a time.** Not an optimisation. Their call log API has
no pagination and no truncation signal: ask for a day with 3,000 calls and it
either returns all of them or quietly returns some, saying `SUCCESS` either way.
An hour is ~125 calls at peak, comfortably under anything that could be cut
short, and a failed slice costs an hour rather than the whole day.

**An empty hour is not a failure.** Their API answers `HTTP 411 "Call Log Details
Not Found"` both for a row that has not been written yet *and* for a range with
no calls in it. Read as retryable, every quiet overnight hour costs three
attempts and is then recorded as a failed slice — an hour of the day that was
never fetched, which the run reports as `PARTIAL` because nothing comes back for
it on its own. `NoRowsYet` carries both meanings and the caller picks.

**`call_rec_path` is often `"-"`.** Cloud Connect fill empty fields with a
placeholder rather than leaving them empty — measured on live traffic, 21 of 26
answered calls in one hour carried `"-"` and only 5 a real URL. Read literally
that is truthy, so the call looks auditable, the row is written `INGESTING`, and
the download fails with `unknown url type: '-'`. `CallLogRecord.recording_url`
treats a placeholder as no value, the same way `agent_did` handles `"N/A"`.

**The window stops 15 minutes short of now, and starts 5 minutes before the
checkpoint.** Cloud Connect do not write the call log row when the call ends; it
appears thirty seconds to a few minutes later, from a different system. A window
running to this instant would cover calls whose rows do not exist yet, and
because the checkpoint then moves past them they would never be fetched again.
Re-fetching a few calls costs nothing — step 4 recognises them.

**A stuck call needs no sweeping up.** There is no state saying "somebody is
working on this", so there is nothing to get stranded in. A worker that dies
mid-audit leaves the row exactly as it found it — `UNPROCESSED` — which is the
same state every other kind of failure leaves it in, and which is what step 5 of
the next run asks for.

**A failed call is not retried inside the run.** A pipeline error goes back to
`UNPROCESSED`, so the *next* run picks it up once the upstream API has had time
to recover; an immediate retry is the worst moment to ask again. A broken
recording goes to `FAILED` with the reason on the row, where a person can see it,
because the third attempt will fail the same way and pay for another
transcription doing it.

**Re-queueing a call another pod is auditing is harmless.** Tracking which
`UNPROCESSED` rows somebody else already holds would be a second source of
truth, which is the thing this design exists to avoid. The duplicate finds a
`PROCESSED` row and is dropped in one read.

**25 audits at a time.** Almost all of the 5–6 minutes an audit takes is waiting
on Soniox and OpenAI. Serially, 3,000 calls is 300 hours of wall clock a day.
The ceiling is not the CPU but OpenAI's tokens-per-minute: one call in flight
measures ~60,000 tpm, so Tier 2 (2M) allows ~33 and Tier 3 (4M) ~66. **The
account needs to be Tier 3 at minimum.**

---

## What the read side depends on

The dashboard reads these tables directly. None of what follows is enforced by a
test in this repo, so the failures are quiet ones — a tile that goes blank, a
panel that stops drawing — and they surface in a different repo from the change
that caused them.

| What the dashboard needs | Where it comes from | What breaks if it moves |
|---|---|---|
| `disqualified` (boolean) | `dashboard_fields()` | the fatal tile and filter — `score = null` cannot replace it |
| `sectionMarks` (partial — see below) | `scores.section_marks` | the scorecard, every section average, and metric-wise sorting |
| `team`, `teamLeaderName` on the row | denormalised at write | every team page; rows file under "no team" |
| `score`, `scoreBand`, `flagCount`, `auditStatus` | projected onto the row | the list view becomes one S3 read per row |
| `auditKey`, `audioKey` | `common/store.py` | the report page and the audio player |
| GSI-2/3/4/5 and their `LIST_PROJECTION` | `provision_aws.py` | the list view; an attribute dropped from the projection reads as absent, not missing |
| the `directory` table | `seed_demo --directory` | teams disappear — it is the only place one exists |
| the audit document's shape | `audit_schema.py` | the report renderer, which parses it directly |

Three of those are worth stating outright because they surprised the port:

- **`sectionMarks` is on the base row and in no index projection.** That is
  deliberate — ~80 bytes copied into four indexes on every write, forever — so
  the dashboard pays one `BatchGetItem` per hundred calls to fill it in. Adding
  it to a projection would be a silent cost increase on every write; removing it
  from the row would blank the strengths panel.
- **A real audit marks six sections, not seven.** Every document in `files/`
  scores six and omits `pitch_flow`. Anything consuming marks has to treat the
  map as partial, and an absent section is *not* a zero — zero is a mark an
  agent can score.
- **`audioKey` is not in any projection either**, so it comes back empty from a
  list query and is only correct on a base-table read.

The dashboard also keeps one table of its own in this account and region —
`call-audit-users`, its sign-in roster. This job never touches it; it is in
`backend/iam/policy.json` only because one key runs both sides in the test
account.

How the screen is reached, scoped and signed into is documented on that side, in
`ml-dashboard/docs/call-auditing.md` and `docs/call-auditing-access.md`. Nothing
in this repo needs to know any of it.

---

## Scaling past one pod

At ~1,500 calls a day one pod doing everything is fine. At 3,000, run one pod on
`--collect-only` and two or three on `--audit-only`.

They never talk to each other, and there is no broker between them — each pod
builds its own queue from `gsi1pk = UNPROCESSED` and drains it with its own 25
threads. Inside a pod the handoff is exact: `Queue.get_nowait` is atomic, so one
worker gets a call and the others move on.

**Across pods the work lists overlap, on purpose.** Two pods reading the index a
second apart see the same rows, and the one that finishes second finds a
`PROCESSED` row and drops the call in a single `GetItem`. That read is the whole
of the cross-pod story: no lock, no lease, no visibility timeout, and nothing to
tune. What it costs is a wasted read per overlap, which is the price of not
running a queue service for a job whose producer and consumers are threads in
one process.

What that does *not* protect against is two pods starting the same audit within
the same second — both reads return `UNPROCESSED`, both run the pipeline, and
the second write wins. It is a real window and a narrow one, and closing it is
what a compare-and-set on the row would be for. If several audit pods become the
normal shape rather than a burst-capacity measure, that is the change to make —
and the note in `audit.py` says so.

---

## Layout

Eleven modules, and each is one of the seven steps, one external system, or one
shape everything agrees on.

```
backend/batch/
  __main__.py     the CLI, the Run, and the row it writes about itself
  cloudconnect.py their call log and recording API — the only place it is spoken
  collect.py      steps 1-3   the day to fetch, fetched, and sorted three ways
  ingest.py       step 4      the recordings into S3, rows go UNPROCESSED
  queue.py        step 5      the job queue, and filling it from the table
  audit.py        step 6      the AI pipeline, and the 25 workers that run it

backend/common/
  config.py       settings, read once from the environment
  trace.py        logging
  models.py       the call log record, the DynamoDB row, statuses, timestamps
  agents.py       the roster file, and resolving an extension to a person
  store.py        DynamoDB, S3, and the keys that address them

backend/tools/    provision_aws (create, verify, rebuild), seed_demo (test data)
backend/iam/      policy.json — the one policy to attach

transcribe.py clean_transcript.py analyze_call.py audit_call.py
                  the AI pipeline, run as subprocesses by audit.py
audit_schema.py   what a valid audit document is
report_data.py    the transcript the report renders
cost.py           token accounting, imported by the three model-calling scripts

agents.json       the roster: extension and DID → a person and a team
prompts/          the marking scheme audit_call.py loads at runtime
files/            ~100 real audited calls, the corpus seed_demo builds from

docker-compose.sim.yml  DynamoDB Local + MinIO, for the rehearsal below
simulate.sh             provision, cold run, second run, and what landed
```

**It used to be twenty-nine modules.** What went, and why:

| Gone | Where it is now | Why |
|---|---|---|
| `storage/` — 8 modules | `common/store.py` | half of it was a second, JSON-file implementation that the batch job refused to start on. Two implementations with one real caller is a layer that costs more to keep true than it pays back — and `STORAGE_BACKEND` went with it |
| `window.py` | `collect.py` | "which day" and "fetch that day" are one question |
| `dispatch.py` | `queue.py` | the queue and the one thing that fills it |
| `audit_pipeline.py` | `audit.py` | the pipeline and the worker that runs it |
| `control.py`, `runner.py` | `__main__.py` | the CLI, the run and the run's own row are one story; the timestamp helpers moved to `models.py`, beside the other time conversion |

---

## Logging

Every layer goes through `backend/common/trace.py` into one configured stream,
and every line carries the run id and, inside a call, the call id:

```
2026-09-20T17:55:57Z INFO  [AUDIT  ] run=20260920T175544Z-a50b0975 call=audio_68  PROCESSED (stub) score=66.0 flags=2 band=NEEDS_IMPROVEMENT
```

Three pods interleave their output in CloudWatch; without `run=` they are
indistinguishable. `LOG_LEVEL=DEBUG` adds every DynamoDB write and S3 object
(~12,000 extra lines on a full run). `LOG_JSON=1` emits one JSON object per line
for a shipper. boto3 is pinned to `WARNING` because at DEBUG it prints entire
request bodies, which for this table means customer phone numbers in the log.

---

## Test data

```bash
python -m backend.tools.seed_demo             # seed the floor from files/
python -m backend.tools.seed_demo --check     # read it back
python -m backend.tools.seed_demo --directory # push agents.json to the roster
python -m backend.tools.seed_demo --wipe      # delete every object and row
```

`--directory` is the one to run after editing `agents.json`; the roster table is
what the batch job caches at the start of every run.

---

## Simulating a deployment

The deployed shape is one pod, started on a schedule, running once and exiting.
So a rehearsal is not "start the service and poke it" — it is "start the pod and
read the exit code", and the thing being rehearsed is a **cold start**: nothing
is carried between runs, so every run is the first one.

### The ladder, cheapest first

```bash
# 1. reads only. No writes, no downloads, no spend. Answers "is the window
#    right and does their API still answer".
python3 -m backend.batch run --dry-run

# 2. fully local. DynamoDB Local and MinIO stand in for AWS; the four AI
#    stages are stubbed. Nothing real is touched and nothing is billed.
docker compose -f docker-compose.sim.yml up -d
./simulate.sh 5
docker compose -f docker-compose.sim.yml down -v

# 3. the container the pod actually runs, against the same fakes.
docker build -t call-auditing .
docker run --rm --env-file .env \
  -e DYNAMO_ENDPOINT_URL=http://host.docker.internal:8000 \
  -e S3_ENDPOINT_URL=http://host.docker.internal:9000 \
  -e STUB_PIPELINE=1 call-auditing

# 4. real models, one call. The only step that spends money, and the only one
#    that proves the pipeline itself works.
python3 -m backend.batch run --max-calls 1
```

`simulate.sh` does what a first deploy does and then what the second night does:
provisions its own table and bucket from nothing, seeds the roster, runs a cold
pod, **runs the same pod again** — which should collect the same window,
recognise every call already stored, and audit nothing — and prints what ended
up in the table. If the second run is not a no-op, idempotency is broken and
that is the bug to chase before anything else.

### What is real in the simulation, and what is not

| Real | Stood in for |
|---|---|
| the Cloud Connect call log and the recordings | DynamoDB → DynamoDB Local |
| every index, the sparse work queue, the 1 MB query page | S3 → MinIO |
| the queue, the 25 workers, the deadline and drain margin | Soniox + OpenAI → `STUB_PIPELINE` |
| the row shape, the S3 key shape, the exit codes | |

The call log is left real because it is read-only and there is no fake worth
writing: the failure this catches most often is *their* API changing shape, and
a fixture cannot catch that.

### The drills worth running

Each of these is a failure that has a defined behaviour, so each is worth
seeing at least once rather than reading about.

```bash
# the cost guard: refuses before touching anything. Exits 2.
AUDIT_MODEL=gpt-5.4-mini python3 -m backend.batch run

# a pod with no time: takes no work, leaves everything UNPROCESSED, exits 0.
BATCH_DEADLINE_MIN=20 BATCH_DRAIN_MARGIN_MIN=20 python3 -m backend.batch run

# two pods at once: both build the same work list and the loser's calls are
# dropped by one row read. Run these in two terminals.
python3 -m backend.batch run --audit-only

# a missed night, picked up by hand. The only thing a dropped checkpoint costs.
python3 -m backend.batch run \
  --window-start 2026-09-18T20:30:00Z --window-end 2026-09-19T20:30:00Z

# the split shape, once one pod is not enough.
python3 -m backend.batch run --collect-only      # one pod
python3 -m backend.batch run --audit-only        # two or three more
```

### What the scheduler has to do

Start the pod after the boundary — **02:05, not 02:00**, so
`BATCH_DAY_BOUNDARY_GRACE_MIN` is insurance rather than the mechanism — give it
longer than `BATCH_DEADLINE_MIN`, and treat the exit code as:

| Code | Meaning | Action |
|---|---|---|
| `0` | done | nothing |
| `1` | finished, some calls failed | a warning; look if it trends |
| `2` | could not run | page someone |

Nothing else. No health check, no port, no readiness probe — a probe against a
container that is supposed to exit reports the successful exit as a failure.

---

## Before production

1. **Confirm the OpenAI tier.** Tier 3 minimum; `AUDIT_CONCURRENCY=25` assumes
   it. On Tier 2 the ceiling is ~33 across *all* pods, not per pod.
2. **Drop the AWS keys from `.env`** and give the pod an IAM role. boto3's
   default chain picks it up with no code change.
3. **Split `backend/iam/policy.json`.** It grants `dynamodb:*` and `s3:*`,
   which is right for a single test account where one key runs provisioning,
   the job and the seeder. The running job needs only
   GetItem/PutItem/UpdateItem/Query/BatchGetItem on the two tables and
   Get/PutObject on the bucket; creating tables belongs to the provisioner.

   The policy also lists `call-audit-users`, which this job never touches — it
   is the dashboard's sign-in table and is here only because the same key runs
   both sides in the test account. It goes with the split.
4. **Set a retention rule for `audio/`.** The lifecycle transitions to Glacier
   but never expires. These are named customers on tape — decide how long they
   may be kept and add an `Expiration`.
5. **Fix the roster.** Live traffic uses extensions 706–712; `agents.json` lists
   701–705 and 717, so real calls file under "Unmapped extension 706". The run
   warns about this on every pass and still ingests them — the placeholder is
   deliberate, because dropping the call would hide the gap.

   It is not cosmetic any more. An unmapped extension gets no `team`, so those
   calls count in the dashboard's org roll-up and appear on no team page at all.
   Measured on a 30-day window: 258 of 337 calls. Add the extensions and re-run
   `seed_demo --directory`.
6. **Enable DynamoDB TTL** on `calls` keyed on `expiresAtEpoch` if run-summary
   rows should expire on their own. Until then `BATCH_RUN_SUMMARY_TTL_DAYS` is
   advisory.
