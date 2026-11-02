"""Stats from balldontlie.io. Box scores need a paid tier; set BALLDONTLIE_API_KEY."""
from datetime import date, timedelta

import httpx

from .. import config
from .base import GameLog, PlayerInfo, expand_positions, minutes_to_float

BASE = "https://api.balldontlie.io/v1"


class BallDontLieProvider:
    def __init__(self, api_key: str | None = None):
        self.client = httpx.Client(headers={"Authorization": api_key or config.BALLDONTLIE_API_KEY}, timeout=30)

    def _paged(self, path: str, params: dict):
        cursor = None
        while True:
            p = dict(params, per_page=100)
            if cursor:
                p["cursor"] = cursor
            r = self.client.get(f"{BASE}{path}", params=p)
            r.raise_for_status()
            body = r.json()
            yield from body["data"]
            cursor = body.get("meta", {}).get("next_cursor")
            if not cursor:
                break

    def players(self, season: str) -> list[PlayerInfo]:
        out = []
        for r in self._paged("/players/active", {}):
            pos = expand_positions(r.get("position") or "")
            if pos:
                out.append(PlayerInfo(int(r["id"]), f"{r['first_name']} {r['last_name']}",
                                      (r.get("team") or {}).get("abbreviation", ""), pos))
        return out

    def game_logs(self, season: str, start: date, end: date) -> list[GameLog]:
        dates = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
        out = []
        for r in self._paged("/stats", {"dates[]": dates}):
            mins = minutes_to_float(r.get("min"))
            if mins == 0 and not r.get("pts"):
                continue  # did not play
            out.append(GameLog(
                player_id=int(r["player"]["id"]), game_id=str(r["game"]["id"]),
                game_date=date.fromisoformat(r["game"]["date"][:10]), minutes=mins,
                pts=r.get("pts") or 0, reb=r.get("reb") or 0, ast=r.get("ast") or 0,
                stl=r.get("stl") or 0, blk=r.get("blk") or 0, tov=r.get("turnover") or 0,
                fgm=r.get("fgm") or 0, fga=r.get("fga") or 0, ftm=r.get("ftm") or 0,
                fta=r.get("fta") or 0, tpm=r.get("fg3m") or 0))
        return out

    def season_averages(self, season: str) -> dict[int, dict]:
        return {}  # skipped: ranks fall back to current-season averages once games are played
