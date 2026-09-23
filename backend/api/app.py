"""The endpoints.

    POST /v1/runs           the full pass: call log -> S3 -> queue -> audit
    POST /v1/runs/audit     the same run, with the call log fetch left out
    GET  /v1/runs/current   what this pod is doing, or last did
    GET  /v1/runs/{runId}   the run summary row, from DynamoDB
    GET  /healthz           the process is up
    GET  /readyz            ...and can reach DynamoDB and S3

── The two triggers ─────────────────────────────────────────────────────────

They are the same run. `POST /v1/runs` is `mode="full"` and does steps 1 to 6;
`POST /v1/runs/audit` is `mode="audit"` and does steps 5 and 6, which is the
whole run minus the part that talks to Cloud Connect.

The second is not a lesser version of the first. It is what you want when the
call log is unreachable, when a previous run hit its deadline with work left,
when a pipeline error put calls back, or when several pods share the load: one
collects and the rest audit. All four are the same state -- rows sitting
UNPROCESSED in the table -- and step 5 finds them with one index query
regardless of how they got there.

Both are **asynchronous**. A run takes hours; the request returns `202` with a
run id as soon as the run has been accepted, and the result is read from the run
row afterwards. A synchronous version of this endpoint would be a request that
every proxy between here and the caller would time out.

── Why it is locked down by default ─────────────────────────────────────────

An unauthenticated trigger is a button that spends money: a loop of requests is
a loop of Soniox and OpenAI bills, and `POST /v1/runs` will also pull ~97 GB a
day from a partner's server. So `PIPELINE_TRIGGER_TOKEN` is required, and if it
is unset the endpoints refuse rather than opening -- a service that fails open
when its config is missing is one that is open in exactly the environment where
somebody forgot to configure it.

The health endpoints are deliberately unauthenticated: kubelet does not send
headers, and neither reveals anything or starts anything.
"""

import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from ..batch.run import OUTCOME_RUNNING, run_rows
from ..common.config import settings
from ..common import store
from ..common.trace import batch, configure, warn
from . import runner

configure()

# The shared secret a trigger has to present. No default: see the module
# docstring on why this refuses rather than opening.
TRIGGER_TOKEN = os.environ.get("PIPELINE_TRIGGER_TOKEN", "").strip()

# What to start when the process comes up: "full", "audit", or "" for nothing.
# This is the "trigger it when the pod gets up" path -- it needs no probe, no
# sidecar and no postStart hook, and it cannot race the server becoming ready
# because it runs after the app has started.
RUN_ON_STARTUP = os.environ.get("RUN_ON_STARTUP", "").strip().lower()

# How long shutdown waits for a run in flight before letting the process go.
# Zero by default: a pod being replaced mid-run is an ordinary event, the rows
# are UNPROCESSED, and the next run picks them up. Raise it if the orchestrator
# gives a long termination grace period and you would rather calls finished.
SHUTDOWN_GRACE_SEC = int(os.environ.get("SHUTDOWN_GRACE_SEC", "0") or 0)


# ------------------------------------------------------------------------
# request and response shapes
# ------------------------------------------------------------------------

class RunRequest(BaseModel):
    """Everything the CLI used to take as a flag. All of it optional.

    An empty body is the ordinary case and means "do what you would do at
    02:00": the most recent complete business day, the configured caps.
    """

    windowStart: str | None = Field(
        default=None,
        description="ISO-8601 UTC. Replaces the daily window -- for a backfill, "
                    "or to re-fetch a day whose slices failed. Not capped. "
                    "Ignored by /v1/runs/audit, which fetches nothing.",
    )
    windowEnd: str | None = Field(
        default=None, description="ISO-8601 UTC. Needs windowStart.",
    )
    maxCalls: int | None = Field(
        default=None, gt=0,
        description=f"Safety cap on one run (default "
                    f"{settings.max_calls_per_run}).",
    )
    deadlineMin: int | None = Field(
        default=None, gt=0,
        description=f"How long this run may take (default "
                    f"{settings.deadline_min}). It stops starting new work "
                    f"{settings.drain_margin_min} minutes before this.",
    )
    dryRun: bool = Field(
        default=False,
        description="Fetch and sort, then stop. Writes nothing, queues "
                    "nothing, spends nothing.",
    )


