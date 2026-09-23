"""Talking to Cloud Connect. The only module that knows their API's shape.

Two calls matter, and they are steps 3 and 5:

    fetch_call_logs_between(a, b)  every call in a time range. The batch job's
                                   only way in: there is no lookup-by-id
                                   endpoint and no pagination, so the range
                                   goes in the URL path and the window is cut
                                   into hours by the caller.

    download_recording(url, sink)  streams call_rec_path wherever it is told to.

`fetch_call_log(unique_token)` is the single-call lookup the webhook needed. It
is kept because it is the only way to ask about one specific call when
something has to be investigated by hand, and because NoRowsYet's two meanings
are only explicable with both callers in view.

Failures are sorted into RetryableError, PermanentError and NoRowsYet so callers
never have to interpret status codes. Anything that might succeed on a second attempt — a
call log row not yet written, a 5xx, a timeout — is retryable.

urllib rather than requests, to keep the dependency list where the rest of the
repo has it. Swapping in httpx is confined to _post_json and download_recording.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from ..common.config import settings
from ..common.models import CallLogRecord, CC_URL_TIME

log = logging.getLogger(__name__)


class CloudConnectError(Exception):
    """Base for anything that went wrong talking to Cloud Connect."""


class RetryableError(CloudConnectError):
    """Worth another attempt: a lagging row, a 5xx, a timeout."""


class PermanentError(CloudConnectError):
    """Will fail the same way next time: bad credentials, a 4xx, bad config."""


class NoRowsYet(RetryableError):
    """Their "Call Log Details Not Found", which means two different things.

    Looking up one token, it is a lag: the call log row is written by a
    different system than the one that ends the call, and it appears anywhere
    from thirty seconds to several minutes later. Coming back is right.

    Sweeping a time range, it is the answer: there were no calls in that hour.
    Measured against the live API, every quiet overnight hour returns exactly
    this. Treated as retryable there, each of those hours costs three attempts
    and thirty seconds of backoff and is then recorded as a *failed slice* --
    which is an hour of the day nobody fetched, because a hole in the window is
    the one thing that must stop it moving. A job that never advances past its
    first quiet night is the failure this subclass exists to prevent.

    Same wire response, two meanings, so the caller decides which it is.
    """


# What Cloud Connect say when a row is not there yet. Matched on the message
# because the status code does not distinguish it from a genuine 4xx: they answer
# HTTP 411 either way.
_NOT_WRITTEN_YET = (
    "call log details not found",
    "no record found",
    "data not found",
)


def _is_not_written_yet(detail):
    lowered = (detail or "").lower()
    return any(phrase in lowered for phrase in _NOT_WRITTEN_YET)


class CloudConnectClient:
    def __init__(self, base_url=None, token_id=None, user_type=None, timeout=None):
        self.base_url = (base_url or settings.cc_base_url).rstrip("/")
        self.token_id = token_id if token_id is not None else settings.cc_token_id
        self.user_type = user_type or settings.cc_user_type
        self.timeout = timeout or settings.cc_timeout_sec

    def _post_json(self, url, payload):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            message = f"HTTP {exc.code} from {url}: {detail}"
            if exc.code >= 500 or exc.code == 429:
                raise RetryableError(message) from exc
            if _is_not_written_yet(detail):
                # They answer "Call Log Details Not Found" with HTTP 411, which
                # is a 4xx -- so the status code alone would call this
                # permanent. It is not: for a single-token lookup it is a row
                # that has not been written yet, and for a range sweep it is an
                # hour with no calls in it. NoRowsYet carries both meanings and
                # lets the caller pick; see its docstring.
                raise NoRowsYet(message) from exc
            raise PermanentError(message) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RetryableError(f"could not reach {url}: {exc}") from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RetryableError(f"non-JSON from {url}: {body[:200]!r}") from exc

    def _call_log_url(self, start, end):
        """POST {base}/api/info/{start}/{end}/{userType}/callLog

        The dates are path segments, to the minute, and are mandatory even when a
        unique_token filter makes them redundant.
        """
        return (
            f"{self.base_url}/api/info"
            f"/{start.strftime(CC_URL_TIME)}"
            f"/{end.strftime(CC_URL_TIME)}"
            f"/{self.user_type}/callLog"
        )

    def _post_call_log(self, start, end, body):
        if not self.token_id:
            raise PermanentError("CC_TOKEN_ID is not set")
        url = self._call_log_url(start, end)
        result = self._post_json(url, {"token_id": self.token_id, **body})
        if str(result.get("status", "")).upper() != "SUCCESS":
            # Some deployments answer 200 with an error body rather than 411,
            # so the same two meanings arrive by this path too.
            if _is_not_written_yet(str(result.get("message", ""))):
                raise NoRowsYet(f"callLog: {result.get('message')!r}")
            raise RetryableError(
                f"callLog status={result.get('status')!r} "
                f"message={result.get('message')!r}"
            )
        return result

    def fetch_call_log(self, unique_token, around=None, window_minutes=None):
        """The token-to-callid step the whole ingestion path hangs on.

        An empty result is raised as retryable rather than returned as None: the
        row can lag behind the hangup event, and the right answer to that is to
        come back in a few seconds.
        """
        anchor = around or datetime.now()
        pad = timedelta(minutes=window_minutes or settings.cc_window_minutes)
        log.info("callLog fetch token=%s window=±%s", unique_token, pad)
        result = self._post_call_log(anchor - pad, anchor + pad,
                                     {"unique_token": unique_token})
        rows = result.get("data") or []
        if not rows:
            raise RetryableError(f"no call log row for {unique_token} yet")
        if len(rows) > 1:
            log.warning("%d rows for token=%s, taking the first", len(rows), unique_token)
        return CallLogRecord(rows[0])

    def fetch_call_logs_between(self, start, end):
        """Every call in a window, unfiltered — the reconciliation query.

        Webhooks get lost, so a sweep over a recent window is what catches the
        calls that never reached us. The same call with a wider range is how a
        historical backfill is done.
        """
        log.info("callLog sweep %s -> %s", start, end)
        try:
            result = self._post_call_log(start, end, {})
        except NoRowsYet:
            # An hour with no calls in it. See NoRowsYet -- for a sweep this is
            # a complete answer, not a lag, and retrying it would turn every
            # quiet night into a failed slice and a false alarm.
            log.info("callLog sweep %s -> %s: no calls in this range", start, end)
            return []
        return [CallLogRecord(row) for row in (result.get("data") or [])]

    def download_recording(self, url, sink, chunk_size=64 * 1024):
        """Stream call_rec_path into a binary file-like.

        UNVERIFIED: the API doc gives a bare https path and says nothing about
        how it is authenticated. It may want token_id, a session cookie, or an
        allowlisted source IP. If it is IP-allowlisted then whatever runs this
        needs a static egress IP — a NAT gateway — which is worth settling before
        the infrastructure is built.
        """
        if not url:
            raise PermanentError("no recording URL")
        request = urllib.request.Request(
            url, headers={"User-Agent": "call-auditing/0.1"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                written = 0
                while chunk := response.read(chunk_size):
                    sink.write(chunk)
                    written += len(chunk)
        except urllib.error.HTTPError as exc:
            # A 404 usually means the PBX has not finished writing the file.
            message = f"HTTP {exc.code} fetching recording {url}"
            if exc.code in (404, 429) or exc.code >= 500:
                raise RetryableError(message) from exc
            raise PermanentError(message) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RetryableError(f"could not fetch recording {url}: {exc}") from exc

        if written == 0:
            raise RetryableError(f"recording {url} was empty")
        return written


client = CloudConnectClient()
