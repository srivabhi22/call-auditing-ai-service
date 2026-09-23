# The whole repo, not just backend/ — the audit pipeline shells out to the four
# scripts at the root (transcribe, clean, analyze, audit), so a backend-only
# image boots fine and then fails on the first call it has to audit.
FROM python:3.12-slim

# PYTHONUNBUFFERED because the run's log is read live while it is going, and
# container log drivers lose buffered output when a task is stopped — which for
# a job with a deadline is exactly when you want the last few lines.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root, and the one writable path owned by it. Everything else in /app is
# read-only to the process.
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /var/lib/call-auditing/workdir \
 && chown -R app:app /var/lib/call-auditing /app
USER app

# The work directory holds AUDIT_CONCURRENCY recordings at once — 25 × ~32 MB
# is about 1 GB, so the pod needs at least that much ephemeral storage.
ENV WORK_DIR=/var/lib/call-auditing/workdir \
    LOG_LEVEL=INFO \
    PORT=8000

EXPOSE 8000

# This was a job that ran once and exited; it is a service that stays up and
# runs when it is asked to. The difference that matters operationally: the
# container's exit code no longer carries the result. A run's outcome is on its
# `RUN#<runId>` row and at `GET /v1/runs/{runId}`, and a container that exits at
# all is now a failure rather than a completed night.
#
#   POST /v1/runs         the full pass, call log included
#   POST /v1/runs/audit   the same run with the fetch left out
#
# Set RUN_ON_STARTUP=full to have the pod trigger itself as it comes up.
#
# **One worker, deliberately.** The run is a process-wide singleton — the job
# queue is module state — so a second uvicorn worker would be a second pod's
# worth of runs inside one container, sharing nothing and colliding on the
# in-process lock that is supposed to stop exactly that. Scale by adding pods,
# which the design already supports: each builds its own work list from the
# UNPROCESSED index and a call another pod finished is dropped by one row read.
#
# Split the work across pods once one is not enough (§7 of the architecture
# doc): one on RUN_ON_STARTUP=full, two or three triggered on /v1/runs/audit.
#
# No HEALTHCHECK: Kubernetes has its own probes and a Docker healthcheck is
# ignored there. Point livenessProbe at /healthz and readinessProbe at /readyz.
# /healthz must be the liveness one — /readyz touches AWS, and restarting a pod
# mid-run because DynamoDB had a bad minute fixes nothing.
CMD ["sh", "-c", "exec uvicorn backend.api.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-graceful-shutdown 30"]