class RunAccepted(BaseModel):
    runId: str
    mode: str
    status: str
    startedAt: float
    message: str


# ------------------------------------------------------------------------
# auth
# ------------------------------------------------------------------------

def require_trigger_token(x_trigger_token: str = Header(default="")):
    """The shared secret, or 401/503.

    503 rather than 401 when the token is unset, because that is a broken
    deployment and not a rejected caller -- and because 401 would invite
    somebody to go looking for the right credential when there isn't one.
    """
    if not TRIGGER_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="PIPELINE_TRIGGER_TOKEN is not configured, so this endpoint "
                   "refuses to start anything. Set it on the deployment.",
        )
    # Constant-time, so the comparison does not leak the prefix it matched.
    import hmac

    if not hmac.compare_digest(x_trigger_token, TRIGGER_TOKEN):
        raise HTTPException(status_code=401, detail="Bad or missing X-Trigger-Token")


# ------------------------------------------------------------------------
# the app
# ------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    batch(f"api up — region={settings.aws_region} "
          f"bucket={settings.audio_bucket} "
          f"tables={settings.calls_table}/{settings.directory_table} "
          f"pipeline={settings.pipeline_mode} "
          f"concurrency={settings.audit_concurrency}")
    if not TRIGGER_TOKEN:
        warn("api", "PIPELINE_TRIGGER_TOKEN is not set — every trigger will be "
                    "refused with 503. This is deliberate: an open trigger is a "
                    "button that spends money.")

    if RUN_ON_STARTUP in ("full", "audit"):
        batch(f"RUN_ON_STARTUP={RUN_ON_STARTUP} — starting a run now")
        try:
            state = runner.start(RUN_ON_STARTUP, trigger="startup")
            batch(f"startup run {state.run_id} is in flight")
        except Exception as exc:  # noqa: BLE001 -- a failed startup run must
            # not stop the pod from serving; the trigger endpoints still work.
            warn("api", f"the startup run could not be started: {exc!r}")
    elif RUN_ON_STARTUP:
        warn("api", f"RUN_ON_STARTUP={RUN_ON_STARTUP!r} is not one of "
                    f"'full', 'audit' or empty — nothing was started")

    yield

    if runner.is_running():
        if SHUTDOWN_GRACE_SEC:
            batch(f"shutting down with a run in flight — waiting up to "
                  f"{SHUTDOWN_GRACE_SEC}s")
            runner.wait(SHUTDOWN_GRACE_SEC)
        if runner.is_running():
            warn("api", "shutting down with a run still in flight. Its calls "
                        "stay UNPROCESSED and the next run queues them again.")


app = FastAPI(
    title="Call auditing pipeline",
    version="1.0.0",
    description=__doc__,
    lifespan=lifespan,
)


# -- health ---------------------------------------------------------------

@app.get("/healthz", tags=["health"])
def healthz():
    """The process is up. Deliberately does not touch AWS.

    Liveness must not fail because DynamoDB had a bad minute -- that restarts a
    pod that is working, in the middle of a run, for a problem a restart cannot
    fix. Reachability is `/readyz`'s job.
    """
    return {"status": "ok", "running": runner.is_running()}


