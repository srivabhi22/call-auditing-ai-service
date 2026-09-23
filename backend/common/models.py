"""The shapes that cross module boundaries.

`CallLogRecord` is one row of what the call log API returns; `CallRow` is the
DynamoDB item the job writes. Everything the batch job knows about a call starts
as the first and ends as the second.

`WebhookEvent` used to be here too -- the form Cloud Connect posted when a call
ended, whose only handle was a `unique_token`. The job reads the call log
directly now, so it always has a `callId` before it writes anything, and dedup
is `put_if_absent`'s condition on the partition key rather than a token lookup.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

# Cloud Connect's timestamps, in every field of every response...
CC_TIME = "%Y-%m-%d %H:%M:%S"
# ...except the two in the call log URL path, which are to the minute.
CC_URL_TIME = "%Y-%m-%d-%H:%M"

# Cloud Connect send no offset with their timestamps. They are read as the
# tenant's local time — IST — and converted on the way in, so everything stored
# is UTC. If the tenant is ever not on IST this constant is the only change.
TENANT_TZ = timezone(timedelta(hours=5, minutes=30))


def utcnow():
    return datetime.now(timezone.utc)


def iso(moment):
    """The one timestamp format this backend writes.

    Seconds, `Z`, no microseconds. It has to sort lexicographically, because
    every index sort key is compared as a string and a microsecond suffix on
    some rows and not others breaks that silently.
    """
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    """Back to a datetime, or None. Never raises -- an unparseable timestamp
    from a row or a command line should not be the thing that ends a run."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _parse_cc_time(raw):
    if not raw:
        return None
    try:
        return datetime.strptime(raw, CC_TIME)
    except ValueError:
        return None


