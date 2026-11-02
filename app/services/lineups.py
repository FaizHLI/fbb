"""Weekly lineups. Each week's lineup locks at 00:00 Monday (league time)."""
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..models import League, LineupSlot, Player, RosterSpot, Team
from ..weeks import first_open_week, is_locked
from .common import LeagueError


def roster_players(db: Session, team_id: int, include_ir: bool = False) -> list[Player]:
    """Active roster (what lineups use). include_ir=True adds players on IR."""
    q = select(Player).join(RosterSpot, RosterSpot.player_id == Player.id).where(RosterSpot.team_id == team_id)
    if not include_ir:
        q = q.where(RosterSpot.on_ir.is_(False))
    return list(db.scalars(q.order_by(Player.rank.is_(None), Player.rank, Player.name)))


def ir_players(db: Session, team_id: int) -> list[Player]:
    return list(db.scalars(select(Player).join(RosterSpot, RosterSpot.player_id == Player.id)
                           .where(RosterSpot.team_id == team_id, RosterSpot.on_ir.is_(True)).order_by(Player.name)))


def eligible(player: Player, slot: str) -> bool:
    return bool(set(player.position_list) & config.SLOT_ELIGIBILITY[slot])


def auto_fill(players: list[Player], open_slots=None) -> dict[int, int]:
    """Best-ranked players into the most restrictive slots first."""
    slots = range(len(config.STARTER_SLOTS)) if open_slots is None else open_slots
    order = sorted(slots,
                   key=lambda i: len(config.SLOT_ELIGIBILITY[config.STARTER_SLOTS[i]]))
    used, out = set(), {}
    for i in order:
        for p in players:  # already sorted best first
            if p.id not in used and eligible(p, config.STARTER_SLOTS[i]):
                out[i] = p.id
                used.add(p.id)
                break
    return out


def _saved(db: Session, team_id: int, week: int) -> dict[int, int | None] | None:
    rows = list(db.scalars(select(LineupSlot).where(LineupSlot.team_id == team_id, LineupSlot.week == week)))
    if not rows:
        return None
    return {r.slot_index: r.player_id for r in rows}


def _write(db: Session, team_id: int, week: int, assignment: dict[int, int | None]) -> None:
    db.execute(delete(LineupSlot).where(LineupSlot.team_id == team_id, LineupSlot.week == week))
    for i in range(len(config.STARTER_SLOTS)):
        db.add(LineupSlot(team_id=team_id, week=week, slot_index=i, player_id=assignment.get(i)))
    db.flush()


def preview_lineup(db: Session, team_id: int, week: int) -> dict[int, int | None]:
    """The saved lineup, or (without saving) what it would be: the most recent
    earlier lineup carried over for players still rostered, holes auto-filled."""
    saved = _saved(db, team_id, week)
    if saved is not None:
        return saved
    roster = roster_players(db, team_id)
    on_roster = {p.id for p in roster}
    prev_week = db.scalar(select(LineupSlot.week).where(LineupSlot.team_id == team_id, LineupSlot.week < week)
                          .order_by(LineupSlot.week.desc()).limit(1))
    if prev_week:
        prev = _saved(db, team_id, prev_week) or {}
        assignment = {i: pid for i, pid in prev.items() if pid in on_roster}
        # fill any holes left by departed players
        holes = [i for i in range(len(config.STARTER_SLOTS)) if i not in assignment]
        assignment.update(auto_fill([p for p in roster if p.id not in assignment.values()], holes))
    else:
        assignment = auto_fill(roster)
    return assignment


def ensure_lineup(db: Session, team_id: int, week: int) -> dict[int, int | None]:
    """Like preview_lineup, but saves it. Called when a week locks or is scored."""
    if _saved(db, team_id, week) is not None:
        return _saved(db, team_id, week)
    assignment = preview_lineup(db, team_id, week)
    _write(db, team_id, week, assignment)
    return assignment


def set_lineup(db: Session, league: League, team: Team, week: int, assignment: dict[int, int | None]) -> None:
    if is_locked(league, week):
        raise LeagueError(f"Week {week} lineup is locked.")
    roster = {p.id: p for p in roster_players(db, team.id)}
    seen = set()
    clean: dict[int, int | None] = {}
    for i, slot in enumerate(config.STARTER_SLOTS):
        pid = assignment.get(i)
        if pid is None:
            clean[i] = None
            continue
        if pid not in roster:
            raise LeagueError("That player isn't on your roster.")
        if pid in seen:
            raise LeagueError(f"{roster[pid].name} is in two slots.")
        if not eligible(roster[pid], slot):
            raise LeagueError(f"{roster[pid].name} can't play {slot}.")
        seen.add(pid)
        clean[i] = pid
    _write(db, team.id, week, clean)


def remove_from_open_lineups(db: Session, league: League, team_id: int, player_id: int) -> None:
    """When a player leaves a roster, empty their slot in every week not yet locked."""
    open_from = first_open_week(league)
    for row in db.scalars(select(LineupSlot).where(
            LineupSlot.team_id == team_id, LineupSlot.player_id == player_id, LineupSlot.week >= open_from)):
        row.player_id = None
    db.flush()
