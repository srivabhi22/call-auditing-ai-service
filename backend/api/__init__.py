"""The HTTP trigger. The only thing in this repo that stays up.

    POST /v1/runs         the full pass: call log -> S3 -> queue -> audit
    POST /v1/runs/audit   the same run with step 1-4 left out

It replaced a CLI whose invocation was the trigger and whose exit code was the
result. The job it starts is unchanged -- `backend.batch.run.Run` is the same
object the command built -- but *when* it happens is now somebody else's
decision: a pod starting, a scheduler, a CI step, or a person with curl.

    app.py      the endpoints, and what they are allowed to start
    runner.py   the one background run this pod may have in flight
"""
