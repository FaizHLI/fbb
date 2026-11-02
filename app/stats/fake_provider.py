"""Made-up but realistic data so you can try the whole app with no internet."""
import hashlib
import random
from datetime import date, timedelta

from .base import GameLog, PlayerInfo

TEAMS = ["ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW", "HOU", "IND", "LAC", "LAL", "MEM",
         "MIA", "MIL", "MIN", "NOP", "NYK", "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS"]
FIRST = ["Jalen", "Marcus", "Tyrese", "Devin", "Isaiah", "Darius", "Andre", "Kevin", "Jaylen", "Malik", "Trey",
         "Cam", "Derrick", "Miles", "Zion", "Evan", "Josh", "Paolo", "Scottie", "Franz"]
LAST = ["Walker", "Brooks", "Carter", "Hayes", "Mitchell", "Holmes", "Banks", "Porter", "Reed", "Bridges",
        "Murray", "Green", "Allen", "Fox", "Young", "Ball", "Hart", "Wells", "Turner", "Cole"]
POS = [["PG"], ["PG", "SG"], ["SG"], ["SG", "SF"], ["SF"], ["SF", "PF"], ["PF"], ["PF", "C"], ["C"]]


def _h(*parts) -> int:
    return int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest(), 16)


class FakeProvider:
    N_PLAYERS = 300

    def _player(self, i: int) -> tuple[PlayerInfo, float]:
        name = f"{FIRST[i % 20]} {LAST[(i * 7 + i // 20) % 20]}"
        if i >= 400:
            name += " Jr."
        info = PlayerInfo(1000 + i, name, TEAMS[i % 30], POS[_h("pos", i) % len(POS)],
                          "OUT" if i % 23 == 5 else "DTD" if i % 31 == 7 else "")
        skill = max(0.25, 1.6 - i / 160)  # lower index = better player
        return info, skill

    def players(self, season: str) -> list[PlayerInfo]:
        seen, out = set(), []
        for i in range(self.N_PLAYERS):
            info, _ = self._player(i)
            if info.name in seen:
                info.name += f" {chr(65 + i % 26)}."
            seen.add(info.name)
            out.append(info)
        return out

    def plays_on(self, team: str, d: date) -> bool:
        return _h("sched", team, d.isoformat()) % 7 < 3  # roughly 2-5 games a week

    def game_logs(self, season: str, start: date, end: date) -> list[GameLog]:
        out = []
        d = start
        while d <= end:
            for i in range(self.N_PLAYERS):
                info, skill = self._player(i)
                if not self.plays_on(info.nba_team, d) or _h("dnp", i, d) % 12 == 0:
                    continue
                r = random.Random(_h("g", i, d))
                fga = max(1, int(r.gauss(14 * skill, 3)))
                fgm = max(0, min(fga, int(fga * r.uniform(0.38, 0.58))))
                tpm = r.randint(0, max(0, fgm // 2))
                fta = max(0, int(r.gauss(4 * skill, 2)))
                ftm = max(0, min(fta, int(fta * r.uniform(0.65, 0.9))))
                out.append(GameLog(
                    player_id=info.id, game_id=f"{d:%Y%m%d}-{info.nba_team}", game_date=d,
                    minutes=round(r.uniform(18, 38) * min(1.0, skill), 1),
                    pts=2 * fgm + tpm + ftm, reb=max(0, int(r.gauss(5 * skill, 2))),
                    ast=max(0, int(r.gauss(4 * skill, 2))), stl=r.randint(0, 3), blk=r.randint(0, 2),
                    tov=r.randint(0, 4), fgm=fgm, fga=fga, ftm=ftm, fta=fta, tpm=tpm))
            d += timedelta(days=1)
        return out

    def schedule(self, start: date, end: date) -> list[tuple[str, date, str]]:
        out, d = [], start
        while d <= end:
            out += [(f"{d:%Y%m%d}-{t}", d, t) for t in TEAMS if self.plays_on(t, d)]
            d += timedelta(days=1)
        return out

    def season_averages(self, season: str) -> dict[int, dict]:
        out = {}
        for i in range(self.N_PLAYERS):
            info, skill = self._player(i)
            out[info.id] = {"pts": 22 * skill, "reb": 5 * skill, "ast": 4 * skill, "stl": 1.2, "blk": 0.8,
                            "tov": 2, "fgm": 8 * skill, "fga": 16 * skill, "ftm": 3 * skill, "fta": 4 * skill,
                            "tpm": 2 * skill, "gp": 60}
        return out
