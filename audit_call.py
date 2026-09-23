#!/usr/bin/env python3
"""Layer 3 of the sales-call pipeline: the call audit report.

    python3 audit_call.py --transcript {id}.clean.json --analysis {id}.analysis.json \
                          --out {id}.audit.json

    python3 audit_call.py --input-dir . --output-dir audits [--force]

Inputs
    {id}.clean.json     turn list from clean_transcript.py   (ground truth)
    {id}.analysis.json  sentiment layer from analyze_call.py (input hypothesis only)
    {prev}.audit.json   the earlier call, on a follow-up      (record, not evidence)

A call named `{id}_follow_up` is a second conversation with a student already on
file. Its earlier call's report is found beside its folder, or named with
--previous-audit, and its `sales_summary.carry_forward` block goes into the prompt
as the record of what has already been covered. The scorecard then reads in both
directions: ground the earlier call covered is not marked again here, and ground it
covered that this call covers *again* steps that criterion down a band per repeat.

Output
    {id}.audit.json         validated against audit_schema.CallAuditDocument
    {id}.audit.error.json   raw model output + validation errors, on failure (exit 1)

Call metadata and the weighted overall score are computed here, in Python — the model
is never asked to count turns or average its own scores.
"""

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from enum import Enum
from typing import Dict, List, Optional, Tuple

from pydantic import ValidationError

import cost
import audit_schema
from audit_schema import (
    SCHEMA_VERSION,
    CRITERION_BANDS,
    CRITERION_MAX,
    CRITERION_TITLE,
    SCORE_CRITERIA,
    SCORE_SECTIONS,
    TOTAL_MARKS,
    AuditReport,
    CallAuditDocument,
    CallMetadata,
    ChunkObservations,
    FlowCheckpoint,
    TalkPattern,
    ComplianceFlag,
    DISQUALIFYING_FLAGS,
    FlagType,
    ImprovementAction,
    Quote,
    ScoreBlock,
    ScoreCard,
    Severity,
    step_down,
)

# --------------------------------------------------------------------------- #
# Configuration — every tunable lives here or in the environment.
# --------------------------------------------------------------------------- #
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

OPENAI_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1") \
    .rstrip("/") + "/chat/completions"

# The audit runs once per call and writes the report the dashboard renders, so it
# is the layer where holding the marking scheme matters most. It ran on gpt-5.4-mini
# until the rest of the pipeline had moved to gpt-5.6-luna; on the measured token
# profile of this stage — ~31k prompt, ~3.3k completion — luna prices the same work
# about 70% lower, which is roughly a quarter off the whole pipeline. Override per
# run with --model, or per environment with AUDIT_MODEL.
MODEL = os.environ.get("AUDIT_MODEL", "gpt-5.6-luna")

# Requests sharing this key are routed to the same prompt cache, so the marking
# scheme — the same twenty thousand tokens on every audit — is paid for once and
# read back thereafter. Change the suffix when the system prompt changes, so a run
# is never routed to a cached copy of a prompt it is no longer sending.
AUDIT_CACHE_KEY = os.environ.get("AUDIT_CACHE_KEY", "arivihan-audit-v4")
TEMPERATURE = float(os.environ.get("AUDIT_TEMPERATURE", "0"))
MAX_TOKENS = int(os.environ.get("AUDIT_MAX_TOKENS", "8000"))
MAX_TOKENS_CHUNK = int(os.environ.get("AUDIT_MAX_TOKENS_CHUNK", "4000"))
# Two retries rather than one: the report's length caps are deliberately tight, and
# a first draft that runs long is the expected failure, not an exceptional one. The
# retry message carries the field name and its limit, so the second attempt is
# usually a trim rather than a rewrite.
SCHEMA_RETRIES = int(os.environ.get("AUDIT_SCHEMA_RETRIES", "2"))
TRANSPORT_RETRIES = int(os.environ.get("AUDIT_TRANSPORT_RETRIES", "2"))
TRANSPORT_BACKOFF_SECONDS = float(os.environ.get("AUDIT_TRANSPORT_BACKOFF", "3"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("AUDIT_TIMEOUT", "300"))

PROMPT_PATH = os.environ.get(
    "AUDIT_PROMPT_PATH", os.path.join(SCRIPT_DIR, "prompts", "audit_system_prompt.md")
)

# Map-reduce thresholds. Long calls are chunked and merged, never truncated.
MAP_REDUCE_TOKEN_THRESHOLD = int(os.environ.get("AUDIT_MAP_REDUCE_THRESHOLD", "12000"))
CHUNK_TURNS = int(os.environ.get("AUDIT_CHUNK_TURNS", "60"))
CHUNK_OVERLAP_TURNS = int(os.environ.get("AUDIT_CHUNK_OVERLAP_TURNS", "6"))
CHARS_PER_TOKEN_ESTIMATE = 3.0  # conservative for Devanagari; no tokenizer dependency

# The marking scheme itself lives in audit_schema.SCORE_SECTIONS: seven sections of
# ten marks. There is no separate weight table any more — a criterion counts for
# exactly what it is worth, and the model never sees or picks the marks.

# Closing is scored on every call, whatever happened.
#
# Every criterion defaults to full marks when the call never reached it — a counsellor
# is not marked down for a pitch the customer never let them give.
#
# The close is nearly the exception. Ending a call is the one thing that happens on
# every call, including the ones that end early, and asking when to call back is
# exactly what a counsellor should do when someone says they are busy — so a call that
# simply ran short is still marked on its close. But a call that never became a sales
# conversation at all, because it turned into an argument or because the student would
# not engage, never reached a close either, and forcing a mark there marks the
# counsellor on a scene the call never got to. The prompt names those two cases
# narrowly; the default is applied here as for any other criterion, and a warning is
# raised so an unreached close is always visible to a reader rather than silent.
DEFAULTED_WITH_NOTICE = frozenset({"closing_and_follow_up"})

# Flow — the order the call ran in, marked from three checkpoints.
#
# Every other line on the card marks one part of the call on its own. This one marks
# the shape of the whole: a pitch is supposed to open with the introduction said in
# one piece, qualify next and in one piece, and — wherever in the call it comes up —
# explain the personalised path in one piece. A counsellor who does all of that has
# run the call in the order it is meant to run in and the line is a 10. It is not a
# reward for turning up: it is the default, and marks come off only where the
# transcript shows the order actually broke.
#
# The model reports which checkpoints held and which broke. The mark is made here,
# from a count of the breaks, for the same reason every other default is applied in
# Python — a rule the model has to remember is a rule that goes missing on the calls
# it matters most for. One break costs a band, two costs another, and a call that
# broke all three never had a flow to mark.
FLOW_CHECKPOINTS = ("introduction_first", "qualification_block",
                    "personalised_path_block")

FLOW_MARK_BY_BREAKS = {0: 10, 1: 7, 2: 5, 3: 0}

# What each break is called in a sentence a counsellor reads, and what they should
# have done instead. Used to write the justification when the model's own sentence
# does not name the break.
FLOW_BREAK_WORDING = {
    "introduction_first": "the introduction was not the first thing on the call, or it "
                          "came out in pieces",
    "qualification_block": "the qualifying questions were not the next thing after the "
                           "introduction, or they were split up and returned to later",
    "personalised_path_block": "the personalised path was left half-explained and "
                                "picked up again later",
}

# The two lines an irrelevant lead does not get marked on.
#
# The rest of the card it does. A call to somebody who was never going to buy a NEET
# course still shows how the counsellor works: whether they introduced themselves,
# whether they asked which exam — which is how they were meant to find this out in the
# first place — whether they let the person speak, and whether they ended it decently.
# The card used to come back untouched at full marks on all eight lines and the score
# was thrown away, which discarded the one thing the call could say about the person
# Arivihan employs.
#
# What genuinely does not apply is the pitch. There is nothing to explain to somebody
# who is not buying, and "benefits for their study" is meaningless when their study is
# for a different exam. Those two default to full marks here rather than being left to
# the model, for the same reason every other default is applied in Python: a rule that
# depends on being remembered goes missing on the calls it matters most for.
#
# A counsellor who pitched for six minutes anyway is not rewarded by this. That failure
# is a failure to qualify, and it lands on `qualification_questions`, which is marked
# in full on these calls.
NOT_APPLICABLE_TO_IRRELEVANT_LEAD = ("why_what_how_explanation",
                                     "benefits_for_their_study")


# Conduct flags do not cost marks. A flag records how the counsellor spoke to the
# student; the scorecard measures how the call was sold. Netting one against the
# other produced a single number that answered neither question — a flagged call
# and a badly-sold one came out the same score — so the two are now reported side
# by side and the flags are read on their own terms. The one exception is below:
# abuse and discrimination void the score outright rather than discounting it.

# The sentiment layer's own flag vocabulary, mapped onto the audit's conduct types.
# Anything unrecognised carries over as `other_misconduct` rather than being dropped:
# the earlier layer heard something, and silently discarding it is how the pattern in
# a pushy call goes unrecorded.
SENTIMENT_FLAG_CATEGORIES = {
    "disrespectful_tone": FlagType.DISRESPECTFUL_CONDUCT,
    "rude": FlagType.DISRESPECTFUL_CONDUCT,
    "condescending": FlagType.DISRESPECTFUL_CONDUCT,
    "shaming_or_guilt_tripping": FlagType.COERCIVE_PRESSURE,
    "guilt_tripping": FlagType.COERCIVE_PRESSURE,
    "pressure": FlagType.COERCIVE_PRESSURE,
    "coercion": FlagType.COERCIVE_PRESSURE,
    "threat": FlagType.COERCIVE_PRESSURE,
    "abusive": FlagType.ABUSIVE_LANGUAGE,
    "abusive_language": FlagType.ABUSIVE_LANGUAGE,
    "offensive": FlagType.ABUSIVE_LANGUAGE,
    "profanity": FlagType.ABUSIVE_LANGUAGE,
    "inappropriate": FlagType.ABUSIVE_LANGUAGE,
    "inappropriate_language": FlagType.ABUSIVE_LANGUAGE,
    "harassment": FlagType.ABUSIVE_LANGUAGE,
    "discriminatory": FlagType.DISCRIMINATORY_REMARK,
    "discriminatory_remark": FlagType.DISCRIMINATORY_REMARK,
    "racist": FlagType.DISCRIMINATORY_REMARK,
    "racism": FlagType.DISCRIMINATORY_REMARK,
    "casteist": FlagType.DISCRIMINATORY_REMARK,
    "sexist": FlagType.DISCRIMINATORY_REMARK,
    "sexism": FlagType.DISCRIMINATORY_REMARK,
    "misogynistic": FlagType.DISCRIMINATORY_REMARK,
    "communal": FlagType.DISCRIMINATORY_REMARK,
}

# Categories the earlier layer can raise that are not conduct at all, and so never
# become flags.
#
# A promise about the course — "your marks will improve", "this path takes you to a
# government college", a guarantee, a surety — is a claim about the product. Whether it
# is true is a real question, and it is answered from `pitch_summary`, which records what
# was claimed. It is not the question `compliance_flags` asks, which is whether the
# student was mistreated. Assurance is how coaching is sold in this market; a counsellor
# promising a result is doing their job, and filing that beside a counsellor who shouted
# at a student makes the flag mean nothing.
#
# These are dropped on carry-over rather than falling through to `other_misconduct`,
# where anything genuinely unrecognised still goes — an unknown category is a category
# nobody has judged yet, and these have been judged.
NON_CONDUCT_CATEGORIES = {
    "misleading_claim",
    "false_claim",
    "false_promise",
    "overpromise",
    "exaggeration",
    "unrealistic_promise",
    "product_claim",
    "misrepresentation",
}


# A category the earlier layer invented that is not in the table above still has to
# reach the right place: these words appearing anywhere in it are enough to treat it
# as disqualifying rather than letting it fall through to `other_misconduct`, where
# it would be a deduction instead of a void.
DISQUALIFYING_KEYWORDS = (
    ("racist", FlagType.DISCRIMINATORY_REMARK),
    ("racial", FlagType.DISCRIMINATORY_REMARK),
    ("caste", FlagType.DISCRIMINATORY_REMARK),
    ("sexist", FlagType.DISCRIMINATORY_REMARK),
    ("sexual", FlagType.DISCRIMINATORY_REMARK),
    ("misogyn", FlagType.DISCRIMINATORY_REMARK),
    ("communal", FlagType.DISCRIMINATORY_REMARK),
    ("religio", FlagType.DISCRIMINATORY_REMARK),
    ("discriminat", FlagType.DISCRIMINATORY_REMARK),
    ("slur", FlagType.DISCRIMINATORY_REMARK),
    ("abus", FlagType.ABUSIVE_LANGUAGE),
    ("profan", FlagType.ABUSIVE_LANGUAGE),
    ("obscen", FlagType.ABUSIVE_LANGUAGE),
    ("vulgar", FlagType.ABUSIVE_LANGUAGE),
    ("harass", FlagType.ABUSIVE_LANGUAGE),
    ("inappropriate", FlagType.ABUSIVE_LANGUAGE),
    ("offensive", FlagType.ABUSIVE_LANGUAGE),
)


def flag_type_for_category(category: str) -> FlagType:
    """Map a sentiment-layer category onto a conduct type.

    Exact matches first, then a keyword sweep so a category this table has never
    seen — "casteist_remark", "sexually_inappropriate" — still lands on the type
    that voids the score rather than the catch-all that merely deducts.
    """
    known = SENTIMENT_FLAG_CATEGORIES.get(category)
    if known:
        return known
    for keyword, flag_type in DISQUALIFYING_KEYWORDS:
        if keyword in category:
            return flag_type
    return FlagType.OTHER_MISCONDUCT

# Scripts the transcripts are legitimately written in. Anything else (the sample
# analysis has an Armenian character spliced into a Hindi word) is transcription
# corruption and is stripped rather than propagated.
ALLOWED_UNICODE_BLOCKS = ("LATIN", "DEVANAGARI")

PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?\d[\d\s\-]{7,}\d)(?!\d)")
FENCE_PATTERN = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
PROMPT_VERSION_PATTERN = re.compile(r"prompt_version:\s*([0-9A-Za-z._-]+)")
WORD_SPLIT_PATTERN = re.compile("[\\s" + re.escape("।॥,.?!;:\"'()\u2013\u2014-") + "]+")

