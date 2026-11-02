import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return eng


engine = make_engine(config.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(eng=None):
    from . import models  # noqa: F401  (register tables)
    Base.metadata.create_all(eng or engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Tests and demos can freeze the clock (timezone-aware datetime). Also settable
# with env var HOOPS_FAKE_NOW, e.g. "2026-12-10T20:00:00-05:00".
FAKE_NOW: datetime | None = None
if os.environ.get("HOOPS_FAKE_NOW"):
    FAKE_NOW = datetime.fromisoformat(os.environ["HOOPS_FAKE_NOW"])


def now_aware() -> datetime:
    return FAKE_NOW if FAKE_NOW is not None else datetime.now(timezone.utc)


def utcnow() -> datetime:
    """Naive UTC timestamp, used for every stored datetime."""
    return now_aware().astimezone(timezone.utc).replace(tzinfo=None)
