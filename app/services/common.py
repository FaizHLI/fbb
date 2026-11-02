from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import League, Transaction


class LeagueError(Exception):
    """A rule was broken. The message is shown to the user."""


def get_league(db: Session) -> League:
    league = db.scalar(select(League).limit(1))
    if not league:
        raise LeagueError("No league yet. Run: python -m app.cli init-league")
    return league


def log(db: Session, kind: str, text: str) -> None:
    db.add(Transaction(kind=kind, text=text[:300]))
