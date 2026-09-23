"""Settings, read once from the environment at import.

Falls back to .env at the repo root so local runs need no exports. On ECS or
Lambda that file is absent and the task definition supplies everything, so
nothing here changes.
"""

import os

# Three levels up: backend/common/config.py -> backend/common -> backend -> repo.
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def _load_dotenv():
    path = os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()


def _flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from None


class Settings:
    # --- Cloud Connect ---------------------------------------------------
    cc_base_url = os.environ.get(
        "CC_BASE_URL", "https://crm2.cloud-connect.in/ccpl_api/v1.4"
    )
    cc_token_id = os.environ.get("CC_TOKEN_ID", "")
    cc_user_type = os.environ.get("CC_USER_TYPE", "TENANT")
    cc_timeout_sec = _int("CC_TIMEOUT_SEC", 30)

    # Padding either side of a call when querying the call log. The window must
    # cover start_date, which precedes the answer time the webhook reports, and
    # absorb clock skew between their host and ours.
    cc_window_minutes = _int("CC_WINDOW_MINUTES", 30)

    # --- the batch run ---------------------------------------------------
    # Every number here is from section 8 of the architecture doc. They are
    # settings rather than constants because the two that bound cost --
    # audit_concurrency and deadline_min -- have to be tuned against the
    # OpenAI tier and the pod's actual lifetime, and neither is known until
    # this runs against production volume.

    # The business-day boundary, on the tenant clock. Every run fetches the
    # most recent complete `hour`-to-`hour` day: at 02:00 the floor is closed
    # and a day's call log rows have all been written, so the window never
    # closes over calls whose rows do not exist yet.
    #
    # There is no checkpoint. The window is the same calendar day however long
    # ago the last run was, so there is no stored marker to be wrong and none to
    # move; re-fetching a day already held costs nothing, because step 3 asks
    # the table which call ids it has and does nothing with those.
    day_start_hour = _int("BATCH_DAY_START_HOUR", 2)
    # How far *before* the boundary a run may start and still be treated as
    # having started on it. A cron set to 02:00 can fire a fraction early, and
    # a pod's clock can sit a second behind the scheduler's; without this, one
    # second of skew makes the run fetch the previous day instead -- and since
    # nothing carries a gap forward, that day is never audited until somebody
    # notices. The grace costs the few minutes at the end of the window, which
    # at 02:00 is a closed floor; the failure it prevents is a whole day.
    day_boundary_grace_min = _int("BATCH_DAY_BOUNDARY_GRACE_MIN", 15)
    # The call log API has no pagination and no truncation signal -- a 24-hour
    # request returns everything or quietly returns some of it. An hour is
    # about 125 calls at peak, small enough that it cannot happen, and a failed
    # slice costs one hour rather than the whole day.
    slice_minutes = _int("BATCH_SLICE_MINUTES", 60)

    # When a half-written INGESTING row is presumed dead. A download takes
    # seconds and retries for at most a couple of minutes, so at 60 nothing can
    # still be working on it -- and the row is only ever written by the same
    # pod that is about to finish or fail it, so this only catches a pod that
    # was killed outright.
    stuck_after_min = _int("BATCH_STUCK_AFTER_MIN", 60)

    # A safety cap on one run, not a target. Reaching it means the window was
    # wrong, and the run says so rather than spending its way through it.
    max_calls_per_run = _int("BATCH_MAX_CALLS_PER_RUN", 5000)
    # How long the pod is expected to live. The run stops starting new work
    # before this and exits cleanly; anything unreached stays UNPROCESSED and
    # tomorrow's run finds it on GSI-1.
    deadline_min = _int("BATCH_DEADLINE_MIN", 360)
    # Stop starting new audits this long before the deadline, so the ones in
    # flight can finish. 20 minutes is about the longest single audit measured.
    drain_margin_min = _int("BATCH_DRAIN_MARGIN_MIN", 20)

    # Downloading is network wait, so serial ingestion wastes most of its time.
    # Four rather than more because at peak this pulls ~97 GB a day from a
    # partner's server whose behaviour under load is not known (section 10).
    download_concurrency = _int("DOWNLOAD_CONCURRENCY", 4)
    # Attempts per recording before that one call is marked FAILED. One broken
    # recording must not kill a 3,000-call run.
    ingest_max_attempts = _int("INGEST_MAX_ATTEMPTS", 3)
    # Backoff base, in seconds: attempt n waits base * 2**(n-1).
    ingest_backoff_sec = _int("INGEST_BACKOFF_SEC", 5)

    # Calls audited side by side, per pod. 3,000 calls x 6 minutes is 300 hours
    # of work a day, which has to be done concurrently or not at all. The
    # ceiling is OpenAI's tokens-per-minute, not the CPU: one call in flight
    # measures ~60,000 tpm, so Tier 3 (4M) allows ~66 and Tier 2 (2M) only ~33.
    # 25 is what three pods share safely on Tier 3.
    audit_concurrency = _int("AUDIT_CONCURRENCY", 25)

    # How long a run summary row is kept. They are tiny and answer "did last
    # night's run work?" without digging through pod logs that are already
    # gone, so the retention is generous.
    run_summary_ttl_days = _int("BATCH_RUN_SUMMARY_TTL_DAYS", 90)

    # DEBUG turns on the boto3 and per-call detail. INFO is the operational
    # log: one line per step, one per call that failed.
    log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    # Structured single-line JSON, for a log shipper. Plain text otherwise,
    # which is what a person tailing `docker logs` wants.
    log_json = _flag("LOG_JSON")

    # --- the AI pipeline's cost guard -----------------------------------
    # The two models the audit and analysis stages run on, read here so the
    # batch job can refuse to start on the wrong one *before* it claims a call.
    # Measured over 100 real audits: luna costs $0.117 for a 25-minute call and
    # gpt-5.4-mini costs $0.381 for identical work. A typo in AUDIT_MODEL is
    # therefore a 3.25x bill that nothing else in the system would notice.
    audit_model = os.environ.get("AUDIT_MODEL", "gpt-5.6-luna")
    analysis_model = os.environ.get("ANALYSIS_MODEL", "gpt-5.6-luna")
    # Set to run on something else deliberately. Not a flag anyone should need.
    allow_any_model = _flag("ALLOW_ANY_MODEL")

    # --- storage ---------------------------------------------------------
    # DynamoDB and S3, always. There used to be a "local" backend writing JSON
    # files under DATA_DIR, so the AI pipeline could run without credentials --
    # but the batch job refused to start on it (the work list is a sparse index
    # and a file store has no such thing), so it was a second implementation
    # with no real caller. See backend/common/store.py.
    audio_bucket = os.environ.get("AUDIO_BUCKET", "call-auditing-media")
    calls_table = os.environ.get("CALLS_TABLE", "calls")
    # The roster. Its own table rather than more item collections in `calls`:
    # it has a completely different write rate -- eight rows changing monthly
    # against thousands a night -- and it is cached whole in-process.
    directory_table = os.environ.get("DIRECTORY_TABLE", "directory")
    aws_region = os.environ.get("AWS_REGION", "ap-south-1")

    # Only for DynamoDB Local, LocalStack or MinIO. Unset against real AWS.
    dynamo_endpoint_url = os.environ.get("DYNAMO_ENDPOINT_URL") or None
    s3_endpoint_url = os.environ.get("S3_ENDPOINT_URL") or None

    # Recordings are named customers on tape. Encryption is always on; setting
    # a KMS key upgrades it from SSE-S3 to SSE-KMS, which is what an auditor
    # asking "who can decrypt this" wants to see.
    s3_kms_key_id = os.environ.get("S3_KMS_KEY_ID", "")

    # A presigned playback URL is a bearer credential for one recording and it
    # will end up in a browser history. Minutes, not days.
    s3_presign_expiry_sec = _int("S3_PRESIGN_EXPIRY_SEC", 900)

    # boto3's defaults are tuned for a script, not a service. Ten connections
    # silently serialises everything past the tenth concurrent call, and the
    # default retry mode has no client-side rate limiting.
    aws_max_attempts = _int("AWS_MAX_ATTEMPTS", 5)
    aws_connect_timeout_sec = _int("AWS_CONNECT_TIMEOUT_SEC", 5)
    aws_read_timeout_sec = _int("AWS_READ_TIMEOUT_SEC", 60)
    aws_max_pool_connections = _int("AWS_MAX_POOL_CONNECTIONS", 25)

    # Where audio is staged for the AI pipeline. Cleared after each call unless
    # KEEP_WORKDIR is set, which is useful when a pipeline stage is misbehaving.
    work_dir = os.environ.get("WORK_DIR", os.path.join(REPO_ROOT, "workdir"))
    keep_workdir = _flag("KEEP_WORKDIR")

    # --- AI pipeline -----------------------------------------------------
    # The real pipeline needs both. Without them the stub runs instead, so the
    # wiring can be exercised end to end with no spend and no keys.
    soniox_api_key = os.environ.get("SONIOX_API_KEY", "")
    openai_api_key = os.environ.get("OPENAI_API_KEY", "")
    force_stub_pipeline = _flag("STUB_PIPELINE")

    @property
    def pipeline_available(self):
        return bool(self.soniox_api_key and self.openai_api_key)

    @property
    def pipeline_mode(self):
        """Which pipeline will actually run — the one thing worth reporting.

        Separate from pipeline_available because STUB_PIPELINE overrides having
        keys, and a boot line that says "real" while the stub runs is worse than
        no line at all.
        """
        if self.force_stub_pipeline:
            return "stub (STUB_PIPELINE set)"
        if not self.pipeline_available:
            missing = []
            if not self.soniox_api_key:
                missing.append("SONIOX_API_KEY")
            if not self.openai_api_key:
                missing.append("OPENAI_API_KEY")
            return f"stub (no {', '.join(missing)})"
        return "real"


settings = Settings()
