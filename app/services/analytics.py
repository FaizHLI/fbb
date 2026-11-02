"""Win probability and power rankings."""
from dataclasses import dataclass, field
from datetime import date
from math import erf, exp, sqrt
from statistics import pstdev

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import League, Matchup, PlayerGame, Team
from ..scoring import PlayerWeek, game_fpts, team_week
from ..weeks import is_locked, today_local, week_end, week_start
from .lineups import ensure_lineup, preview_lineup
from .schedule import standings

GAMES_PER_DAY = 3.4 / 7   # an average NBA team plays ~3.4 games a week
PRIOR_MEAN, PRIOR_SD, PRIOR_N = 25.0, 10.0, 3  # used to steady players with few games


def _phi(z: float) -> float:
    return 0.5 * (1 + erf(z / sqrt(2)))


def lineup_for(db: Session, league: League, team_id: int, week: int) -> dict[int, int | None]:
    return ensure_lineup(db, team_id, week) if is_locked(league, week) else preview_lineup(db, team_id, week)


def matchup_view(db: Session, league: League, m: Matchup):
    """Live scores and breakdowns for both teams."""
    if m.final:
        hs, hrows = team_week(db, league, m.home_team_id, m.week)
        as_, arows = team_week(db, league, m.away_team_id, m.week)
        return m.home_score, hrows, m.away_score, arows
    hs, hrows = team_week(db, league, m.home_team_id, m.week, lineup_for(db, league, m.home_team_id, m.week))
    as_, arows = team_week(db, league, m.away_team_id, m.week, lineup_for(db, league, m.away_team_id, m.week))
    return hs, hrows, as_, arows


def _profiles(db: Session, player_ids, before: date) -> dict[int, tuple[float, float]]:
    """(mean, sd) of fantasy points per game this season, shrunk toward a prior."""
    ids = [p for p in player_ids if p]
    scores: dict[int, list[float]] = {p: [] for p in ids}
    if ids:
        for g in db.scalars(select(PlayerGame).where(PlayerGame.player_id.in_(ids), PlayerGame.game_date <= before)):
            scores[g.player_id].append(game_fpts(g))
    out = {}
    for pid, s in scores.items():
        n = len(s)
        mean = (sum(s) + PRIOR_MEAN * PRIOR_N) / (n + PRIOR_N)
        sd = pstdev(s) if n >= 3 else PRIOR_SD
        sd = (sd * n + PRIOR_SD * PRIOR_N) / (n + PRIOR_N)
        out[pid] = (mean, max(sd, 4.0))
    return out


def _team_projection(league: League, week: int, rows: list[PlayerWeek], profiles, today: date) -> tuple[float, float]:
    start, end = week_start(league, week), week_end(league, week)
    days_left = 0 if today > end else (end - max(today, start)).days + 1
    remaining = GAMES_PER_DAY * days_left
    mean = var = 0.0
    for r in rows:
        if not r.player_id:
            continue
        mu, sd = profiles.get(r.player_id, (PRIOR_MEAN, PRIOR_SD))
        k = r.games
        if k > 0:
            n = k + remaining
            mean += (r.total + remaining * mu) / n
            var += remaining * sd * sd / (n * n)
        elif remaining > 0:
            p0 = exp(-remaining)                       # chance of no games at all
            cond_var = sd * sd / max(remaining, 1.0)
            mean += (1 - p0) * mu
            var += (1 - p0) * cond_var + p0 * (1 - p0) * mu * mu
    return mean, var


@dataclass
class WinProb:
    home: float          # 0-100
    away: float
    home_proj: float
    away_proj: float


