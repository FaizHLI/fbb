"""Free agent adds, drops and waiver claims."""
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..db import utcnow
from ..models import League, Player, RosterSpot, Team, WaiverClaim, WaiverHold
from .common import LeagueError, log
from .lineups import remove_from_open_lineups


def owner_id(db: Session, player_id: int) -> int | None:
    return db.scalar(select(RosterSpot.team_id).where(RosterSpot.player_id == player_id))


def roster_count(db: Session, team_id: int) -> int:
    """Players counting toward the roster limit (IR spots don't)."""
    return len(list(db.scalars(select(RosterSpot.id).where(RosterSpot.team_id == team_id,
                                                           RosterSpot.on_ir.is_(False)))))


def healthy_on_ir(db: Session, team_id: int) -> list[Player]:
    return [s.player for s in db.scalars(select(RosterSpot).where(RosterSpot.team_id == team_id,
                                                                    RosterSpot.on_ir.is_(True)))
            if not s.player.ir_eligible]


def require_clean_ir(db: Session, team: Team) -> None:
    bad = healthy_on_ir(db, team.id)
    if bad:
        raise LeagueError(f"{bad[0].name} is healthy but still on IR. Activate or drop him first.")


def move_to_ir(db: Session, league: League, team: Team, player_id: int) -> None:
    _require_moves_open(league)
    spot = db.scalar(select(RosterSpot).where(RosterSpot.player_id == player_id, RosterSpot.team_id == team.id))
    if not spot:
        raise LeagueError("That player isn't on your roster.")
    if spot.on_ir:
        raise LeagueError(f"{spot.player.name} is already on IR.")
    if not spot.player.ir_eligible:
        raise LeagueError(f"Only players listed OUT can go on IR.")
    used = len(list(db.scalars(select(RosterSpot.id).where(RosterSpot.team_id == team.id, RosterSpot.on_ir.is_(True)))))
    if used >= config.IR_SLOTS:
        raise LeagueError("Your IR spot is full.")
    spot.on_ir = True
    remove_from_open_lineups(db, league, team.id, player_id)
    log(db, "ir", f"{team.name} moved {spot.player.name} to IR")
    db.flush()


def activate_from_ir(db: Session, league: League, team: Team, player_id: int) -> None:
    _require_moves_open(league)
    spot = db.scalar(select(RosterSpot).where(RosterSpot.player_id == player_id, RosterSpot.team_id == team.id,
                                              RosterSpot.on_ir.is_(True)))
    if not spot:
        raise LeagueError("That player isn't on your IR.")
    if roster_count(db, team.id) >= config.ROSTER_MAX:
        raise LeagueError("Your roster is full. Drop someone to activate him.")
    spot.on_ir = False
    log(db, "ir", f"{team.name} activated {spot.player.name} from IR")
    db.flush()


def waiver_until(db: Session, player_id: int, now: datetime | None = None) -> datetime | None:
    hold = db.scalar(select(WaiverHold).where(WaiverHold.player_id == player_id))
    if hold and hold.until > (now or utcnow()):
        return hold.until
    return None


def _require_moves_open(league: League) -> None:
    if league.status not in ("season", "playoffs"):
        raise LeagueError("Adds and drops open after the draft.")


def _drop(db: Session, league: League, team: Team, player_id: int, now: datetime) -> Player:
    spot = db.scalar(select(RosterSpot).where(RosterSpot.player_id == player_id, RosterSpot.team_id == team.id))
    if not spot:
        raise LeagueError("That player isn't on your roster.")
    player = spot.player
    db.delete(spot)
    remove_from_open_lineups(db, league, team.id, player_id)
    db.execute(delete(WaiverHold).where(WaiverHold.player_id == player_id))
    db.add(WaiverHold(player_id=player_id, until=now + timedelta(hours=config.WAIVER_HOURS)))
    db.flush()
    return player


def _add(db: Session, team: Team, player: Player, now: datetime) -> None:
    db.add(RosterSpot(team_id=team.id, player_id=player.id, acquired_at=now))
    db.flush()


def drop_player(db: Session, league: League, team: Team, player_id: int) -> None:
    _require_moves_open(league)
    p = _drop(db, league, team, player_id, utcnow())
    log(db, "drop", f"{team.name} dropped {p.name}")