# Free-text fields are never allowed to carry raw PII. Verbatim evidence is exempt
# (it is the record), and so are identifier fields the CRM joins on.
PII_EXEMPT_KEYS = {"verbatim", "first_name", "call_id", "transcript", "analysis"}


# --------------------------------------------------------------------------- #
# Config / environment helpers
# --------------------------------------------------------------------------- #
def load_api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key.strip()

    env_path = os.path.join(SCRIPT_DIR, ".env")
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip() == "OPENAI_API_KEY":
                    return value.strip().strip("'\"")

    raise SystemExit("OPENAI_API_KEY not found in the environment or %s" % env_path)


def load_prompt() -> Tuple[str, str]:
    """Return (system_prompt, prompt_version) from the versioned markdown file."""
    if not os.path.isfile(PROMPT_PATH):
        raise SystemExit("System prompt not found at %s" % PROMPT_PATH)
    with open(PROMPT_PATH, "r", encoding="utf-8") as handle:
        text = handle.read()
    match = PROMPT_VERSION_PATTERN.search(text)
    return text, (match.group(1) if match else "unversioned")


def validate_marking_scheme() -> None:
    """Fail at startup if the scheme and the schema have drifted apart."""
    missing = set(SCORE_CRITERIA) - set(ScoreCard.model_fields)
    extra = set(ScoreCard.model_fields) - set(SCORE_CRITERIA)
    if missing or extra:
        raise SystemExit(
            "SCORE_SECTIONS does not match ScoreCard (missing=%s, unknown=%s)"
            % (sorted(missing), sorted(extra))
        )
    for section in SCORE_SECTIONS:
        marks = sum(marks for _, _, marks in section["criteria"])
        if marks != 10:
            raise SystemExit("Section '%s' is worth %d marks, expected 10"
                             % (section["key"], marks))
    # The bands are the ladder a follow-up repeat is stepped down, and they are
    # also written out in the prompt under *Allowed scores*. A ladder whose top
    # rung is not the criterion's maximum would quietly cap the whole criterion.
    for name in SCORE_CRITERIA:
        bands = CRITERION_BANDS.get(name)
        if not bands:
            raise SystemExit("No allowed scores listed for '%s'" % name)
        if list(bands) != sorted(bands) or bands[0] != 0:
            raise SystemExit("Bands for '%s' must run upward from 0" % name)
        if bands[-1] != CRITERION_MAX[name]:
            raise SystemExit(
                "Top band for '%s' is %d but the criterion is worth %d"
                % (name, bands[-1], CRITERION_MAX[name])
            )


# --------------------------------------------------------------------------- #
# Robust text handling
# --------------------------------------------------------------------------- #
def sanitize_text(value: str) -> Tuple[str, bool]:
    """Drop characters from scripts these calls are never written in.

    The sentiment layer can contain corrupted mixed-script text. It must never
    reach the model as if it were meaningful, nor be copied into the output.
    Returns (clean_text, was_modified).
    """
    if not isinstance(value, str):
        return value, False

    kept = []
    modified = False
    for char in value:
        if char.isspace() or not char.isalpha():
            # Punctuation, digits, currency signs and whitespace pass through,
            # minus control characters.
            if unicodedata.category(char).startswith("C") and not char.isspace():
                modified = True
                continue
            kept.append(char)
            continue
        try:
            name = unicodedata.name(char)
        except ValueError:
            modified = True
            continue
        if any(name.startswith(block) for block in ALLOWED_UNICODE_BLOCKS):
            kept.append(char)
        else:
            modified = True

    cleaned = re.sub(r"\s+", " ", "".join(kept)).strip()
    return cleaned, modified


def sanitize_structure(value):
    """Recursively sanitise a JSON-ish structure. Returns (value, was_modified)."""
    modified = False
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        out = []
        for item in value:
            cleaned, changed = sanitize_structure(item)
            modified = modified or changed
            out.append(cleaned)
        return out, modified
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            cleaned, changed = sanitize_structure(item)
            modified = modified or changed
            out[key] = cleaned
        return out, modified
    return value, False


