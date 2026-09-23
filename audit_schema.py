"""Dashboard contract for the call audit layer.

Every enum the dashboard filters or groups on lives here, and nowhere else.
`AuditReport` is what the LLM must return; `CallAuditDocument` is what gets
written to `{id}.audit.json` (report + deterministic metadata + provenance).

Typing stays Python 3.9 compatible (Optional/List, no PEP 604 unions).
"""

import contextvars
import typing
from enum import Enum
from typing import Annotated, Dict, List, Optional

import annotated_types
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --------------------------------------------------------------------------- #
# Enums — the dashboard contract. Add values here, never inline in prompts.
# --------------------------------------------------------------------------- #
class Speaker(str, Enum):
    EMPLOYEE = "Employee"
    CUSTOMER = "Customer"


class ObjectionType(str, Enum):
    ALREADY_ENROLLED_COMPETITOR = "already_enrolled_competitor"
    PRICE = "price"
    TRUST_CREDIBILITY = "trust_credibility"
    PARENT_DECISION = "parent_decision"
    TIME_AVAILABILITY = "time_availability"
    LANGUAGE_MEDIUM = "language_medium"
    CONTENT_DOUBT = "content_doubt"
    NOT_INTERESTED = "not_interested"
    NO_OBJECTION_RAISED = "no_objection_raised"
    OTHER = "other"


class HandlingQuality(str, Enum):
    IGNORED = "ignored"
    DEFLECTED = "deflected"
    ACKNOWLEDGED_NOT_ADDRESSED = "acknowledged_not_addressed"
    ADDRESSED_PARTIALLY = "addressed_partially"
    ADDRESSED_WELL = "addressed_well"


class Outcome(str, Enum):
    CONVERTED_PAID = "converted_paid"
    VERBAL_COMMITMENT = "verbal_commitment"
    DEMO_AGREED = "demo_agreed"
    CALLBACK_SCHEDULED = "callback_scheduled"
    INTERESTED_NO_COMMITMENT = "interested_no_commitment"
    OBJECTION_UNRESOLVED = "objection_unresolved"
    NOT_INTERESTED = "not_interested"
    NOT_QUALIFIED = "not_qualified"
    WRONG_NUMBER = "wrong_number"
    CALL_DROPPED = "call_dropped"


class CallPhase(str, Enum):
    OPENING = "opening"
    VERIFICATION = "verification"
    DISCOVERY = "discovery"
    PITCH = "pitch"
    OBJECTION = "objection"
    CLOSE = "close"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FlagType(str, Enum):
    """Conduct problems only.

    This list is deliberately short. Compliance here means *how the employee
    treated the person on the phone* — not what they claimed about the product.
    Quoted prices, course validity, outcome talk, competitor mentions and how the
    number was sourced are ordinary sales conversation: they are recorded in
    `pitch_summary` where a reviewer can check them against the price list, and
    they are not flagged. Flagging them buried the calls where someone was
    actually mistreated, which is the only thing this section is for.
    """

    ABUSIVE_LANGUAGE = "abusive_language"
    DISRESPECTFUL_CONDUCT = "disrespectful_conduct"
    COERCIVE_PRESSURE = "coercive_pressure"
    DISCRIMINATORY_REMARK = "discriminatory_remark"
    OTHER_MISCONDUCT = "other_misconduct"


# Conduct that ends the conversation about how well the call was sold. A call
# carrying one of these gets no score at all — there is no marking scheme under
# which abusing a student or making a racist or sexist remark is recoverable, and
# a number next to it would invite someone to weigh it against the selling.
DISQUALIFYING_FLAGS = (
    FlagType.ABUSIVE_LANGUAGE,
    FlagType.DISCRIMINATORY_REMARK,
)


class DecisionMaker(str, Enum):
    SELF = "self"
    PARENT = "parent"
    BOTH = "both"


class FlowCheckpoint(str, Enum):
    """How one checkpoint of the expected pitch order was handled.

    `not_reached` is not a soft `broken`: it means the call never arrived at that
    part of the conversation — nobody was qualified because the student hung up at
    turn three, the personalised path was never mentioned at all — and it costs
    nothing, the same way an unreached criterion keeps its full marks.
    """

    HELD = "held"
    BROKEN = "broken"
    NOT_REACHED = "not_reached"


# --------------------------------------------------------------------------- #
# The scorecard — the marking scheme the floor is graded on.
#
# Seven sections of 10 marks each, 70 in all. The marks *are* the weighting, so
# there is no separate weight table: a criterion matters as much as it is worth.
# The model awards marks per criterion and never totals them; `audit_call.py`
# does the arithmetic. Section titles and marks live here, and nowhere else.
# --------------------------------------------------------------------------- #
SCORE_SECTIONS = (
    {
        "key": "introduction",
        "title": "Introduction",
        "criteria": (
            ("bda_and_company_intro", "BDA and company intro", 5),
            ("purpose_of_call", "Purpose of the call", 5),
        ),
    },
    {
        "key": "qualification",
        "title": "Qualifications of student",
        "criteria": (
            ("qualification_questions", "Qualifying questions asked", 10),
        ),
    },
    {
        "key": "product_and_pricing",
        "title": "Product pricing & feature explanation",
        "criteria": (
            ("why_what_how_explanation", "Explained in a why / what / how manner", 5),
            ("benefits_for_their_study", "Benefits of that feature for their study", 5),
        ),
    },
    {
        "key": "rebuttal",
        "title": "Rebuttal given for objection",
        "criteria": (
            ("rebuttal_for_objections", "Rebuttal given for objection", 10),
        ),
    },
    {
        "key": "conversation",
        "title": "Two-way conversation",
        "criteria": (
            ("two_way_conversation", "Kept the student talking", 10),
        ),
    },
    {
        "key": "closing",
        "title": "Closing and follow up",
        "criteria": (
            ("closing_and_follow_up", "Closing and follow up", 10),
        ),
    },
    {
        "key": "pitch_flow",
        "title": "Flow of the pitch",
        "criteria": (
            ("pitch_flow_order", "Pitch run in the expected order", 10),
        ),
    },
)

