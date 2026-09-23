"""A day's totals, at three scopes, written to the `rollups` table.

The dashboard's question is "how did the floor / this team / this agent do over
these N days", and until now every answer to it walked GSI-3 for every day in
the window and aggregated a few thousand call rows in the API process. That is
the same arithmetic over the same rows every time anybody moves the date picker,
and it gets slower as the table gets larger -- a month of peak volume is ~90,000
rows read to produce about forty numbers.

This does the arithmetic once, when the day is finished, and stores the result:

    OVERALL                   2026-09-20
    TEAM#MP                   2026-09-20
    AGENT#MP#pankaj-kourav    2026-09-20

**Every stored number is a sum or an extreme, never an average.** Averages do
not add up -- the mean of three days is not the mean of the three daily means
unless every day had the same call count -- so the row carries `scoreSum` and
`scoredCalls` and the caller divides once, over the whole span it is showing.
Same for the per-metric numbers: `metricSums` and `metricCounts` per criterion,
divided at the end. `scoreMax` and `scoreMin` are extremes, which do combine
across days (max of maxes), so they are stored directly.

A day is **rebuilt from the call rows, not incremented as calls land.** The
increments would be cheaper and would be wrong by the end of the first week: a
re-audit, a retried run or a hand-corrected row all change a day that is already
counted, and a counter that has drifted looks exactly like one that has not.
Rebuilding is one GSI-3 query per day -- the read this exists to spare the *read
path*, paid once by the batch job -- and it is idempotent, so running it twice
on the same day lands the same numbers.

The three scopes are computed in one pass, so the teams always sum to the floor.
An agent the roster gives no team to is filed under `UNASSIGNED` rather than
dropped, which is what keeps that true.

    from backend.batch import rollup
    rollup.rebuild_days(["2026-09-20", "2026-09-21"])
"""

from datetime import datetime, timezone

from ..common.models import ProcessingStatus
from ..common.store import (
    SCOPE_AGENT, SCOPE_OVERALL, SCOPE_TEAM, UNASSIGNED_TEAM,
    calls as calls_repo, rollups as rollups_repo, rollup_pk, slug,
)
from ..common.trace import batch, warn
from .collect import AgentCache

# The criteria a scorecard awards, from the audit schema. Imported lazily and
# defensively for the same reason `models._score_sections` is: the schema lives
# at the repo root next to the pipeline, and a backend that cannot import it
# should still roll up call counts and flags rather than fail outright.
def _criteria():
    try:
        from audit_schema import CRITERION_MAX
    except Exception:  # noqa: BLE001 -- see above
        return ()
    return tuple(CRITERION_MAX)


# A call counts as needing review when the audit raised at least one flag and
# the call was not disqualified -- that is the queue a team leader works
# through, and a disqualified call is already its own tile. The severity
# breakdown is stored alongside it (`severityCalls`), so a UI that would rather
# define review as "HIGH only" can do that without this being rebuilt.
def _is_review(row):
    return bool(row.get("flagCount") or 0) and not row.get("disqualified")


