"""Fantasy week (matchup period) math, in the league time zone.

By default week N is the 7 days from start_date + 7(N-1). A league can instead
store custom periods (league.periods), e.g. copied from an ESPN schedule, where
weeks can be shorter or longer."""
import json
from datetime import date, datetime, timedelta
from math import log2

from . import config
from .models import League


def now_local() -> datetime:
    from . import db
    return db.now_aware().astimezone(config.LEAGUE_TZ)


def today_local() -> date:
    return now_local().date()


def periods(league: League) -> list[tuple[date, date]] | None:
    raw = getattr(league, "periods", "") or ""
    if not raw:
        return None
    return [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in json.loads(raw)]


def week_start(league: League, week: int) -> date:
    ps = periods(league)
    if ps:
        if week <= len(ps):
            return ps[max(week, 1) - 1][0]
        return ps[-1][1] + timedelta(days=1 + 7 * (week - len(ps) - 1))
    return league.start_date + timedelta(days=7 * (week - 1))


def week_end(league: League, week: int) -> date:
    ps = periods(league)
    if ps and 1 <= week <= len(ps):
        return ps[week - 1][1]
    return week_start(league, week) + timedelta(days=6)


def week_for_date(league: League, d: date) -> int:
    """0 before the season, can exceed total_weeks after it."""
    ps = periods(league)
    if ps:
        if d < ps[0][0]:
            return 0
        for i, (a, b) in enumerate(ps, start=1):
            if d <= b:
                return i  # also covers a gap before a period (counts as that period)
        return len(ps) + 1
    if d < league.start_date:
        return 0
    return (d - league.start_date).days // 7 + 1


def playoff_weeks(league: League) -> int:
    ps = periods(league)
    if ps:
        return len(ps) - league.regular_season_weeks
    return int(log2(league.playoff_teams)) if league.playoff_teams > 1 else 0


def total_weeks(league: League) -> int:
    return league.regular_season_weeks + playoff_weeks(league)


def current_week(league: League, today: date | None = None) -> int:
    w = week_for_date(league, today or today_local())
    return max(1, min(w, total_weeks(league)))


def is_locked(league: League, week: int, today: date | None = None) -> bool:
    """A week's lineup locks at 00:00 on its Monday."""
    return (today or today_local()) >= week_start(league, week)


def first_open_week(league: League, today: date | None = None) -> int:
    """The earliest week whose lineup can still be changed."""
    return max(1, week_for_date(league, today or today_local()) + 1)


def week_label(league: League, week: int) -> str:
    s, e = week_start(league, week), week_end(league, week)
    tag = f"Matchup {week}" if periods(league) else f"Week {week}"
    if week > league.regular_season_weeks:
        tag = f"Playoffs R{week - league.regular_season_weeks}"
    return f"{tag} · {s:%b %-d}-{e:%b %-d}"
