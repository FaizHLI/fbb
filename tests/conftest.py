import os
import tempfile
from datetime import date, datetime

import pytest

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["STATS_PROVIDER"] = "fake"
os.environ["SECRET_KEY"] = "test"

from app import config, db as dbmod  # noqa: E402
from app.db import Base, SessionLocal, engine, init_db  # noqa: E402

START = date(2026, 11, 9)  # a Monday


def set_now(iso: str) -> None:
    """Freeze the clock. Naive times are league (Eastern) time."""
    dt = datetime.fromisoformat(iso)
    dbmod.FAKE_NOW = dt if dt.tzinfo else dt.replace(tzinfo=config.LEAGUE_TZ)


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    init_db()
    s = SessionLocal()
    set_now("2026-11-01T12:00:00")
    yield s
    s.close()
    dbmod.FAKE_NOW = None


@pytest.fixture()
def league8(db):
    """A league with 8 teams and players loaded, still in setup."""
    from app.cli import create_league, create_team
    from app.jobs import sync_players
    lg = create_league(db, "Test League", "2026-27", START, weeks=4, playoff_teams=4, draft_timer=60)
    for i in range(8):
        create_team(db, f"Team {i + 1}", f"user{i + 1}", "pw", commish=(i == 0))
    sync_players(db, lg)
    db.commit()
    return lg