class _Bucket:
    """The running totals for one scope on one day."""

    def __init__(self, criteria):
        self.totalCalls = 0
        self.auditedCalls = 0
        self.scoredCalls = 0
        self.totalFlags = 0
        self.flaggedCalls = 0
        self.fatalCalls = 0
        self.reviewCalls = 0
        self.scoreSum = 0.0
        self.scoreMax = None
        self.scoreMin = None
        self.totalDurationSec = 0
        self.statusCounts = {}
        self.severityCalls = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        self.metricSums = {name: 0.0 for name in criteria}
        self.metricCounts = {name: 0 for name in criteria}
        # Filled by the caller for the agent scope, where the key is a name and
        # anything wanting to get back to the call rows needs the extension.
        self.labels = {}

    def add(self, row):
        self.totalCalls += 1

        status = row.get("processingStatus") or "UNKNOWN"
        self.statusCounts[status] = self.statusCounts.get(status, 0) + 1
        if status == ProcessingStatus.PROCESSED:
            self.auditedCalls += 1

        duration = row.get("durationSec")
        if isinstance(duration, (int, float)):
            self.totalDurationSec += int(duration)

        flags = int(row.get("flagCount") or 0)
        self.totalFlags += flags
        if flags:
            self.flaggedCalls += 1
        if row.get("disqualified"):
            self.fatalCalls += 1
        if _is_review(row):
            self.reviewCalls += 1

        severity = str(row.get("auditStatus") or "").upper()
        if severity in self.severityCalls:
            self.severityCalls[severity] += 1

        score = row.get("score")
        if isinstance(score, (int, float)):
            # A disqualified call carries no score at all (see
            # `audit.dashboard_fields`), so it is counted as fatal above and
            # left out of every average here rather than entering it as a zero.
            self.scoredCalls += 1
            self.scoreSum += float(score)
            self.scoreMax = score if self.scoreMax is None else max(self.scoreMax, score)
            self.scoreMin = score if self.scoreMin is None else min(self.scoreMin, score)

        marks = row.get("criterionMarks") or {}
        for name, mark in marks.items():
            if not isinstance(mark, (int, float)):
                continue
            # `setdefault`, not indexing: a criterion added to the schema after
            # this process started should land in the roll-up, not raise.
            self.metricSums[name] = self.metricSums.get(name, 0.0) + float(mark)
            self.metricCounts[name] = self.metricCounts.get(name, 0) + 1

    def item(self, pk, date, scope, team="", written_at=""):
        """The row, as it is stored. Empty maps are dropped, so a day with no
        audited calls carries counts and nothing else.
        """
        item = {
            "PK": pk,
            "SK": date,
            "scope": scope,
            "entity": "ROLLUP",
            "dateKey": date,
            "totalCalls": self.totalCalls,
            "auditedCalls": self.auditedCalls,
            "scoredCalls": self.scoredCalls,
            "totalFlags": self.totalFlags,
            "flaggedCalls": self.flaggedCalls,
            "fatalCalls": self.fatalCalls,
            "reviewCalls": self.reviewCalls,
            "scoreSum": round(self.scoreSum, 4),
            "scoreMax": self.scoreMax,
            "scoreMin": self.scoreMin,
            "totalDurationSec": self.totalDurationSec,
            "statusCounts": self.statusCounts or None,
            "severityCalls": {k: v for k, v in self.severityCalls.items() if v}
                             or None,
            # Per metric, so the six sections and the eight criteria are both
            # derivable: a section is exactly the sum of its criteria, and a sum
            # of sums is a sum. Counts are separate because a criterion can be
            # absent from an individual audit, and dividing by the call count
            # would then quietly understate it.
            "metricSums": {k: round(v, 4) for k, v in self.metricSums.items() if v}
                          or None,
            "metricCounts": {k: v for k, v in self.metricCounts.items() if v}
                            or None,
            "updatedAt": written_at,
        }
        if team:
            item["teamId"] = team
        item.update(self.labels)
        return item


def _agent_keys(roster):
    """extension -> (teamId, agent key), for every agent the roster lists.

    The key is the agent's name, slugged -- `AGENT#MP#pankaj-kourav` -- because
    these rows are read by a dashboard and nothing joins on them. Two agents on
    one team sharing a name would otherwise share a roll-up and silently sum
    together, so a shared name is disambiguated with the extension. That is
    decided from the **whole roster**, not from the day being built: deciding it
    per day would give one Pankaj the plain key on a day the other took no
    calls, and a different key the day they did.
    """
    agents = roster.all_agents()
    by_name = {}
    for agent in agents:
        by_name.setdefault(
            (agent.state or UNASSIGNED_TEAM, slug(agent.name)), []
        ).append(agent.extension)

    keys = {}
    for (team, name_slug), extensions in by_name.items():
        shared = len(extensions) > 1
        for extension in extensions:
            keys[extension] = (
                team, f"{name_slug}-{extension}" if shared else name_slug
            )
        if shared:
            warn("batch", f"roster: {len(extensions)} agents on {team} share "
                          f"the name {name_slug!r} — their roll-up keys carry "
                          f"the extension ({', '.join(sorted(extensions))})")
    return keys


def day_window(date):
    """The (start, end) a day is read with, as `calls.query` compares them.

    GSI-3's sort key is `<startedAt>#<callId>` and the bounds are compared to it
    as *strings*, so the bounds have to be strings that bracket every timestamp
    the day can carry -- and `startedAt` carries its offset (`...T09:14:03+05:30`
    on a seeded row, `...Z` on some others).

    The lower bound is the bare `2026-09-20T`, which sorts below any time on
    that day. `2026-09-20T00:00:00Z` does not: at the offset, `Z` sorts *above*
    the `+05:30` of a call at midnight local time, and that call -- and every
    one in the first five and a half hours -- is silently missing from the day.

    The upper bound can be a real time, because every character that can follow
    the seconds (`+`, `-`, `.`, `Z`) sorts at or below `Z`, and the repository
    appends its own high sentinel for the call id.
    """
    return f"{date}T", f"{date}T23:59:59Z"


