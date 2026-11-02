"""Regular season schedule, scoring weeks, standings and the playoff bracket."""
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import League, Matchup, Team
from ..scoring import team_week
from ..weeks import now_local, total_weeks, week_end
from .common import LeagueError, log
from .lineups import ensure_lineup

FINALIZE_HOUR = 10  # finalize a week at 10:00 the Monday after it ends (late games done)


def round_robin(team_ids: list[int]) -> list[list[tuple[int, int]]]:
    ids = list(team_ids)
    if len(ids) % 2:
        raise LeagueError("Need an even number of teams.")
    n = len(ids)
    rounds = []
    for r in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = ids[i], ids[n - 1 - i]
            pairs.append((a, b) if r % 2 == 0 else (b, a))
        rounds.append(pairs)
        ids = [ids[0]] + [ids[-1]] + ids[1:-1]
    return rounds


def create_regular_season(db: Session, league: League) -> None:
    db.execute(delete(Matchup))
    team_ids = [t.id for t in db.scalars(select(Team).order_by(Team.draft_slot, Team.id))]
    rounds = round_robin(team_ids)
    for week in range(1, league.regular_season_weeks + 1):
        cycle, idx = divmod(week - 1, len(rounds))
        for a, b in rounds[idx]:
            home, away = (a, b) if cycle % 2 == 0 else (b, a)
            db.add(Matchup(week=week, home_team_id=home, away_team_id=away))
    db.flush()


@dataclass
class Standing:
    team: Team
    wins: int = 0
    losses: int = 0
    ties: int = 0
    pf: float = 0.0
    pa: float = 0.0
    seed: int = 0

    @property
    def games(self) -> int:
        return self.wins + self.losses + self.ties

    @property
    def pct(self) -> float:
        return (self.wins + 0.5 * self.ties) / self.games if self.games else 0.0

    @property
    def record(self) -> str:
        return f"{self.wins}-{self.losses}" + (f"-{self.ties}" if self.ties else "")


def standings(db: Session, league: League, through_week: int | None = None) -> list[Standing]:
    rows = {t.id: Standing(t) for t in db.scalars(select(Team))}
    q = select(Matchup).where(Matchup.final.is_(True), Matchup.is_playoff.is_(False))
    if through_week:
        q = q.where(Matchup.week <= through_week)
    for m in db.scalars(q):
        h, a = rows[m.home_team_id], rows[m.away_team_id]
        h.pf += m.home_score; h.pa += m.away_score
        a.pf += m.away_score; a.pa += m.home_score
        if m.home_score > m.away_score:
            h.wins += 1; a.losses += 1
        elif m.home_score < m.away_score:
            a.wins += 1; h.losses += 1
        else:
            h.ties += 1; a.ties += 1
    out = sorted(rows.values(), key=lambda s: (-s.pct, -s.pf, s.team.id))
    for i, s in enumerate(out, start=1):
        s.seed = i
        s.pf, s.pa = round(s.pf, 1), round(s.pa, 1)
    return out


def score_matchup(db: Session, league: League, m: Matchup) -> tuple[float, float]:
    ensure_lineup(db, m.home_team_id, m.week)
    ensure_lineup(db, m.away_team_id, m.week)
    hs, _ = team_week(db, league, m.home_team_id, m.week)
    as_, _ = team_week(db, league, m.away_team_id, m.week)
    return hs, as_


def finalize_week(db: Session, league: League, week: int) -> None:
    for m in db.scalars(select(Matchup).where(Matchup.week == week, Matchup.final.is_(False))):
        m.home_score, m.away_score = score_matchup(db, league, m)
        if m.home_score > m.away_score:
            m.winner_team_id = m.home_team_id
        elif m.away_score > m.home_score:
            m.winner_team_id = m.away_team_id
        else:
            m.winner_team_id = m.home_team_id if m.is_playoff else None  # higher seed wins playoff ties
        m.final = True
    db.flush()
    log(db, "week", f"Week {week} final")
    if week == league.regular_season_weeks:
        create_playoff_round(db, league, 1)
        league.status = "playoffs"
    elif week > league.regular_season_weeks:
        rnd = week - league.regular_season_weeks
        if week == total_weeks(league):
            league.status = "done"
        else:
            create_playoff_round(db, league, rnd + 1)
    db.flush()


def week_is_due(league: League, week: int) -> bool:
    end = week_end(league, week)
    now = now_local()
    return now.date() > end + timedelta(days=1) or (now.date() == end + timedelta(days=1) and now.hour >= FINALIZE_HOUR)


def finalize_due_weeks(db: Session, league: League) -> list[int]:
    done = []
    if league.status not in ("season", "playoffs"):
        return done
    for week in range(1, total_weeks(league) + 1):
        open_m = db.scalar(select(Matchup).where(Matchup.week == week, Matchup.final.is_(False)).limit(1))
        if open_m and week_is_due(league, week):
            finalize_week(db, league, week)
            done.append(week)
    return done


def bracket_order(n: int) -> list[int]:
    """Seed order so 1 and 2 can only meet in the final: n=4 -> [1,4,2,3]."""
    order = [1]
    while len(order) < n:
        size = len(order) * 2
        order = [x for s in order for x in (s, size + 1 - s)]
    return order


def create_playoff_round(db: Session, league: League, rnd: int) -> None:
    week = league.regular_season_weeks + rnd
    if db.scalar(select(Matchup).where(Matchup.week == week).limit(1)):
        return
    if rnd == 1:
        table = standings(db, league)
        seeds = {s.seed: s.team.id for s in table[:league.playoff_teams]}
        order = bracket_order(league.playoff_teams)
        for slot in range(league.playoff_teams // 2):
            hi, lo = sorted((order[2 * slot], order[2 * slot + 1]))
            db.add(Matchup(week=week, home_team_id=seeds[hi], away_team_id=seeds[lo],
                           is_playoff=True, playoff_round=1, bracket_slot=slot))
    else:
        prev = {m.bracket_slot: m for m in db.scalars(
            select(Matchup).where(Matchup.is_playoff.is_(True), Matchup.playoff_round == rnd - 1))}
        seed_of = {s.team.id: s.seed for s in standings(db, league)}
        for slot in range(len(prev) // 2):
            w1, w2 = prev[2 * slot].winner_team_id, prev[2 * slot + 1].winner_team_id
            hi, lo = sorted((w1, w2), key=lambda t: seed_of[t])
            db.add(Matchup(week=week, home_team_id=hi, away_team_id=lo,
                           is_playoff=True, playoff_round=rnd, bracket_slot=slot))
    db.flush()
