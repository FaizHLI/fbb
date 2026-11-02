"""Live snake draft. Every page poll calls `check_timer`, so no background worker is needed."""
import random
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import config
from ..db import utcnow
from ..models import DraftPick, League, Player, RosterSpot, Team
from .common import LeagueError, log


def teams_in_order(db: Session) -> list[Team]:
    return list(db.scalars(select(Team).order_by(Team.draft_slot.is_(None), Team.draft_slot, Team.id)))


def set_draft_order(db: Session, league: League, team_ids: list[int] | None = None) -> None:
    if league.status != "setup":
        raise LeagueError("Draft order can only change before the draft.")
    teams = list(db.scalars(select(Team)))
    if team_ids:
        by_id = {t.id: t for t in teams}
        if sorted(team_ids) != sorted(by_id):
            raise LeagueError("Order must include every team once.")
        ordered = [by_id[i] for i in team_ids]
    else:
        ordered = teams[:]
        random.shuffle(ordered)
    for i, t in enumerate(ordered, start=1):
        t.draft_slot = i
    db.flush()


def start_draft(db: Session, league: League) -> None:
    if league.status != "setup":
        raise LeagueError("The draft already started.")
    teams = teams_in_order(db)
    if len(teams) != league.num_teams:
        raise LeagueError(f"Need {league.num_teams} teams, have {len(teams)}.")
    if any(t.draft_slot is None for t in teams):
        set_draft_order(db, league)
        teams = teams_in_order(db)
    if not db.scalar(select(func.count(Player.id))):
        raise LeagueError("No players loaded. Run the player sync first.")
    db.execute(delete(DraftPick))
    overall = 1
    for rnd in range(1, config.ROSTER_MAX + 1):
        order = teams if rnd % 2 == 1 else list(reversed(teams))
        for t in order:
            db.add(DraftPick(overall=overall, round=rnd, team_id=t.id))
            overall += 1
    league.status = "drafting"
    league.pick_started_at = utcnow()
    log(db, "draft", "The draft started")
    db.flush()


def current_pick(db: Session) -> DraftPick | None:
    return db.scalar(select(DraftPick).where(DraftPick.player_id.is_(None)).order_by(DraftPick.overall).limit(1))


def drafted_ids(db: Session) -> set[int]:
    return set(db.scalars(select(DraftPick.player_id).where(DraftPick.player_id.is_not(None))))


def best_available(db: Session) -> Player | None:
    taken = drafted_ids(db)
    q = select(Player).where(Player.active.is_(True)).order_by(Player.rank.is_(None), Player.rank, Player.name)
    if taken:
        q = q.where(Player.id.not_in(taken))
    return db.scalar(q.limit(1))


def make_pick(db: Session, league: League, team: Team | None, player_id: int,
              *, by_commish: bool = False, auto: bool = False) -> DraftPick:
    if league.status != "drafting":
        raise LeagueError("The draft isn't running.")
    pick = current_pick(db)
    if not pick:
        raise LeagueError("The draft is over.")
    if not by_commish and not auto and (team is None or pick.team_id != team.id):
        raise LeagueError("It's not your pick.")
    player = db.get(Player, player_id)
    if not player or not player.active:
        raise LeagueError("Player not found.")
    if player_id in drafted_ids(db):
        raise LeagueError(f"{player.name} is already taken.")
    pick.player_id, pick.made_at, pick.auto = player_id, utcnow(), auto
    db.add(RosterSpot(team_id=pick.team_id, player_id=player_id))
    league.pick_started_at = utcnow()
    db.flush()
    if current_pick(db) is None:
        _finish(db, league)
    return pick


def _finish(db: Session, league: League) -> None:
    league.status = "season"
    league.pick_started_at = None
    # Waiver order starts as reverse draft order.
    teams = teams_in_order(db)
    for i, t in enumerate(reversed(teams), start=1):
        t.waiver_priority = i
    from .schedule import create_regular_season
    create_regular_season(db, league)
    log(db, "draft", "The draft finished. Good luck!")


def seconds_left(league: League) -> int | None:
    if league.status != "drafting" or not league.draft_timer_seconds or not league.pick_started_at:
        return None
    left = league.draft_timer_seconds - (utcnow() - league.pick_started_at).total_seconds()
    return max(0, int(left))


def check_timer(db: Session, league: League) -> bool:
    """Autopick if the clock ran out. Returns True if a pick was made."""
    # One autopick per check; the next team gets a fresh clock.
    if seconds_left(league) != 0:
        return False
    p = best_available(db)
    if not p:
        return False
    make_pick(db, league, None, p.id, auto=True)
    return True


def undo_last_pick(db: Session, league: League) -> None:
    if league.status != "drafting":
        raise LeagueError("Undo only works during the draft.")
    last = db.scalar(select(DraftPick).where(DraftPick.player_id.is_not(None))
                     .order_by(DraftPick.overall.desc()).limit(1))
    if not last:
        raise LeagueError("No picks to undo.")
    db.execute(delete(RosterSpot).where(RosterSpot.player_id == last.player_id))
    last.player_id, last.made_at, last.auto = None, None, False
    league.pick_started_at = utcnow()
    db.flush()
