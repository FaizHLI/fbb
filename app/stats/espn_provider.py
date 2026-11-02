"""Free stats from ESPN's public site API (no key). Names match ESPN fantasy,
so imported drafts line up exactly. Unofficial, so the format can change; the
parser checks what it needs and fails with a clear message if it's missing."""
from datetime import date, datetime, timedelta

import httpx

from .. import config
from .base import GameLog, PlayerInfo, expand_positions, injury_code, minutes_to_float

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
TEAM_FIX = {"SA": "SAS", "NY": "NYK", "GS": "GSW", "NO": "NOP", "WSH": "WAS", "UTAH": "UTA", "PHO": "PHX"}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; hoops-league/1.0)", "Accept": "application/json"}


def _team(abbr: str) -> str:
    return TEAM_FIX.get(abbr, abbr)


def _made_att(v: str) -> tuple[int, int]:
    try:
        m, a = str(v).split("-")
        return int(m), int(a)
    except ValueError:
        return 0, 0


def _int(v) -> int:
    try:
        return int(str(v).replace("+", ""))
    except ValueError:
        return 0


def parse_box(summary: dict, game_id: str, game_date: date) -> list[GameLog]:
    """Turn an ESPN summary (boxscore) into game logs for players who played."""
    teams = (summary.get("boxscore") or {}).get("players")
    if teams is None:
        raise ValueError("ESPN summary has no boxscore.players")
    out = []
    for team in teams:
        for block in team.get("statistics", []):
            cols = [str(c).upper() for c in (block.get("labels") or block.get("names") or [])]
            if not cols:
                continue
            idx = {c: i for i, c in enumerate(cols)}
            missing = {"MIN", "FG", "3PT", "FT", "REB", "AST", "STL", "BLK", "TO", "PTS"} - set(idx)
            if missing:
                raise ValueError(f"ESPN boxscore missing columns {sorted(missing)}; got {cols}")
            for row in block.get("athletes", []):
                stats = row.get("stats") or []
                if row.get("didNotPlay") or len(stats) < len(cols):
                    continue
                mins = minutes_to_float(stats[idx["MIN"]])
                if mins <= 0:
                    continue
                fgm, fga = _made_att(stats[idx["FG"]])
                tpm, _ = _made_att(stats[idx["3PT"]])
                ftm, fta = _made_att(stats[idx["FT"]])
                out.append(GameLog(
                    player_id=int(row["athlete"]["id"]), game_id=str(game_id), game_date=game_date, minutes=mins,
                    pts=_int(stats[idx["PTS"]]), reb=_int(stats[idx["REB"]]), ast=_int(stats[idx["AST"]]),
                    stl=_int(stats[idx["STL"]]), blk=_int(stats[idx["BLK"]]), tov=_int(stats[idx["TO"]]),
                    fgm=fgm, fga=fga, ftm=ftm, fta=fta, tpm=tpm))
    return out


def completed_events(scoreboard: dict) -> list[tuple[str, date]]:
    """(event id, US Eastern game date) for finished games."""
    out = []
    for ev in scoreboard.get("events", []):
        status = ((ev.get("status") or {}).get("type") or {})
        if not (status.get("completed") or status.get("state") == "post"):
            continue
        when = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(config.LEAGUE_TZ).date()
        out.append((str(ev["id"]), when))
    return out


def parse_schedule(scoreboard: dict) -> list[tuple[str, date, str]]:
    """(event id, US Eastern date, team) for every scheduled or played game."""
    out = []
    for ev in scoreboard.get("events", []):
        when = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(config.LEAGUE_TZ).date()
        for comp in (ev.get("competitions") or [{}])[:1]:
            for c in comp.get("competitors", []):
                abbr = ((c.get("team") or {}).get("abbreviation")) or ""
                if abbr:
                    out.append((str(ev["id"]), when, _team(abbr)))
    return out


def parse_roster(body: dict, team_abbr: str) -> list[PlayerInfo]:
    athletes = body.get("athletes", [])
    if athletes and isinstance(athletes[0], dict) and "items" in athletes[0]:  # grouped by position
        athletes = [a for grp in athletes for a in grp.get("items", [])]
    out = []
    for a in athletes:
        pos = expand_positions(((a.get("position") or {}).get("abbreviation")) or "")
        name = a.get("displayName") or a.get("fullName")
        inj = a.get("injuries") or []
        status = (inj[0].get("status") or "") if inj and isinstance(inj[0], dict) else ""
        if pos and name:
            out.append(PlayerInfo(int(a["id"]), name, _team(team_abbr), pos, injury_code(status)))
    return out


class EspnProvider:
    def __init__(self):
        self.client = httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True)

    def _get(self, path: str, **params) -> dict:
        r = self.client.get(f"{BASE}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def players(self, season: str) -> list[PlayerInfo]:
        body = self._get("/teams")
        teams = [t["team"] for t in body["sports"][0]["leagues"][0]["teams"]]
        out = []
        for t in teams:
            out += parse_roster(self._get(f"/teams/{t['id']}/roster"), t.get("abbreviation", ""))
        if not out:
            raise ValueError("ESPN returned no players")
        return out

    def game_logs(self, season: str, start: date, end: date) -> list[GameLog]:
        out = []
        d = start
        while d <= end:
            for event_id, game_date in completed_events(self._get("/scoreboard", dates=d.strftime("%Y%m%d"), seasontype=2)):
                out += parse_box(self._get("/summary", event=event_id), event_id, game_date)
            d += timedelta(days=1)
        return out

    def season_averages(self, season: str) -> dict[int, dict]:
        return {}  # ranks come from this season's games once synced

    def schedule(self, start: date, end: date) -> list[tuple[str, date, str]]:
        out = []
        d = start
        while d <= end:
            out += parse_schedule(self._get("/scoreboard", dates=d.strftime("%Y%m%d"), seasontype=2))
            d += timedelta(days=1)
        return out
