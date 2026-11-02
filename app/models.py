"""Database tables. One install = one league."""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (Boolean, Date, DateTime, Float, ForeignKey, Integer,
                        String, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow


class League(Base):
    __tablename__ = "league"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    season: Mapped[str] = mapped_column(String(10))           # e.g. "2026-27"
    num_teams: Mapped[int] = mapped_column(Integer, default=8)
    start_date: Mapped[date] = mapped_column(Date)            # Monday of fantasy week 1
    regular_season_weeks: Mapped[int] = mapped_column(Integer, default=20)
    playoff_teams: Mapped[int] = mapped_column(Integer, default=4)  # 2, 4 or 8
    draft_timer_seconds: Mapped[int] = mapped_column(Integer, default=90)  # 0 = no timer
    # setup -> drafting -> season -> playoffs -> done
    status: Mapped[str] = mapped_column(String(20), default="setup")
    pick_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_player_sync: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_stats_sync: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    is_commish: Mapped[bool] = mapped_column(Boolean, default=False)
    team: Mapped[Optional["Team"]] = relationship(back_populates="user", uselist=False)


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    abbrev: Mapped[str] = mapped_column(String(6), default="")
    manager: Mapped[str] = mapped_column(String(80), default="")
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    draft_slot: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # 1 = first pick
    waiver_priority: Mapped[int] = mapped_column(Integer, default=99)          # 1 = first claim
    user: Mapped[Optional[User]] = relationship(back_populates="team")


class Player(Base):
    __tablename__ = "players"
    id: Mapped[int] = mapped_column(primary_key=True)          # stats provider's player id
    name: Mapped[str] = mapped_column(String(80), index=True)
    nba_team: Mapped[str] = mapped_column(String(5), default="")
    positions: Mapped[str] = mapped_column(String(20), default="")  # "PG,SG"
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # pre-draft rank
    injury: Mapped[str] = mapped_column(String(10), default="")  # "", "DTD", "OUT", "SUSP"

    @property
    def ir_eligible(self) -> bool:
        return self.injury == "OUT"

    @property
    def position_list(self) -> list[str]:
        return [p for p in self.positions.split(",") if p]


class RosterSpot(Base):
    __tablename__ = "roster_spots"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), unique=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    on_ir: Mapped[bool] = mapped_column(Boolean, default=False)  # IR spots don't count toward the roster limit
    player: Mapped[Player] = relationship()


class LineupSlot(Base):
    __tablename__ = "lineup_slots"
    __table_args__ = (UniqueConstraint("team_id", "week", "slot_index"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    week: Mapped[int] = mapped_column(Integer, index=True)
    slot_index: Mapped[int] = mapped_column(Integer)
    player_id: Mapped[Optional[int]] = mapped_column(ForeignKey("players.id"), nullable=True)


class PlayerGame(Base):
    __tablename__ = "player_games"
    __table_args__ = (UniqueConstraint("player_id", "game_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    game_id: Mapped[str] = mapped_column(String(30))
    game_date: Mapped[date] = mapped_column(Date, index=True)
    minutes: Mapped[float] = mapped_column(Float, default=0)
    pts: Mapped[int] = mapped_column(Integer, default=0)
    reb: Mapped[int] = mapped_column(Integer, default=0)
    ast: Mapped[int] = mapped_column(Integer, default=0)
    stl: Mapped[int] = mapped_column(Integer, default=0)
    blk: Mapped[int] = mapped_column(Integer, default=0)
    tov: Mapped[int] = mapped_column(Integer, default=0)
    fgm: Mapped[int] = mapped_column(Integer, default=0)
    fga: Mapped[int] = mapped_column(Integer, default=0)
    ftm: Mapped[int] = mapped_column(Integer, default=0)
    fta: Mapped[int] = mapped_column(Integer, default=0)
    tpm: Mapped[int] = mapped_column(Integer, default=0)


class Matchup(Base):
    __tablename__ = "matchups"
    id: Mapped[int] = mapped_column(primary_key=True)
    week: Mapped[int] = mapped_column(Integer, index=True)
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    home_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    away_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    final: Mapped[bool] = mapped_column(Boolean, default=False)
    winner_team_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teams.id"), nullable=True)
    # Playoffs: home team is the higher seed and wins ties.
    is_playoff: Mapped[bool] = mapped_column(Boolean, default=False)
    playoff_round: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bracket_slot: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    home_team: Mapped[Team] = relationship(foreign_keys=[home_team_id])
    away_team: Mapped[Team] = relationship(foreign_keys=[away_team_id])


class DraftPick(Base):
    __tablename__ = "draft_picks"
    id: Mapped[int] = mapped_column(primary_key=True)
    overall: Mapped[int] = mapped_column(Integer, unique=True)
    round: Mapped[int] = mapped_column(Integer)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    player_id: Mapped[Optional[int]] = mapped_column(ForeignKey("players.id"), nullable=True)
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    made_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    team: Mapped[Team] = relationship()
    player: Mapped[Optional[Player]] = relationship()


class WaiverHold(Base):
    """A dropped player who can only be claimed (not added) until `until`."""
    __tablename__ = "waiver_holds"
    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), unique=True)
    until: Mapped[datetime] = mapped_column(DateTime)


class WaiverClaim(Base):
    __tablename__ = "waiver_claims"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    add_player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    drop_player_id: Mapped[Optional[int]] = mapped_column(ForeignKey("players.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/won/lost/failed/cancelled
    note: Mapped[str] = mapped_column(String(200), default="")
    team: Mapped[Team] = relationship()
    add_player: Mapped[Player] = relationship(foreign_keys=[add_player_id])
    drop_player: Mapped[Optional[Player]] = relationship(foreign_keys=[drop_player_id])


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    proposer_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    receiver_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    # pending/accepted/rejected/cancelled/vetoed/failed
    status: Mapped[str] = mapped_column(String(20), default="pending")
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    proposer: Mapped[Team] = relationship(foreign_keys=[proposer_team_id])
    receiver: Mapped[Team] = relationship(foreign_keys=[receiver_team_id])
    items: Mapped[list["TradeItem"]] = relationship(cascade="all, delete-orphan")


class TradeItem(Base):
    __tablename__ = "trade_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    from_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    player: Mapped[Player] = relationship()


class Transaction(Base):
    """Plain-text league activity log."""
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(String(300))


class TeamGame(Base):
    """NBA schedule: one row per team per game (past and upcoming)."""
    __tablename__ = "team_games"
    __table_args__ = (UniqueConstraint("game_id", "team"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[str] = mapped_column(String(30))
    game_date: Mapped[date] = mapped_column(Date, index=True)
    team: Mapped[str] = mapped_column(String(5), index=True)
