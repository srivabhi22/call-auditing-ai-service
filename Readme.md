# Call auditing

Fetches a day of calls from Cloud Connect, stores the recordings, scores each
one with an AI pipeline, and leaves the result in DynamoDB and S3 for the
dashboard to read.

It is an HTTP service. A run is started by a request; nothing is scheduled from
inside and nothing is carried between runs.

---

## How it works

One run, seven steps.

```
  1  window     the most recent complete 02:00-to-02:00 day, tenant time (IST)
  2  collect    ask Cloud Connect for that day, one hour at a time
  3  sort       split what came back four ways
  4  ingest     download each recording to S3, write the row UNPROCESSED
  5  queue      put every UNPROCESSED call id on an in-process queue
  6  audit      workers drain it: AI pipeline -> 4 JSONs in S3 -> PROCESSED
  7  summary    write the run row
```

**Step 3 splits every fetched call four ways:**

| pile | what happens |
|---|---|
| already known | the callId is in DynamoDB — ignored |
| off roster | the agent extension is not in `agents.json` — dropped, no row |
| not auditable | never answered, or no recording — stored `SKIPPED` |
| to do | a real recorded call nobody has seen — the work list |

**How a call moves:**

```
new call ──► INGESTING ──► UNPROCESSED ──► PROCESSED
                 │              ▲
                 └──► FAILED    └── a pipeline error puts it back, and the
                                    next run queues it again
call with no recording ──► SKIPPED
```

**Four things that make it restartable.**

- The window is computed from the clock, not a checkpoint. Re-running a day
  costs nothing: step 3 asks the table which ids it already holds.
- Step 5 reads `gsi1pk = UNPROCESSED` from the table, not step 4's output — so
  yesterday's leftovers, calls a pipeline error put back, and calls this run
  just downloaded are all picked up by one query.
- The queue is in memory and is never a source of truth. A pod that dies with
  300 jobs on it loses nothing; those rows are still UNPROCESSED.
- A run stops *starting* work `BATCH_DRAIN_MARGIN_MIN` before its deadline and
  lets what is in flight finish.

**Outcome**, written to the run row: `OK`, `PARTIAL` (the run finished, some
calls failed), or `FAILED` (it could not run at all).

---

## API

The endpoints are open — this is an internal service. A repeat trigger is
cheap by construction: a second concurrent one gets `409` instead of a second
run, and re-running a day already collected downloads nothing and audits
nothing. Set `PIPELINE_TRIGGER_TOKEN` to require an `X-Trigger-Token` header
instead.

| method | path | |
|---|---|---|
| `POST` | `/v1/runs` | The full pass, steps 1–6. `202` + `runId`. |
| `POST` | `/v1/runs/audit` | The same run with the call log fetch left out — steps 5–6 over whatever is already `UNPROCESSED`. `202` + `runId`. |
| `GET` | `/v1/runs/current` | What this pod is doing, or last did. |
| `GET` | `/v1/runs/{runId}` | The run summary row from DynamoDB. |
| `GET` | `/healthz` | The process is up. No AWS calls. Use for liveness. |
| `GET` | `/readyz` | …and DynamoDB and S3 are reachable. Use for readiness. |

> **Note.** Nothing in this service triggers itself. A run happens only when
> something outside calls one of these endpoints — a scheduler, a CI step, or a
> person. The pod stays up and idle until it is asked.

```bash
# The full pass. An empty body means the most recent complete business day.
curl -X POST http://localhost:8000/v1/runs \
  -H "Content-Type: application/json" \
  -d '{}'

# The full pass over a named window, capped. All four fields are optional.
curl -X POST http://localhost:8000/v1/runs \
  -H "Content-Type: application/json" \
  -d '{"windowStart":"2026-09-19T20:30:00Z","windowEnd":"2026-09-20T20:30:00Z","maxCalls":500,"dryRun":false}'

# Audit whatever is already UNPROCESSED. Fetches nothing.
curl -X POST http://localhost:8000/v1/runs/audit \
  -H "Content-Type: application/json" \
  -d '{}'

# What this pod is doing, or last did.
curl http://localhost:8000/v1/runs/current

# The run summary row.
curl http://localhost:8000/v1/runs/20260922T094547Z-828bda97

# Liveness.
curl http://localhost:8000/healthz

# Readiness.
curl http://localhost:8000/readyz
```

---

## Where things are