def to_iso_utc(raw):
    """Cloud Connect's local timestamp as ISO-8601 UTC, which is what the table
    stores and what the sort key is built from.

    Their timestamps carry no offset. They are read as IST, which is what the
    tenant runs on — if that turns out to be wrong the fix is one constant here
    rather than a migration, which is why the conversion lives in one place.
    """
    parsed = _parse_cc_time(raw)
    if parsed is None:
        return ""
    return (
        parsed.replace(tzinfo=TENANT_TZ)
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def to_ten_digits(raw):
    """A phone number as the ten digits that identify it, or "" if it is not one.

    Cloud Connect send the same number four ways -- "917050356192" with the
    country code, "+917307148543" with a plus, "07050356192" with a trunk zero,
    and "7971509217" bare -- so a row stored as received cannot be matched
    against a row stored differently, and the dashboard shows whichever form
    happened to arrive.

    Short values are returned empty rather than padded: an extension like "706"
    and an IVR id like "98765" are not phone numbers, and storing them in a phone
    field is how an extension ends up rendered as a customer.
    """
    digits = "".join(c for c in str(raw or "") if c.isdigit())
    if len(digits) < 10:
        return ""
    return digits[-10:]


# What Cloud Connect put in a field that has nothing in it. They do not send
# empty strings; they send one of these, and each reads as a real value to
# anything that only checks for truthiness.
_NO_VALUE = frozenset({"", "-", "n/a", "na", "null", "none"})


@dataclass(frozen=True)
class CallLogRecord:
    """One row of the call log API's data array.

    Kept as the raw payload with typed accessors over it. The billing fields —
    the dozen did_/gw_ pulse and duration ones — are ignored.
    """

    payload: dict = field(repr=False)

    def _s(self, key):
        value = self.payload.get(key)
        return str(value).strip() if value is not None else ""

    def _i(self, key):
        try:
            return int(self.payload.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    call_id = property(lambda self: self._s("callid"))
    unique_token = property(lambda self: self._s("unique_token"))
    direction = property(lambda self: self._s("call_direction"))
    caller = property(lambda self: self._s("caller"))
    caller_name = property(lambda self: self._s("caller_name"))
    callee = property(lambda self: self._s("callee"))
    callee_name = property(lambda self: self._s("callee_name"))
    outbound_caller_id = property(lambda self: self._s("outbound_caller_id"))
    did = property(lambda self: self._s("did"))
    start_date = property(lambda self: self._s("start_date"))
    answer_time = property(lambda self: self._s("answer_time"))
    end_date = property(lambda self: self._s("end_date"))
    call_sec = property(lambda self: self._i("call_sec"))
    answer_sec = property(lambda self: self._i("answer_sec"))
    call_status = property(lambda self: self._s("call_status"))
    hangup_by = property(lambda self: self._s("hangup_by"))
    hangup_reason = property(lambda self: self._s("hangup_reason"))

    @property
    def recording_url(self):
        """`call_rec_path`, or "" when there is no recording.

        Cloud Connect fill the field with a placeholder rather than leaving it
        empty -- measured against live traffic, 21 of 26 answered calls in one
        hour carried `"-"` and only 5 a real URL. Read literally that is a
        truthy value, so `is_auditable` says yes, the row is written INGESTING,
        and the download fails with `unknown url type: '-'`. Every one of those
        calls then sits as FAILED needing a human, for the entirely ordinary
        reason that the PBX did not record it.

        Same shape as `agent_did`'s "N/A" problem, and handled the same way:
        a placeholder is not a value.
        """
        raw = self._s("call_rec_path")
        if raw.lower() in _NO_VALUE or not raw.lower().startswith(("http://", "https://")):
            return ""
        return raw

    @property
    def is_inbound(self):
        return self.direction.upper() == "INBOUND"

    @property
    def agent_extension(self):
        """Which side of the call the agent is on depends on direction.

        Outbound: the agent's extension dials, so it is `caller`. Inbound: the
        customer dials in and the extension receives, so it is `callee`. Mapping
        `caller` unconditionally files every inbound call under an agent named
        after a phone number, which the dashboard then groups on.
        """
        return self.callee if self.is_inbound else self.caller

    @property
    def customer_phone(self):
        """The other party's number, as ten digits.

        Outbound: the agent's extension dials, so the customer is `callee`.
        Inbound: the customer dials in, so they are `caller`.
        """
        return to_ten_digits(self.caller if self.is_inbound else self.callee)

    @property
    def agent_did(self):
        """The agent's own number, as ten digits.

        Which field holds it depends on direction, and the unused one is not
        empty but filled with a placeholder:

            OUTBOUND   outbound_caller_id "07971509211"   did "N/A"
            INBOUND    did                "07971509211"   outbound_caller_id ""

        Reading `did` unconditionally therefore stores the string "N/A" against
        every outbound call, which is most of them. Both are tried regardless of
        direction so a row that fills the other one still resolves.
        """
        primary = self.did if self.is_inbound else self.outbound_caller_id
        return to_ten_digits(primary) or to_ten_digits(
            self.outbound_caller_id if self.is_inbound else self.did
        )

    @property
    def agent_display_name(self):
        return self.callee_name if self.is_inbound else self.caller_name

    @property
    def started_at_iso(self):
        return to_iso_utc(self.start_date)

    @property
    def is_auditable(self):
        """Answered, with a recording to fetch."""
        return (
            self.call_status.lower() == "answered"
            and bool(self.recording_url)
            and self.call_sec > 0
        )


class ProcessingStatus:
    """The spec names UNPROCESSED and PROCESSED. The rest are operational.

    INGESTING marks a row claimed but whose audio is not up yet; FAILED exists
    so a call that broke is visible rather than absent.

    There is no SKIPPED. A call that was never answered or that the PBX kept no
    recording for is not written at all -- step 3 drops it, the way it drops a
    call belonging to an extension not on the roster. It used to be stored, so
    that the sweep would not rediscover and re-check it on every run; the
    re-check turned out to be two field comparisons on a row already fetched,
    while the row was 200 bytes and put two thirds of the table in front of
    every reader who then had to filter it back out. The call log remains the
    record of what was not audited, and the run row carries the count and the
    reasons.
    """

    INGESTING = "INGESTING"
    UNPROCESSED = "UNPROCESSED"
    # Legacy. Nothing writes it since the work became queued jobs -- a job is
    # taken by exactly one worker, which is what this status used to say. Kept
    # because rows written by the old build still carry it, and
    # `dispatch._revive_legacy` reads them back into the work list.
    AUDITING = "AUDITING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    DISCARDED = "DISCARDED"


# The statuses that mean "something still has to happen to this call". These and
# only these are in GSI-1; see `CallRow.workStatus`.
WORK_STATUSES = frozenset({
    ProcessingStatus.INGESTING,
    ProcessingStatus.UNPROCESSED,
    ProcessingStatus.AUDITING,
    ProcessingStatus.FAILED,
})


def audio_key_for(call_id, started_at_iso):
    """The recording's key. See `storage/layout.py` for the shape."""
    from . import store as layout

    return layout.audio_key(call_id, started_at_iso)


def audit_key_for(call_id, started_at_iso):
    """The audit document's key."""
    from . import store as layout

    return layout.artifact_key(call_id, started_at_iso, "audit")


# The four score bands the dashboard colours on. The 60 and 75 boundaries are
# the ones the report UI already uses; 85 splits its top band so "good" does not
# cover everything from a pass to a perfect call.
SCORE_BANDS = ((85, "EXCELLENT"), (75, "GOOD"), (60, "NEEDS_IMPROVEMENT"))


def score_band(score):
    """POOR | NEEDS_IMPROVEMENT | GOOD | EXCELLENT, or "" when unscored."""
    if score is None:
        return ""
    for floor, name in SCORE_BANDS:
        if score >= floor:
            return name
    return "POOR"


@dataclass
class CallRow:
    """The DynamoDB item, exactly as the table spec defines it.

        PK = CALL#<callId>
        SK = META

    The sort key is a constant. It makes each call an item collection, so
    sibling rows (a coaching note, an appeal, a re-audit) can be added later and read with the call in one query, with no migration. The time
    ordering every list view needs lives on the index sort keys instead, which is
    where a range query can actually use it -- the base table is only ever read
    by id.

    callId is Cloud Connect's own `callid`. The spec's example is a ULID, but
    generating one would mean storing their id alongside it and keeping a second
    index to get back from one to the other; theirs is already immutable and
    unique, so it is used directly. If the dashboard needs ULIDs later this is
    the one place that changes.

    The audit fields are absent until phase 2 fills them, so a row is written
    twice: once at ingestion and once when the audit lands.
    """

    callId: str
    customerPhone: str
    startedAt: str
    durationSec: int
    # agentExtension is the key, not the name. Names in the roster are typed by
    # hand and arrive inconsistently cased — "NAMAN SHARMA" next to "rajjan
    # prajapati" — and two agents can share one. The extension is Cloud
    # Connect's own identifier for the handset and is what the dashboard groups
    # on; the name rides along for display.
    #
    # It is *not* stored as an attribute of its own: `gsi2pk` is
    # `AGENT#<ext>#<month>` and already carries it, and `hydrate` reads it back
    # out. Same for callId and startedAt, which PK and `ord` carry.
    agentExtension: str
    agentName: str
    processingStatus: str
    # When processingStatus last changed. One timestamp rather than the three
    # this row used to carry -- ingestedAt, auditedAt and failedAt -- because
    # each of those was only ever written at one status, so at most one of them
    # was ever the answer to "when did this row last move?". The stall sweep
    # asks exactly that question, and asked it of ingestedAt, which is why
    # there were three.
    statusAt: str = ""
    callDirection: str = ""

    # Filled by the audit worker. Deliberately narrow: the full audit document
    # lives in S3 at auditKey, and only what a list view has to sort, filter or
    # colour on is copied onto the row. Anything else belongs in the document.
    score: Optional[float] = None
    flagCount: Optional[int] = None
    # The highest flag severity on the call — LOW, MEDIUM or HIGH, matching
    # audit_schema.Severity. Absent, not "NONE", when the audit found no flags:
    # an absent attribute keeps those rows out of any severity filter, which is
    # what "no flags on this call" should mean.
    auditStatus: str = ""
    # True when audit_call.py set `scores.disqualified`: misconduct that voids
    # the scorecard rather than lowering it. Such a call carries no score at
    # all, and `score = None` cannot say so on its own -- a FAILED call has no
    # score either. This is the only attribute that distinguishes
    # "disqualified" from "never audited", and the dashboard's fatal tile and
    # filter are both it.
    disqualified: bool = False
    # Every metric the scorecard awards, copied off the audit: the eight
    # criteria and their marks. ~110 bytes that save one S3 GetObject per call
    # on any panel that shows a breakdown.
    #
    # The criteria and not the six sections, because a section is exactly the
    # sum of its criteria and so is derivable -- `hydrate` puts `sectionMarks`
    # back for callers that only want that. Storing the sections was strictly
    # less information for the same bytes.
    criterionMarks: Optional[dict] = None
    # Why the last failure failed. Sparse: absent on a row that never failed,
    # and cleared when one succeeds.
    failureReason: str = ""

    @property
    def pk(self):
        return f"CALL#{self.callId}"

    SK_VALUE = "META"

    @property
    def sk(self):
        return self.SK_VALUE

    @property
    def timeOrdinal(self):
        """The sort key every index uses: time first, id to break ties.

        Two calls can start in the same second on different handsets, so the id
        is appended -- without it the second one overwrites the first in the
        index.
        """
        return f"{self.startedAt}#{self.callId}"

    @property
    def dateKey(self):
        from . import store as layout

        return layout.date_key(self.startedAt)

    @property
    def monthKey(self):
        return self.dateKey[:7]

    @property
    def workStatus(self):
        """The status while work is outstanding, or "" once it is not.

        Written as GSI-1's partition key and *removed* at PROCESSED or
        DISCARDED, which drops the row out of the index. Indexing every status
        instead would pile every call ever finished into one PROCESSED partition
        forever -- a hot partition, and a full copy of the table nobody reads.
        """
        return (
            self.processingStatus
            if self.processingStatus in WORK_STATUSES else ""
        )

    @property
    def audioKey(self):
        """Where the recording is, derived rather than stored.

        `store.audio_key` is a pure function of the call id and its start, both
        of which PK and `ord` already carry -- so storing the result was ~45
        bytes on every row to hold something reconstructible in a string format.

        A call that was never auditable has no recording and no key. That used
        to be said by storing `""`; it is now said by `processingStatus`, which
        had to be stored anyway and already means exactly that.
        """
        if self.processingStatus == ProcessingStatus.DISCARDED:
            return ""
        return audio_key_for(self.callId, self.startedAt)

    @property
    def auditKey(self):
        """Where the audit document is. Only a PROCESSED call has one."""
        if self.processingStatus != ProcessingStatus.PROCESSED:
            return ""
        return audit_key_for(self.callId, self.startedAt)

    @property
    def scoreBand(self):
        """POOR | NEEDS_IMPROVEMENT | GOOD | EXCELLENT, or "" when unscored.

        A pure function of `score`, so it is computed on read. It was stored so
        that it could key an index later without a backfill; nothing ever keyed
        it, and two copies of one number is how an API and a UI drift on where
        a boundary is.
        """
        return score_band(self.score)

    def index_keys(self):
        """The GSI key attributes. Empty values are dropped on write, which is
        what makes GSI-1 sparse rather than needing a separate table.

        One sort key, `ord`, for all three indexes rather than three identical
        copies of it. DynamoDB is happy for one attribute to be the sort key of
        several GSIs, and `gsi1sk`, `gsi2sk` and `gsi3sk` were byte-for-byte the
        same string -- ~90 bytes of the same value written three times on every
        row, and three times again into each index item.
        """
        return {
            "gsi1pk": self.workStatus,
            "gsi2pk": f"AGENT#{self.agentExtension}#{self.monthKey}",
            "gsi3pk": f"DAY#{self.dateKey}",
            "ord": self.timeOrdinal,
        }

    @classmethod
    def from_call_log(cls, record, agent, status):
        """One call log row as the row this table stores.

        `agent` is still resolved and still passed: the roster decides the
        *name*, and a call whose extension is not on it does not reach here at
        all. What is no longer copied off it is `team`, `teamLeaderName`,
        `agentAccountId` and `agentDid` -- four attributes denormalised onto
        every call to save an N+1 against the roster. The read side
        caches that table whole already, so resolving them from
        `agentExtension` in memory costs nothing and removes the backfill that
        a denormalised copy needs every time somebody changes team.
        """
        return cls(
            callId=record.call_id,
            customerPhone=record.customer_phone,
            startedAt=record.started_at_iso,
            durationSec=record.call_sec,
            # The call's own extension wins over the roster's. They differ only
            # when the DID fallback resolved a name for an extension the roster
            # does not list — and in that case the extension is real and the
            # roster is the thing that is wrong. Filing it under the matched
            # agent's extension would group the two together and hide the gap.
            agentExtension=record.agent_extension or agent.extension,
            # The roster's name when the agent is in it, the call's own name
            # when they are not. `agent.name` cannot be the condition: an
            # unmapped agent still gets a name from the directory -- the
            # "Unmapped extension 706" placeholder -- which is truthy and would
            # always win over the real thing. `known` is the question actually
            # being asked.
            #
            # Cloud Connect's caller_name is usually just the extension repeated,
            # so this is often "706" rather than a person. That is still better
            # than a placeholder: it is what their system believes, and it says
            # the roster is missing an entry rather than burying that in prose.
            agentName=(
                agent.name if agent.known
                else (record.agent_display_name or agent.name)
            ),
            processingStatus=status,
            callDirection=record.direction,
            statusAt=iso(utcnow()),
        )

    # The attributes that are actually written. Everything else a caller can
    # read off a row -- callId, startedAt, agentExtension, audioKey, auditKey,
    # scoreBand, dateKey -- is recomputed by `hydrate` from the keys, which
    # carry it already.
    STORED = (
        "processingStatus", "statusAt", "durationSec", "customerPhone",
        "callDirection", "agentName",
        "score", "flagCount", "auditStatus", "disqualified", "criterionMarks",
        "failureReason",
    )

    def to_item(self):
        """The item as DynamoDB stores it, keys first.

        Seventeen attributes at most, and fewer on a row that has not been
        audited. It was thirty-eight; the difference is everything a key or a
        pure function could produce instead, which is a third of the bytes and
        all of the ways two copies of one fact can disagree.
        """
        item = {"PK": self.pk, "SK": self.sk}
        # Empty means absent. `auditStatus = ""` is "the audit found no flags"
        # and `failureReason = ""` is "nothing has failed" -- both of which are
        # said better by the attribute not being there, which also keeps those
        # rows out of any filter on it. `clean_item` already drops None for the
        # same reason; this is the same rule for the string case.
        item.update({
            name: value for name in self.STORED
            if (value := getattr(self, name)) not in ("", None)
        })
        item.update(self.index_keys())
        return item



# ------------------------------------------------------------------------
# the scorecard
# ------------------------------------------------------------------------

def _score_sections():
    """`audit_schema.SCORE_SECTIONS`, or None if it cannot be imported.

    Imported lazily and by path rather than declared here, because the marking
    scheme has exactly one definition and a second copy in this module is a
    second thing to keep honest -- the kind that goes wrong silently, months
    later, when somebody adds a criterion in one place.

    None rather than an exception when it is missing: `hydrate` runs on every
    read, and a call should still come back without its section roll-up if the
    schema module is not importable for some reason.
    """
    import sys

    from .config import REPO_ROOT

    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    try:
        from audit_schema import SCORE_SECTIONS
    except Exception:  # noqa: BLE001 -- see the docstring
        return None
    return SCORE_SECTIONS


def section_marks(criterion_marks):
    """The six section totals, summed from the criteria.

    A section is exactly the sum of its criteria -- checked against every
    document in the corpus, 100 of 100 -- which is why only the criteria are
    stored. This is the other half of that trade.

    A criterion the audit did not mark is left out of the sum rather than
    counted as zero, and a section with nothing marked is omitted entirely:
    zero is a real mark an agent can score, so a defaulted zero is
    indistinguishable from a bad one.
    """
    if not criterion_marks:
        return None
    sections = _score_sections()
    if not sections:
        return None

    out = {}
    for section in sections:
        marks = [
            criterion_marks[name]
            for name, _, _ in section["criteria"]
            if criterion_marks.get(name) is not None
        ]
        if marks:
            out[section["key"]] = sum(marks)
    return out or None

# ------------------------------------------------------------------------
# reading a row back
# ------------------------------------------------------------------------

def hydrate(item):
    """A stored item as the full logical call.

    The table holds seventeen attributes; a caller wants the twenty-five it
    used to hold. The difference is entirely recomputable, and this is the one
    place that does it, so no reader has to know which attributes are real and
    which are derived:

        callId          from PK          "CALL#167472638"
        startedAt       from ord         "2026-09-20T12:50:57Z#167472638"
        agentExtension  from gsi2pk      "AGENT#705#2026-09"
        dateKey         from startedAt
        audioKey        audio_key_for(callId, startedAt), by status
        auditKey        audit_key_for(callId, startedAt), by status
        scoreBand       score_band(score)

    `gsi2pk` is why it is in every index projection: without it a row read
    through GSI-1 or GSI-3 could not say whose call it was. It is one attribute
    carrying what three used to.

    Everything else about the item is passed through untouched, so a row read
    from the base table and one read from an index differ only in how much of
    it is there -- which is what the projection already decided.
    """
    if not item:
        return item

    row = dict(item)
    pk = str(row.get("PK") or "")
    if pk.startswith("CALL#") and "callId" not in row:
        row["callId"] = pk.split("#", 1)[1]

    ordinal = str(row.get("ord") or "")
    if ordinal and "startedAt" not in row:
        row["startedAt"] = ordinal.split("#", 1)[0]

    agent_pk = str(row.get("gsi2pk") or "")
    if agent_pk.startswith("AGENT#") and "agentExtension" not in row:
        # "AGENT#705#2026-09" -> "705". rsplit, because the month is the one
        # part whose shape is fixed; an extension with a "#" in it would be a
        # different problem entirely.
        row["agentExtension"] = agent_pk[len("AGENT#"):].rsplit("#", 1)[0]

    call_id = row.get("callId")
    started = row.get("startedAt")
    status = row.get("processingStatus")

    if call_id and started:
        from . import store as layout

        row.setdefault("dateKey", layout.date_key(started))
        row.setdefault(
            "audioKey",
            "" if status == ProcessingStatus.DISCARDED
            else layout.audio_key(call_id, started),
        )
        row.setdefault(
            "auditKey",
            layout.artifact_key(call_id, started, "audit")
            if status == ProcessingStatus.PROCESSED else "",
        )

    if "score" in row or status == ProcessingStatus.PROCESSED:
        row.setdefault("scoreBand", score_band(row.get("score")))

    # The section roll-up, summed from the criteria the row does store. Every
    # reader that asked for `sectionMarks` before the row stopped carrying it
    # still gets it, and gets the breakdown underneath it as well.
    if row.get("criterionMarks") and "sectionMarks" not in row:
        if rolled := section_marks(row["criterionMarks"]):
            row["sectionMarks"] = rolled

    return row
