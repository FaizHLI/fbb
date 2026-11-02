"""Average-per-game scoring.

Each starter's weekly score = their fantasy points that week / games they played.
A team's weekly score = the sum of its starters' weekly averages.
So a starter with 4 games and a starter with 2 games are on equal footing:
only how well they played per game matters.
"""
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .models import League, LineupSlot, Player, PlayerGame
from .weeks import week_end, week_start


def game_fpts(g) -> float:
    return float(sum(getattr(g, stat) * w for stat, w in config.SCORING.items()))


@dataclass
class PlayerWeek:
    player_id: int | None
    slot: str
    name: str = "Empty"
    nba_team: str = ""
    games: int = 0
    total: float = 0.0
    game_scores: list[float] = field(default_factory=list)

    @property
    def avg(self) -> float:
        if self.games < config.MIN_GAMES_FOR_AVG or self.games == 0:
            return 0.0
        return round(self.total / self.games, 1)


def games_by_player(db: Session, league: League, week: int, player_ids) -> dict[int, list[PlayerGame]]:
    ids = [p for p in player_ids if p]
    out: dict[int, list[PlayerGame]] = {p: [] for p in ids}
    if not ids:
        return out
    rows = db.scalars(
        select(PlayerGame)
        .where(PlayerGame.player_id.in_(ids))
        .where(PlayerGame.game_date >= week_start(league, week))
        .where(PlayerGame.game_date <= week_end(league, week))
        .order_by(PlayerGame.game_date)
    )
    for g in rows:
        out[g.player_id].append(g)
    return out


def team_week(db: Session, league: League, team_id: int, week: int,
              lineup: dict[int, int | None] | None = None) -> tuple[float, list[PlayerWeek]]:
    """Score and per-starter breakdown for a lineup (default: the saved one)."""
    slots = lineup if lineup is not None else {s.slot_index: s.player_id for s in db.scalars(
        select(LineupSlot).where(LineupSlot.team_id == team_id, LineupSlot.week == week))}
    games = games_by_player(db, league, week, slots.values())
    players = {p.id: p for p in db.scalars(select(Player).where(Player.id.in_([p for p in slots.values() if p])))}
    rows = []
    for i, slot_name in enumerate(config.STARTER_SLOTS):
        pid = slots.get(i)
        pw = PlayerWeek(player_id=pid, slot=slot_name)
        if pid and pid in players:
            pw.name, pw.nba_team = players[pid].name, players[pid].nba_team
            scores = [game_fpts(g) for g in games.get(pid, [])]
            pw.games, pw.total, pw.game_scores = len(scores), sum(scores), scores
        rows.append(pw)
    return round(sum(r.avg for r in rows), 1), rows


def player_avgs(db: Session, player_ids, start=None, end=None) -> dict[int, tuple[int, float]]:
    """(games, avg fpts) per player over a date range. Used for player lists."""
    ids = list(player_ids)
    if not ids:
        return {}
    q = select(PlayerGame).where(PlayerGame.player_id.in_(ids))
    if start:
        q = q.where(PlayerGame.game_date >= start)
    if end:
        q = q.where(PlayerGame.game_date <= end)
    acc: dict[int, list[float]] = {}
    for g in db.scalars(q):
        acc.setdefault(g.player_id, []).append(game_fpts(g))
    return {pid: (len(v), round(sum(v) / len(v), 1)) for pid, v in acc.items()}
