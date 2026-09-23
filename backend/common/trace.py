"""Stage logging, in one place.

Every line the job emits goes through here. It was a bare `print` per stage,
which is fine for watching one call go past and useless the morning after a
3,000-call run: no timestamp, no level, no way to tell which of three pods wrote
it, and nothing a log shipper can parse.

The call sites did not change -- `db(...)`, `s3(...)`, `audit(...)` are the same
functions with the same signatures. What changed is where they go:

    trace(source, message)  ->  logging.getLogger("callaudit.<source>").info(...)

so the storage layer, the AI pipeline and the batch steps all land in one
stream, at one level, with one format.

Two pieces of context ride along on every record, because they are what makes a
line searchable six hours later:

    runId    which run wrote it. Three auditor pods interleave their output in
             CloudWatch; without this they are indistinguishable.
    callId   which call it is about. Set for the duration of one call by
             `call_context`, so nothing has to thread it through by hand -- and
             cleared afterwards, so a line from the orchestration is not
             mislabelled with the last call it happened to touch.

Both are ContextVars rather than globals: the audit step runs 25 calls at once
in a thread pool, and a global would have every thread overwriting the others'
call id. `copy_context()` at submit time is what carries the run id into each
worker thread.

LOG_JSON=1 emits one JSON object per line instead, for a shipper that wants
fields rather than a regex.
"""

import contextlib
import json
import logging
import os
import sys
import time
from contextvars import ContextVar

# The two identifiers that make a line traceable. Empty until a run sets them.
_run_id = ContextVar("run_id", default="")
_call_id = ContextVar("call_id", default="")

# The source labels used across the repo, kept here so the width of the column
# is a constant rather than whatever the longest one happens to be today.
_LABEL_WIDTH = 7

_configured = False


class _ContextFilter(logging.Filter):
    """Attach runId and callId to every record, including boto3's own.

    A filter rather than a custom Logger class: third-party libraries build
    their loggers before this module is imported, and a filter on the handler
    reaches those too.
    """

    def filter(self, record):
        record.run_id = _run_id.get()
        record.call_id = _call_id.get()
        # "callaudit.db" -> "db". Anything else -- a module logger, botocore --
        # is reduced to its last component: the full dotted path is 40 columns
        # of prefix repeated on every line, and the part that identifies it is
        # always the end.
        name = record.name
        record.source = (
            name.split(".", 1)[1] if name.startswith("callaudit.")
            else name.rsplit(".", 1)[-1]
        )
        return True


class _PlainFormatter(logging.Formatter):
    """What someone tailing the pod's output reads.

    UTC throughout. The pod, the tenant and whoever is reading are not reliably
    in the same zone, and a log that needs the reader to know which one it was
    written in is a log that gets misread during an incident.
    """

    converter = time.gmtime
    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03dZ"

    def format(self, record):
        stamp = self.formatTime(record)
        label = f"[{record.source.upper():<{_LABEL_WIDTH}}]"
        prefix = f"{stamp} {record.levelname:<5} {label}"
        if run := getattr(record, "run_id", ""):
            prefix += f" run={run}"
        if call := getattr(record, "call_id", ""):
            prefix += f" call={call}"
        text = f"{prefix}  {record.getMessage()}"
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


class _JsonFormatter(logging.Formatter):
    """One object per line, for a shipper.

    `ensure_ascii=False` because agent names and customer-facing text are not
    ASCII, and escaping them makes the field unreadable in every viewer.
    """

    converter = time.gmtime

    def format(self, record):
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "source": record.source,
            "msg": record.getMessage(),
        }
        if run := getattr(record, "run_id", ""):
            payload["runId"] = run
        if call := getattr(record, "call_id", ""):
            payload["callId"] = call
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure(level=None, json_mode=None, force=False):
    """Install the handler. Idempotent -- calling it twice does not double lines.

    stdout rather than stderr: everything here is ordinary operational output,
    and a container log driver that treats stderr as an error stream would
    otherwise flag a healthy run as failing.
    """
    global _configured
    if _configured and not force:
        return

    from .config import settings

    level = (level or settings.log_level or "INFO").upper()
    json_mode = settings.log_json if json_mode is None else json_mode

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if json_mode else _PlainFormatter())
    handler.addFilter(_ContextFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))

    # boto3 at INFO narrates every retry and every signature; at DEBUG it prints
    # entire request bodies, which for this table means customer phone numbers
    # in the log. Pinned to WARNING unless someone is deliberately debugging the
    # AWS layer, which is what AWS_LOG_LEVEL is for.
    aws_level = os.environ.get("AWS_LOG_LEVEL", "WARNING").upper()
    for noisy in ("boto3", "botocore", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(getattr(logging, aws_level, logging.WARNING))

    _configured = True


def set_run_id(run_id):
    """Tag every subsequent line with this run. Returns the token to reset."""
    return _run_id.set(run_id or "")


def current_run_id():
    return _run_id.get()


@contextlib.contextmanager
def call_context(call_id):
    """Tag every line inside the block with this call, then stop.

    The reset is in a finally on purpose: a call that raises is exactly the one
    whose surrounding lines must not inherit its id.
    """
    token = _call_id.set(str(call_id or ""))
    try:
        yield
    finally:
        _call_id.reset(token)


def trace(source, message, level=logging.INFO, **kwargs):
    logging.getLogger(f"callaudit.{source}").log(level, message, **kwargs)


# The stage labels. Same names and signatures as before, so no call site moved.
def boot(message):
    trace("boot", message)


def work(message):
    trace("work", message)


def db(message):
    # One line per DynamoDB write. At 3,000 calls a run that is ~12,000 lines of
    # storage chatter around the lines an operator actually reads, so it sits at
    # DEBUG and LOG_LEVEL=DEBUG is how a storage problem is investigated.
    trace("db", message, level=logging.DEBUG)


def s3(message):
    trace("s3", message, level=logging.DEBUG)


def audit(message):
    trace("audit", message)


def ai(message):
    trace("ai", message)


def batch(message):
    """The run's own narration: one line per step, and the counts it produced."""
    trace("batch", message)


def warn(source, message):
    trace(source, message, level=logging.WARNING)


def error(source, message, exc_info=False):
    trace(source, message, level=logging.ERROR, exc_info=exc_info)


# Configured at import rather than from main(). `storage/__init__` emits its
# boot line while it is being imported, which is before any main() has run --
# with no handler installed that line is swallowed by logging's lastResort,
# which only passes WARNING and above. The one thing a boot line exists to do
# is to say what the process connected to, so losing it is not acceptable.
configure()
