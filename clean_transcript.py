#!/usr/bin/env python3
"""
Clean a transcript produced by transcribe.py down to speaker + statement only.

Usage:
    python3 clean_transcript.py 73806481141783351066.transcript.json
    python3 clean_transcript.py in.json -o clean.json --swap --no-merge

Output (order preserved, one object per turn):
    [
      {"Employee": "हैलो।"},
      {"Customer": "कौन?"},
      {"Employee": "क्या मेरी बात आर्या से हो रही है?"}
    ]
"""

import argparse
import json
import os
import re
import sys

# How roles in the source transcript are renamed in the cleaned output.
ROLE_ALIASES = {
    "employee": "Employee",
    "agent": "Employee",
    "sales": "Employee",
    "customer": "Customer",
    "user": "Customer",
    "student": "Customer",
    "client": "Customer",
}


def load_turns(path):
    """Return the list of turn dicts from a transcribe.py output file."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (IOError, OSError) as exc:
        sys.exit("Could not read %s: %s" % (path, exc))
    except ValueError as exc:
        sys.exit("%s is not valid JSON: %s" % (path, exc))

    if isinstance(data, dict):
        turns = data.get("transcript")
    elif isinstance(data, list):
        turns = data
    else:
        turns = None

    if not isinstance(turns, list):
        sys.exit(
            "Expected a JSON object with a 'transcript' list (or a bare list) in %s"
            % path
        )
    return turns


def normalise_role(raw_role, swap):
    role = ROLE_ALIASES.get(str(raw_role).strip().lower(), str(raw_role).strip())
    if swap:
        if role == "Employee":
            return "Customer"
        if role == "Customer":
            return "Employee"
    return role


def clean_text(text):
    """Collapse whitespace and drop filler-only artefacts."""
    text = re.sub(r"\s+", " ", str(text)).strip()
    text = re.sub(r"\s+([।,.?!])", r"\1", text)
    return text


def build_clean_turns(turns, swap, merge, drop_fillers):
    """Turn the detailed transcript into an ordered list of {Role: statement}."""
    fillers = {"हाँ", "हा", "हूँ", "हूं", "जी", "ok", "okay", "hmm", "हम्म", "अ", "ा"}
    cleaned = []

    for turn in turns:
        if not isinstance(turn, dict):
            continue
        role = normalise_role(turn.get("speaker", "Unknown"), swap)
        text = clean_text(turn.get("text", turn.get("statement", "")))
        if not text:
            continue
        if drop_fillers and text.strip(" ।.?!,").lower() in fillers:
            continue

        # Join back-to-back turns from the same speaker into one statement.
        if merge and cleaned and cleaned[-1][0] == role:
            cleaned[-1][1] = ("%s %s" % (cleaned[-1][1], text)).strip()
        else:
            cleaned.append([role, text])

    return [{role: text} for role, text in cleaned]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Strip a transcript down to {speaker: statement} turns."
    )
    parser.add_argument("transcript", help="Path to the JSON produced by transcribe.py")
    parser.add_argument("-o", "--output",
                        help="Output path (default: <input basename>.clean.json)")
    parser.add_argument("--swap", action="store_true",
                        help="Swap the Employee/Customer labels "
                             "(use when the speaker roles were detected backwards)")
    parser.add_argument("--no-merge", dest="merge", action="store_false",
                        help="Keep every original turn instead of merging "
                             "consecutive turns by the same speaker")
    parser.add_argument("--drop-fillers", action="store_true",
                        help="Drop standalone filler turns (जी, हाँ, hmm, ok ...)")
    parser.add_argument("--print", dest="print_text", action="store_true",
                        help="Also print the cleaned turns to stdout")
    return parser.parse_args()


def main():
    args = parse_args()

    input_path = os.path.abspath(args.transcript)
    if not os.path.isfile(input_path):
        sys.exit("Transcript file not found: %s" % input_path)

    base = re.sub(r"\.transcript$", "", os.path.splitext(input_path)[0])
    output_path = args.output or (base + ".clean.json")

    turns = load_turns(input_path)
    cleaned = build_clean_turns(turns, args.swap, args.merge, args.drop_fillers)

    if not cleaned:
        sys.exit("No usable turns found in %s" % input_path)

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(cleaned, fh, ensure_ascii=False, indent=2)

    print("Cleaned transcript written to %s (%d turns, from %d)"
          % (output_path, len(cleaned), len(turns)), file=sys.stderr)

    if args.print_text:
        for turn in cleaned:
            for role, text in turn.items():
                print("%s: %s" % (role, text))


if __name__ == "__main__":
    main()
