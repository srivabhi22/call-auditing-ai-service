#!/usr/bin/env bash
#
# Rehearse the deployed job end to end, against the fakes in
# docker-compose.sim.yml. No AWS, no model spend, nothing written anywhere real.
#
#   docker compose -f docker-compose.sim.yml up -d
#   ./simulate.sh [N]        N = the per-run cap, default 5
#
# What this proves, in order: the job provisions its own table and bucket from
# nothing; a cold first run fetches yesterday's calls and audits them; a second
# run of the same day is a no-op; and the exit code says which of the three
# happened.
set -euo pipefail

export DYNAMO_ENDPOINT_URL=http://localhost:8000
export S3_ENDPOINT_URL=http://localhost:9000
# DynamoDB Local and MinIO both want credentials and neither checks them. The
# region has to be set because boto3 refuses to sign without one.
export AWS_ACCESS_KEY_ID=simulated
export AWS_SECRET_ACCESS_KEY=simulated-secret
export AWS_REGION=eu-north-1
export CALLS_TABLE=calls
export DIRECTORY_TABLE=directory
export AUDIO_BUCKET=call-auditing-sim
# The four AI stages are replaced by a schema-valid stub, so the whole path runs
# with no keys and no spend.
export STUB_PIPELINE=1
# A pod is alive for hours; a rehearsal should not be. This is what makes the
# drain margin observable in a minute instead of six.
export BATCH_DEADLINE_MIN=${BATCH_DEADLINE_MIN:-25}
export BATCH_DRAIN_MARGIN_MIN=${BATCH_DRAIN_MARGIN_MIN:-20}

MAX=${1:-5}

echo "── provisioning (as a first deploy would) ───────────────────────────────"
python3 -m backend.tools.provision_aws
python3 -m backend.tools.provision_aws --verify

echo
echo "── seeding the roster ───────────────────────────────────────────────────"
# The roster is the one thing the job cannot invent: Cloud Connect gives an
# extension and only the directory table maps it to a person and a team.
python3 -m backend.tools.seed_demo --directory

# There is no CLI any more: a run is started over HTTP, so the rehearsal starts
# the service the pod would run and triggers it the way the pod would be
# triggered. `run_once` blocks until the run leaves flight, which is what a
# scheduled invocation used to do by exiting.
export PIPELINE_TRIGGER_TOKEN=${PIPELINE_TRIGGER_TOKEN:-simulate}
PORT=${PORT:-8088}
BASE="http://127.0.0.1:${PORT}"

echo
echo "── starting the service ─────────────────────────────────────────────────"
python3 -m uvicorn backend.api.app:app --host 127.0.0.1 --port "$PORT" &
API_PID=$!
trap 'kill "$API_PID" 2>/dev/null' EXIT

for _ in $(seq 1 30); do
  curl -sf "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -s "$BASE/readyz"; echo

run_once() {
  curl -s -X POST "$BASE/$1" \
    -H "X-Trigger-Token: $PIPELINE_TRIGGER_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"maxCalls\": $MAX}"
  echo
  # Poll rather than sleep: the run is asynchronous now, and how long it takes
  # is the thing being measured.
  while [ "$(curl -s "$BASE/healthz" | python3 -c 'import sys,json;print(json.load(sys.stdin)["running"])')" = "True" ]; do
    sleep 2
  done
  curl -s "$BASE/v1/runs/current" -H "X-Trigger-Token: $PIPELINE_TRIGGER_TOKEN"; echo
}

echo
echo "── run 1: a cold pod ────────────────────────────────────────────────────"
run_once v1/runs

echo
echo "── run 2: the same pod again, same day ──────────────────────────────────"
# Should collect the same window, recognise every call already stored, queue
# nothing and audit nothing. This is the property that makes "just trigger it
# again" the answer to almost every failure.
run_once v1/runs

echo
echo "── run 3: the audit-only trigger ────────────────────────────────────────"
# The second endpoint: the same run with the call log fetch left out. With
# everything already PROCESSED it should queue nothing, which is what an
# auditor pod finding no work looks like.
run_once v1/runs/audit

echo
echo "── what is in the table ─────────────────────────────────────────────────"
python3 - <<'PY'
import collections
from backend.tools.provision_aws import _scan_all
items = _scan_all("calls")
calls = [i for i in items if str(i.get("PK", "")).startswith("CALL#")]
runs = [i for i in items if str(i.get("PK", "")).startswith("RUN#")]
print("calls:", dict(collections.Counter(c.get("processingStatus") for c in calls)))
print("runs :", [(r.get("outcome"), r.get("audited"), r.get("queued")) for r in runs])
PY
