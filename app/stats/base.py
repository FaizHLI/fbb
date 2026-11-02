from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass
class PlayerInfo:
    id: int
    name: str
    nba_team: str
    positions: list[str]  # subset of PG, SG, SF, PF, C


@dataclass
class GameLog:
    player_id: int
    game_id: str
    game_date: date
    minutes: float
    pts: int
    reb: int
    ast: int
    stl: int
    blk: int
    tov: int
    fgm: int
    fga: int
    ftm: int
    fta: int
    tpm: int


class StatsProvider(Protocol):
    def players(self, season: str) -> list[PlayerInfo]: ...
    def game_logs(self, season: str, start: date, end: date) -> list[GameLog]: ...
    def season_averages(self, season: str) -> dict[int, dict]:
        """Per-game averages keyed by player id, with the same stat keys as GameLog.
        Used to rank players before the draft. May return {} if unsupported."""
        ...


def expand_positions(raw: str) -> list[str]:
    """Turn 'G', 'F-C', 'PG', 'Guard-Forward' into fantasy positions."""
    out: list[str] = []
    for part in raw.replace("/", "-").split("-"):
        p = part.strip().upper()
        if not p:
            continue
        if p in ("PG", "SG", "SF", "PF", "C"):
            mapped = [p]
        elif p.startswith("G"):
            mapped = ["PG", "SG"]
        elif p.startswith("F"):
            mapped = ["SF", "PF"]
        elif p.startswith("C"):
            mapped = ["C"]
        else:
            mapped = []
        for m in mapped:
            if m not in out:
                out.append(m)
    order = ["PG", "SG", "SF", "PF", "C"]
    return sorted(out, key=order.index)


def previous_season(season: str) -> str:
    start = int(season[:4]) - 1
    return f"{start}-{str(start + 1)[2:]}"


def minutes_to_float(m) -> float:
    if m is None or m == "":
        return 0.0
    if isinstance(m, (int, float)):
        return float(m)
    s = str(m)
    if ":" in s:
        mm, ss = s.split(":")[:2]
        return int(mm or 0) + int(ss or 0) / 60
    try:
        return float(s)
    except ValueError:
        return 0.0