# Flattened lookups the rest of the pipeline reads.
SCORE_CRITERIA = tuple(
    name for section in SCORE_SECTIONS for name, _, _ in section["criteria"]
)
CRITERION_MAX = {
    name: marks for section in SCORE_SECTIONS for name, _, marks in section["criteria"]
}
CRITERION_TITLE = {
    name: title for section in SCORE_SECTIONS for name, title, _ in section["criteria"]
}
TOTAL_MARKS = sum(CRITERION_MAX.values())

# The marks each criterion is allowed to take, worst band first. These are the
# same numbers the system prompt gives the model under *Allowed scores*; they live
# here as well because the pipeline now has to move a mark between bands on its
# own — a follow-up call that re-asks what the previous call already established is
# stepped down one band per repeat, and stepping down is only meaningful against
# the ladder the criterion actually has. Keeping the two copies honest is what
# `audit_call.validate_marking_scheme` checks at startup.
CRITERION_BANDS = {
    "bda_and_company_intro": (0, 3, 5),
    "purpose_of_call": (0, 3, 5),
    "qualification_questions": (0, 3, 7, 10),
    "why_what_how_explanation": (0, 2, 3, 5),
    "benefits_for_their_study": (0, 3, 5),
    "rebuttal_for_objections": (0, 2, 5, 7, 10),
    "two_way_conversation": (0, 2, 5, 7, 10),
    "closing_and_follow_up": (0, 3, 7, 10),
    # Three checkpoints, and the ladder is a count of the ones that broke: all three
    # held is 10, one broken 7, two broken 5, all three broken 0. `audit_call.py`
    # does that arithmetic itself from the checkpoints the model reports, so this
    # ladder is read rather than chosen — see `enforce_pitch_flow`.
    "pitch_flow_order": (0, 5, 7, 10),
}


def step_down(criterion: str, marks: int, steps: int) -> int:
    """`marks` moved `steps` bands down that criterion's own ladder.

    Rounded onto the ladder first, so a mark the model returned off-band does not
    escape the deduction. Floors at 0 rather than going negative.
    """
    bands = CRITERION_BANDS[criterion]
    at = max((index for index, band in enumerate(bands) if band <= marks), default=0)
    return bands[max(0, at - max(0, steps))]


# --------------------------------------------------------------------------- #
# Shared building blocks
# --------------------------------------------------------------------------- #
# Length caps. These are backstops against runaway output, not the house style.
#
# The style — a floor auditor writing in clauses, "no intro, direct question puche,
# sirf price se convince kar rahe" — is asked for in each field's description and in
# the system prompt, which is where a length belongs, because it is guidance the
# model can weigh against saying the thing properly. It used to be enforced here
# instead, at four times tighter than these numbers, and a report that ran thirty
# characters over was rejected and regenerated from scratch: a correct
# twenty-thousand-token audit thrown away, twice on some calls, to get a shorter
# sentence back.
#
# So the caps sit far above anything the model writes in normal operation. Reaching
# one now means something has genuinely gone wrong — a paragraph where a clause was
# asked for — and even then the string is trimmed at a sentence or word boundary and
# the trim recorded, never rejected. The dashboard reserves room for the full length,
# so a long value is ugly rather than broken. Everything that bears on correctness —
# turn indices, enums, marks, required fields — is still rejected as strictly as ever.
Clause = Annotated[str, Field(max_length=400)]
Line = Annotated[str, Field(max_length=500)]

# What was trimmed while parsing the current report. A ContextVar rather than a
# module global so concurrent parses — the map-reduce path runs chunks in parallel —
# cannot write into each other's list.
_trims: "contextvars.ContextVar[Optional[List[str]]]" = contextvars.ContextVar(
    "audit_schema_trims", default=None
)


def collect_trims():
    """Start recording trims for one parse. Returns the list they land in."""
    record: List[str] = []
    _trims.set(record)
    return record


def _max_len_in(metadata):
    for item in metadata or ():
        if isinstance(item, annotated_types.MaxLen):
            return item.max_length
    return None


def _max_length_of(field):
    """The character cap declared on a field, or None."""
    return _max_len_in(field.metadata)


def _item_cap_of(field):
    """The per-item character cap on a list-of-capped-strings field, or None.

    `snapshot_points: List[Clause]` caps the list at six entries and each entry at
    Clause's length. The field's own metadata carries the first; the second is on the
    item type, and it is the one that used to reject a whole report over one long
    bullet.
    """
    args = typing.get_args(field.annotation)
    if not args:
        return None
    item = args[0]
    if typing.get_origin(item) is not Annotated:
        return None
    item_args = typing.get_args(item)
    if not item_args or item_args[0] is not str:
        return None
    for meta in item_args[1:]:
        cap = _max_len_in(getattr(meta, "metadata", ()))
        if cap is not None:
            return cap
        if isinstance(meta, annotated_types.MaxLen):
            return meta.max_length
    return None


def _note_trim(cls, name, was, cap):
    record = _trims.get()
    if record is not None:
        record.append("%s.%s ran to %d characters against a %d limit and was trimmed "
                      "to fit." % (cls.__name__, name, was, cap))


