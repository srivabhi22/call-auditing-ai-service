"""Configuration, the row shapes, and everything that talks to AWS.

    config.py   settings, read once from the environment
    trace.py    logging
    models.py   the call log record, the DynamoDB row, statuses, timestamps
    agents.py   the roster file, and resolving an extension to a person
    store.py    DynamoDB, S3, and the keys that address them

Everything above this is the batch job. Nothing in here knows what a queue or a
pod is, and nothing in here imports from `backend.batch`.

The separation is not decoration. The read side of this system — the dashboard
that renders the data — lives in the ml-dashboard repo and agrees with us about
exactly four things, all of them in here: the table's key shape and indexes
(`store.py`), the bucket's key shape (`store.py`), the row (`models.CallRow`)
and the roster (`agents.py`). A change in this package reaches across a repo
boundary; a change in `backend/batch/` does not.
"""