```
backend/api/          the HTTP service — the only thing that stays up
  app.py              the endpoints, auth, health
  runner.py           the one background run this pod may have in flight

backend/batch/        the run itself
  run.py              steps 1–7 in order, and the RUN# summary row
  cloudconnect.py     their call log and recording API
  collect.py          steps 1–3  the day, fetched and sorted four ways
  ingest.py           step 4     download recordings to S3
  queue.py            step 5     the in-process queue, filled from the table
  audit.py            step 6     the AI pipeline and the worker pool
  rollup.py           step 6b    a day's totals, per overall/team/agent

backend/common/       everything shared
  config.py           settings, read once from the environment
  models.py           CallRow, CallLogRecord, and hydrate() for reads
  store.py            DynamoDB, S3, and the keys that address them
  agents.py           extension -> agent, backed by agents.json
  trace.py            structured logging

backend/iam/policy.json   the IAM policy the pod needs

transcribe.py         the four AI stages, run as subprocesses by audit.py,
clean_transcript.py   in this order
analyze_call.py
audit_call.py
audit_schema.py       the audit document's schema (pydantic)
report_data.py        aligns the clean and raw transcripts for the report
cost.py               token and spend accounting

prompts/              the audit system prompt
agents.json           the roster: extension, name, DID, team, team leader
.env.example          every variable, with what it does

Dockerfile              service image; CMD is uvicorn, one worker
requirements.txt        pinned
```

**Storage.** Three DynamoDB tables and one S3 bucket:

- `calls` — one item per call (`PK = CALL#<id>`, `SK = META`), plus `RUN#` rows
  for run summaries. Three GSIs, all sorted on `ord`: work queue, agent-time,
  day-time.
- `rollups` — one row per scope per day, so the dashboard adds up a span
  instead of re-aggregating every call in it:

      PK                        SK           what it holds
      OVERALL                   2026-09-20   the whole floor, that day
      TEAM#MP                   2026-09-20   one team
      AGENT#MP#pankaj-kourav    2026-09-20   one agent, under their team

  Every number on the row is a **sum or an extreme, never an average** —
  `totalCalls`, `totalFlags`, `fatalCalls`, `reviewCalls`, `scoreSum`,
  `scoredCalls`, `scoreMax`, `scoreMin`, and `metricSums`/`metricCounts` per
  scorecard criterion — because sums add across days and averages do not. A
  span is one range query and one division at the end. One GSI, `scope`-by-day,
  answers "every team" or "every agent" over a window.

  Written by the run (step 6b) for the days it touched, and rebuilt from the
  call rows rather than incremented — so it is idempotent, and a day is
  re-summed correctly by any later run that touches it.
- `sessions` — one row per processing session: is the night finished?

      PK                             SK           the row
      SESSION#2026-09-23T02:00:04Z   PENDING      the run is working
      SESSION#2026-09-23T02:00:04Z   COMPLETED    every job it took on has
                                                  reached a final state

  With `totalCallsProcessed` and `callsFailed`, plus the counts that make those
  two readable: `callsSkipped`, `callsNotReached`, `callsQueued`,
  `queueRemaining`, and the run's `outcome`.

  The status is the **sort key**, so finishing a session replaces the row
  rather than updating it — DynamoDB cannot change a key attribute, so it is a
  put of COMPLETED then a delete of PENDING, in that order, and a reader that
  lands between the two sees both and takes COMPLETED. A session still saying
  PENDING long after its start is a pod that was killed. No index — the key is
  the whole access pattern. `RUN#` in the calls table stays the detailed
  bookkeeping; this is the single flag anything downstream waits on.

- S3 — `audio/`, `transcript/`, `clean/`, `analysis/`, `audit/`, each keyed
  `<prefix>/<yyyy>/<mm>/<dd>/<callId>`, plus `roster/agents.json`.

**The roster is a file, not a table.** `agents.json` at the repo root is the
only place a reporting line exists — Cloud Connect has no concept of a team, and
gives an extension rather than a person. This service reads it from disk
(reloaded when the mtime changes, so a correction needs no restart) and
republishes it to `s3://<bucket>/roster/agents.json` at the start of every run.
The dashboard reads it from there, because it is a separate deployment and
cannot read this disk.

There used to be a `directory` DynamoDB table holding a transcription of that
same file, synchronised by remembering to run a seeding command. Two rosters
that can disagree is worse than one that can be out of date: the producer would
filter calls against one and the dashboard would group them under the other, and
nobody notices until a month of reports is attributed to the wrong people.
Every run republishes it, so the object is never more than a day behind the
file the pod was built with.
