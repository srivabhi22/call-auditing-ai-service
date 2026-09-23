#!/usr/bin/env python3
"""
Transcribe a sales call with speaker diarization using the Soniox async STT API.

Any format Soniox accepts will do — aac, aiff, amr, asf, flac, m4a, mp3, mp4, ogg,
wav, webm — it detects the format itself.

Usage:
    python3 transcribe.py 73806481141783351066.wav
    python3 transcribe.py call.m4a -o out.json --languages hi en --employee-speaker 1

Output: JSON transcript with one entry per utterance:
    {"speaker": "Employee", "start": "00:00:01.240", "end": "...", "text": "..."}

The API key is read from the SONIOX_API_KEY environment variable, or from a .env
file next to this script.
"""

import argparse
import contextlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import warnings
import wave

import cost

API_BASE = "https://api.soniox.com/v1"
DEFAULT_MODEL = "stt-async-v5"

# What the async API detects on its own. Anything else is refused before upload
# rather than after, so the caller is told which formats are on the list.
SUPPORTED_FORMATS = ("aac", "aiff", "amr", "asf", "flac", "m4a", "mp3", "mp4",
                     "ogg", "wav", "webm")

# A sales call comes off the dialer as 48 kHz PCM — ~5.7 MB a minute — and speech
# models work at 16 kHz, so everything above that is upload time bought for nothing.
UPLOAD_SAMPLE_RATE = 16000

# Words/phrases a sales rep of Arivihan is far more likely to say than a student.
EMPLOYEE_MARKERS = [
    "arivihan", "अरिविहान", "platform", "प्लेटफॉर्म", "course", "कोर्स",
    "batch", "बैच", "admission", "एडमिशन", "fees", "फीस", "fee",
    "payment", "पेमेंट", "discount", "डिस्काउंट", "offer", "ऑफर",
    "subscription", "सब्सक्रिप्शन", "validity", "वैलिडिटी",
    "app", "ऐप", "demo", "डेमो", "faculty", "टीचर", "teacher",
    "link", "लिंक", "register", "रजिस्टर", "enroll", "join kar",
    "hamare", "हमारे", "hamari", "हमारी", "company", "कंपनी",
    "sir ji", "namaste", "नमस्ते", "hello sir", "baat kar raha",
    "bata raha", "samjha raha", "aapko", "आपको",
]

# Words a student/parent is more likely to say.
CUSTOMER_MARKERS = [
    "mummy", "मम्मी", "papa", "पापा", "ghar", "घर", "school", "स्कूल",
    "college", "कॉलेज", "sochunga", "सोचूंगा", "sochta", "baad me",
    "बाद में", "abhi nahi", "अभी नहीं", "paise nahi", "पैसे नहीं",
    "mere paas", "मेरे पास", "main padh", "मैं पढ़",
]


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #
def load_api_key(env_path):
    """Return the Soniox API key from the environment or a .env file."""
    key = os.environ.get("SONIOX_API_KEY")
    if key:
        return key.strip()

    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip() == "SONIOX_API_KEY":
                    return value.strip().strip("'\"")

    sys.exit(
        "SONIOX_API_KEY not found. Set it in the environment or in %s" % env_path
    )


# --------------------------------------------------------------------------- #
# Minimal HTTP layer (stdlib only, no third-party deps)
# --------------------------------------------------------------------------- #
def _request(method, url, api_key, data=None, headers=None):
    hdrs = {"Authorization": "Bearer %s" % api_key}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        sys.exit("Soniox API error %s on %s %s:\n%s" % (exc.code, method, url, detail))
    except urllib.error.URLError as exc:
        sys.exit("Could not reach the Soniox API (%s)" % exc.reason)
    return json.loads(body) if body.strip() else {}


def api_get(path, api_key):
    return _request("GET", API_BASE + path, api_key)


