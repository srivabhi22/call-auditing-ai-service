"""Build the day roll-ups from the call rows already in DynamoDB.

    python3 -m backend.tools.build_rollups --days 30
    python3 -m backend.tools.build_rollups --start 2026-09-01 --end 2026-09-22
    python3 -m backend.tools.build_rollups --date 2026-09-20 --dry-run
    python3 -m backend.tools.build_rollups --days 30 --check

The batch run rebuilds the days it touched on its way out (`backend.batch.run`,
step 6b), so this is for the two cases that are not a run: backfilling a table
that already has calls in it, and re-summing a day after rows were corrected,
re-audited or re-seeded by hand.

Safe to run repeatedly. A day is rebuilt from scratch rather than incremented,
so the second run over a day writes the same numbers as the first — and a scope
that no longer has calls on that day has its stale row deleted rather than left
to be added into every span that covers it.

`--dry-run` prints the rows it would write and touches nothing. `--check` reads
the roll-ups back and re-aggregates the same days from the call rows, printing
any disagreement — which is the one thing worth running after a backfill, since
a wrong roll-up is invisible in a dashboard that trusts it.
"""

import argparse
import sys
from datetime import date as date_type, timedelta

from ..batch import rollup
from ..batch.collect import AgentCache
from ..common.config import settings
from ..common.store import (
    SCOPE_AGENT, SCOPE_OVERALL, SCOPE_TEAM, rollups as rollups_repo,
)

# Every number the roll-up stores as a plain total, in the order it is printed.
# The maps (`metricSums`, `statusCounts`, ...) are compared too, by `--check`,
# but they are not worth a column.
COLUMNS = ("totalCalls", "auditedCalls", "scoredCalls", "totalFlags",
           "fatalCalls", "reviewCalls", "scoreSum", "scoreMax", "scoreMin")


def _dates(args):
    if args.date:
        return [args.date]
    if args.start or args.end:
        start = date_type.fromisoformat(args.start or args.end)
        end = date_type.fromisoformat(args.end or args.start)
        if end < start:
            raise SystemExit("--end is before --start")
        return [(start + timedelta(days=n)).isoformat()
                for n in range((end - start).days + 1)]
    # The default window ends *today*: a run rebuilds yesterday on its way out,
    # and a backfill asked for "the last 30 days" means the last 30 days.
    today = date_type.today()
    return [(today - timedelta(days=n)).isoformat()
            for n in reversed(range(args.days))]


def _print(items):
    print(f"{'PK':<34} {'SK':<12} " +
          " ".join(f"{c:>12}" for c in COLUMNS))
    for item in items:
        print(f"{item['PK']:<34} {item['SK']:<12} " + " ".join(
            f"{'' if item.get(c) is None else item[c]:>12}" for c in COLUMNS
        ))


def build(dates, dry_run=False):
    roster = AgentCache()
    if dry_run:
        for day in dates:
            rows = rollup.day_calls(day)
            items = rollup.build_day(day, rows, roster)
            print(f"\n{day}: {len(rows)} call(s) -> {len(items)} row(s)")
            _print(items)
        print("\ndry run — nothing was written")
        return 0

    counts = rollup.rebuild_days(dates, roster=roster)
    print(f"{len(dates)} day(s): {counts['rollupRowsWritten']} row(s) written, "
          f"{counts['rollupRowsDeleted']} stale row(s) deleted")
    if counts["rollupDaysFailed"]:
        print(f"{counts['rollupDaysFailed']} day(s) FAILED — see the log above")
        return 1
    return 0


def check(dates):
    """Re-aggregate the days and compare, row by row, against what is stored."""
    roster = AgentCache()
    mismatches = 0
    for day in dates:
        rows = rollup.day_calls(day)
        expected = {i["PK"]: i for i in rollup.build_day(day, rows, roster)}
        stored = {
            i["PK"]: i
            for scope in (SCOPE_OVERALL, SCOPE_TEAM, SCOPE_AGENT)
            for i in rollups_repo.day(scope, day)
        }

        for pk in sorted(set(expected) | set(stored)):
            want, have = expected.get(pk), stored.get(pk)
            if want is None:
                print(f"{day} {pk}: STALE — stored but no calls")
                mismatches += 1
                continue
            if have is None:
                print(f"{day} {pk}: MISSING — {want['totalCalls']} call(s) "
                      f"not rolled up")
                mismatches += 1
                continue
            # `updatedAt` is when it was written, not what it counted.
            differing = [
                field for field in set(want) | set(have)
                if field != "updatedAt" and want.get(field) != have.get(field)
            ]
            if differing:
                print(f"{day} {pk}: differs on {', '.join(sorted(differing))}")
                for field in sorted(differing):
                    print(f"    stored {have.get(field)!r} != "
                          f"recomputed {want.get(field)!r}")
                mismatches += 1

        total = expected.get(SCOPE_OVERALL, {}).get("totalCalls", 0)
        teams = sum(v["totalCalls"] for k, v in expected.items()
                    if k.startswith(f"{SCOPE_TEAM}#"))
        if total != teams:
            print(f"{day}: the teams sum to {teams} but the floor is {total}")
            mismatches += 1
        print(f"{day}: {len(rows)} call(s), {len(stored)} stored row(s)"
              + ("" if not mismatches else "  <-- see above"))

    if mismatches:
        print(f"\n{mismatches} disagreement(s). Re-run without --check to "
              f"rebuild these days.")
        return 1
    print("\nevery stored roll-up matches the call rows")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--date", help="one day, yyyy-mm-dd")
    group.add_argument("--start", help="first day, yyyy-mm-dd (inclusive)")
    parser.add_argument("--end", help="last day, yyyy-mm-dd (inclusive)")
    parser.add_argument("--days", type=int, default=30,
                        help="how many days back from today, when no range is "
                             "given (default: 30)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the rows, write nothing")
    parser.add_argument("--check", action="store_true",
                        help="compare what is stored against the call rows")
    args = parser.parse_args(argv)

    dates = _dates(args)
    print(f"table {settings.rollups_table}  from {dates[0]} to {dates[-1]} "
          f"({len(dates)} day(s))")
    return check(dates) if args.check else build(dates, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
