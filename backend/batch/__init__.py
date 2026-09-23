"""The batch job. One run does the whole pass; the API decides when.

    POST /v1/runs         the full pass, call log included
    POST /v1/runs/audit   the same run with the fetch left out

There used to be a CLI here and the pod exited when it finished. It is now a
service that stays up and runs when it is asked to, so that a scheduler, a pod
starting, or a person with curl can all trigger the same thing the same way.
What a run *does* did not change: DynamoDB holds the to-do list and an
in-process queue hands it out within a run, so a run that dies is resumed by
the next one rather than recovered by hand.

    run.py          the run itself, and the run-summary row
    cloudconnect.py their call log and recording API
    collect.py      steps 1-3  the 02:00-to-02:00 day, fetched and sorted
    ingest.py       step 4     download the recordings to S3, rows UNPROCESSED
    queue.py        step 5     the job queue, and filling it from the table
    audit.py        step 6     the AI pipeline, and the workers that run it
"""
