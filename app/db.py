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
    else:
        # Free hosted databases drop idle connections; check before use.
        kwargs.update(pool_pre_ping=True, pool_recycle=300, pool_size=3, max_overflow=2)
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
    eng = eng or engine
    Base.metadata.create_all(eng)
    _add_missing_columns(eng)


def _add_missing_columns(eng) -> None:
    """Tiny auto-migration: add columns that were added to models after the
    database was created. Only handles new columns, which is all we need."""
    from sqlalchemy import inspect, text
    insp = inspect(eng)
    with eng.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl_type = col.type.compile(dialect=eng.dialect)
                default = ""
                if col.default is not None and col.default.is_scalar:
                    v = col.default.arg
                    default = " DEFAULT " + ("TRUE" if v is True else "FALSE" if v is False else
                                             str(v) if isinstance(v, (int, float)) else "'" + str(v).replace("'", "''") + "'")
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl_type}{default}'))


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
