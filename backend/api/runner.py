"""The one run this pod may have in flight, and the thread it happens on.

A run takes hours. No HTTP client waits that long and no load balancer would
let it, so a trigger starts the work and returns immediately with a run id; the
result is read back from the run row, which is where it was always written.

**One at a time, per pod.** `queue.job_queue` is a module-level singleton, so
two runs in one process would share one queue and hand the same call to two
workers. That is what `_lock` and `_current` prevent, and it is a property of
this process rather than of the system: two *pods* running at once is fine and
always was -- they build the same work list from the same index, and the
duplicate is dropped by the row read at the top of `audit_one`.

**A daemon thread, not an asyncio task.** The run is blocking and CPU-adjacent
throughout -- boto3, subprocesses, two thread pools of its own -- and none of it
is awaitable. On the event loop it would block every health probe for the length
of the run, which is the one thing that must keep answering.
"""

import threading
import time

from ..batch.run import Run
from ..common.trace import batch, error


class RunState:
    """What a trigger started, and how it ended. Read by `GET /v1/runs/current`.

    Deliberately thin: the counts, the failures and the outcome are on the run
    row in DynamoDB, which survives the pod and is what anyone asking the
    morning after will read. This exists so that a caller who has just triggered
    a run can see it is alive without waiting for it to finish.
    """

    def __init__(self, run_id, mode, started_at, trigger):
        self.run_id = run_id
        self.mode = mode
        self.started_at = started_at
        self.trigger = trigger
        self.finished_at = None
        self.exit_code = None
        self.outcome = None
        self.error = None

    @property
    def running(self):
        return self.finished_at is None

    def as_dict(self):
        return {
            "runId": self.run_id,
            "mode": self.mode,
            "trigger": self.trigger,
            "running": self.running,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "elapsedSec": round(
                (self.finished_at or time.time()) - self.started_at, 1
            ),
            "outcome": self.outcome,
            "exitCode": self.exit_code,
            "error": self.error,
        }


class AlreadyRunning(Exception):
    """A run is in flight on this pod. The caller gets 409 and the run id."""

    def __init__(self, state):
        super().__init__(f"run {state.run_id} is already in flight")
        self.state = state


_lock = threading.Lock()
_current = None
_last = None


def current():
    """The run in flight, or the last one to finish, or None."""
    with _lock:
        return _current or _last


def is_running():
    with _lock:
        return _current is not None and _current.running


def start(mode, trigger="api", **kwargs):
    """Build a `Run`, start it on a thread, and return its state immediately.

    The `Run` is constructed *here*, on the calling thread, so that anything
    that fails while building it -- a window that does not parse, a bad
    argument -- fails the HTTP request with a 400 instead of disappearing into a
    background thread and surfacing only in the log.
    """
    global _current, _last

    with _lock:
        if _current is not None and _current.running:
            raise AlreadyRunning(_current)

        run = Run(mode=mode, **kwargs)
        state = RunState(run.run_id, mode, time.time(), trigger)
        _current = state

    def work():
        global _current, _last
        try:
            state.exit_code = run.execute()
            state.outcome = run.outcome
        except Exception as exc:  # noqa: BLE001 -- the thread must not die
            # silently; `Run.execute` has its own try/finally and writes the
            # summary row, so reaching here means something outside it broke.
            state.exit_code = 2
            state.outcome = "FAILED"
            state.error = f"{type(exc).__name__}: {exc}"
            error("api", f"run {run.run_id} crashed outside the run: {exc!r}",
                  exc_info=True)
        finally:
            state.finished_at = time.time()
            with _lock:
                _last = state
                _current = None
            batch(f"run {run.run_id} finished: {state.outcome} "
                  f"in {state.as_dict()['elapsedSec']}s")

    thread = threading.Thread(
        target=work, name=f"run-{run.run_id}", daemon=True
    )
    thread.start()
    return state


def wait(timeout=None):
    """Block until the run in flight finishes. Used only by shutdown.

    Returns True if nothing is running by the time it returns. A pod being
    replaced mid-run is an ordinary event -- the rows are UNPROCESSED and the
    next run picks them up -- so this is a courtesy, not a guarantee, and the
    caller carries on either way.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while is_running():
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(0.5)
    return True