def api_post_json(path, api_key, payload):
    return _request(
        "POST",
        API_BASE + path,
        api_key,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def api_delete(path, api_key):
    return _request("DELETE", API_BASE + path, api_key)


def upload_file(path, api_key):
    """Upload the audio file as multipart/form-data and return its file id."""
    boundary = "----soniox%s" % uuid.uuid4().hex
    filename = os.path.basename(path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    with open(path, "rb") as fh:
        file_bytes = fh.read()

    body = b"".join([
        ("--%s\r\n" % boundary).encode(),
        ('Content-Disposition: form-data; name="file"; filename="%s"\r\n'
         % filename).encode(),
        ("Content-Type: %s\r\n\r\n" % content_type).encode(),
        file_bytes,
        ("\r\n--%s--\r\n" % boundary).encode(),
    ])

    return _request(
        "POST",
        API_BASE + "/files",
        api_key,
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
    )


def wait_for_transcription(transcription_id, api_key, poll_seconds, timeout_seconds):
    """Poll the job until it is completed, or give up at the timeout."""
    started = time.time()
    while True:
        info = api_get("/transcriptions/%s" % transcription_id, api_key)
        status = info.get("status")
        if status == "completed":
            return info
        if status == "error":
            sys.exit("Transcription failed: %s" % info.get("error_message", info))
        if time.time() - started > timeout_seconds:
            sys.exit("Timed out after %ss waiting for transcription %s"
                     % (timeout_seconds, transcription_id))
        print("  status: %s ..." % status, file=sys.stderr)
        time.sleep(poll_seconds)


# --------------------------------------------------------------------------- #
# Getting the audio small enough to send quickly
# --------------------------------------------------------------------------- #
def is_supported(path):
    """True when Soniox will recognise this file by its extension."""
    return os.path.splitext(path)[1].lstrip(".").lower() in SUPPORTED_FORMATS


def find_ffmpeg():
    """ffmpeg from PATH, or the build imageio-ffmpeg puts inside the venv."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:                                  # noqa: BLE001 - optional
        return None


def _flac_via_ffmpeg(source, target):
    """16 kHz mono FLAC — a fraction of the original, and lossless to the model.

    ffmpeg reads every format Soniox accepts, so this is the path for all of them;
    the wav-only fallback below exists for a machine with no ffmpeg at all.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False
    result = subprocess.run(
        [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", source,
         "-ac", "1", "-ar", str(UPLOAD_SAMPLE_RATE), "-c:a", "flac", target],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and os.path.isfile(target) and os.path.getsize(target) > 0


def _downsampled_wav(source, target):
    """Fallback when ffmpeg is missing: mix to mono and drop to 16 kHz with audioop."""
    try:
        with warnings.catch_warnings():
            # Deprecated since 3.11 and gone in 3.13, but there is no stdlib
            # replacement and this path only runs when ffmpeg is missing.
            warnings.simplefilter("ignore", DeprecationWarning)
            import audioop
        with contextlib.closing(wave.open(source, "rb")) as src:
            channels, width, rate = src.getnchannels(), src.getsampwidth(), src.getframerate()
            if width not in (1, 2, 4) or rate <= UPLOAD_SAMPLE_RATE:
                return False
            frames = src.readframes(src.getnframes())
        if channels > 1:
            frames = audioop.tomono(frames, width, 0.5, 0.5) if channels == 2 else frames
            if channels > 2:
                return False
        frames, _ = audioop.ratecv(frames, width, 1, rate, UPLOAD_SAMPLE_RATE, None)
        with contextlib.closing(wave.open(target, "wb")) as out:
            out.setnchannels(1)
            out.setsampwidth(width)
            out.setframerate(UPLOAD_SAMPLE_RATE)
            out.writeframes(frames)
        return True
    except Exception:                                  # noqa: BLE001 - leave the file alone
        return False


@contextlib.contextmanager
def upload_ready(path, enabled=True):
    """Yield the path to send, cleaning up any temporary copy afterwards.

    Nothing here changes what Soniox hears: the model runs at 16 kHz whatever it is
    given, so re-encoding first only moves the resampling off the wire. Anything
    that cannot be re-encoded is sent exactly as it arrived.
    """
    if not enabled:
        yield path
        return

    original = os.path.getsize(path)
    workdir = tempfile.mkdtemp(prefix="soniox-upload-")
    try:
        flac = os.path.join(workdir, "audio.flac")
        if _flac_via_ffmpeg(path, flac) and os.path.getsize(flac) < original:
            yield flac
            return
        small = os.path.join(workdir, "audio.wav")
        if _downsampled_wav(path, small) and os.path.getsize(small) < original:
            yield small
            return
        yield path
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Transcript shaping
# --------------------------------------------------------------------------- #
def format_timestamp(ms):
    if ms is None:
        return None
    ms = int(ms)
    hours, rem = divmod(ms, 3600000)
    minutes, rem = divmod(rem, 60000)
    seconds, millis = divmod(rem, 1000)
    return "%02d:%02d:%02d.%03d" % (hours, minutes, seconds, millis)


def group_tokens(tokens, max_gap_ms):
    """Merge consecutive tokens into utterances, splitting on speaker change
    or a silence longer than max_gap_ms."""
    utterances = []
    current = None

    for token in tokens:
        text = token.get("text", "")
        if not text.strip():
            # Keep whitespace inside an utterance, but never start one with it.
            if current:
                current["text"] += text
            continue

        speaker = str(token.get("speaker", "unknown"))
        start_ms = token.get("start_ms")
        end_ms = token.get("end_ms")

        gap_too_big = (
            current is not None
            and start_ms is not None
            and current["end_ms"] is not None
            and start_ms - current["end_ms"] > max_gap_ms
        )

        if current is None or current["speaker"] != speaker or gap_too_big:
            current = {
                "speaker": speaker,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": text,
            }
            utterances.append(current)
        else:
            current["text"] += text
            if end_ms is not None:
                current["end_ms"] = end_ms

    for utt in utterances:
        utt["text"] = re.sub(r"\s+", " ", utt["text"]).strip()

    return [u for u in utterances if u["text"]]


def score_speakers(utterances):
    """Score each speaker id on how much they sound like the sales employee."""
    scores = {}
    for utt in utterances:
        low = utt["text"].lower()
        stats = scores.setdefault(
            utt["speaker"],
            {"marker_score": 0, "chars": 0, "utterances": 0, "first_ms": utt["start_ms"]},
        )
        stats["chars"] += len(utt["text"])
        stats["utterances"] += 1
        for marker in EMPLOYEE_MARKERS:
            if marker in low:
                stats["marker_score"] += 1
        for marker in CUSTOMER_MARKERS:
            if marker in low:
                stats["marker_score"] -= 1
    return scores


def identify_employee(utterances, forced_speaker=None):
    """Decide which diarized speaker id is the Arivihan employee.

    Heuristic order: explicit override -> sales-vocabulary markers ->
    who talks the most (the rep does most of the talking on a pitch call).
    """
    scores = score_speakers(utterances)
    if not scores:
        return None, scores

    if forced_speaker is not None:
        return str(forced_speaker), scores

    ranked = sorted(
        scores.items(),
        key=lambda kv: (kv[1]["marker_score"], kv[1]["chars"]),
        reverse=True,
    )
    top, second = ranked[0], (ranked[1] if len(ranked) > 1 else None)

    if second is None or top[1]["marker_score"] != second[1]["marker_score"]:
        return top[0], scores

    # Marker scores tied: fall back to who spoke the most.
    talkiest = max(scores.items(), key=lambda kv: kv[1]["chars"])
    return talkiest[0], scores


def build_transcript(utterances, employee_speaker):
    transcript = []
    for utt in utterances:
        role = "Employee" if utt["speaker"] == employee_speaker else "Customer"
        transcript.append({
            "speaker": role,
            "speaker_id": utt["speaker"],
            "start": format_timestamp(utt["start_ms"]),
            "end": format_timestamp(utt["end_ms"]),
            "start_ms": utt["start_ms"],
            "end_ms": utt["end_ms"],
            "text": utt["text"],
        })
    return transcript


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args():
    parser = argparse.ArgumentParser(
        description="Transcribe a sales call with Employee/Customer speaker labels."
    )
    parser.add_argument("audio",
                        help="Path to the recording (%s)" % ", ".join(SUPPORTED_FORMATS))
    parser.add_argument("-o", "--output",
                        help="Output JSON path (default: <audio>.transcript.json)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Soniox model (default: %s)" % DEFAULT_MODEL)
    parser.add_argument("--languages", nargs="*", default=["hi", "en"],
                        help="Language hints (default: hi en)")
    parser.add_argument("--employee-speaker",
                        help="Force which diarized speaker id is the employee "
                             "(e.g. 1), instead of auto-detecting")
    parser.add_argument("--max-gap-ms", type=int, default=2000,
                        help="Silence gap that starts a new utterance "
                             "for the same speaker (default: 2000)")
    parser.add_argument("--poll-seconds", type=float, default=1.0,
                        help="Polling interval while waiting (default: 1)")
    parser.add_argument("--no-compress", dest="compress", action="store_false",
                        help="Upload the recording untouched instead of re-encoding "
                             "it to 16 kHz mono first")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="Max seconds to wait for the transcription (default: 1800)")
    parser.add_argument("--keep-remote", action="store_true",
                        help="Keep the uploaded file and transcription on Soniox "
                             "(they are deleted by default)")
    parser.add_argument("--save-raw", action="store_true",
                        help="Also write the raw Soniox token response next to the output")
    parser.add_argument("--print-text", action="store_true",
                        help="Print a readable Speaker: text version to stdout")
    return parser.parse_args()


def main():
    args = parse_args()

    audio_path = os.path.abspath(args.audio)
    if not os.path.isfile(audio_path):
        sys.exit("Audio file not found: %s" % audio_path)
    if not is_supported(audio_path):
        sys.exit("Soniox does not accept %s files. Supported formats: %s"
                 % (os.path.splitext(audio_path)[1] or "extensionless",
                    ", ".join(SUPPORTED_FORMATS)))

    script_dir = os.path.dirname(os.path.abspath(__file__))
    api_key = load_api_key(os.path.join(script_dir, ".env"))

    output_path = args.output or (os.path.splitext(audio_path)[0] + ".transcript.json")

    with upload_ready(audio_path, args.compress) as send_path:
        print("Uploading %s (%.1f MB) ..."
              % (os.path.basename(audio_path), os.path.getsize(send_path) / 1e6),
              file=sys.stderr)
        uploaded = upload_file(send_path, api_key)

    file_id = uploaded.get("id")
    if not file_id:
        sys.exit("Upload did not return a file id: %s" % uploaded)

    payload = {
        "file_id": file_id,
        "model": args.model,
        "enable_speaker_diarization": True,
        "enable_language_identification": True,
    }
    if args.languages:
        payload["language_hints"] = args.languages

    print("Starting transcription (model=%s) ..." % args.model, file=sys.stderr)
    created = api_post_json("/transcriptions", api_key, payload)
    transcription_id = created.get("id")
    if not transcription_id:
        sys.exit("Transcription request did not return an id: %s" % created)

    try:
        info = wait_for_transcription(
            transcription_id, api_key, args.poll_seconds, args.timeout
        )
        result = api_get("/transcriptions/%s/transcript" % transcription_id, api_key)
    finally:
        if not args.keep_remote:
            try:
                api_delete("/files/%s" % file_id, api_key)
            except SystemExit:
                pass

    tokens = result.get("tokens") or []
    if not tokens:
        sys.exit("Soniox returned no tokens. Raw response: %s" % result)

    utterances = group_tokens(tokens, args.max_gap_ms)
    employee_speaker, scores = identify_employee(utterances, args.employee_speaker)
    transcript = build_transcript(utterances, employee_speaker)

    speaker_mapping = {}
    for speaker_id in sorted(scores):
        speaker_mapping[speaker_id] = (
            "Employee" if speaker_id == employee_speaker else "Customer"
        )

    document = {
        "audio_file": os.path.basename(audio_path),
        "model": args.model,
        "language_hints": args.languages,
        "audio_duration_ms": info.get("audio_duration_ms"),
        "speaker_mapping": speaker_mapping,
        "speaker_role_detection": (
            "manual" if args.employee_speaker else "auto (sales-vocabulary heuristic)"
        ),
        "speaker_stats": scores,
        # Soniox bills by the hour of audio, so the charge is fully determined by
        # the duration above — recorded at write time so the pipeline's first stage
        # is costed like the rest, not reconstructed later from a rate card that
        # may have moved.
        "cost": cost.soniox_block(info.get("audio_duration_ms"), args.model),
        "transcript": transcript,
    }

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(document, fh, ensure_ascii=False, indent=2)
    print("Transcript written to %s (%d utterances, %.1f min, $%.4f)"
          % (output_path, len(transcript),
             (info.get("audio_duration_ms") or 0) / 60000.0,
             document["cost"]["cost_usd"]), file=sys.stderr)

    if args.save_raw:
        raw_path = os.path.splitext(output_path)[0] + ".raw.json"
        with open(raw_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print("Raw Soniox response written to %s" % raw_path, file=sys.stderr)

    if not args.keep_remote:
        api_delete("/transcriptions/%s" % transcription_id, api_key)


    if args.print_text:
        for entry in transcript:
            print("[%s] %s: %s" % (entry["start"], entry["speaker"], entry["text"]))


if __name__ == "__main__":
    main()
