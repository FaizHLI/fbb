"""Weekly projections and the best-lineup optimizer.

A starter's expected score for the week = projected fantasy points per game
x chance they play at least one game that week (a starter with no games scores 0).

- Points per game: last 14 days blended with the season (recent form counts more
  once there are enough recent games), steadied toward a league prior early on.
- Chance of playing: from the NBA schedule (team games left this week) and how
  often the player has suited up for his team's recent games (catches injuries).
"""
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..models import League, Player, PlayerGame, TeamGame
from ..scoring import game_fpts
from ..weeks import today_local, week_end, week_start
from .lineups import eligible

PRIOR, PRIOR_N = 22.0, 3
DEFAULT_WEEK_GAMES = 3.4


@dataclass
class Projection:
    player_id: int
    ppg: float           # projected fantasy points per game
    games_left: float    # scheduled team games left in the week (estimate if unknown)
    play_rate: float     # share of recent team games the player played
    expected: float      # ppg x P(at least one game)
    known_schedule: bool


def team_games(db: Session, teams: set[str], start: date, end: date) -> dict[str, list[date]]:
    out: dict[str, list[date]] = {t: [] for t in teams}
    if not teams:
        return out
    for g in db.scalars(select(TeamGame).where(TeamGame.team.in_(teams), TeamGame.game_date >= start,
                                               TeamGame.game_date <= end)):
        out.setdefault(g.team, []).append(g.game_date)
    return out


def schedule_known(db: Session, start: date, end: date) -> bool:
    return db.scalar(select(TeamGame.id).where(TeamGame.game_date >= start, TeamGame.game_date <= end).limit(1)) is not None


def project(db: Session, league: League, players: list[Player], week: int,
            today: date | None = None) -> dict[int, Projection]:
    today = today or today_local()
    ws, we = week_start(league, week), week_end(league, week)
    from_day = max(ws, today)
    ids = [p.id for p in players]
    games: dict[int, list[PlayerGame]] = {i: [] for i in ids}
    if ids:
        for g in db.scalars(select(PlayerGame).where(PlayerGame.player_id.in_(ids),
                                                     PlayerGame.game_date < from_day)):
            games[g.player_id].append(g)
    teams = {p.nba_team for p in players if p.nba_team}
    known = schedule_known(db, ws, we)
    upcoming = team_games(db, teams, from_day, we)
    recent_team = team_games(db, teams, from_day - timedelta(days=14), from_day - timedelta(days=1))

    out = {}
    for p in players:
        gs = games.get(p.id, [])
        season = [game_fpts(g) for g in gs]
        recent = [game_fpts(g) for g in gs if g.game_date >= from_day - timedelta(days=14)]
        s_avg = (sum(season) + PRIOR * PRIOR_N) / (len(season) + PRIOR_N)
        if len(recent) >= 3:
            w = min(len(recent), 6) / 10          # up to 60% weight on the last two weeks
            ppg = (1 - w) * s_avg + w * (sum(recent) / len(recent))
        else:
            ppg = s_avg

        # How often he's played when his team played lately.
        team_dates = recent_team.get(p.nba_team, [])
        played = {g.game_date for g in gs}
        if len(team_dates) >= 3:
            rate = sum(1 for d in team_dates if d in played) / len(team_dates)
            last3 = sorted(team_dates)[-3:]
            if not any(d in played for d in last3):
                rate = min(rate, 0.05)            # missed the last 3: likely out
        else:
            rate = 0.9 if season else 0.7
        rate = max(0.02, min(rate, 0.98))

        left = float(len(upcoming.get(p.nba_team, []))) if known else \
            DEFAULT_WEEK_GAMES * ((we - from_day).days + 1) / 7
        if from_day > we:
            left = 0.0
        p_any = 1 - (1 - rate) ** left if left > 0 else 0.0
        # If he already played this week, his slot is locked in for at least one game.
        if any(ws <= g.game_date <= we for g in gs):
            p_any = 1.0
        out[p.id] = Projection(p.id, round(ppg, 1), left, round(rate, 2), round(ppg * p_any, 1), known)
    return out


def best_lineup(players: list[Player], value: dict[int, float]) -> dict[int, int]:
    """Exact best assignment of players to starter slots (max total value).
    DP over slots with a bitmask of used players; rosters are small so this is instant."""
    slots = config.STARTER_SLOTS
    cands = [p for p in players if value.get(p.id, 0) > 0] or players
    cands = sorted(cands, key=lambda p: -value.get(p.id, 0))[:20]
    n = len(cands)
    elig = [[i for i, p in enumerate(cands) if eligible(p, s)] for s in slots]
    best: dict[int, tuple[float, tuple]] = {0: (0.0, ())}
    for si in range(len(slots)):
        nxt: dict[int, tuple[float, tuple]] = {}
        for mask, (score, picks) in best.items():
            options = [(None, 0.0)] + [(i, value.get(cands[i].id, 0)) for i in elig[si] if not mask >> i & 1]
            for i, v in options:
                m2 = mask if i is None else mask | (1 << i)
                cand = (score + v, picks + (i,))
                if m2 not in nxt or cand[0] > nxt[m2][0]:
                    nxt[m2] = cand
        best = nxt
    _, picks = max(best.values(), key=lambda t: t[0])
    return {si: cands[i].id for si, i in enumerate(picks) if i is not None}