def redact_pii(value, key: Optional[str] = None):
    """Strip phone-like digit runs from free-text fields (verbatim evidence exempt)."""
    if isinstance(value, str):
        if key in PII_EXEMPT_KEYS:
            return value
        return PHONE_PATTERN.sub("[REDACTED_PHONE]", value)
    if isinstance(value, list):
        return [redact_pii(item, key) for item in value]
    if isinstance(value, dict):
        return {name: redact_pii(item, name) for name, item in value.items()}
    return value


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def call_id_from_path(path: str) -> str:
    name = os.path.basename(path)
    for suffix in (".clean.json", ".analysis.json", ".transcript.json", ".audit.json",
                   ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return os.path.splitext(name)[0]


def load_turns(path: str) -> List[Tuple[str, str]]:
    """Accept the clean format ([{Role: text}]) or the detailed transcribe.py format."""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    if isinstance(data, dict):
        data = data.get("transcript", [])

    turns: List[Tuple[str, str]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if "speaker" in entry:
            speaker = str(entry.get("speaker", "Unknown"))
            text = str(entry.get("text", entry.get("statement", "")))
            pairs = [(speaker, text)]
        else:
            pairs = [(str(role), str(text)) for role, text in entry.items()]

        for speaker, text in pairs:
            clean, _ = sanitize_text(text)
            if clean:
                role = "Employee" if speaker.strip().lower() == "employee" else "Customer"
                turns.append((role, clean))

    if not turns:
        raise SystemExit("No usable turns found in %s" % path)
    return turns


# --------------------------------------------------------------------------- #
# Follow-up calls
# --------------------------------------------------------------------------- #
# A second conversation with the same student is named after the first one:
# `audio_92` is the call, `audio_92_follow_up` is the call back. That suffix is the
# whole of the convention — the recording carries it, every derived file inherits
# it, and the follow-up's outputs live in a folder of the same name inside the
# parent call's folder:
#
#     files/audio_92/audio_92.audit.json
#     files/audio_92/audio_92_follow_up/audio_92_follow_up.audit.json
#
# which is what lets the audit of the second call find the first one's report by
# walking one directory up, with no index and nothing to keep in step.
FOLLOW_UP_SUFFIX = "_follow_up"


def is_follow_up_id(call_id: str) -> bool:
    return call_id.endswith(FOLLOW_UP_SUFFIX) and len(call_id) > len(FOLLOW_UP_SUFFIX)


def parent_call_id(call_id: str) -> Optional[str]:
    """`audio_92` from `audio_92_follow_up`, or None if this is a first call."""
    return call_id[: -len(FOLLOW_UP_SUFFIX)] if is_follow_up_id(call_id) else None


def find_previous_audit(transcript_path: str) -> Optional[str]:
    """The earlier call's audit file, for a follow-up that has one on disk.

    Looked for beside the follow-up's folder — the layout above — and then in the
    folder itself, which is where a flat working directory would put it. A
    follow-up whose parent has not been audited yet is not an error: it still
    audits, against the transcript alone, and the report says the earlier call was
    not available.
    """
    call_id = call_id_from_path(transcript_path)
    parent = parent_call_id(call_id)
    if not parent:
        return None
    here = os.path.dirname(os.path.abspath(transcript_path))
    for folder in (os.path.dirname(here), here):
        candidate = os.path.join(folder, "%s.audit.json" % parent)
        if os.path.isfile(candidate):
            return candidate
    return None


def load_previous_call(path: Optional[str]) -> Tuple[Dict[str, object], List[str]]:
    """The earlier call, reduced to what the next audit is allowed to see.

    Only the carry-forward ledger and the handful of facts that frame it: what was
    established, what was asked, what was explained, where it was left. Not the
    marks — the second call is not graded against how the first one scored, and a
    model shown "the last one got 34/70" reaches for consistency with a number
    instead of reading the transcript. Not the coaching either, and none of the
    prose written for a human reader: it is all judgement about a call this audit
    is not auditing.

    Everything is sanitised on the way in, exactly as the sentiment hypothesis is:
    it is a file from another run, and the audit treats it as input, never as a
    trusted record.
    """
    warnings: List[str] = []
    if not path:
        return {}, warnings
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (ValueError, OSError) as problem:
        warnings.append("The previous call's audit at %s could not be read (%s), so "
                        "this follow-up was audited without it."
                        % (os.path.basename(path), problem))
        return {}, warnings

    report = (document.get("report") or {}) if isinstance(document, dict) else {}
    summary = report.get("sales_summary") or {}
    carry = summary.get("carry_forward") or {}
    if not isinstance(carry, dict) or not any(carry.values()):
        warnings.append(
            "The previous call's audit (%s) carries no carry-forward summary — it "
            "predates the follow-up layer — so this call was audited without a "
            "record of what the earlier one covered."
            % os.path.basename(path)
        )

    previous = {
        "call_id": document.get("call_id") if isinstance(document, dict) else None,
        "outcome": ((report.get("outcome_block") or {}).get("outcome")),
        "primary_blocker": summary.get("primary_blocker"),
        "call_in_brief": summary.get("call_in_brief"),
        "carry_forward": carry if isinstance(carry, dict) else {},
    }
    previous = {key: value for key, value in previous.items() if value}
    cleaned, changed = sanitize_structure(previous)
    if changed:
        warnings.append("Unreadable characters were stripped from the previous "
                        "call's summary before it was used.")
    return cleaned, warnings


def load_sentiment_hypothesis(path: Optional[str]) -> Tuple[Dict[str, object], List[str]]:
    """Load the sentiment layer defensively. A broken file is a warning, not a failure."""
    warnings: List[str] = []
    if not path:
        return {}, ["No sentiment layer supplied; audit ran on the transcript alone."]
    if not os.path.isfile(path):
        return {}, ["Sentiment layer file not found (%s); continued without it."
                    % os.path.basename(path)]

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (ValueError, OSError) as exc:
        return {}, ["Sentiment layer unreadable (%s); continued without it." % exc]

    analysis = raw.get("analysis", raw) if isinstance(raw, dict) else {}
    if not isinstance(analysis, dict):
        return {}, ["Sentiment layer had an unexpected shape; continued without it."]

    # Only the fields the audit is allowed to see as a hypothesis.
    subset = {
        key: analysis.get(key)
        for key in ("overall_sentiment", "sentiment_score", "call_tone_summary",
                    "employee_tone", "customer_tone", "tone_progression",
                    "customer_interest_level", "inappropriate_language_found",
                    "flagged_phrases", "notes")
        if key in analysis
    }
    cleaned, modified = sanitize_structure(subset)
    if modified:
        warnings.append(
            "Sentiment layer contained corrupted or out-of-script characters; "
            "they were stripped before use and were not copied into this report."
        )
    return cleaned, warnings


# --------------------------------------------------------------------------- #
# A. Deterministic metadata
# --------------------------------------------------------------------------- #
def word_count(text: str) -> int:
    return len([word for word in re.split(r"\s+", text) if word])


def read_audio_timing(transcript_path: str) -> Tuple[Optional[int], List[Tuple[str, int, int]]]:
    """Recover the length of the recording and who spoke when.

    The cleaned turn list is text only, so timing lives one file back in the
    `{id}.transcript.json` the cleaner was fed. Both are looked up rather than
    required: a call audited from turns alone still produces a report, just without
    a duration or a measured talk pattern.
    """
    directory = os.path.dirname(os.path.abspath(transcript_path))
    call_id = call_id_from_path(transcript_path)
    candidate = os.path.join(directory, "%s.transcript.json" % call_id)
    if not os.path.isfile(candidate):
        return None, []
    try:
        with open(candidate, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (ValueError, OSError):
        return None, []
    if not isinstance(raw, dict):
        return None, []

    value = raw.get("audio_duration_ms")
    duration = int(value) if isinstance(value, (int, float)) and value >= 0 else None

    segments: List[Tuple[str, int, int]] = []
    for entry in raw.get("transcript") or []:
        if not isinstance(entry, dict):
            continue
        start, end = entry.get("start_ms"), entry.get("end_ms")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        if end < start:
            continue
        speaker = "Employee" \
            if str(entry.get("speaker", "")).strip().lower() == "employee" else "Customer"
        segments.append((speaker, int(start), int(end)))

    segments.sort(key=lambda segment: segment[1])
    if duration is None and segments:
        duration = segments[-1][2]
    return duration, segments


def measure_talk_pattern(segments: List[Tuple[str, int, int]]) -> Optional[TalkPattern]:
    """Work out how much of the call was one-sided, in seconds rather than turns.

    The model reads text, so it cannot tell a forty-second explanation from a
    four-second one — and that difference is the whole of this metric. The measuring
    happens here, deterministically, and the model is handed the numbers.

    A "stretch" runs from the moment the employee starts talking to the moment the
    customer next says anything, so back-to-back employee utterances with a pause
    between them count as one stretch. That is what it sounds like to the customer.
    """
    if not segments:
        return None

    stretches: List[int] = []
    gaps: List[int] = []
    stretch_start: Optional[int] = None
    last_customer_start: Optional[int] = None
    contributions = 0

    for speaker, start, end in segments:
        if speaker == "Employee":
            if stretch_start is None:
                stretch_start = start
            continue
        # A customer utterance closes any open employee stretch.
        if stretch_start is not None:
            stretches.append(max(0, start - stretch_start))
            stretch_start = None
        contributions += 1
        if last_customer_start is not None:
            gaps.append(start - last_customer_start)
        last_customer_start = start

    # A final employee stretch that runs to the end of the call still counts.
    if stretch_start is not None:
        stretches.append(max(0, segments[-1][2] - stretch_start))

    median_gap = None
    if gaps:
        ordered = sorted(gaps)
        middle = len(ordered) // 2
        median_gap = ordered[middle] if len(ordered) % 2 \
            else (ordered[middle - 1] + ordered[middle]) // 2

    return TalkPattern(
        longest_employee_stretch_ms=max(stretches) if stretches else 0,
        employee_stretches_over_45s=sum(1 for span in stretches if span > 45_000),
        employee_stretches_over_60s=sum(1 for span in stretches if span > 60_000),
        employee_stretches_over_90s=sum(1 for span in stretches if span > 90_000),
        median_customer_gap_ms=median_gap,
        customer_contributions=contributions,
    )


def compute_metadata(call_id: str, turns: List[Tuple[str, str]],
                     duration_ms: Optional[int] = None,
                     talk_pattern: Optional[TalkPattern] = None) -> CallMetadata:
    employee_words = sum(word_count(t) for s, t in turns if s == "Employee")
    customer_words = sum(word_count(t) for s, t in turns if s == "Customer")
    total_words = employee_words + customer_words

    # A monologue is a run of consecutive Employee turns with no customer input.
    longest = 0
    run = 0
    for speaker, text in turns:
        if speaker == "Employee":
            run += word_count(text)
            longest = max(longest, run)
        else:
            run = 0

    return CallMetadata(
        call_id=call_id,
        duration_ms=duration_ms,
        talk_pattern=talk_pattern,
        total_turns=len(turns),
        employee_turns=sum(1 for s, _ in turns if s == "Employee"),
        customer_turns=sum(1 for s, _ in turns if s == "Customer"),
        employee_word_count=employee_words,
        customer_word_count=customer_words,
        talk_ratio=round(employee_words / total_words, 4) if total_words else 0.0,
        longest_employee_monologue_words=longest,
    )


def apply_repetition_deductions(report: AuditReport) -> Tuple[Dict[str, int], List[str]]:
    """Charge a follow-up for the ground it covered twice.

    A follow-up is given the previous call's carry-forward record and marked
    against it in both directions. One direction is already free: a criterion the
    earlier call covered and this one did not comes back `reached: false` and takes
    full marks below, so the counsellor is not marked down for declining to repeat
    themselves. This is the other direction — the counsellor who asked the student
    her class for the second time, or re-explained the path the first call had
    already explained. That is not a neutral act. The student answered it once, and
    the call spent its minutes re-establishing what was on file instead of moving
    the decision on.

    The model reports the repeats and names the criterion each lands on; the
    arithmetic is here, as it is for every other number in the scorecard. Each
    repeat steps its criterion one band down its own ladder — 10 → 7 → 3 → 0 on
    qualification, 5 → 3 → 0 on the intro lines — so one slip costs a band and a
    call that ran the whole first script again lands near the floor. A band rather
    than a fixed number of marks because the ladders are different lengths, and
    "three marks off" is a scratch on one criterion and half of another.

    Nothing is deducted on a criterion the model marked `reached: false`: it cannot
    both have skipped that ground and gone over it twice, and the full marks it
    takes below would swallow the deduction anyway. That contradiction is worth
    saying out loud, so it is warned about rather than silently resolved.

    Returns (marks removed per criterion, warnings).
    """
    deductions: Dict[str, int] = {}
    warnings: List[str] = []
    repeats = report.call_continuity.repeated_from_previous
    if not repeats or not report.call_continuity.is_follow_up:
        if repeats:
            warnings.append(
                "%d repeat(s) of the previous call were listed on a call not marked "
                "as a follow-up; no marks were taken off for them." % len(repeats)
            )
        return deductions, warnings

    counts: Dict[str, int] = {}
    for repeat in repeats:
        counts[repeat.criterion] = counts.get(repeat.criterion, 0) + 1

    for name, steps in sorted(counts.items()):
        criterion = getattr(report.scorecard, name)
        if not criterion.reached:
            warnings.append(
                "'%s' was marked as never reached, yet %d repeat(s) of the previous "
                "call were listed against it; no marks were taken off."
                % (CRITERION_TITLE[name], steps)
            )
            continue
        before = min(criterion.awarded, CRITERION_MAX[name])
        after = step_down(name, before, steps)
        if after == before:
            continue
        criterion.awarded = after
        deductions[name] = before - after
        warnings.append(
            "Follow-up: '%s' went over ground the previous call had already covered "
            "(%s), so it steps down %d band%s, %d → %d."
            % (CRITERION_TITLE[name],
               "; ".join(repeat.what for repeat in repeats
                         if repeat.criterion == name),
               steps, "" if steps == 1 else "s", before, after)
        )
    return deductions, warnings


def enforce_pitch_flow(report: AuditReport) -> List[str]:
    """Mark the flow line from its checkpoints rather than from the model's number.

    The model reads the call and says, for each of the three checkpoints, whether the
    order held, broke, or was never reached. Everything after that is arithmetic:
    full marks, less a band for each break. Doing it here rather than trusting the
    number keeps the flow line consistent across calls in a way no amount of prompt
    wording does — two calls that broke the same checkpoint come out the same mark.

    A checkpoint the call never reached costs nothing. That is the same rule the rest
    of the card runs on: a counsellor is not marked down for a part of the call the
    customer never let them get to, and the personalised path never coming up is the
    ordinary case on a short call, not a flow break. Where all three were unreached
    there was no flow to judge, so the criterion itself is marked unreached and keeps
    its full marks.

    Returns warnings.
    """
    warnings: List[str] = []
    criterion = report.scorecard.pitch_flow_order
    checkpoints = criterion.checkpoints()

    broken = [name for name, state in checkpoints.items()
              if state == FlowCheckpoint.BROKEN]
    unreached = [name for name, state in checkpoints.items()
                 if state == FlowCheckpoint.NOT_REACHED]

    if len(unreached) == len(checkpoints):
        # Nothing of the pitch happened — a wrong number, a student who rang off at
        # turn two. There is no order to judge, so the line defaults like any other.
        criterion.reached = False
        criterion.awarded = CRITERION_MAX["pitch_flow_order"]
        return warnings

    criterion.reached = True
    mark = FLOW_MARK_BY_BREAKS[len(broken)]
    if criterion.awarded != mark:
        warnings.append(
            "Flow of the pitch: %d of the three checkpoints broke (%s), which is %d "
            "marks; the audit returned %d, and the computed mark stands."
            % (len(broken),
               ", ".join(broken) if broken else "none",
               mark, criterion.awarded)
        )
    criterion.awarded = mark

    # The justification has to name what broke, because the mark on its own does not.
    # Where the model wrote a sentence that never mentions the break, say it plainly
    # rather than leaving a 5 on the page with no reason attached to it.
    if broken:
        lowered = (criterion.justification or "").lower()
        if not any(word in lowered
                   for word in ("order", "first", "later", "split", "piece", "flow",
                                "before", "back to")):
            criterion.justification = (
                "The call did not run in the expected order: %s."
                % "; ".join(FLOW_BREAK_WORDING[name] for name in broken)
            )
    return warnings


def compute_scores(report: AuditReport,
                   previous_call_id: Optional[str] = None) -> Tuple[ScoreBlock, List[str]]:
    """Total the scorecard and put it on a 0-100 scale.

    A criterion the model over-marked is clamped rather than trusted — awarding 8
    out of 5 would silently inflate the call. Conduct flags are counted but cost
    nothing, so the final score is the marks as awarded; only a disqualifying flag
    changes it, by voiding it. This still runs *after* `reconcile_conduct_flags`,
    which is what decides whether a carried-over flag is disqualifying. Returns
    (scores, warnings).
    """
    warnings: List[str] = []

    # The flow line is made from its checkpoints, and it happens first so the
    # repetition step below and the clamps after it all see the real mark rather than
    # whatever number the model put beside the checkpoints.
    warnings.extend(enforce_pitch_flow(report))

    # Before the defaulting below, so a repeat is charged against the mark the
    # model actually awarded rather than against a criterion that has just been
    # handed full marks for never having been reached.
    deductions, repetition_warnings = apply_repetition_deductions(report)
    warnings.extend(repetition_warnings)

    irrelevant = not report.lead_relevance.is_relevant
    not_applicable: List[str] = []
    if irrelevant:
        # The pitch lines, forced rather than trusted — see the note on
        # NOT_APPLICABLE_TO_IRRELEVANT_LEAD. Everything else on the card is marked
        # as it came back, because on this call it is the whole of what there is
        # to know about the counsellor.
        for name in NOT_APPLICABLE_TO_IRRELEVANT_LEAD:
            criterion = getattr(report.scorecard, name)
            not_applicable.append(name)
            # Only worth saying when a mark actually moved. The `reached` flag is
            # corrected either way, silently: a model that marked the pitch and gave
            # it full marks anyway has cost the reader nothing.
            if criterion.awarded != CRITERION_MAX[name]:
                warnings.append(
                    "'%s' does not apply to a lead who was never a NEET aspirant, so "
                    "it takes the full %d rather than the %d returned."
                    % (CRITERION_TITLE[name], CRITERION_MAX[name], criterion.awarded)
                )
            criterion.reached = False
            criterion.awarded = CRITERION_MAX[name]

    # A criterion the call never reached carries full marks, and that is applied here
    # rather than trusted to the model: a default that depends on remembering a rule
    # is a default that goes missing on the calls it matters most for.
    for name in SCORE_CRITERIA:
        criterion = getattr(report.scorecard, name)
        if criterion.reached:
            continue
        if name in DEFAULTED_WITH_NOTICE:
            warnings.append(
                "'%s' was marked as never reached — it is scored on all but the calls "
                "that never became a sales conversation, so check the reason given: %s"
                % (CRITERION_TITLE[name], criterion.justification)
            )
        cap = CRITERION_MAX[name]
        if criterion.awarded != cap:
            warnings.append(
                "'%s' was not reached on this call, so it takes the full %d rather "
                "than the %d returned." % (CRITERION_TITLE[name], cap, criterion.awarded)
            )
            criterion.awarded = cap

    awarded = report.scorecard.awarded()

    for name, marks in awarded.items():
        cap = CRITERION_MAX[name]
        if marks > cap:
            warnings.append(
                "'%s' was marked %d out of a maximum of %d; clamped to %d."
                % (CRITERION_TITLE[name], marks, cap, cap)
            )
            awarded[name] = cap
            getattr(report.scorecard, name).awarded = cap

    section_marks = {}
    section_max = {}
    for section in SCORE_SECTIONS:
        names = [name for name, _, _ in section["criteria"]]
        section_marks[section["key"]] = sum(awarded[name] for name in names)
        section_max[section["key"]] = sum(CRITERION_MAX[name] for name in names)

    earned = sum(awarded.values())
    before = round(earned * 100.0 / TOTAL_MARKS, 1)

    # Flags are recorded against the call, not charged to it: the score that comes
    # out is the score the selling earned. The two zeroes are kept in the block so
    # the report can still say, in as many words, that conduct took nothing off.
    flag_count = len(report.compliance_flags)
    share = 0.0
    penalty = 0.0
    final = before

    # Abuse and discrimination are the one thing that still moves the number, and
    # they do not discount it — there is no marking scheme under which they are
    # traded off against a good pitch, so the score is voided and the report says so
    # in words. Everything else on the page still renders — the reader still needs
    # to know what happened on the call.
    # Arivihan teaches NEET, and half this marking scheme is a scheme for selling to a
    # NEET aspirant. On somebody who was never a buyer the pitch lines measure nothing
    # and are defaulted above — but the rest of the card measures the counsellor, not
    # the sale, and it is marked. The score that comes out says how the call was
    # conducted; the label says who it was to, and the report carries both. Flags stand
    # either way: how the counsellor spoke to them is read the same way whoever picked
    # up.
    if irrelevant:
        warnings.append(
            "Irrelevant lead: %s. The two product lines do not apply and take full "
            "marks; the rest of the card is marked as usual, so %.1f/100 reads as how "
            "the call was conducted rather than how it sold."
            % (report.lead_relevance.reason or "the student was not a NEET aspirant",
               final)
        )

    disqualifying = sorted({
        flag.flag_type.value for flag in report.compliance_flags
        if flag.flag_type in DISQUALIFYING_FLAGS
    })
    if disqualifying:
        final = 0.0
        warnings.append(
            "Score voided: the call carries %s. No mark is given."
            % ", ".join(name.replace("_", " ") for name in disqualifying)
        )
    elif flag_count:
        warnings.append(
            "%d conduct flag(s) recorded; no marks deducted, final %.1f/100."
            % (flag_count, final)
        )

    return ScoreBlock(
        is_follow_up=report.call_continuity.is_follow_up,
        previous_call_id=previous_call_id,
        repetition_deductions=deductions,
        earned_marks=earned,
        max_marks=TOTAL_MARKS,
        score_before_flags=before,
        flag_count=flag_count,
        flag_penalty_share=share,
        flag_penalty=penalty,
        disqualified=bool(disqualifying),
        disqualifying_flags=disqualifying,
        irrelevant_lead=irrelevant,
        irrelevant_reason=report.lead_relevance.reason if irrelevant else None,
        not_applicable_criteria=not_applicable,
        overall_score_100=final,
        section_marks=section_marks,
        section_max=section_max,
        criterion_marks=awarded,
        criterion_max=dict(CRITERION_MAX),
    ), warnings


# --------------------------------------------------------------------------- #
# Prompt assembly
# --------------------------------------------------------------------------- #
def render_numbered_transcript(turns: List[Tuple[str, str]], start: int = 0) -> str:
    return "\n".join(
        "[%d] %s: %s" % (start + offset, speaker, text)
        for offset, (speaker, text) in enumerate(turns)
    )


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN_ESTIMATE)


def spoken_duration(ms: Optional[int]) -> str:
    """A span of time the way someone says it out loud, not the way it is stored.

    The metadata is milliseconds because that is what the timings are, and the model
    was copying them straight into a justification a floor manager then had to read:
    "talked 162360 ms at longest" tells them nothing they can act on. Handed the
    words instead, it writes the words.
    """
    if ms is None:
        return "unknown"
    seconds = int(round(ms / 1000.0))
    if seconds < 90:
        return "%d sec" % seconds
    minutes, rest = divmod(seconds, 60)
    return "%d min" % minutes if rest == 0 else "%d min %d sec" % (minutes, rest)


def describe_talk_pattern(pattern: Optional[TalkPattern]) -> str:
    """The talk pattern in the words the justification is meant to use."""
    if pattern is None:
        return "No timings available for this recording — judge from how the turns alternate."

    # The deciding figure is given its own line, above the others and labelled as
    # what it is. Listed flat — "over 45 sec: 2; over 60 sec: 1; over 90 sec: 0" —
    # the three read as three thresholds of increasing severity, and a call with a
    # 67-second stretch and nothing over ninety comes back marked down a band for
    # it. The rule that only ninety seconds costs a mark is in the prompt, three
    # hundred lines from here; this is the copy that sits next to the numbers.
    lines = [
        "- stretches over 90 sec: %d  ← THE DECIDING FIGURE for two_way_conversation."
        % pattern.employee_stretches_over_90s,
        "  0 of them is 10 marks. Not 7, not 5 — 10, whatever the figures below say.",
        "  One is 7, two is 5, three or more (or one running several minutes) is 2.",
        "- longest unbroken counsellor stretch: %s"
        % spoken_duration(pattern.longest_employee_stretch_ms),
        "- stretches over 45 sec: %d; over 60 sec: %d  ← context only. Neither is a"
        % (pattern.employee_stretches_over_45s, pattern.employee_stretches_over_60s),
        "  deduction, on this criterion or any other. They say how the call was paced.",
        "- the student spoke %d time(s)" % pattern.customer_contributions,
    ]
    if pattern.median_customer_gap_ms is not None:
        lines.append("- usually about %s between the student's contributions"
                     % spoken_duration(pattern.median_customer_gap_ms))
    return "\n".join(lines)


PREVIOUS_CALL_HEADING = (
    "## PREVIOUS CALL (this is a follow-up; the record of the earlier conversation)"
)


def render_previous_call(previous: Dict[str, object]) -> str:
    """The earlier call as a block in the user message.

    Given its own heading rather than folded into the metadata, because it is the
    one input that changes how the scorecard is read: ground it lists as covered is
    ground this call is not marked for skipping, and ground it lists as covered that
    this call covers *again* is a deduction. It is a record of another call, not
    evidence about this one — nothing in it can be cited as a turn, and the
    transcript in front of the model is still the only ground truth.
    """
    if not previous:
        return ""
    return "\n\n".join([
        PREVIOUS_CALL_HEADING,
        "The audit of the call before this one. It is a RECORD, not evidence: never "
        "quote it, never cite a turn index for it, and never state anything from it "
        "as something that happened on this call. Its only use is knowing what has "
        "already been covered — see *The follow-up call* in your instructions.",
        json.dumps(previous, ensure_ascii=False, indent=2),
    ])


def build_user_message(transcript_block: str, hypothesis: Dict[str, object],
                       metadata: CallMetadata, schema: Dict[str, object],
                       instruction: str,
                       previous: Optional[Dict[str, object]] = None) -> str:
    sections = [
        "## CALL METADATA (already computed — do not recompute, do not return)",
        json.dumps(metadata.model_dump(), ensure_ascii=False, indent=2),
        "## TALK PATTERN, IN WORDS (the same numbers above, said the way to write them)",
        describe_talk_pattern(metadata.talk_pattern),
    ]
    previous_block = render_previous_call(previous or {})
    if previous_block:
        sections.append(previous_block)
    sections.extend([
        "## TRANSCRIPT (ground truth; cite these turn indices as evidence)",
        transcript_block,
        "## SENTIMENT LAYER (input hypothesis only — may be wrong; never quote it)",
        json.dumps(hypothesis, ensure_ascii=False, indent=2) if hypothesis
        else "(not available)",
        "## JSON SCHEMA the response must satisfy",
        json.dumps(schema, ensure_ascii=False),
        "## TASK",
        instruction,
    ])
    return "\n\n".join(sections)


# --------------------------------------------------------------------------- #
# OpenAI transport
# --------------------------------------------------------------------------- #
def _post(payload: Dict[str, object], api_key: str) -> Dict[str, object]:
    request = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer %s" % api_key,
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def complete(messages: List[Dict[str, str]], api_key: str,
             max_tokens: int) -> Tuple[str, Dict[str, object]]:
    """One JSON-mode completion, with transport retries and parameter fallbacks.

    Returns the content and the provider's `usage` block. The usage was previously
    dropped on the floor, which left the most expensive stage in the pipeline as the
    only one nobody could cost — including the retries below, which are whole extra
    calls and now show up as such.
    """
    payload: Dict[str, object] = {
        "model": MODEL,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": TEMPERATURE,
        "max_completion_tokens": max_tokens,
        # The marking scheme runs to some twenty thousand tokens and is identical on
        # every call audited. Sharing a cache key routes each audit to where the last
        # one left that block, so it is billed at the cached rate instead of being
        # re-read from scratch. Bump the suffix whenever the system prompt changes.
        "prompt_cache_key": AUDIT_CACHE_KEY,
    }

    attempt = 0
    while True:
        try:
            body = _post(payload, api_key)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            # Newer models reject some legacy parameters; drop them and retry once each.
            if exc.code == 400 and "temperature" in detail and "temperature" in payload:
                payload.pop("temperature")
                continue
            if exc.code == 400 and "prompt_cache_key" in detail \
                    and "prompt_cache_key" in payload:
                payload.pop("prompt_cache_key")
                continue
            if exc.code == 400 and "max_completion_tokens" in detail:
                payload["max_tokens"] = payload.pop("max_completion_tokens")
                continue
            if exc.code in (429, 500, 502, 503, 504) and attempt < TRANSPORT_RETRIES:
                attempt += 1
                time.sleep(TRANSPORT_BACKOFF_SECONDS * attempt)
                continue
            raise SystemExit("OpenAI API error %s:\n%s" % (exc.code, detail[:2000]))
        except urllib.error.URLError as exc:
            if attempt < TRANSPORT_RETRIES:
                attempt += 1
                time.sleep(TRANSPORT_BACKOFF_SECONDS * attempt)
                continue
            raise SystemExit("Could not reach the OpenAI API (%s)" % exc.reason)

    try:
        content = body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError):
        raise SystemExit("Unexpected OpenAI response: %s" % json.dumps(body)[:2000])

    if not content.strip():
        raise SystemExit("Model returned empty content (finish_reason=%s)"
                         % body["choices"][0].get("finish_reason"))
    usage = body.get("usage")
    return content, (usage if isinstance(usage, dict) else {})


def strip_fences(text: str) -> str:
    return FENCE_PATTERN.sub("", text.strip()).strip()


class SchemaFailure(Exception):
    """Raised when the model could not produce a schema-valid report."""

    def __init__(self, errors: List[str], raw_output: str):
        super().__init__("; ".join(errors)[:500])
        self.errors = errors
        self.raw_output = raw_output


def drop_forbidden_extras(payload: object, error: ValidationError) -> List[str]:
    """Delete the keys pydantic rejected as `extra_forbidden`, in place.

    `StrictModel` forbids unknown keys so hallucinated structure is caught rather
    than rendered. The cost is that ONE stray key — a `criterion` copied across from
    `coaching_actions` into `improvements`, say — throws away an otherwise complete
    thirty-field report, and a re-roll is both slower and no more likely to be right
    than deleting the key. So the extras named in the error are removed and the
    report is validated again; every deletion is returned as a warning, so the
    structural slip stays visible instead of being silently accepted.

    Only `extra_forbidden` is touched. A missing field, a bad enum or an
    out-of-range number still fails and still goes to the model to fix.
    """
    dropped: List[str] = []
    for item in error.errors():
        if item.get("type") != "extra_forbidden":
            continue
        location = item.get("loc") or ()
        if not location:
            continue

        node = payload
        for step in location[:-1]:
            try:
                node = node[step]
            except (KeyError, IndexError, TypeError):
                node = None
                break
        key = location[-1]
        if isinstance(node, dict) and key in node:
            del node[key]
            dropped.append(".".join(str(step) for step in location))
    return dropped


def complete_validated(model_cls, system_prompt: str, user_message: str,
                       api_key: str, max_tokens: int,
                       warnings: Optional[List[str]] = None,
                       ledger: Optional["cost.UsageLedger"] = None):
    """Call the model, validate against `model_cls`, retry with the error appended.

    Unknown keys are deleted and revalidated before a retry is spent on them; each
    deletion is appended to `warnings`.

    Every attempt is recorded on `ledger`, including the ones whose output is thrown
    away — a rejected response is billed exactly like an accepted one.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    errors: List[str] = []
    raw = ""

    def note_trims(trims):
        """Say which prose fields were cut to fit, so the trim is never silent."""
        if not trims or warnings is None:
            return
        warnings.append(" ".join(dict.fromkeys(trims))[:400])

    def note_retries():
        """Record what the discarded attempts got wrong.

        A retry is a whole second call to the model, and until now a run that failed
        once and succeeded on the retry looked identical in the output to one that
        got it right first time. The cost was invisible and so was the cause, which
        is the wrong way round: a field the model reliably trips over is a field
        worth rewording, and nobody could see which one it was.
        """
        if not errors or warnings is None:
            return
        warnings.append(
            "Took %d extra model call(s): the first response was rejected — %s"
            % (len(errors), " | ".join(errors)[:400].replace("\n", " "))
        )

    for attempt in range(SCHEMA_RETRIES + 1):
        content, usage = complete(messages, api_key, max_tokens)
        if ledger is not None:
            ledger.record(usage)
        raw = strip_fences(content)
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            failure = "Response was not valid JSON: %s" % exc
        else:
            trims = audit_schema.collect_trims()
            try:
                report = model_cls.model_validate(parsed)
            except ValidationError as exc:
                dropped = drop_forbidden_extras(parsed, exc)
                if dropped:
                    try:
                        report = model_cls.model_validate(parsed)
                    except ValidationError as second:
                        failure = "Schema validation failed:\n%s" % second
                    else:
                        if warnings is not None:
                            warnings.append(
                                "Model returned %d key(s) the schema does not define; "
                                "they were dropped: %s."
                                % (len(dropped), ", ".join(dropped))
                            )
                        note_trims(trims)
                        note_retries()
                        return report
                else:
                    failure = "Schema validation failed:\n%s" % exc
            else:
                note_trims(trims)
                note_retries()
                return report

        errors.append(failure)
        if attempt == SCHEMA_RETRIES:
            break
        print("  schema attempt %d failed, retrying with the error appended"
              % (attempt + 1), file=sys.stderr)
        messages = messages + [
            {"role": "assistant", "content": raw[:20000]},
            {"role": "user",
             "content": "Your previous response was rejected.\n\n%s\n\n"
                        "Return the corrected JSON object only — no markdown fences, no "
                        "commentary. Keep every field that was already correct, fix only "
                        "what the error names, and keep all evidence_turns anchored to "
                        "real turn indices. Where a field is too long, cut it to clauses "
                        "rather than trimming a word off the end: drop subjects, "
                        "articles and connectives, and keep the finding." % failure},
        ]

    raise SchemaFailure(errors, raw)


# --------------------------------------------------------------------------- #
# Audit passes
# --------------------------------------------------------------------------- #
SINGLE_PASS_INSTRUCTION = (
    "Audit this call and return the JSON report. Every non-metadata claim must carry "
    "the turn indices it came from. Do not compute an overall score. Write "
    "`sales_summary` last, after the rest of the report is settled, and derive it "
    "only from findings you have already made above."
)

# Appended to whichever instruction the run uses when a PREVIOUS CALL block is in
# the message. It says what to do with that block in three lines; the reasoning
# behind each is in the system prompt, under *The follow-up call*. It is worth
# repeating here because the instruction is the last thing in the message and the
# marking scheme is twenty thousand tokens above it.
FOLLOW_UP_INSTRUCTION = (
    "\n\nThis call is a FOLLOW-UP and the previous call's record is above. Set "
    "`call_continuity.is_follow_up` to true. Ground the previous call already "
    "covered and this call did not is `reached: false` at full marks — do not mark "
    "it down. Ground the previous call already covered that this call covered AGAIN "
    "goes in `call_continuity.repeated_from_previous`, one entry per repeat, naming "
    "the criterion it lands on; do not deduct for it yourself, the pipeline steps "
    "the criterion down a band for each. Everything the previous record does not "
    "cover is marked exactly as on a first call."
)

CHUNK_INSTRUCTION = (
    "This is ONE CHUNK of a longer call. Do not write the final report and do not judge "
    "the call as a whole — you cannot see all of it. Extract only what this chunk "
    "supports, using the absolute turn indices shown, and return the chunk-observations "
    "JSON object."
)

REDUCE_INSTRUCTION = (
    "Below are ordered observations extracted from every chunk of one long call, with "
    "absolute turn indices preserved. Merge them into a single audit report for the whole "
    "call. Deduplicate: the same objection appearing in several chunks is ONE objection "
    "with later occurrences in repeated_at_turns. Use only turn indices that appear in "
    "the observations. Do not compute an overall score."
)


def chunk_turns(turns: List[Tuple[str, str]]) -> List[Tuple[int, List[Tuple[str, str]]]]:
    """Split into overlapping windows, returning (absolute_start_index, turns)."""
    step = max(1, CHUNK_TURNS - CHUNK_OVERLAP_TURNS)
    chunks = []
    start = 0
    while start < len(turns):
        chunks.append((start, turns[start:start + CHUNK_TURNS]))
        if start + CHUNK_TURNS >= len(turns):
            break
        start += step
    return chunks


def run_single_pass(turns, hypothesis, metadata, system_prompt, api_key,
                    warnings=None, ledger=None, previous=None) -> AuditReport:
    instruction = SINGLE_PASS_INSTRUCTION + (FOLLOW_UP_INSTRUCTION if previous else "")
    user_message = build_user_message(
        render_numbered_transcript(turns),
        hypothesis,
        metadata,
        AuditReport.model_json_schema(),
        instruction,
        previous,
    )
    return complete_validated(AuditReport, system_prompt, user_message, api_key,
                              MAX_TOKENS, warnings, ledger)


def run_map_reduce(turns, hypothesis, metadata, system_prompt, api_key,
                   warnings=None, ledger=None, previous=None) -> AuditReport:
    observations = []
    windows = chunk_turns(turns)
    for position, (start, window) in enumerate(windows, start=1):
        print("  chunk %d/%d (turns %d-%d)"
              % (position, len(windows), start, start + len(window) - 1),
              file=sys.stderr)
        user_message = build_user_message(
            render_numbered_transcript(window, start=start),
            {},  # the hypothesis belongs to the whole call, not to a chunk
            metadata,
            ChunkObservations.model_json_schema(),
            CHUNK_INSTRUCTION,
        )
        chunk = complete_validated(ChunkObservations, system_prompt, user_message,
                                   api_key, MAX_TOKENS_CHUNK, warnings, ledger)
        observations.append(chunk.model_dump(mode="json"))

    # The previous call belongs to the reduce step and not to the chunks: a chunk
    # is told in as many words that it cannot judge the call as a whole, and
    # deciding what was covered twice is exactly that kind of judgement.
    reduce_sections = [
        "## CALL METADATA (already computed — do not recompute, do not return)",
        json.dumps(metadata.model_dump(), ensure_ascii=False, indent=2),
    ]
    previous_block = render_previous_call(previous or {})
    if previous_block:
        reduce_sections.append(previous_block)
    reduce_sections.extend([
        "## CHUNK OBSERVATIONS (ordered; turn indices are absolute)",
        json.dumps(observations, ensure_ascii=False, indent=2),
        "## SENTIMENT LAYER (input hypothesis only — may be wrong; never quote it)",
        json.dumps(hypothesis, ensure_ascii=False, indent=2) if hypothesis
        else "(not available)",
        "## JSON SCHEMA the response must satisfy",
        json.dumps(AuditReport.model_json_schema(), ensure_ascii=False),
        "## TASK",
        REDUCE_INSTRUCTION + (FOLLOW_UP_INSTRUCTION if previous else ""),
    ])
    reduce_message = "\n\n".join(reduce_sections)
    return complete_validated(AuditReport, system_prompt, reduce_message, api_key,
                              MAX_TOKENS, warnings, ledger)


# --------------------------------------------------------------------------- #
# Post-validation guardrails
# --------------------------------------------------------------------------- #
def reanchor_flag_quotes(report: AuditReport,
                         turns: List[Tuple[str, str]]) -> List[str]:
    """Move a flag's quote to the turn its words are actually in.

    The model cites the turn it believes it read, and on a long call it is
    occasionally one out. That used to be cosmetic. It is not: the sentiment-layer
    carry-over skips a phrase only when the audit already covered *that turn*, and
    `dedupe_flags` merges only flags whose turns overlap — so a quote filed one turn
    late meant the same remark came back a second time from the earlier layer and
    survived deduplication as a separate finding. One line, two flags, on a call
    where the reviewer counts flags.

    Only moved where the transcript settles it: the quoted words have to be findable
    in a turn, and the flag keeps its original anchor when they are not. Nothing is
    dropped here and no flag is created — this only corrects where one points.
    """
    warnings: List[str] = []
    for flag in report.compliance_flags:
        phrase = (flag.quote.verbatim or "").strip()
        if not phrase:
            continue
        stated = flag.quote.turn_index
        # `locate_phrase` checks the stated turn first, so a flag whose quote really
        # is where it says comes straight back unchanged.
        found = locate_phrase(phrase, stated, "Employee", turns)
        if found is None or found == stated:
            continue
        warnings.append(
            "A flag quoted turn %d but the words are in turn %d; re-anchored to the "
            "transcript." % (stated, found)
        )
        flag.quote.turn_index = found
        flag.occurrence_turns = sorted(
            {found} | {turn for turn in flag.occurrence_turns if turn != stated}
        )
    return warnings


def dedupe_flags(report: AuditReport) -> None:
    """Merge flags the model emitted twice for the same pattern.

    Deduplication is deterministic here rather than prompted, so the dashboard's
    flag counts never depend on how well the model followed instructions.
    """
    rank = {"low": 0, "medium": 1, "high": 2}
    merged: List = []
    for flag in report.compliance_flags:
        turns = set(flag.occurrence_turns) | {flag.quote.turn_index}
        for existing in merged:
            existing_turns = set(existing.occurrence_turns) | {existing.quote.turn_index}
            # Same pattern reported twice: same type, overlapping turns.
            if existing.flag_type == flag.flag_type and (existing_turns & turns):
                if rank[existing.severity.value] < rank[flag.severity.value]:
                    existing.severity = flag.severity
                union = sorted(existing_turns | turns)
                existing.occurrence_turns = union
                existing.occurrences = max(existing.occurrences, flag.occurrences,
                                           len(union))
                break
        else:
            flag.occurrence_turns = sorted(turns)
            merged.append(flag)
    report.compliance_flags = merged


# "turn 13", "turns 13, 19 and 28", "(turns 24, 26)", "at turn 30" — the whole
# citation, including the preposition that introduces it. Separators repeat, so
# ", and " is one boundary rather than two.
TURN_CITATION_PATTERN = re.compile(
    r"\s*[(\[]?\s*(?:at|in|on|by|from|during|see|through|across|over)?\s*"
    r"turns?\s+\d+(?:(?:\s*(?:,|and|&|-|\u2013)\s*)+\d+)*\s*[)\]]?",
    re.IGNORECASE,
)
LEFTOVER_PUNCTUATION = re.compile(r"\s+([,.;:])")


def scrub_sales_summary(report: AuditReport) -> List[str]:
    """Strip turn citations from the sales layer.

    The sections above are evidence and keep their citations. `sales_summary` is
    read by someone who never sees the transcript, so a stray "at turn 13" there
    is noise the reader cannot act on. Stripping it here means the fix holds for
    every consumer, not just the React app.
    """
    warnings = []

    def clean(text: str) -> Optional[str]:
        """Return the cleaned text, or None when there was nothing to strip."""
        stripped = TURN_CITATION_PATTERN.sub(" ", text)
        if stripped == text:
            return None
        out = LEFTOVER_PUNCTUATION.sub(r"\1", re.sub(r"\s{2,}", " ", stripped)).strip()
        # Removing a leading "At turn 30 " leaves the sentence starting lower-case.
        return out[:1].upper() + out[1:] if out else out

    def rewrite(value, where: str):
        """Clean one field, returning the replacement or None to leave it alone."""
        # Enums subclass `str`, so an isinstance check alone would rewrite
        # `pitch_relevance` into a value the schema does not accept.
        if isinstance(value, Enum) or not isinstance(value, str):
            return None
        cleaned = clean(value)
        if cleaned is None:
            return None
        warnings.append("Stripped a turn citation from sales_summary.%s" % where)
        return cleaned

    def scrub_block(block, prefix: str) -> None:
        """Clean every string and list-of-strings on one model, in place."""
        for name in type(block).model_fields:
            value = getattr(block, name)
            where = "%s%s" % (prefix, name)
            replacement = rewrite(value, where)
            if replacement is not None:
                setattr(block, name, replacement)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, ImprovementAction):
                        for part in ("title", "detail"):
                            fixed = rewrite(getattr(item, part),
                                            "%s[%d].%s" % (where, index, part))
                            if fixed is not None:
                                setattr(item, part, fixed)
                    else:
                        fixed = rewrite(item, "%s[%d]" % (where, index))
                        if fixed is not None:
                            value[index] = fixed

    summary = report.sales_summary
    scrub_block(summary, "")
    # The carry-forward ledger is nested a level down and is read by the *next*
    # audit rather than by a person, which makes a stray "at turn 13" worse there
    # than anywhere else on the page: the next call has no transcript to resolve
    # it against, so the index would be carried forward as though it meant
    # something.
    scrub_block(summary.carry_forward, "carry_forward.")
    return warnings


def check_carry_forward(report: AuditReport) -> List[str]:
    """Say so when a call leaves nothing for the next one to be marked against.

    `carry_forward` has a default, because a schema that rejects a whole report
    over a missing ledger costs a twenty-thousand-token audit to get one back. The
    cost of the default is that an omission is silent — and it stays silent until
    the student is called back weeks later and that call is marked as a first call
    because there was nothing on file. So it is checked here instead.

    Genuinely empty is possible: a wrong number establishes nothing and asks
    nothing. That call has nothing to carry forward and the warning is the right
    thing to say about it either way.
    """
    carry = report.sales_summary.carry_forward
    filled = [name for name in type(carry).model_fields if getattr(carry, name)]
    if filled:
        return []
    return ["The report carries no `sales_summary.carry_forward` ledger, so a "
            "follow-up to this call will have no record of what it covered and "
            "will be marked as a first call."]


def locate_phrase(phrase: str, stated_turn: Optional[int], speaker: Optional[str],
                  turns: List[Tuple[str, str]]) -> Optional[int]:
    """Find which turn a flagged phrase actually belongs to.

    The sentiment layer numbers its turns independently of this pipeline — in the
    sample data it counts from one where the audit counts from zero — so trusting
    its index quotes the wrong speaker. The phrase itself is the reliable key, so
    it is searched for: the stated turn and its neighbours first, then the whole
    transcript. Returns None when the phrase is nowhere in the call.
    """
    needle = re.sub(r"\s+", " ", phrase).strip()
    if not needle:
        return None

    def matches(index: int) -> bool:
        if not 0 <= index < len(turns):
            return False
        if speaker and turns[index][0].lower() != speaker.lower():
            return False
        return needle in re.sub(r"\s+", " ", turns[index][1])

    if stated_turn is not None:
        # Neighbours first: an off-by-one is the common case and the cheapest fix.
        for candidate in (stated_turn, stated_turn - 1, stated_turn + 1):
            if matches(candidate):
                return candidate

    for index in range(len(turns)):
        if matches(index):
            return index
    return None


def reconcile_conduct_flags(report: AuditReport,
                            hypothesis: Dict[str, object],
                            turns: List[Tuple[str, str]]) -> List[str]:
    """Make sure nothing the sentiment layer flagged is quietly lost.

    The audit is supposed to review every phrase the earlier layer flagged and
    raise it as a conduct flag where it holds up. Being supposed to is not a
    guarantee, and a dropped flag is invisible — the report simply looks clean. So
    every flagged turn the audit did not cover is carried over here, deterministically,
    marked `source='sentiment_layer'` so a reviewer can see which findings the audit
    itself stood behind.

    The transcript stays the evidence: the quote is taken from the turn, not from the
    earlier layer's paraphrase, and a flag pointing outside the transcript is dropped.

    The one thing that stops a carry-over is the audit saying it looked and rejected
    it, in `sentiment_layer_agreement.dismissed_turns`. Coverage alone cannot tell a
    turn the audit forgot from one it read and threw out — ASR garble, a line not
    addressed to the customer — and re-adding a judged rejection put phantom flags on
    clean calls, which is the burying this section was narrowed to avoid. A dismissal
    is recorded as a warning rather than a flag, so nothing disappears silently.
    """
    raw = hypothesis.get("flagged_phrases") or []
    if not isinstance(raw, list):
        return []

    covered = set()
    for flag in report.compliance_flags:
        covered.add(flag.quote.turn_index)
        covered.update(flag.occurrence_turns)

    # A turn cannot be both raised and dismissed; a flag the audit itself wrote wins.
    dismissed = {
        turn for turn in report.sentiment_layer_agreement.dismissed_turns
        if turn not in covered
    }

    warnings: List[str] = []
    carried = 0
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        stated = entry.get("turn")
        stated = stated if isinstance(stated, int) else None
        phrase = str(entry.get("phrase") or "").strip()
        speaker = str(entry.get("speaker") or "").strip() or None

        turn = locate_phrase(phrase, stated, speaker, turns)
        if turn is None:
            warnings.append(
                "Could not find the phrase the sentiment layer flagged at turn %r "
                "anywhere in the transcript; it was carried over unanchored."
                % (stated,)
            )
            turn = stated if stated is not None and 0 <= stated < len(turns) else 0
        elif stated is not None and turn != stated:
            warnings.append(
                "Sentiment layer flag said turn %d but the phrase is at turn %d; "
                "anchored to the transcript." % (stated, turn)
            )
        if turn in covered:
            continue
        # The two layers number turns differently, and the audit dismisses by the
        # index it was shown. Accept either the transcript-anchored turn or the one
        # the sentiment layer stated, so an off-by-one does not resurrect a flag the
        # audit explicitly threw out.
        if turn in dismissed or (stated is not None and stated in dismissed):
            warnings.append(
                "Sentiment layer flagged turn %d; the audit read it and judged it not a "
                "conduct problem, so it was not raised: %s"
                % (turn, report.sentiment_layer_agreement.notes or "see notes.")
            )
            continue

        category = str(entry.get("category") or "").strip().lower()
        if category in NON_CONDUCT_CATEGORIES:
            # Not dropped silently: the reviewer is told the earlier layer raised it and
            # why it is not on the report, which is the same courtesy the carry-over
            # itself exists to provide in the other direction.
            warnings.append(
                "Sentiment layer flagged turn %d as '%s' — a claim about the product, "
                "not conduct, so it was not raised as a flag." % (turn, category)
            )
            continue

        severity = str(entry.get("severity") or "").strip().lower()
        if severity not in {level.value for level in Severity}:
            severity = "medium"

        # A conduct problem that ran across several turns arrives as one entry with
        # the rest of its turns in `also_at_turns`. Carry them into occurrence_turns
        # so the flag keeps its full span instead of shrinking to its first line.
        occurrences = [turn]
        # The sentiment layer counts turns from one and this pipeline from zero, so
        # apply the same offset the phrase search just established for the anchor.
        offset = turn - stated if stated is not None else 0
        for extra in entry.get("also_at_turns") or []:
            if not isinstance(extra, int):
                continue
            shifted = extra + offset
            if 0 <= shifted < len(turns) and shifted not in occurrences:
                occurrences.append(shifted)

        report.compliance_flags.append(ComplianceFlag(
            flag_type=flag_type_for_category(category),
            severity=Severity(severity),
            quote=Quote(verbatim=phrase or turns[turn][1], gloss_en="", turn_index=turn),
            why_flagged=str(entry.get("reason") or "Flagged by the sentiment layer."),
            occurrence_turns=sorted(occurrences),
            source="sentiment_layer",
        ))
        covered.update(occurrences)
        carried += 1

    if carried:
        warnings.append(
            "Carried over %d conduct flag(s) that the sentiment layer raised and the "
            "audit did not pick up. They are marked source='sentiment_layer'."
            % carried
        )
    return warnings


def check_unlinked_repeats(report: AuditReport,
                           turns: List[Tuple[str, str]]) -> List[str]:
    """Warn when a Customer restated an objection that the report did not link.

    The model is inconsistent about anchoring an objection at its first, indirect
    surfacing (a blocking fact disclosed during discovery) and linking the later
    restatement. That is a dashboard-visible miss, so it is detected here in Python
    instead of being left to the prompt.
    """
    def tokens(text: str) -> set:
        # Split on whitespace and punctuation only: \W would shred Devanagari,
        # whose combining vowel marks are not word characters to `re`.
        parts = WORD_SPLIT_PATTERN.split(text.lower())
        return {word for word in parts if len(word) > 2}

    warnings = []
    for objection in report.objections:
        cited = {objection.quote.turn_index} | set(objection.repeated_at_turns)
        anchor = tokens(objection.quote.verbatim)
        if not anchor:
            continue
        for index, (speaker, text) in enumerate(turns):
            if speaker != "Customer" or index in cited or index > objection.quote.turn_index:
                continue
            other = tokens(text)
            if not other:
                continue
            overlap = len(anchor & other) / min(len(anchor), len(other))
            if overlap >= 0.5:
                warnings.append(
                    "Turn %d looks like an earlier surfacing of the '%s' objection "
                    "anchored at turn %d, but was not linked in repeated_at_turns."
                    % (index, objection.objection_type.value,
                       objection.quote.turn_index)
                )
    return warnings


def check_evidence_bounds(report: AuditReport, total_turns: int) -> List[str]:
    """Flag citations that point outside the transcript — a hallucination tell."""
    out_of_range = set()

    def walk(value, key=None):
        if isinstance(value, dict):
            for name, item in value.items():
                walk(item, name)
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif isinstance(value, int) and not isinstance(value, bool):
            if key in ("turn_index", "start_turn", "end_turn") or (
                key in ("evidence_turns", "response_turn_indices", "repeated_at_turns",
                        "occurrence_turns", "outcome_evidence_turns", "turn_range")
            ):
                if value < 0 or value >= total_turns:
                    out_of_range.add(value)

    walk(report.model_dump(mode="json"))
    if out_of_range:
        return ["Report cited turn indices outside the transcript (0-%d): %s"
                % (total_turns - 1, sorted(out_of_range))]
    return []


# --------------------------------------------------------------------------- #
# Orchestration for one call
# --------------------------------------------------------------------------- #
def upstream_costs(transcript_path: str, analysis_path: Optional[str],
                   duration_ms: Optional[int]) -> Dict[str, object]:
    """What the transcribe and analyse stages already spent on this call.

    The audit is the last stage to touch a call, which makes its output the only
    place a whole-pipeline number can be assembled. Both upstream files are read
    best-effort: a call transcribed before costs were recorded still audits, it just
    reports what it knows. Missing stages are marked, never guessed at as zero — a
    silent zero would read as "free" rather than "unmeasured".
    """
    directory = os.path.dirname(os.path.abspath(transcript_path))
    call_id = call_id_from_path(transcript_path)
    stages: Dict[str, object] = {}

    transcript_file = os.path.join(directory, "%s.transcript.json" % call_id)
    stt_model = None
    if os.path.isfile(transcript_file):
        try:
            with open(transcript_file, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            stt_model = raw.get("model") if isinstance(raw, dict) else None
            recorded = raw.get("cost") if isinstance(raw, dict) else None
        except (ValueError, OSError):
            recorded = None
        # A transcript written before this existed has no block; the rate is public
        # and the duration is on file, so it can be recomputed rather than lost.
        stages["transcribe"] = (recorded if isinstance(recorded, dict)
                                else cost.soniox_block(duration_ms, stt_model))

    if analysis_path and os.path.isfile(analysis_path):
        try:
            with open(analysis_path, "r", encoding="utf-8") as handle:
                analysis = json.load(handle)
        except (ValueError, OSError):
            analysis = {}
        block = analysis.get("cost") if isinstance(analysis, dict) else None
        if isinstance(block, dict):
            stages["analyse"] = block
        elif isinstance(analysis, dict) and analysis.get("token_usage"):
            # Pre-`cost` analyses: the raw usage is there, so price it now. The
            # conduct scan's summed block lost its cached split before it was
            # written, so anything recovered from one is an upper bound.
            model = analysis.get("model")
            whole = cost.UsageLedger(model, "analyse.whole_call")
            whole.record(analysis.get("token_usage") or {})
            scan = cost.UsageLedger(model, "analyse.conduct_scan")
            scan.record(analysis.get("scan_token_usage") or {})
            stages["analyse"] = {
                "whole_call": whole.as_dict(),
                "conduct_scan": scan.as_dict(),
                "total_usd": round(cost.combine(whole, scan) or 0.0, 6),
                "note": "Recovered from raw usage written before cost tracking; the "
                        "conduct-scan figure has no cached-token split and is an "
                        "upper bound.",
            }
    return stages


def stage_total(block: object) -> Optional[float]:
    """The dollars in a stage block, whatever shape it was written in."""
    if not isinstance(block, dict):
        return None
    for key in ("total_usd", "cost_usd"):
        value = block.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def audit_one(transcript_path: str, analysis_path: Optional[str], out_path: str,
              api_key: str, system_prompt: str, prompt_version: str,
              previous_audit_path: Optional[str] = None) -> int:
    call_id = call_id_from_path(transcript_path)
    turns = load_turns(transcript_path)
    hypothesis, warnings = load_sentiment_hypothesis(analysis_path)
    duration_ms, segments = read_audio_timing(transcript_path)
    metadata = compute_metadata(call_id, turns, duration_ms,
                                measure_talk_pattern(segments))

    # A follow-up finds its parent's report by name unless one was named for it.
    # A first call never looks, and a follow-up whose parent has not been audited
    # yet still runs — the transcript alone can establish a follow-up, and the
    # report says the earlier record was missing rather than refusing to audit.
    if previous_audit_path is None:
        previous_audit_path = find_previous_audit(transcript_path)
    previous, previous_warnings = load_previous_call(previous_audit_path)
    warnings.extend(previous_warnings)
    previous_call_id = None
    if previous_audit_path:
        previous_call_id = (previous.get("call_id") or parent_call_id(call_id)
                            or call_id_from_path(previous_audit_path))
    if is_follow_up_id(call_id) and not previous_audit_path:
        warnings.append(
            "%s is named as a follow-up to %s, but no audit for that call is on "
            "disk, so this one was scored without a record of what was already "
            "covered. Audit the earlier call first and re-run this one."
            % (call_id, parent_call_id(call_id))
        )

    transcript_block = render_numbered_transcript(turns)
    use_map_reduce = estimate_tokens(transcript_block) > MAP_REDUCE_TOKEN_THRESHOLD
    mode = "map_reduce" if use_map_reduce else "single_pass"

    print("Auditing %s (%d turns, %s, model=%s%s)"
          % (call_id, len(turns), mode, MODEL,
             ", follow-up to %s" % previous_call_id if previous else ""),
          file=sys.stderr)

    ledger = cost.UsageLedger(MODEL, "audit.%s" % mode)
    runner = run_map_reduce if use_map_reduce else run_single_pass
    try:
        report = runner(turns, hypothesis, metadata, system_prompt, api_key,
                        warnings, ledger, previous)
    except SchemaFailure as failure:
        error_path = os.path.join(
            os.path.dirname(os.path.abspath(out_path)), "%s.audit.error.json" % call_id
        )
        with open(error_path, "w", encoding="utf-8") as handle:
            json.dump({
                "call_id": call_id,
                "model": MODEL,
                "prompt_version": prompt_version,
                "schema_version": SCHEMA_VERSION,
                "processing_mode": mode,
                "validation_errors": failure.errors,
                # A run that fails validation has still been billed for every
                # attempt, so the cost is written even though the report is not.
                "cost": {"audit": ledger.as_dict()},
                "raw_model_output": failure.raw_output,
            }, handle, ensure_ascii=False, indent=2)
        print("Schema validation failed after %d retr%s — raw output written to %s"
              % (SCHEMA_RETRIES, "y" if SCHEMA_RETRIES == 1 else "ies", error_path),
              file=sys.stderr)
        return 1

    # Anchors first: the carry-over and the dedupe both match on turn indices, so a
    # quote pointing at the wrong turn defeats each of them in turn.
    warnings.extend(reanchor_flag_quotes(report, turns))
    warnings.extend(reconcile_conduct_flags(report, hypothesis, turns))
    dedupe_flags(report)
    warnings.extend(scrub_sales_summary(report))
    warnings.extend(check_carry_forward(report))
    warnings.extend(check_unlinked_repeats(report, turns))
    warnings.extend(check_evidence_bounds(report, len(turns)))

    # Handed the previous call's record, the audit is a follow-up whatever the
    # model concluded from the transcript: the pipeline knows something the
    # transcript does not, and a report that came back `is_follow_up: false` would
    # be scored against the first-call scheme with the earlier call in the prompt.
    if previous and not report.call_continuity.is_follow_up:
        report.call_continuity.is_follow_up = True
        report.call_continuity.basis = report.call_continuity.basis or (
            "The previous call (%s) was audited on this student; the transcript "
            "itself carries no reference back to it." % previous_call_id
        )
        warnings.append(
            "The transcript carries no reference to an earlier call, but %s was "
            "audited as a follow-up to %s because that call's record was supplied."
            % (call_id, previous_call_id)
        )

    scores, score_warnings = compute_scores(report, previous_call_id)
    warnings.extend(score_warnings)

    stages = upstream_costs(transcript_path, analysis_path, duration_ms)
    stages["audit"] = ledger.as_dict()
    known = [stage_total(block) for block in stages.values()]
    measured = [value for value in known if value is not None]
    call_cost = {
        "stages": stages,
        "total_usd": round(sum(measured), 6),
        "complete": len(measured) == len(known) and "transcribe" in stages
                    and "analyse" in stages,
        "audio_seconds": round((duration_ms or 0) / 1000.0, 2),
        "usd_per_audio_hour": (round(sum(measured) / (duration_ms / 3_600_000.0), 4)
                               if duration_ms else None),
        "price_table_version": cost.PRICE_TABLE_VERSION,
    }

    document = CallAuditDocument(
        call_id=call_id,
        prompt_version=prompt_version,
        model=MODEL,
        schema_version=SCHEMA_VERSION,
        processing_mode=mode,
        source_files={
            "transcript": os.path.basename(transcript_path),
            "analysis": os.path.basename(analysis_path) if analysis_path else "",
            "previous_audit": (os.path.relpath(previous_audit_path,
                                               os.path.dirname(os.path.abspath(out_path)))
                               if previous_audit_path else ""),
        },
        metadata=metadata,
        scores=scores,
        report=report,
        sentiment_layer_input=hypothesis,
        cost=call_cost,
        warnings=warnings,
    )

    # Redact only the model-authored subtrees; provenance and ids are joined on
    # by the CRM and must survive verbatim.
    payload = document.model_dump(mode="json")
    payload["report"] = redact_pii(payload["report"])
    payload["sentiment_layer_input"] = redact_pii(payload["sentiment_layer_input"])
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    print("  " + ledger.summary_line(), file=sys.stderr)
    print("  call total $%.4f%s%s"
          % (call_cost["total_usd"],
             "" if call_cost["complete"] else " (audit stage only — upstream costs "
                                              "not on file)",
             (" = $%.3f/audio-hour" % call_cost["usd_per_audio_hour"])
             if call_cost["usd_per_audio_hour"] else ""),
          file=sys.stderr)
    print("  → %s (%d/%d marks = %s, outcome %s, %d flag(s))"
          % (out_path, document.scores.earned_marks, document.scores.max_marks,
             "NO SCORE — %s" % ", ".join(document.scores.disqualifying_flags)
             if document.scores.disqualified
             else "NOT SCORED — irrelevant lead"
             if document.scores.irrelevant_lead
             else "%.0f/100" % document.scores.overall_score_100,
             report.outcome_block.outcome.value, len(report.compliance_flags)),
          file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args():
    parser = argparse.ArgumentParser(
        description="Audit a sales call from its cleaned transcript and sentiment layer."
    )
    parser.add_argument("--transcript", help="Path to {id}.clean.json")
    parser.add_argument("--analysis", help="Path to {id}.analysis.json")
    parser.add_argument("--out", help="Path to write {id}.audit.json")
    parser.add_argument("--previous-audit",
                        help="Path to the earlier call's {id}.audit.json, for a "
                             "follow-up. Found automatically for a call named "
                             "{id}%s; pass it to override or to use a name that "
                             "does not follow the convention." % FOLLOW_UP_SUFFIX)
    parser.add_argument("--input-dir", help="Batch mode: directory of *.clean.json")
    parser.add_argument("--output-dir", help="Batch mode: where audits are written")
    parser.add_argument("--force", action="store_true",
                        help="Batch mode: re-audit ids that already have an audit file")
    parser.add_argument("--model", help="Override the model (default: %s)" % MODEL)
    return parser.parse_args()


def main() -> int:
    global MODEL
    args = parse_args()
    if args.model:
        MODEL = args.model

    validate_marking_scheme()
    system_prompt, prompt_version = load_prompt()
    api_key = load_api_key()

    if args.input_dir:
        output_dir = args.output_dir or args.input_dir
        os.makedirs(output_dir, exist_ok=True)
        # Follow-ups last, and each after its own parent: a follow-up audited
        # before the call it follows has no record to be marked against, and the
        # ordering is the whole of what it takes to avoid that in batch mode.
        transcripts = sorted(
            (os.path.join(args.input_dir, name)
             for name in os.listdir(args.input_dir)
             if name.endswith(".clean.json")),
            key=lambda path: (is_follow_up_id(call_id_from_path(path)),
                              parent_call_id(call_id_from_path(path))
                              or call_id_from_path(path)),
        )
        if not transcripts:
            print("No *.clean.json files in %s" % args.input_dir, file=sys.stderr)
            return 1

        failures = 0
        for transcript_path in transcripts:
            call_id = call_id_from_path(transcript_path)
            out_path = os.path.join(output_dir, "%s.audit.json" % call_id)
            if os.path.exists(out_path) and not args.force:
                print("Skipping %s (audit exists; use --force)" % call_id,
                      file=sys.stderr)
                continue
            analysis_path = os.path.join(args.input_dir, "%s.analysis.json" % call_id)
            parent = parent_call_id(call_id)
            previous = os.path.join(output_dir, "%s.audit.json" % parent) if parent else None
            failures += audit_one(
                transcript_path,
                analysis_path if os.path.isfile(analysis_path) else None,
                out_path, api_key, system_prompt, prompt_version,
                previous if previous and os.path.isfile(previous) else None,
            )
        return 1 if failures else 0

    if not args.transcript:
        print("Provide --transcript (single call) or --input-dir (batch).",
              file=sys.stderr)
        return 2
    if not os.path.isfile(args.transcript):
        print("Transcript not found: %s" % args.transcript, file=sys.stderr)
        return 2

    call_id = call_id_from_path(args.transcript)
    out_path = args.out or os.path.join(
        os.path.dirname(os.path.abspath(args.transcript)), "%s.audit.json" % call_id
    )
    analysis_path = args.analysis
    if analysis_path is None:
        guess = os.path.join(os.path.dirname(os.path.abspath(args.transcript)),
                             "%s.analysis.json" % call_id)
        analysis_path = guess if os.path.isfile(guess) else None

    if args.previous_audit and not os.path.isfile(args.previous_audit):
        print("Previous audit not found: %s" % args.previous_audit, file=sys.stderr)
        return 2

    return audit_one(args.transcript, analysis_path, out_path, api_key,
                     system_prompt, prompt_version, args.previous_audit)


if __name__ == "__main__":
    sys.exit(main())