def win_probability(db: Session, league: League, m: Matchup, view=None, today: date | None = None) -> WinProb:
    hs, hrows, as_, arows = view or matchup_view(db, league, m)
    if m.final:
        h = 100.0 if m.winner_team_id == m.home_team_id else 0.0 if m.winner_team_id == m.away_team_id else 50.0
        return WinProb(h, 100 - h, m.home_score, m.away_score)
    today = today or today_local()
    ids = [r.player_id for r in hrows + arows]
    profiles = _profiles(db, ids, min(today, week_end(league, m.week)))
    hm, hv = _team_projection(league, m.week, hrows, profiles, today)
    am, av = _team_projection(league, m.week, arows, profiles, today)
    sd = sqrt(hv + av)
    if sd < 1e-6:
        p = 1.0 if hm > am else 0.0 if hm < am else (1.0 if m.is_playoff else 0.5)
    else:
        p = _phi((hm - am) / sd)
    p = min(max(p * 100, 1.0), 99.0) if sd >= 1e-6 else p * 100
    return WinProb(round(p), round(100 - p), round(hm, 1), round(am, 1))


# ---- Power rankings ---------------------------------------------------------

@dataclass
class PowerRow:
    team: Team
    rank: int = 0
    prev_rank: int | None = None
    score: float = 0.0
    record: str = "0-0"
    all_play_w: int = 0
    all_play_l: int = 0
    all_play_t: int = 0
    avg_score: float = 0.0
    last3: float = 0.0
    weekly: list[float] = field(default_factory=list)

    @property
    def move(self) -> int:
        return 0 if self.prev_rank is None else self.prev_rank - self.rank

    @property
    def all_play(self) -> str:
        return f"{self.all_play_w}-{self.all_play_l}" + (f"-{self.all_play_t}" if self.all_play_t else "")


def _rank(db: Session, league: League, through: int) -> list[PowerRow]:
    """Power score (0-100) = 40% all-play win %, 30% actual win %, 30% last-3-week scoring
    scaled between the league's lowest and highest."""
    teams = {t.id: PowerRow(t) for t in db.scalars(select(Team))}
    by_week: dict[int, dict[int, float]] = {}
    for m in db.scalars(select(Matchup).where(Matchup.final.is_(True), Matchup.is_playoff.is_(False),
                                              Matchup.week <= through)):
        by_week.setdefault(m.week, {})[m.home_team_id] = m.home_score
        by_week[m.week][m.away_team_id] = m.away_score
    for week in sorted(by_week):
        scores = by_week[week]
        for tid, s in scores.items():
            row = teams[tid]
            row.weekly.append(s)
            for oid, o in scores.items():
                if oid == tid:
                    continue
                if s > o:
                    row.all_play_w += 1
                elif s < o:
                    row.all_play_l += 1
                else:
                    row.all_play_t += 1
    table = {s.team.id: s for s in standings(db, league, through_week=through)}
    recents = []
    for tid, row in teams.items():
        row.record = table[tid].record
        row.avg_score = round(sum(row.weekly) / len(row.weekly), 1) if row.weekly else 0.0
        last = row.weekly[-3:]
        row.last3 = round(sum(last) / len(last), 1) if last else 0.0
        recents.append(row.last3)
    lo, hi = (min(recents), max(recents)) if recents else (0, 0)
    for tid, row in teams.items():
        ap_games = row.all_play_w + row.all_play_l + row.all_play_t
        ap = (row.all_play_w + 0.5 * row.all_play_t) / ap_games if ap_games else 0.5
        wp = table[tid].pct if table[tid].games else 0.5
        rec = (row.last3 - lo) / (hi - lo) if hi > lo else 0.5
        row.score = round(100 * (0.4 * ap + 0.3 * wp + 0.3 * rec), 1)
    out = sorted(teams.values(), key=lambda r: (-r.score, -r.avg_score, r.team.id))
    for i, r in enumerate(out, start=1):
        r.rank = i
    return out


def power_rankings(db: Session, league: League) -> tuple[int, list[PowerRow]]:
    """Returns (through_week, rows). Movement compares to the week before."""
    last_final = db.scalar(select(Matchup.week).where(Matchup.final.is_(True), Matchup.is_playoff.is_(False))
                           .order_by(Matchup.week.desc()).limit(1)) or 0
    rows = _rank(db, league, last_final)
    if last_final >= 2:
        prev = {r.team.id: r.rank for r in _rank(db, league, last_final - 1)}
        for r in rows:
            r.prev_rank = prev[r.team.id]
    return last_final, rows