def _shorten(text, cap):
    """Cut `text` to `cap` characters, at a sentence end if there is one.

    Preference order: the last sentence that fits, then the last whole word, then a
    hard cut. An ellipsis marks the last two cases so a reader can see the sentence
    was cut rather than written that way.
    """
    head = text[:cap]
    for stop in (". ", "! ", "? ", "। "):
        at = head.rfind(stop)
        if at >= cap // 2:
            return head[: at + 1].strip()
    at = head.rfind(" ")
    trimmed = (head[:at] if at >= cap // 2 else head[: cap - 1]).rstrip(" ,;:-—–")
    return (trimmed + "…")[:cap]


class StrictModel(BaseModel):
    """Reject anything the dashboard does not know how to render."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    @model_validator(mode="before")
    @classmethod
    def _trim_overlong_prose(cls, data):
        """Shorten over-long strings instead of failing the whole report over them."""
        if not isinstance(data, dict):
            return data
        changed = None
        for name, field in cls.model_fields.items():
            cap = _max_length_of(field)
            if cap is None or name not in data:
                continue
            value = data[name]
            if not isinstance(value, str) or len(value) <= cap:
                continue
            if changed is None:
                changed = dict(data)
            changed[name] = _shorten(value, cap)
            _note_trim(cls, name, len(value), cap)

        # Same again for the entries of a list of capped strings, where the cap is on
        # the item type rather than on the field.
        for name, field in cls.model_fields.items():
            item_cap = _item_cap_of(field)
            if item_cap is None or name not in data:
                continue
            values = (changed or data)[name]
            if not isinstance(values, list):
                continue
            if not any(isinstance(v, str) and len(v) > item_cap for v in values):
                continue
            if changed is None:
                changed = dict(data)
            trimmed = []
            for entry in values:
                if isinstance(entry, str) and len(entry) > item_cap:
                    _note_trim(cls, name, len(entry), item_cap)
                    entry = _shorten(entry, item_cap)
                trimmed.append(entry)
            changed[name] = trimmed

        return changed if changed is not None else data


class Quote(StrictModel):
    """Verbatim evidence: original language kept as-is, plus an English gloss.

    `turn_index` is normally a real turn. The one exception is the placeholder
    objection a call gets when nothing was raised: there is no turn to point at, and
    the model kept writing -1 or null there and having the whole report rejected for
    it — a retry that cost a second full-size call and changed nothing. -1 is now
    allowed and means "no turn"; `Objection` below is what holds the line on where
    that is legitimate.
    """

    verbatim: str = Field(default="", description="Exact words from the transcript, "
                                                  "original script")
    gloss_en: str = Field(default="", description="Short English gloss of the verbatim quote")
    turn_index: int = Field(ge=-1, description="Turn this was said in. -1 only on the "
                                               "'no_objection_raised' placeholder, "
                                               "where there is no turn to cite.")


class Claim(StrictModel):
    """A free-text judgement that must point back at the turns it came from."""

    text: str = Field(description="English. Empty string is not allowed; use null field.")
    evidence_turns: List[int] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# B. Call narrative
# --------------------------------------------------------------------------- #
class CustomerProfile(StrictModel):
    """Only what was factually established on the call. Never inferred."""

    first_name: Optional[str] = Field(None, description="First name only, if stated")
    class_or_status: Optional[str] = None
    attempts_given: Optional[int] = Field(None, ge=0)
    is_dropper: Optional[bool] = None
    target_exam: Optional[str] = None
    target_exam_year: Optional[int] = None
    current_platform: Optional[str] = None
    price_paid_elsewhere: Optional[str] = None
    decision_maker: Optional[DecisionMaker] = None
    location: Optional[str] = None
    evidence_turns: List[int] = Field(default_factory=list)
    not_established: List[str] = Field(
        default_factory=list,
        description="Profile fields the employee never established on this call",
    )


class TimelinePhase(StrictModel):
    phase: CallPhase
    start_turn: int = Field(ge=0)
    end_turn: int = Field(ge=0)
    summary: str = Field(description="English, one line")


class DiscoveryQuality(StrictModel):
    established_need_before_pitching: bool
    questions_asked: List[str] = Field(default_factory=list)
    questions_not_asked: List[str] = Field(default_factory=list)
    assessment: str
    evidence_turns: List[int] = Field(default_factory=list)


class PitchSummary(StrictModel):
    features_claimed: List[str] = Field(default_factory=list)
    pricing_claimed: Optional[str] = None
    faculty_claimed: List[str] = Field(default_factory=list)
    guarantees_claimed: List[str] = Field(default_factory=list)
    summary: str
    evidence_turns: List[int] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# C. Objection handling
# --------------------------------------------------------------------------- #
class Objection(StrictModel):
    objection_type: ObjectionType
    quote: Quote
    repeated_at_turns: List[int] = Field(
        default_factory=list,
        description="Later turns where the SAME objection was raised again. "
                    "A repeat is not a separate objection.",
    )
    employee_response_summary: str
    response_turn_indices: List[int] = Field(default_factory=list)
    handling_quality: HandlingQuality
    resolved: bool
    what_should_have_been_asked: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _allow_unanchored_placeholder(cls, data):
        """Let the `no_objection_raised` placeholder come back without a turn.

        A real objection must still cite the turn it surfaced in — that anchoring is
        the point of the whole objections section. But the placeholder is not an
        objection at all, and demanding a turn index for it was rejecting reports
        over a field with no honest value. A null or missing index on the placeholder
        becomes -1; anywhere else it stays an error.
        """
        if not isinstance(data, dict):
            return data
        if data.get("objection_type") != "no_objection_raised":
            return data
        quote = data.get("quote")
        if quote is None:
            data = {**data, "quote": {"verbatim": "", "gloss_en": "", "turn_index": -1}}
            return data
        if isinstance(quote, dict) and quote.get("turn_index") is None:
            data = {**data, "quote": {**quote, "turn_index": -1}}
        return data

    @model_validator(mode="after")
    def _real_objections_are_anchored(self):
        """-1 is the placeholder's licence, not everyone's."""
        if (self.objection_type is not ObjectionType.NO_OBJECTION_RAISED
                and self.quote.turn_index < 0):
            raise ValueError(
                "quote.turn_index must be the turn the objection surfaced in; only "
                "the 'no_objection_raised' placeholder may leave it unanchored"
            )
        return self


# --------------------------------------------------------------------------- #
# D. Outcome
# --------------------------------------------------------------------------- #
class NextStep(StrictModel):
    action: Optional[str] = None
    owner: Optional[str] = Field(None, description="'employee', 'customer' or null")
    scheduled_for: Optional[str] = Field(
        None, description="As agreed on the call, e.g. 'Wednesday' — do not invent a time"
    )
    is_specific: bool = Field(
        description="False when the commitment lacks a concrete time/date or confirmation"
    )
    evidence_turns: List[int] = Field(default_factory=list)


class OutcomeBlock(StrictModel):
    outcome: Outcome
    outcome_reasoning: str = Field(description="2-3 sentences citing turn indices")
    outcome_evidence_turns: List[int] = Field(default_factory=list)
    next_step: NextStep
    conversion_probability: float = Field(ge=0.0, le=1.0)
    probability_drivers: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# E. Scored rubric
# --------------------------------------------------------------------------- #
class ScoreCriterion(StrictModel):
    """Marks awarded on one line of the marking scheme.

    `awarded` is bounded 0-10 here because pydantic cannot see which criterion it
    belongs to; `audit_call.py` checks it against that criterion's real maximum.

    Every criterion starts at full marks and is only marked down once the call
    actually reaches it. `reached` is what separates a mark the counsellor earned
    from one they were given because the call never got that far — without it a
    defaulted 10 and a perfect 10 are the same number on the page, and a reader
    cannot tell a call that went well from one that ended at turn three.
    """

    reached: bool = Field(
        True,
        description="True when the call actually got to this part and the mark "
                    "reflects how it went. False when the call never reached it — "
                    "the customer ended it, said they were busy, it was a wrong "
                    "number — in which case the mark stays at full and the "
                    "justification says why this part never came up.",
    )
    awarded: int = Field(
        ge=0, le=10,
        description="Marks given, from 0 up to this criterion's maximum. Whole marks "
                    "only. Full marks when `reached` is false. When it is true, the "
                    "marks say how well this part of the call actually went.",
    )
    justification: str = Field(
        max_length=800,
        description="One plain-English sentence a counsellor can read without being "
                    "taught the shorthand: what they did and why it scored that. "
                    "'He gave his name and said he was from Arivihan, but never said "
                    "why he was calling' is right. When `reached` is false, say why "
                    "this part never came up: 'Wrong number, so the call ended before "
                    "there was anything to explain.' No jargon, no turn numbers.",
    )
    evidence_turns: List[int] = Field(default_factory=list)


class PitchFlowCriterion(ScoreCriterion):
    """The flow line, which carries its three checkpoints alongside the mark.

    Every other criterion asks how well one part of the call was done. This one asks
    whether the parts came in the order a pitch is supposed to run in, and whether
    each was delivered in one piece rather than dribbled out across the call. That
    is three separate observations, and they are recorded separately so the report
    can say *which* one broke rather than only that the flow was worth 5.

    The model reports the three checkpoints; `audit_call.enforce_pitch_flow` turns
    them into the mark, the same division of labour as everywhere else in the
    scorecard — the model reads the call, the pipeline does the arithmetic.
    """

    introduction_first: FlowCheckpoint = Field(
        FlowCheckpoint.HELD,
        description="`held` when the counsellor opened the call with the whole "
                    "introduction — who they are, that they are from Arivihan, and "
                    "why they are ringing — said as one piece before anything else. "
                    "The student interrupting inside it does not break it. `broken` "
                    "when the call started somewhere else (straight into questions or "
                    "the pitch) or when a piece of the introduction only arrived later, "
                    "after the counsellor had moved on to other business. "
                    "`not_reached` only when there was no opening at all.",
    )
    qualification_block: FlowCheckpoint = Field(
        FlowCheckpoint.HELD,
        description="`held` when the qualifying questions came next — the first thing "
                    "the counsellor did after the introduction — and were put together "
                    "as one stretch rather than scattered through the call. It does not "
                    "matter how far into the call that stretch falls, only that nothing "
                    "else of the counsellor's own business came first. Interruptions "
                    "inside it do not break it. `broken` when the counsellor pitched, "
                    "quoted a price or explained a feature before qualifying, or when "
                    "the qualifying questions were split up and returned to later. "
                    "`not_reached` when the call ended before there was any qualifying "
                    "to do, or on a follow-up where it was settled on the earlier call.",
    )
    personalised_path_block: FlowCheckpoint = Field(
        FlowCheckpoint.HELD,
        description="`held` when the personalised path or personalised feature, wherever "
                    "in the call it came up, was explained in one piece — what it is, how "
                    "it works, and what it does for this student — or when the student "
                    "interrupted and took the conversation somewhere else, which is not "
                    "the counsellor's doing. `broken` when the counsellor themselves left "
                    "it half-explained and came back to it later, or spread the three "
                    "parts across the call. `not_reached` when the personalised path never "
                    "came up, which is the ordinary case on a short call.",
    )

    def checkpoints(self) -> Dict[str, FlowCheckpoint]:
        return {
            "introduction_first": self.introduction_first,
            "qualification_block": self.qualification_block,
            "personalised_path_block": self.personalised_path_block,
        }


class ScoreCard(StrictModel):
    """Every criterion in `SCORE_SECTIONS`, scored. The model never totals these."""

    # Introduction — 10
    bda_and_company_intro: ScoreCriterion
    purpose_of_call: ScoreCriterion
    # Qualifications of student — 10
    qualification_questions: ScoreCriterion
    # Product pricing & feature explanation — 10
    why_what_how_explanation: ScoreCriterion
    benefits_for_their_study: ScoreCriterion
    # Rebuttal given for objection — 10
    rebuttal_for_objections: ScoreCriterion
    # Two-way conversation — 10
    two_way_conversation: ScoreCriterion
    # Closing and follow up — 10
    closing_and_follow_up: ScoreCriterion
    # Flow of the pitch — 10
    pitch_flow_order: PitchFlowCriterion

    def awarded(self) -> Dict[str, int]:
        return {name: getattr(self, name).awarded for name in SCORE_CRITERIA}


# --------------------------------------------------------------------------- #
# F. Compliance & risk
# --------------------------------------------------------------------------- #
class ComplianceFlag(StrictModel):
    flag_type: FlagType
    severity: Severity
    quote: Quote
    why_flagged: str
    occurrences: int = Field(
        1, ge=1, description="How many times this pattern occurs across the call"
    )
    occurrence_turns: List[int] = Field(default_factory=list)
    source: str = Field(
        "audit",
        description="'audit' when the audit raised it, 'sentiment_layer' when it was "
                    "carried over from the earlier layer and the audit did not cover it",
    )


# --------------------------------------------------------------------------- #
# G. Coaching
# --------------------------------------------------------------------------- #
class CoachingAction(StrictModel):
    action: str = Field(description="Imperative, specific to this call")
    criterion: str = Field(
        description="The scorecard criterion this action improves, e.g. "
                    "'rebuttal_for_objections'",
    )
    evidence_turns: List[int] = Field(default_factory=list)


class Coaching(StrictModel):
    strengths: List[Claim] = Field(min_length=2, max_length=4)
    missed_opportunities: List[Claim] = Field(default_factory=list)
    coaching_actions: List[CoachingAction] = Field(min_length=3, max_length=5)
    best_line: Optional[Quote] = None
    worst_line: Optional[Quote] = None


# --------------------------------------------------------------------------- #
# H. Sales summary — the layer the report UI actually renders
# --------------------------------------------------------------------------- #
# Everything above is the auditor's record: scored, quoted and traceable. This
# block is the sales read of the same call, written for a counsellor or floor
# manager who has 60 seconds and never sees the transcript. It carries no scores
# and no turn indices — it is a restatement of findings already made above, not a
# second audit. If the two ever disagree, the sections above are the truth.
class ImprovementAction(StrictModel):
    """One thing the counsellor should do differently, written for them to read."""

    title: str = Field(
        max_length=200,
        description="Imperative headline, at most 6 words, e.g. 'Handle the blocker first'",
    )
    detail: str = Field(
        max_length=600,
        description="One clause of at most 18 words, tied to what happened on this call. "
                    "No 'the counsellor should have' preamble — start with the action.",
    )


class CarryForward(StrictModel):
    """What the next call needs to know, and nothing else.

    Every other field in `sales_summary` is written for a person: a floor manager
    with sixty seconds, or the counsellor who made the call. This block is written
    for the **next audit**. When a follow-up call comes through the pipeline, the
    previous call's `carry_forward` is fed to the model as the record of what has
    already been covered, and it is what lets the audit tell a counsellor picking up
    where they left off from one asking the student their class for the second time.

    So it is a ledger, not prose. No tone, no manner, no judgement of how the call
    went, no coaching, no marks — none of that helps the next call and all of it
    costs room. Facts established, questions put, ground covered, threads left open.
    One short clause each, at most about twelve words, `field: value` where there is
    a value. A call that established nothing on a line returns an empty list for it
    rather than a sentence saying so.
    """

    student_details: List[Clause] = Field(
        default_factory=list, max_length=10,
        description="Every fact the call established about the student, one per "
                    "entry, shortest form that carries it: '12th class, NEET 2026', "
                    "'currently with PW, paid ~4000', 'father decides', 'wants "
                    "biology help', 'Indore'. Established facts only — nothing "
                    "inferred, nothing the counsellor merely asserted, and nothing "
                    "about how anybody sounded.",
    )
    questions_asked: List[Clause] = Field(
        default_factory=list, max_length=12,
        description="What the counsellor asked the student, one question per entry, "
                    "in the shortest form that identifies it: 'which class', 'which "
                    "exam and year', 'what she is studying from now', 'budget', "
                    "'who pays'. Every question actually put, whether or not it was "
                    "answered — the next call is marked on whether it asks these "
                    "again, so an unanswered question still belongs here.",
    )
    information_given: List[Clause] = Field(
        default_factory=list, max_length=12,
        description="What the counsellor told the student, one item per entry: "
                    "'price 5999 for full year', 'personalised path explained', "
                    "'live doubt sessions', 'demo link sent on WhatsApp', 'refund "
                    "policy not covered'. Include the answer to anything the student "
                    "asked. This is the ground that does not need covering again.",
    )
    student_questions: List[Clause] = Field(
        default_factory=list, max_length=8,
        description="What the student asked, one per entry, with whether it was "
                    "answered: 'asked about refund — answered', 'asked if classes "
                    "are recorded — not answered'. An unanswered one is the first "
                    "thing the next call owes them.",
    )
    open_threads: List[Clause] = Field(
        default_factory=list, max_length=8,
        description="What was left hanging and what either side undertook to do: "
                    "'will speak to father, callback Sunday', 'link to be sent', "
                    "'payment pending', 'wants to compare with PW first'. This is "
                    "what the next call has to pick up.",
    )


class SalesSummary(StrictModel):
    """The one-screen report. Short sentences, no scores, no turn numbers."""

    auditor_note: str = Field(
        max_length=1200,
        description="The whole call as a floor auditor writes it: short clauses strung "
                    "with commas, no sentence structure, no preamble, at most 40 words. "
                    "'no intro, direct question puche, path short me samjhaya, compare "
                    "nhi kiya, sirf price se convince kar rahe' is the register — write "
                    "that in English: 'no intro, went straight to questions, explained "
                    "the path too briefly, never compared with PW, sold on price alone'.",
    )
    snapshot_points: List[Clause] = Field(
        min_length=2, max_length=6,
        description="Everything worth knowing about the student, one fact per bullet. "
                    "Lead with who they are — class or status, exam, current platform, "
                    "what they have already paid — one fact per line, not a strip: "
                    "'12th class, NEET prep', 'currently with PW, paid 4000 there'. "
                    "Then what changes how the next call should be run: their stated "
                    "problem, who decides, what the counsellor never found out.",
    )
    call_tone: str = Field(
        # 130 was too tight to hold what this field asks for. It wants three things —
        # the counsellor's manner, the student's manner, and the note the call ended
        max_length=800,
        description="One line on how the call sounded — the counsellor's manner, the "
                    "student's manner, and where it ended up. Plain description, not "
                    "a verdict, e.g. 'Warm and patient on both sides, though she "
                    "stayed short with him after the price came up.'",
    )
    call_in_brief: str = Field(
        max_length=1000,
        description="What happened, in at most 35 words. Clauses, not a paragraph.",
    )
    primary_blocker: str = Field(
        max_length=250,
        description="The one thing that decided the result, e.g. 'Already enrolled "
                    "with PW'. Use 'None' only when the call converted cleanly.",
    )
    conversion_reasons: List[Clause] = Field(
        min_length=1, max_length=4,
        description="Why the call landed where it did. One clause each, no sentences.",
    )
    improvements: List[ImprovementAction] = Field(min_length=3, max_length=4)
    bottom_line: str = Field(
        max_length=1200,
        description="The single most important sentence in the report: why the call "
                    "went the way it did, and where the next one should start. At "
                    "most 45 words.",
    )

    # The three plain-English blocks the counsellor reads. Everything above is
    # written in the clipped register a floor auditor uses with other auditors;
    # these are the same findings said to the person who made the call, in ordinary
    # sentences they can act on without being taught to read the shorthand first.
    what_happened: str = Field(
        max_length=2500,
        description="Plain English, 60-110 words, ordinary sentences. What the "
                    "counsellor did well on this call and what went wrong, in that "
                    "order, naming the actual moments — 'she said she was already "
                    "studying with PW and you moved on to features without asking "
                    "why', not 'objection handling was weak'. No marks, no turn "
                    "numbers, no jargon, no bullet points.",
    )
    conversion_status: str = Field(
        max_length=2000,
        description="Plain English, 40-80 words, ordinary sentences. Whether the call "
                    "sold, and if not, how far it got and the real reason it stopped "
                    "there. Say what was actually agreed and what was left open — "
                    "'you offered to call at 7 but she never said yes, so nothing is "
                    "booked'. No marks, no turn numbers, no jargon.",
    )
    how_to_improve: Optional[str] = Field(
        None,
        max_length=2500,
        description="Plain English, 60-120 words, ordinary sentences, addressed to the "
                    "counsellor as 'you'. The two or three things to do differently, "
                    "each tied to what happened on this call and concrete enough to "
                    "do on the next one. Null ONLY when the call was genuinely well "
                    "run and there is nothing worth changing.",
    )

    # Written for the next audit rather than for a reader — see CarryForward.
    carry_forward: CarryForward = Field(default_factory=CarryForward)


# --------------------------------------------------------------------------- #
# I. Cross-layer reconciliation
# --------------------------------------------------------------------------- #
class SentimentLayerAgreement(StrictModel):
    """The audit's read of the earlier layer, including what it threw out.

    `dismissed_turns` is the one field here the pipeline acts on. Every phrase the
    sentiment layer flagged and the audit did not raise as a conduct flag is carried
    over automatically, because a flag that goes missing leaves a report that simply
    looks clean. That safety net cannot tell a turn the audit *forgot* from one it
    read and rejected — so a rejection has to be said out loud. Listing a turn here
    stops the carry-over and records the dismissal in the warnings instead, where a
    reviewer still sees it.
    """

    agrees: bool
    notes: str = Field(description="Where the audit differs from the sentiment layer")
    dismissed_turns: List[int] = Field(
        default_factory=list,
        description="Turns the sentiment layer flagged that you read and judged NOT to "
                    "be conduct problems — ASR garble, a line not addressed to the "
                    "customer, ordinary sales talk. Only turns you actually examined and "
                    "explained in `notes`. Never list a turn you also raised as a "
                    "compliance flag, and never use this to clear a flag you agree with.",
    )


# --------------------------------------------------------------------------- #
# The model-authored report
# --------------------------------------------------------------------------- #
class RepeatedContent(StrictModel):
    """One thing this follow-up went over that the previous call had already done.

    The point of a follow-up is to move the decision on, not to run the first call
    again. A counsellor who asks a student their class for the second time, or
    re-explains the personalised path the previous call already explained, has spent
    the call re-establishing what was on file — and the student has to sit through
    being asked something they already answered.

    Judged only against the **PREVIOUS CALL** block supplied with the audit, never
    against a guess about what an earlier call probably covered. `criterion` names
    which line of the marking scheme the repeat lands on; `audit_call.compute_scores`
    steps that criterion down one band for each entry. The model does not apply the
    deduction itself — it reports the repeat and the pipeline does the arithmetic,
    the same division of labour as everywhere else in the scorecard.

    What is not a repeat: confirming something in passing ("aap 12th mein hain na?"
    as a lead-in), answering a question the student asked again themselves, or
    covering ground the previous call listed as an open thread. Re-establishing a
    fact because the student contradicted it is not a repeat either.
    """

    criterion: str = Field(
        description="Which marking-scheme criterion this repeat lands on — one of "
                    + ", ".join(sorted(CRITERION_MAX)) + ".",
    )
    what: str = Field(
        max_length=300,
        description="What was gone over again, in the shortest form that names it: "
                    "'asked her class again', 're-explained the personalised path'.",
    )
    already_covered: str = Field(
        max_length=300,
        description="The line from the previous call's carry-forward that shows it "
                    "was already done: '12th class, NEET 2026' or 'personalised "
                    "path explained'. Quote the previous record, not this call.",
    )
    evidence_turns: List[int] = Field(default_factory=list)

    @field_validator("criterion")
    @classmethod
    def _known_criterion(cls, value):
        if value not in CRITERION_MAX:
            raise ValueError("unknown criterion %r; expected one of %s"
                             % (value, ", ".join(sorted(CRITERION_MAX))))
        return value


class CallContinuity(StrictModel):
    """Whether this call is a first contact or a follow-up on an earlier one.

    A follow-up is a different job from a cold call. The introduction has already
    happened, the student has already been qualified and pitched, and what is left
    is the decision. Marking a follow-up against the full first-call scheme punishes
    a counsellor for not repeating work they already did, so the scorecard changes
    shape when this is true — see the system prompt.

    `is_follow_up` is true only where the **transcript itself** shows the two have
    spoken before. A counsellor who has clearly rung this number before but whose
    call carries no trace of it is a first call as far as the audit is concerned:
    the audit sees one recording and nothing else.
    """

    is_follow_up: bool = Field(
        False,
        description="True only when the transcript shows an earlier conversation "
                    "between these two — either of them referring back to it — or "
                    "when the PREVIOUS CALL block was supplied with this audit.",
    )
    basis: Optional[str] = Field(
        None, max_length=400,
        description="Required when is_follow_up is true: the words that establish it, "
                    "in one line. 'She says he had sent her the link yesterday.'",
    )
    repeated_from_previous: List["RepeatedContent"] = Field(
        default_factory=list, max_length=12,
        description="Only when a PREVIOUS CALL block was supplied: ground this call "
                    "went over that the previous call had already covered. Do not "
                    "deduct for these yourself — list them and the pipeline steps "
                    "the criterion down a band for each.",
    )
    evidence_turns: List[int] = Field(default_factory=list)


class LeadRelevance(StrictModel):
    """Whether the person on the call was someone Arivihan can sell to at all.

    Arivihan teaches NEET. A call to somebody preparing for something else — JEE,
    boards alone, a government exam, a graduate, a wrong number, someone with no
    exam in their life — is not a sales call that went badly; it is a lead that
    should never have been dialled. The marking scheme has nothing to say about it,
    so the score is withheld and the report says why. Conduct is still read: how a
    counsellor spoke to somebody matters whether or not they were a buyer.

    `is_relevant` stays true unless the transcript actually establishes the student
    is not a NEET aspirant. An exam that never came up is not a reason to withhold
    the score — that call is marked as usual.
    """

    is_relevant: bool = Field(
        True,
        description="False only when the call establishes the person is not a NEET "
                    "aspirant. True when they are, and true when it never came up.",
    )
    reason: Optional[str] = Field(
        None, max_length=800,
        description="Required when is_relevant is false: what the transcript "
                    "established, in one plain sentence — 'he is preparing for JEE, "
                    "not NEET' or 'she has finished her B.Com and is job hunting'.",
    )
    evidence_turns: List[int] = Field(default_factory=list)


class AuditReport(StrictModel):
    call_purpose: Claim
    customer_profile: CustomerProfile
    lead_relevance: LeadRelevance = Field(default_factory=LeadRelevance)
    call_continuity: CallContinuity = Field(default_factory=CallContinuity)
    discovery_quality: DiscoveryQuality
    pitch_summary: PitchSummary
    timeline: List[TimelinePhase] = Field(default_factory=list)

    objections: List[Objection] = Field(default_factory=list)

    outcome_block: OutcomeBlock
    scorecard: ScoreCard
    compliance_flags: List[ComplianceFlag] = Field(default_factory=list)
    coaching: Coaching
    sales_summary: SalesSummary
    sentiment_layer_agreement: SentimentLayerAgreement


# --------------------------------------------------------------------------- #
# Map-reduce intermediate (long calls only)
# --------------------------------------------------------------------------- #
class ChunkObservations(StrictModel):
    """What one chunk pass extracts, before the reduce step writes the report."""

    turn_range: List[int] = Field(min_length=2, max_length=2)
    phases_present: List[CallPhase] = Field(default_factory=list)
    profile_facts: List[Claim] = Field(default_factory=list)
    questions_asked: List[Claim] = Field(default_factory=list)
    objections_raised: List[Quote] = Field(default_factory=list)
    employee_responses: List[Claim] = Field(default_factory=list)
    pitch_claims: List[Claim] = Field(default_factory=list)
    compliance_candidates: List[Quote] = Field(default_factory=list)
    notable_quotes: List[Quote] = Field(default_factory=list)
    chunk_summary: str


# --------------------------------------------------------------------------- #
# Deterministic metadata + the written document
# --------------------------------------------------------------------------- #
class TalkPattern(StrictModel):
    """How the floor was shared, measured from the transcript's own timings.

    The model cannot see how long a turn took — it reads text — so the shape of the
    conversation is measured here and handed to it as fact. It scores the
    `two_way_conversation` metric against these numbers plus the transcript; it
    never recomputes them.
    """

    longest_employee_stretch_ms: int = Field(
        ge=0,
        description="Longest continuous span the employee held the floor with no "
                    "customer speech at all",
    )
    employee_stretches_over_45s: int = Field(
        ge=0, description="How many such spans ran longer than 45 seconds"
    )
    employee_stretches_over_60s: int = Field(
        0, ge=0,
        description="How many ran longer than 60 seconds. Context on how the call was "
                    "paced, never a deduction — only the 90-second count moves a mark",
    )
    employee_stretches_over_90s: int = Field(
        ge=0, description="How many ran longer than 90 seconds"
    )
    median_customer_gap_ms: Optional[int] = Field(
        None, ge=0,
        description="Median time between one customer contribution and the next. "
                    "Null when the customer spoke fewer than twice.",
    )
    customer_contributions: int = Field(ge=0)


class CallMetadata(StrictModel):
    """Computed in Python. The model never sees these as something to produce."""

    call_id: str
    duration_ms: Optional[int] = Field(
        None, ge=0,
        description="Length of the recording, read from the transcript file when it "
                    "is available alongside the cleaned turns",
    )
    total_turns: int
    employee_turns: int
    customer_turns: int
    employee_word_count: int
    customer_word_count: int
    talk_ratio: float = Field(
        ge=0.0, le=1.0, description="Employee words / total words"
    )
    longest_employee_monologue_words: int
    talk_pattern: Optional[TalkPattern] = Field(
        None,
        description="Shape of the conversation, when the transcript file with its "
                    "timings is available alongside the cleaned turns",
    )


class ScoreBlock(StrictModel):
    """The marking scheme totalled up. Computed in Python, never by the model.

    The four sections are added up (`earned_marks`) and put on a 0-100 scale as
    `score_before_flags`, which is also the final score: conduct flags are counted
    but cost nothing, so `flag_penalty_share` and `flag_penalty` are always 0 and
    are kept only so a report can state that in as many words. The one thing that
    still moves the number is a disqualifying flag, which voids the score outright:
    `disqualified` is true, `overall_score_100` is 0, and the report shows words
    instead of a number.

    An irrelevant lead is scored too, which it did not used to be. The two product
    lines are given full marks unjudged and listed in `not_applicable_criteria` —
    there is no pitch to make to somebody who is not buying — and the remaining six
    are marked as on any other call, because how a counsellor introduces themselves,
    qualifies, handles a question, shares the floor and ends a call does not depend
    on the person having been a buyer. `irrelevant_lead` stays true so the report can
    say what kind of number it is showing.
    """

    earned_marks: int = Field(ge=0, description="Marks across the four sections")
    max_marks: int = Field(gt=0)
    score_before_flags: float = Field(
        ge=0.0, le=100.0, description="The marks on a 0-100 scale, before conduct"
    )
    flag_count: int = Field(0, ge=0)
    flag_penalty_share: float = Field(
        0.0, ge=0.0, le=1.0,
        description="Always 0.0 — conduct flags no longer discount the score",
    )
    flag_penalty: float = Field(
        0.0, ge=0.0, le=100.0, description="Always 0.0 — kept for older reports"
    )
    disqualified: bool = Field(
        False, description="True when the call carries abusive or discriminatory conduct"
    )
    disqualifying_flags: List[str] = Field(
        default_factory=list, description="The flag types that voided the score"
    )
    irrelevant_lead: bool = Field(
        False,
        description="True when the person was not a NEET aspirant. The call is still "
                    "scored, but only on the lines that measure the counsellor rather "
                    "than the sale — see `not_applicable_criteria` — and the report "
                    "labels it so the number is read for what it is.",
    )
    irrelevant_reason: Optional[str] = Field(
        None, description="Why the lead was irrelevant, from `lead_relevance.reason`"
    )
    not_applicable_criteria: List[str] = Field(
        default_factory=list,
        description="Criteria that did not apply to this call and were given full "
                    "marks without being judged — the two product lines on an "
                    "irrelevant lead. Empty on an ordinary call.",
    )
    overall_score_100: float = Field(
        ge=0.0, le=100.0, description="The final figure the report leads with"
    )
    is_follow_up: bool = Field(
        False,
        description="True when this call was audited as a follow-up — the scorecard "
                    "then defaults the ground the earlier call already covered, and "
                    "charges for ground it covered twice.",
    )
    previous_call_id: Optional[str] = Field(
        None,
        description="The earlier call whose carry-forward summary was fed to this "
                    "audit, when there was one. Null on a follow-up recognised only "
                    "from the transcript, with no previous audit on disk.",
    )
    repetition_deductions: Dict[str, int] = Field(
        default_factory=dict,
        description="Marks taken off per criterion for going over ground the "
                    "previous call had already covered — one band per repeat. "
                    "Empty on every call that is not a follow-up.",
    )
    section_marks: Dict[str, int] = Field(
        description="Marks per section key, e.g. {'introduction': 7}"
    )
    section_max: Dict[str, int]
    criterion_marks: Dict[str, int]
    criterion_max: Dict[str, int]


class CallAuditDocument(StrictModel):
    """The file the dashboard reads: `{id}.audit.json`."""

    call_id: str
    prompt_version: str
    model: str
    schema_version: str
    generated_by: str = "audit_call.py"
    processing_mode: str = Field("single_pass", description="single_pass | map_reduce")
    source_files: Dict[str, str]
    metadata: CallMetadata
    scores: ScoreBlock
    report: AuditReport
    sentiment_layer_input: Dict[str, object] = Field(
        default_factory=dict,
        description="Sanitised sentiment-layer summary that was fed in as a hypothesis",
    )
    cost: Dict[str, object] = Field(
        default_factory=dict,
        description="What this call cost to produce: per-stage tokens and dollars "
                    "for transcription, analysis and the audit itself, plus the "
                    "rate-card version they were priced against.",
    )
    warnings: List[str] = Field(default_factory=list)


SCHEMA_VERSION = "2.4.0"