def add_free_agent(db: Session, league: League, team: Team, player_id: int, drop_id: int | None = None) -> None:
    _require_moves_open(league)
    now = utcnow()
    player = db.get(Player, player_id)
    if not player:
        raise LeagueError("Player not found.")
    if owner_id(db, player_id):
        raise LeagueError(f"{player.name} is already on a team.")
    if waiver_until(db, player_id, now):
        raise LeagueError(f"{player.name} is on waivers. Put in a claim instead.")
    require_clean_ir(db, team)
    dropped = _drop(db, league, team, drop_id, now) if drop_id else None
    if roster_count(db, team.id) >= config.ROSTER_MAX:
        raise LeagueError("Your roster is full. Pick someone to drop (not an IR player).")
    _add(db, team, player, now)
    db.execute(delete(WaiverHold).where(WaiverHold.player_id == player_id))
    log(db, "add", f"{team.name} added {player.name}" + (f", dropped {dropped.name}" if dropped else ""))


def submit_claim(db: Session, league: League, team: Team, player_id: int, drop_id: int | None = None) -> WaiverClaim:
    _require_moves_open(league)
    player = db.get(Player, player_id)
    if not player or not waiver_until(db, player_id):
        raise LeagueError("That player isn't on waivers.")
    if owner_id(db, player_id):
        raise LeagueError(f"{player.name} is already on a team.")
    if drop_id and owner_id(db, drop_id) != team.id:
        raise LeagueError("The player you'd drop isn't on your roster.")
    require_clean_ir(db, team)
    if roster_count(db, team.id) >= config.ROSTER_MAX and not drop_id:
        raise LeagueError("Your roster is full. Pick someone to drop.")
    dup = db.scalar(select(WaiverClaim).where(WaiverClaim.team_id == team.id, WaiverClaim.add_player_id == player_id,
                                              WaiverClaim.status == "pending"))
    if dup:
        raise LeagueError("You already have a claim on this player.")
    claim = WaiverClaim(team_id=team.id, add_player_id=player_id, drop_player_id=drop_id)
    db.add(claim)
    db.flush()
    return claim


def cancel_claim(db: Session, team: Team, claim_id: int) -> None:
    c = db.get(WaiverClaim, claim_id)
    if not c or c.team_id != team.id or c.status != "pending":
        raise LeagueError("Claim not found.")
    c.status = "cancelled"


def _move_to_back(db: Session, team: Team) -> None:
    teams = list(db.scalars(select(Team).order_by(Team.waiver_priority, Team.id)))
    teams.remove(team)
    teams.append(team)
    for i, t in enumerate(teams, start=1):
        t.waiver_priority = i


def process_waivers(db: Session, league: League, now: datetime | None = None) -> list[str]:
    """Resolve every expired waiver hold. Highest priority valid claim wins,
    then that team drops to the back of the order. Returns a summary."""
    now = now or utcnow()
    results = []
    holds = list(db.scalars(select(WaiverHold).where(WaiverHold.until <= now).order_by(WaiverHold.until)))
    for hold in holds:
        claims = list(db.scalars(
            select(WaiverClaim).join(Team, Team.id == WaiverClaim.team_id)
            .where(WaiverClaim.add_player_id == hold.player_id, WaiverClaim.status == "pending")
            .order_by(Team.waiver_priority, WaiverClaim.created_at)))
        winner = None
        for c in claims:
            if winner:
                c.status, c.note = "lost", f"Lost to {winner.team.name}"
                continue
            if owner_id(db, hold.player_id):
                c.status, c.note = "failed", "Player already rostered"
                continue
            if c.drop_player_id and owner_id(db, c.drop_player_id) != c.team_id:
                c.status, c.note = "failed", "Drop player no longer on roster"
                continue
            if healthy_on_ir(db, c.team_id):
                c.status, c.note = "failed", "Healthy player on IR"
                continue
            if not c.drop_player_id and roster_count(db, c.team_id) >= config.ROSTER_MAX:
                c.status, c.note = "failed", "Roster full"
                continue
            if c.drop_player_id:
                _drop(db, league, c.team, c.drop_player_id, now)
            _add(db, c.team, c.add_player, now)
            c.status, winner = "won", c
            _move_to_back(db, c.team)
            msg = f"{c.team.name} claimed {c.add_player.name}" + (
                f", dropped {c.drop_player.name}" if c.drop_player else "")
            log(db, "waiver", msg)
            results.append(msg)
        db.delete(hold)
        db.flush()
    return results
