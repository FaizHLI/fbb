"""Free stats from stats.nba.com through the `nba_api` package.

Note: stats.nba.com blocks many cloud/datacenter IPs. If syncing hangs or times
out on your host, run the sync job from a home computer, or switch to balldontlie.
"""
import time
from datetime import date, datetime

from .base import GameLog, PlayerInfo, expand_positions, minutes_to_float

TIMEOUT = 60


def _rows(endpoint) -> list[dict]:
    data = endpoint.get_normalized_dict()
    return next(iter(data.values())) if data else []


class NbaApiProvider:
    def players(self, season: str) -> list[PlayerInfo]:
        from nba_api.stats.endpoints import playerindex
        rows = _rows(playerindex.PlayerIndex(season=season, timeout=TIMEOUT))
        out = []
        for r in rows:
            if r.get("ROSTER_STATUS") in (0, "0"):
                continue
            positions = expand_positions(r.get("POSITION") or "")
            if not positions:
                continue
            name = f"{r.get('PLAYER_FIRST_NAME', '')} {r.get('PLAYER_LAST_NAME', '')}".strip()
            out.append(PlayerInfo(int(r["PERSON_ID"]), name, r.get("TEAM_ABBREVIATION") or "", positions))
        return out

    def game_logs(self, season: str, start: date, end: date) -> list[GameLog]:
        from nba_api.stats.endpoints import playergamelogs
        rows = _rows(playergamelogs.PlayerGameLogs(
            season_nullable=season,
            season_type_nullable="Regular Season",
            date_from_nullable=start.strftime("%m/%d/%Y"),
            date_to_nullable=end.strftime("%m/%d/%Y"),
            timeout=TIMEOUT,
        ))
        time.sleep(0.6)  # be polite to stats.nba.com
        out = []
        for r in rows:
            gd = r["GAME_DATE"][:10]
            out.append(GameLog(
                player_id=int(r["PLAYER_ID"]), game_id=str(r["GAME_ID"]),
                game_date=datetime.strptime(gd, "%Y-%m-%d").date(),
                minutes=minutes_to_float(r.get("MIN")),
                pts=int(r.get("PTS") or 0), reb=int(r.get("REB") or 0), ast=int(r.get("AST") or 0),
                stl=int(r.get("STL") or 0), blk=int(r.get("BLK") or 0), tov=int(r.get("TOV") or 0),
                fgm=int(r.get("FGM") or 0), fga=int(r.get("FGA") or 0),
                ftm=int(r.get("FTM") or 0), fta=int(r.get("FTA") or 0), tpm=int(r.get("FG3M") or 0),
            ))
        return out

    def season_averages(self, season: str) -> dict[int, dict]:
        from nba_api.stats.endpoints import leaguedashplayerstats
        rows = _rows(leaguedashplayerstats.LeagueDashPlayerStats(
            season=season, per_mode_detailed="PerGame", timeout=TIMEOUT))
        keys = {"pts": "PTS", "reb": "REB", "ast": "AST", "stl": "STL", "blk": "BLK", "tov": "TOV",
                "fgm": "FGM", "fga": "FGA", "ftm": "FTM", "fta": "FTA", "tpm": "FG3M"}
        return {int(r["PLAYER_ID"]): {k: float(r.get(v) or 0) for k, v in keys.items()} | {"gp": int(r.get("GP") or 0)}
                for r in rows}