@app.get("/readyz", tags=["health"])
def readyz(response: Response):
    """...and the tables and bucket it needs are reachable and exist.

    Cheap on purpose: `describe_table` and `head_bucket`, no scan and no query.
    A readiness probe that reads rows is a readiness probe that costs money
    every few seconds for the life of the pod.
    """
    checks, ok = {}, True
    try:
        dynamo = store.client("dynamodb", settings.dynamo_endpoint_url)
        for table in (settings.calls_table, settings.directory_table):
            dynamo.describe_table(TableName=table)
            checks[f"dynamodb:{table}"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["dynamodb"] = f"{type(exc).__name__}: {exc}"
        ok = False
    try:
        store.client("s3", settings.s3_endpoint_url).head_bucket(
            Bucket=settings.audio_bucket
        )
        checks[f"s3:{settings.audio_bucket}"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["s3"] = f"{type(exc).__name__}: {exc}"
        ok = False

    if not TRIGGER_TOKEN:
        checks["trigger-token"] = "NOT CONFIGURED — triggers will 503"
        ok = False

    if not ok:
        response.status_code = 503
    return {"status": "ok" if ok else "unready", "checks": checks}


# -- the triggers ---------------------------------------------------------

def _start(mode, body):
    """Shared by both triggers. The only difference between them is `mode`."""
    if body.windowEnd and not body.windowStart:
        raise HTTPException(
            status_code=400,
            detail="windowEnd needs windowStart. An end with no start is "
                   "almost always a typo, and guessing a start would fetch a "
                   "period nobody asked for.",
        )
    try:
        state = runner.start(
            mode,
            trigger="api",
            dry_run=body.dryRun,
            window_start=body.windowStart,
            window_end=body.windowEnd,
            max_calls=body.maxCalls,
            deadline_min=body.deadlineMin,
        )
    except runner.AlreadyRunning as exc:
        # 409 and the run id, so a caller that retries can tell "mine is
        # already going" from "somebody else's is".
        raise HTTPException(
            status_code=409,
            detail={
                "error": "a run is already in flight on this pod",
                "runId": exc.state.run_id,
                "mode": exc.state.mode,
                "startedAt": exc.state.started_at,
            },
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    return RunAccepted(
        runId=state.run_id,
        mode=state.mode,
        status=OUTCOME_RUNNING,
        startedAt=state.started_at,
        message=(f"accepted; poll GET /v1/runs/{state.run_id} for the result, "
                 f"or GET /v1/runs/current while it is in flight"),
    )


@app.post(
    "/v1/runs",
    status_code=202,
    response_model=RunAccepted,
    dependencies=[Depends(require_trigger_token)],
    tags=["runs"],
    summary="Run the whole pipeline",
)
def start_full_run(body: RunRequest = RunRequest()):
    """Steps 1-6: fetch the day from the call log, filter it to the roster,
    download the recordings to S3, queue every UNPROCESSED call and audit it.

    This is what a scheduled pod does at 02:00, and what `RUN_ON_STARTUP=full`
    starts when the pod comes up. An empty body means the most recent complete
    business day.
    """
    return _start("full", body)


@app.post(
    "/v1/runs/audit",
    status_code=202,
    response_model=RunAccepted,
    dependencies=[Depends(require_trigger_token)],
    tags=["runs"],
    summary="Audit what is already waiting",
)
def start_audit_run(body: RunRequest = RunRequest()):
    """Steps 5-6 only: take every call already sitting UNPROCESSED in DynamoDB,
    put it on the queue, and drain it through the AI pipeline.

    The same run as `POST /v1/runs` with the Cloud Connect fetch left out, so it
    touches no external API and downloads no new recordings. Use it when the
    call log is unreachable, when a previous run hit its deadline with work
    left, when a pipeline error put calls back, or to add audit capacity
    alongside a pod that is collecting -- all four are the same UNPROCESSED rows
    and step 5 finds them with one index query.

    `windowStart` and `windowEnd` are ignored here; there is nothing to fetch.
    """
    return _start("audit", body)


# -- reading a run back ---------------------------------------------------

@app.get(
    "/v1/runs/current",
    dependencies=[Depends(require_trigger_token)],
    tags=["runs"],
    summary="What this pod is doing",
)
def get_current_run():
    """The run in flight on *this pod*, or the last one it finished.

    Per-pod on purpose: it answers "did my trigger take?" and nothing else.
    "What happened last night" is a question about the table, not about a pod
    that may since have been replaced -- that is `GET /v1/runs/{runId}`.
    """
    state = runner.current()
    if state is None:
        return {"running": False, "message": "no run has been started on this pod"}
    return state.as_dict()


@app.get(
    "/v1/runs/{run_id}",
    dependencies=[Depends(require_trigger_token)],
    tags=["runs"],
    summary="The run summary row",
)
def get_run(run_id: str):
    """The `RUN#<runId>` row from DynamoDB: the counts, the failures, the outcome.

    This is the durable answer and it survives the pod. A row still saying
    `RUNNING` long after it should have finished is a pod that was killed.
    """
    row = run_rows.read_run(run_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"no run row for {run_id}")
    return row
