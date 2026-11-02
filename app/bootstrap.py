"""First-run setup from environment variables, so a cloud host needs no shell.

Runs on every start (see start.sh) and does nothing once the league exists.

  LEAGUE_NAME      e.g. Hoops League
  LEAGUE_SEASON    e.g. 2026-27
  LEAGUE_START     Monday of week 1, e.g. 2026-11-09   (required)
  LEAGUE_TEAMS     number of teams (default 8)
  LEAGUE_WEEKS     regular season weeks (default 20)
  PLAYOFF_TEAMS    2, 4 or 8 (default 4)
  ADMIN_USERNAME   commissioner login                   (required)
  ADMIN_PASSWORD   commissioner password                (required)

After the first deploy, log in as the admin and either import teams and a draft
from ESPN on the Commissioner page, or add teams and run a live draft.
"""
import os
import traceback
from datetime import date

from sqlalchemy import select

from .auth import hash_password
from .cli import create_league
from .db import SessionLocal, init_db
from .models import League, User


def main():
    init_db()
    db = SessionLocal()
    try:
        if db.scalar(select(League)):
            print("bootstrap: league exists, nothing to do")
            return
        need = ["LEAGUE_START", "ADMIN_USERNAME", "ADMIN_PASSWORD"]
        missing = [k for k in need if not os.environ.get(k)]
        if missing:
            print(f"bootstrap: set {', '.join(missing)} to create the league")
            return
        league = create_league(
            db, os.environ.get("LEAGUE_NAME", "Hoops League"), os.environ.get("LEAGUE_SEASON", "2026-27"),
            date.fromisoformat(os.environ["LEAGUE_START"]), num_teams=int(os.environ.get("LEAGUE_TEAMS", 8)),
            weeks=int(os.environ.get("LEAGUE_WEEKS", 20)), playoff_teams=int(os.environ.get("PLAYOFF_TEAMS", 4)))
        db.add(User(username=os.environ["ADMIN_USERNAME"].strip().lower(),
                    password_hash=hash_password(os.environ["ADMIN_PASSWORD"]), is_commish=True))
        db.commit()
        print(f"bootstrap: created {league.name}")
        try:
            from .jobs import sync_players
            print(f"bootstrap: synced {sync_players(db, league)} players")
            db.commit()
        except Exception:
            db.rollback()
            print("bootstrap: player sync failed (the job loop will retry):")
            traceback.print_exc()
    finally:
        db.close()


if __name__ == "__main__":
    main()
