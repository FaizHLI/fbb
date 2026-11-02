"""Background jobs. Run `python -m app.jobs tick` every 15-30 minutes (cron, or the
built-in loop: `python -m app.jobs loop`). Every step is safe to repeat."""
import argparse
import time
import traceback
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .db import SessionLocal, init_db, utcnow
from .models import League, Matchup, Player, PlayerGame, Team
from .scoring import game_fpts, player_avgs
from .services.common import get_league
from .services.lineups import ensure_lineup
from .services.roster import process_waivers
from .services.schedule import finalize_due_weeks
from .stats import get_provider
from .stats.base import previous_season
from .weeks import current_week, is_locked, today_local


def sync_players(db: Session, league: League, provider=None) -> int:
    provider = provider or get_provider()
    infos = provider.players(league.season)
    seen = set()
    for info in infos:
        p = db.get(Player, info.id)
        if not p:
            p = Player(id=info.id, name=info.name)
            db.add(p)
        p.name, p.nba_team, p.positions, p.active = info.name, info.nba_team, ",".join(info.positions), True
        p.injury = getattr(info, "injury", "") or ""
        seen.add(info.id)
    db.flush()
    from .importer import merge_placeholders
    merge_placeholders(db)
    for p in db.scalars(select(Player).where(Player.id > 0)):
        if p.id not in seen:
            p.active = False  # keeps history; rostered players stay rostered
    db.flush()
    rank_players(db, league, provider)
    league.last_player_sync = utcnow()
    return len(infos)


def rank_players(db: Session, league: League, provider=None) -> None:
    """Pre-draft ranks from last season's per-game fantasy points. Once this season
    has games, rank by this season instead (blended until ~10 games)."""
    provider = provider or get_provider()
    try:
        prev = provider.season_averages(previous_season(league.season))
    except Exception:
        prev = {}
    prev_fp = {}
    for pid, s in prev.items():
        class _G: pass
        g = _G()
        for k in config.SCORING:
            setattr(g, k, s.get(k, 0))
        prev_fp[pid] = game_fpts(g)
    players = list(db.scalars(select(Player).where(Player.active.is_(True))))
    cur = player_avgs(db, [p.id for p in players])
    value = {}
    for p in players:
        gp, avg = cur.get(p.id, (0, 0.0))
        base = prev_fp.get(p.id)
        if base is None:
            value[p.id] = avg if gp else -1
        else:
            w = min(gp, 10) / 10
            value[p.id] = w * avg + (1 - w) * base
    for i, p in enumerate(sorted(players, key=lambda p: -value[p.id]), start=1):
        p.rank = i if value[p.id] >= 0 else None
    db.flush()


def sync_stats(db: Session, league: League, days_back: int = 3, provider=None) -> int:
    provider = provider or get_provider()
    end = today_local()
    if league.last_stats_sync is None:
        days_back = 400  # first sync: backfill from three weeks before the league starts
    start = max(league.start_date - timedelta(days=21), end - timedelta(days=days_back))
    if end < start:
        return 0
    logs = provider.game_logs(league.season, start, end)
    known = set(db.scalars(select(Player.id)))
    n = 0
    for g in logs:
        if g.player_id not in known:
            continue  # unknown player; picked up on next player sync
        row = db.scalar(select(PlayerGame).where(PlayerGame.player_id == g.player_id, PlayerGame.game_id == g.game_id))
        if not row:
            row = PlayerGame(player_id=g.player_id, game_id=g.game_id, game_date=g.game_date)
            db.add(row)
        for k in ("minutes", "pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga", "ftm", "fta", "tpm"):
            setattr(row, k, getattr(g, k))
        n += 1
    league.last_stats_sync = utcnow()
    db.flush()
    return n


def sync_schedule(db: Session, league: League, provider=None, back: int = 14, ahead: int = 14) -> int:
    """NBA schedule around today, used for projections and win probability."""
    provider = provider or get_provider()
    if not hasattr(provider, "schedule"):
        return 0
    from .models import TeamGame
    today = today_local()
    rows = provider.schedule(today - timedelta(days=back), today + timedelta(days=ahead))
    have = {(g.game_id, g.team) for g in db.scalars(select(TeamGame).where(
        TeamGame.game_date >= today - timedelta(days=back), TeamGame.game_date <= today + timedelta(days=ahead)))}
    n = 0
    for gid, d, team in rows:
        if (gid, team[:5]) not in have:
            db.add(TeamGame(game_id=gid, game_date=d, team=team[:5]))
            have.add((gid, team[:5]))
            n += 1
    db.flush()
    return n


def lock_lineups(db: Session, league: League) -> None:
    """Freeze this week's lineups so later roster moves can't change them."""
    if league.status not in ("season", "playoffs"):
        return
    week = current_week(league)
    if not is_locked(league, week):
        return
    team_ids = set()
    for m in db.scalars(select(Matchup).where(Matchup.week == week)):
        team_ids |= {m.home_team_id, m.away_team_id}
    if league.status == "season":
        team_ids |= set(db.scalars(select(Team.id)))
    for tid in team_ids:
        ensure_lineup(db, tid, week)


def tick(provider=None, log=print) -> None:
    db = SessionLocal()
    try:
        from .models import League as _League
        league = db.scalar(select(_League).limit(1))
        if not league:
            log("no league yet; nothing to do")
            return
        provider = provider or get_provider()
        if not league.last_player_sync or utcnow() - league.last_player_sync > timedelta(minutes=50):
            n = sync_players(db, league, provider)
            injured = {}
            for p in db.scalars(select(Player).where(Player.active.is_(True), Player.injury != "")):
                injured[p.injury] = injured.get(p.injury, 0) + 1
            log(f"players: {n}; injury statuses: {injured or 'none'}")
            db.commit()
        if league.status in ("season", "playoffs"):
            try:
                wide = utcnow().hour in (9, 21)  # twice a day look further out
                back = 30 if not league.last_stats_sync else (14 if wide else 2)
                log(f"schedule rows: {sync_schedule(db, league, provider, back=back, ahead=14 if wide else 8)}")
                db.commit()
            except Exception as e:  # schedule is nice to have; never block scoring
                db.rollback()
                log(f"schedule sync failed: {e}")
            log(f"stat lines: {sync_stats(db, league, provider=provider)}")
            db.commit()
            lock_lineups(db, league)
            for r in process_waivers(db, league):
                log(f"waiver: {r}")
            for w in finalize_due_weeks(db, league):
                log(f"finalized week {w}")
            if utcnow().hour == 9:  # once a day (~4-5am ET), refresh in-season ranks
                rank_players(db, league, provider)
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(prog="python -m app.jobs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick", help="run all jobs once")
    lp = sub.add_parser("loop", help="run tick forever")
    lp.add_argument("--minutes", type=int, default=20)
    sub.add_parser("sync-players")
    sp = sub.add_parser("sync-stats")
    sp.add_argument("--days", type=int, default=3, help="days back (use a big number to backfill)")
    args = ap.parse_args()
    init_db()
    if args.cmd == "tick":
        tick()
    elif args.cmd == "loop":
        while True:
            try:
                tick()
            except Exception:
                traceback.print_exc()
            time.sleep(args.minutes * 60)
    else:
        db = SessionLocal()
        league = get_league(db)
        if args.cmd == "sync-players":
            print(f"players: {sync_players(db, league)}")
        else:
            print(f"stat lines: {sync_stats(db, league, days_back=args.days)}")
        db.commit()
        db.close()


if __name__ == "__main__":
    main()