def day_calls(date, repository=None):
    """Every call row that started on `date`. One GSI-3 day partition."""
    start, end = day_window(date)
    return (repository or calls_repo).query(start=start, end=end)


def build_day(date, rows, roster):
    """The rows for one day, one per scope. Pure: reads nothing, writes nothing.

    `rows` is every call row whose `startedAt` falls on `date`, hydrated -- what
    `calls.query(start=date, end=date)` returns.
    """
    criteria = _criteria()
    written_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    keys = _agent_keys(roster)

    overall = _Bucket(criteria)
    teams, agents = {}, {}

    for row in rows:
        extension = str(row.get("agentExtension") or "").strip()
        team, agent_key = keys.get(extension, (None, None))
        if agent_key is None:
            # An extension the roster does not list. It still has calls and
            # still has a name on the row, so it is filed under UNASSIGNED
            # rather than dropped -- dropping it is what would stop the teams
            # summing to the floor.
            team = UNASSIGNED_TEAM
            agent_key = slug(row.get("agentName") or "") or f"ext-{extension or 'unknown'}"

        overall.add(row)

        bucket = teams.get(team)
        if bucket is None:
            bucket = teams[team] = _Bucket(criteria)
        bucket.add(row)

        bucket = agents.get((team, agent_key))
        if bucket is None:
            bucket = agents[(team, agent_key)] = _Bucket(criteria)
            bucket.labels = {
                "agentName": row.get("agentName") or "",
                "agentExtension": extension,
            }
        bucket.add(row)

    items = [overall.item(rollup_pk(SCOPE_OVERALL), date, SCOPE_OVERALL,
                          written_at=written_at)]
    items += [
        bucket.item(rollup_pk(SCOPE_TEAM, team), date, SCOPE_TEAM, team,
                    written_at)
        for team, bucket in sorted(teams.items())
    ]
    items += [
        bucket.item(rollup_pk(SCOPE_AGENT, team, agent_key), date, SCOPE_AGENT,
                    team, written_at)
        for (team, agent_key), bucket in sorted(agents.items())
    ]
    return items


def rebuild_day(date, repository=None, rollups=None, roster=None):
    """Read the day's calls, write its roll-up rows, drop the stale ones.

    Returns `(written, deleted)`. The delete is what makes a rebuild honest
    after a wipe-and-reseed or an agent leaving: a row nobody rewrites is a
    number the dashboard would keep adding into every span that covers the day.
    """
    repository = repository or calls_repo
    rollups = rollups or rollups_repo
    roster = roster or AgentCache()

    rows = day_calls(date, repository)
    items = build_day(date, rows, roster)
    written = rollups.put_many(items)

    fresh = {(i["PK"], i["SK"]) for i in items}
    stale = [
        (existing["PK"], existing["SK"])
        for scope in (SCOPE_OVERALL, SCOPE_TEAM, SCOPE_AGENT)
        for existing in rollups.day(scope, date)
        if (existing["PK"], existing["SK"]) not in fresh
    ]
    deleted = rollups.delete_many(stale)

    batch(f"rollup {date}: {len(rows)} call(s) -> {written} row(s)"
          + (f", {deleted} stale row(s) removed" if deleted else ""))
    return written, deleted


def rebuild_days(dates, repository=None, rollups=None, roster=None):
    """Several days. One roster load and one repository for all of them.

    A failure on one day is reported and the rest are still built: the roll-ups
    are a read-side convenience, and a day that could not be summed must not
    take the run's exit code with it.
    """
    roster = roster or AgentCache()
    written = deleted = failed = 0
    for date in sorted(set(d for d in dates if d)):
        try:
            a, b = rebuild_day(date, repository, rollups, roster)
            written += a
            deleted += b
        except Exception as exc:  # noqa: BLE001 -- see the docstring
            failed += 1
            warn("batch", f"rollup {date} failed: {exc!r}")
    return {"rollupRowsWritten": written, "rollupRowsDeleted": deleted,
            "rollupDaysFailed": failed}
