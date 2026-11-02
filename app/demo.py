"""Build a demo league with fake stats so you can click around.

  STATS_PROVIDER=fake DATABASE_URL=sqlite:///./demo.db python -m app.demo
  STATS_PROVIDER=fake DATABASE_URL=sqlite:///./demo.db HOOPS_FAKE_NOW=2026-12-17T20:00:00-05:00 uvicorn app.main:app

Log in as demo1 ... demo8, password "demo" (demo1 is commissioner).
"""
from datetime import date, datetime, timedelta

from . import config, db as dbmod
from .cli import create_league, create_team
from .db import Base, SessionLocal, engine, init_db
from .jobs import lock_lineups, sync_players, sync_schedule, sync_stats
from .services import draft, roster, schedule
from .stats.fake_provider import FakeProvider

NAMES = ["Rim Rockers", "Splash Bros Lite", "Bench Mob", "Free Throw Merchants",
         "Load Management FC", "Brick City", "Pick & Pop Culture", "Triple Double Trouble"]


def main(weeks_played: int = 5):
    Base.metadata.drop_all(engine)
    init_db()
    db = SessionLocal()
    fake = FakeProvider()
    start = date(2026, 11, 9)
    dbmod.FAKE_NOW = datetime(2026, 11, 2, 12, tzinfo=config.LEAGUE_TZ)
    lg = create_league(db, "Hoops League", "2026-27", start, weeks=14, playoff_teams=4, draft_timer=90)
    for i, n in enumerate(NAMES, start=1):
        create_team(db, n, f"demo{i}", "demo", commish=(i == 1))
    sync_players(db, lg, fake)
    draft.set_draft_order(db, lg)
    draft.start_draft(db, lg)
    while lg.status == "drafting":
        p = draft.current_pick(db)
        draft.make_pick(db, lg, p.team, draft.best_available(db).id)
    db.commit()
    for week in range(1, weeks_played + 1):
        ws = start + timedelta(days=7 * (week - 1))
        dbmod.FAKE_NOW = datetime.combine(ws, datetime.min.time()).replace(hour=1, tzinfo=config.LEAGUE_TZ)
        lock_lineups(db, lg)
        dbmod.FAKE_NOW = datetime.combine(ws + timedelta(days=7), datetime.min.time()).replace(hour=11, tzinfo=config.LEAGUE_TZ)
        sync_stats(db, lg, days_back=8, provider=fake)
        schedule.finalize_due_weeks(db, lg)
        db.commit()
    # partway through the next week
    ws = start + timedelta(days=7 * weeks_played)
    dbmod.FAKE_NOW = datetime.combine(ws + timedelta(days=2), datetime.min.time()).replace(hour=23, tzinfo=config.LEAGUE_TZ)
    lock_lineups(db, lg)
    sync_stats(db, lg, days_back=3, provider=fake)
    sync_schedule(db, lg, fake, back=60, ahead=14)
    db.commit()
    print(f"Demo ready. Set HOOPS_FAKE_NOW={dbmod.FAKE_NOW.isoformat()}")
    db.close()


if __name__ == "__main__":
    main()
