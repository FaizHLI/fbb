"""Setup commands.

  python -m app.cli init-league --name "My League" --season 2026-27 --start 2026-11-09
  python -m app.cli add-team --team "My Team" --user me --password secret --commish
  python -m app.cli reset-password --user me --password newsecret
"""
import argparse
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import hash_password
from .db import SessionLocal, init_db
from .models import League, Team, User
from .services.common import LeagueError


def create_league(db: Session, name: str, season: str, start: date, *, num_teams: int = 8,
                  weeks: int = 20, playoff_teams: int = 4, draft_timer: int = 90) -> League:
    if db.scalar(select(League)):
        raise LeagueError("A league already exists in this database.")
    if start.weekday() != 0:
        raise LeagueError("Start date must be a Monday.")
    if playoff_teams not in (2, 4, 8) or playoff_teams > num_teams:
        raise LeagueError("Playoff teams must be 2, 4 or 8 and no more than the number of teams.")
    if num_teams % 2:
        raise LeagueError("Number of teams must be even.")
    league = League(name=name, season=season, num_teams=num_teams, start_date=start,
                    regular_season_weeks=weeks, playoff_teams=playoff_teams, draft_timer_seconds=draft_timer)
    db.add(league)
    db.flush()
    return league


def create_team(db: Session, team_name: str, username: str, password: str, commish: bool = False) -> Team:
    username = username.strip().lower()
    if db.scalar(select(User).where(User.username == username)):
        raise LeagueError(f"Username {username} is taken.")
    league = db.scalar(select(League))
    if league and len(list(db.scalars(select(Team)))) >= league.num_teams:
        raise LeagueError("The league is full.")
    if league and league.status != "setup":
        raise LeagueError("Teams can only be added before the draft.")
    user = User(username=username, password_hash=hash_password(password), is_commish=commish)
    db.add(user)
    db.flush()
    team = Team(name=team_name.strip(), user_id=user.id)
    db.add(team)
    db.flush()
    return team


def main():
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init-league")
    p.add_argument("--name", required=True)
    p.add_argument("--season", required=True, help='NBA season, e.g. "2026-27"')
    p.add_argument("--start", required=True, help="Monday of fantasy week 1, YYYY-MM-DD")
    p.add_argument("--teams", type=int, default=8)
    p.add_argument("--weeks", type=int, default=20, help="regular season weeks")
    p.add_argument("--playoff-teams", type=int, default=4)
    p.add_argument("--draft-timer", type=int, default=90, help="seconds per pick, 0 = none")
    t = sub.add_parser("add-team")
    t.add_argument("--team", required=True)
    t.add_argument("--user", required=True)
    t.add_argument("--password", required=True)
    t.add_argument("--commish", action="store_true")
    r = sub.add_parser("reset-password")
    r.add_argument("--user", required=True)
    r.add_argument("--password", required=True)
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if args.cmd == "init-league":
            lg = create_league(db, args.name, args.season, date.fromisoformat(args.start), num_teams=args.teams,
                               weeks=args.weeks, playoff_teams=args.playoff_teams, draft_timer=args.draft_timer)
            print(f"Created {lg.name}")
        elif args.cmd == "add-team":
            tm = create_team(db, args.team, args.user, args.password, args.commish)
            print(f"Added {tm.name} ({args.user})")
        elif args.cmd == "reset-password":
            u = db.scalar(select(User).where(User.username == args.user.lower()))
            if not u:
                raise LeagueError("No such user.")
            u.password_hash = hash_password(args.password)
            print("Password updated")
        db.commit()
    except LeagueError as e:
        db.rollback()
        raise SystemExit(f"Error: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
