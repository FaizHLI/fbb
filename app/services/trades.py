from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..db import utcnow
from ..models import League, RosterSpot, Team, Trade, TradeItem
from .common import LeagueError, log
from .lineups import remove_from_open_lineups
from .roster import owner_id, roster_count


def propose(db: Session, league: League, proposer: Team, receiver: Team,
            give_ids: list[int], get_ids: list[int], note: str = "") -> Trade:
    if league.status not in ("season", "playoffs"):
        raise LeagueError("Trades open after the draft.")
    if proposer.id == receiver.id:
        raise LeagueError("You can't trade with yourself.")
    if not give_ids and not get_ids:
        raise LeagueError("Pick at least one player.")
    for pid in give_ids:
        if owner_id(db, pid) != proposer.id:
            raise LeagueError("You can only offer players on your roster.")
    for pid in get_ids:
        if owner_id(db, pid) != receiver.id:
            raise LeagueError("You can only ask for players on their roster.")
    t = Trade(proposer_team_id=proposer.id, receiver_team_id=receiver.id, note=note[:200])
    t.items = [TradeItem(player_id=p, from_team_id=proposer.id) for p in give_ids] + \
              [TradeItem(player_id=p, from_team_id=receiver.id) for p in get_ids]
    db.add(t)
    db.flush()
    return t


def _summary(t: Trade) -> str:
    a = ", ".join(i.player.name for i in t.items if i.from_team_id == t.proposer_team_id) or "nothing"
    b = ", ".join(i.player.name for i in t.items if i.from_team_id == t.receiver_team_id) or "nothing"
    return f"{t.proposer.name} sends {a} to {t.receiver.name} for {b}"


def accept(db: Session, league: League, team: Team, trade_id: int) -> None:
    t = db.get(Trade, trade_id)
    if not t or t.status != "pending" or t.receiver_team_id != team.id:
        raise LeagueError("Trade not found.")
    # Re-check: rosters may have changed since the offer.
    for item in t.items:
        if owner_id(db, item.player_id) != item.from_team_id:
            raise LeagueError(f"{item.player.name} is no longer on that team. Reject this trade.")
    from .roster import require_clean_ir
    require_clean_ir(db, t.proposer)
    require_clean_ir(db, t.receiver)
    spots = {i.player_id: db.scalar(select(RosterSpot).where(RosterSpot.player_id == i.player_id)) for i in t.items}
    for team_id in (t.proposer_team_id, t.receiver_team_id):
        out_active = sum(1 for i in t.items if i.from_team_id == team_id and not spots[i.player_id].on_ir)
        in_n = sum(1 for i in t.items if i.from_team_id != team_id)
        if roster_count(db, team_id) - out_active + in_n > config.ROSTER_MAX:
            raise LeagueError("A team would go over the roster limit. Drop someone first.")
    for item in t.items:
        to_team = t.receiver_team_id if item.from_team_id == t.proposer_team_id else t.proposer_team_id
        spot = spots[item.player_id]
        spot.team_id, spot.acquired_at, spot.on_ir = to_team, utcnow(), False
        remove_from_open_lineups(db, league, item.from_team_id, item.player_id)
    t.status, t.resolved_at = "accepted", utcnow()
    log(db, "trade", _summary(t))
    db.flush()


def reject(db: Session, team: Team, trade_id: int) -> None:
    t = db.get(Trade, trade_id)
    if not t or t.status != "pending" or t.receiver_team_id != team.id:
        raise LeagueError("Trade not found.")
    t.status, t.resolved_at = "rejected", utcnow()


def cancel(db: Session, team: Team, trade_id: int) -> None:
    t = db.get(Trade, trade_id)
    if not t or t.status != "pending" or t.proposer_team_id != team.id:
        raise LeagueError("Trade not found.")
    t.status, t.resolved_at = "cancelled", utcnow()


def veto(db: Session, trade_id: int) -> None:
    t = db.get(Trade, trade_id)
    if not t or t.status != "pending":
        raise LeagueError("Only pending trades can be vetoed.")
    t.status, t.resolved_at = "vetoed", utcnow()
    log(db, "trade", f"Commissioner vetoed: {_summary(t)}")
