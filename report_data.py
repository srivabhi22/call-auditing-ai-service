#!/usr/bin/env python3
"""What the report app is handed: the audit, with the conversation attached.

The audit file quotes a flagged line and names its turn; the report shows that line
inside the exchange it happened in, with a clock time against each turn. Neither
the audit nor the cleaned transcript carries times — only the raw transcript does —
so the two are re-aligned here rather than changing what any pipeline stage writes.
"""

import json
import os
import re

# The cleaner merges back-to-back turns by the same speaker, so a cleaned turn can
# span several transcribed utterances. Both stages are applied the same way here.
from clean_transcript import clean_text, normalise_role


def _raw_utterances(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    turns = data.get("transcript") if isinstance(data, dict) else data
    return turns if isinstance(turns, list) else []


def _merged(utterances):
    """The raw utterances grouped the way clean_transcript.py groups them."""
    groups = []
    for turn in utterances:
        if not isinstance(turn, dict):
            continue
        role = normalise_role(turn.get("speaker", "Unknown"), False)
        text = clean_text(turn.get("text", turn.get("statement", "")))
        if not text:
            continue
        if groups and groups[-1]["speaker"] == role:
            groups[-1]["text"] = ("%s %s" % (groups[-1]["text"], text)).strip()
            groups[-1]["end_ms"] = turn.get("end_ms")
        else:
            groups.append({"speaker": role, "text": text,
                           "start_ms": turn.get("start_ms"),
                           "end_ms": turn.get("end_ms")})
    return groups


def _clean_turns(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    turns = data.get("turns") if isinstance(data, dict) else data
    out = []
    for turn in turns or []:
        if not isinstance(turn, dict):
            continue
        if "speaker" in turn:
            out.append({"speaker": turn.get("speaker"),
                        "text": turn.get("text") or turn.get("utterance") or ""})
        else:
            for speaker, text in turn.items():   # {"Employee": "..."} — one key
                out.append({"speaker": speaker, "text": text})
    return out


def display_transcript(clean_path, raw_path=None):
    """The turns the report renders: speaker, text, and a start time where known.

    The cleaned transcript is the authority on what the turns are — it is what the
    audit was scored against, and `quote.turn_index` indexes into it. Times are
    lifted from the raw transcript only when re-running the merge reproduces the
    cleaned turns exactly; a partial or shifted alignment would put the wrong clock
    against a quote, which is worse than showing no clock at all.
    """
    if not clean_path or not os.path.isfile(clean_path):
        return None
    turns = _clean_turns(clean_path)
    if not turns:
        return None

    timed = _merged(_raw_utterances(raw_path)) if raw_path and os.path.isfile(raw_path) else []
    if len(timed) == len(turns):
        for turn, source in zip(turns, timed):
            if source.get("start_ms") is not None:
                turn["start_ms"] = source["start_ms"]
                turn["end_ms"] = source.get("end_ms")
    return turns


def call_stem(name):
    """`audio_8` from any of its files — the id every stage names its output after."""
    base = os.path.basename(name)
    return re.sub(r"\.(transcript|clean|analysis|audit)$", "",
                  os.path.splitext(base)[0])


def published(audit_path):
    """The audit as the app reads it: the file, plus the conversation behind it."""
    with open(audit_path, "r", encoding="utf-8") as handle:
        audit = json.load(handle)

    folder = os.path.dirname(os.path.abspath(audit_path))
    stem = call_stem(audit_path)
    named = (audit.get("source_files") or {}).get("transcript")
    clean = os.path.join(folder, os.path.basename(named)) if named else None
    if not clean or not os.path.isfile(clean):
        clean = os.path.join(folder, "%s.clean.json" % stem)

    transcript = display_transcript(clean, os.path.join(folder, "%s.transcript.json" % stem))
    if transcript:
        audit["transcript"] = transcript
    return audit
