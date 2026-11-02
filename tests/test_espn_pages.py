"""ESPN Schedule and Rosters page imports (made-up teams/players in ESPN's copied-text format)."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import config
from app.cli import create_league
from app.importer import (import_league, import_rosters, import_schedule, parse_recap, parse_rosters,
                          parse_schedule, parse_teams)
from app.main import app
from app.models import Matchup, Player, RosterSpot, Team
from app.services import lineups, schedule
from app.services.common import LeagueError
from app.weeks import current_week, total_weeks, week_end, week_for_date, week_label, week_start
from tests.conftest import set_now
from tests.test_import import TEAMS, TEAMS_CSV, recap_text

NAMES = [n for n, _ in TEAMS]
HEADER = "Edit\nAWAY TEAM\n\t\nTEAM MANAGER(S)\n\t\nScore\n\t\nScore\n\t\nTEAM MANAGER(S)\n\t\nHOME TEAM\n"


def sched_block(title, pairs, scores=None):
    out = [title, HEADER]
    for k, (a, h) in enumerate(pairs):
        out += [a, "(0-1-0)", "\t", "Manager A", "\t"]
        if scores:
            out += [str(scores[k][0]), "\t", str(scores[k][1]), "\t"]
        else:
            out += ["\t", "\t"]
        out += ["Manager H", "\t", h, "(1-0-0)"]
    return "\n".join(out)


def schedule_text():
    p1 = [(NAMES[0], NAMES[1]), (NAMES[2], NAMES[3]), (NAMES[4], NAMES[5]), (NAMES[6], NAMES[7])]
    p2 = [(NAMES[0], NAMES[2]), (NAMES[1], NAMES[3]), (NAMES[4], NAMES[6]), (NAMES[5], NAMES[7])]
    p3 = [(NAMES[0], NAMES[3]), (NAMES[1], NAMES[2]), (NAMES[4], NAMES[7]), (NAMES[5], NAMES[6])]
    return "\n".join([
        sched_block("Matchup 1 (Oct 20 - 25)", p1, [(1264, 1306), (1089, 1132), (1246, 985), (1359, 1004)]),
        sched_block("Matchup 2 (Oct 26 - Nov 1)", p2),
        sched_block("Matchup 3 (Nov 30 - Dec 13)", p3),     # a two-week matchup
        "Playoff Round 1 (Dec 14 - 27)Matchups to be determined",
        "Playoff Round 2 (Dec 28 - Jan 10)Matchups to be determined",
    ])


def roster_text(rosters):
    out = ["Menu", "League Rosters"]
    for team, entries in rosters.items():
        out += [team, "(1-0-0)", "SLOT", "\t", "PLAYER", "\t", "ACQ"]
        for slot, name, tp, tag in entries:
            out += [slot, "\t"]
            if name is None:
                out += ["Empty", "\t"]
            else:
                out += [name + name, name + tag, tp, "\t", "Draft"]
        out += ["View Team", "Propose Trade"]
    return "\n".join(out)


@pytest.fixture()
def imported(db):
    set_now("2026-10-28T12:00:00")
    lg = create_league(db, "Hoops", "2026-27", date(2026, 10, 19))
    import_league(db, parse_teams(TEAMS_CSV), parse_recap(recap_text()), "DEL")
    db.commit()
    return lg


def test_parse_schedule_dates_and_scores():
    ps = parse_schedule(schedule_text(), NAMES, "2026-27")
    assert [(p.start, p.end, p.playoff_round) for p in ps] == [
        (date(2026, 10, 20), date(2026, 10, 25), None), (date(2026, 10, 26), date(2026, 11, 1), None),
        (date(2026, 11, 30), date(2026, 12, 13), None), (date(2026, 12, 14), date(2026, 12, 27), 1),
        (date(2026, 12, 28), date(2027, 1, 10), 2)]
    g = ps[0].games[0]
    assert (g.away, g.home, g.away_score, g.home_score) == (NAMES[0], NAMES[1], 1264, 1306)
    assert ps[1].games[0].away_score is None


def test_import_schedule(db, imported):
    lg = imported
    res = import_schedule(db, lg, parse_schedule(schedule_text(), NAMES, "2026-27"))
    db.commit()
    assert res == {"weeks": 3, "playoff_rounds": 2, "results": 4}
    assert lg.regular_season_weeks == 3 and lg.playoff_teams == 4 and total_weeks(lg) == 5
    assert week_start(lg, 1) == date(2026, 10, 20) and week_end(lg, 3) == date(2026, 12, 13)
    assert week_for_date(lg, date(2026, 12, 1)) == 3 and week_for_date(lg, date(2026, 12, 20)) == 4
    assert week_for_date(lg, date(2026, 11, 15)) == 3  # gap before matchup 3 belongs to it
    assert week_label(lg, 1).startswith("Matchup 1")
    assert current_week(lg) == 2
    finals = list(db.scalars(select(Matchup).where(Matchup.final.is_(True))))
    assert len(finals) == 4
    table = {s.team.name: s for s in schedule.standings(db, lg)}
    assert table[NAMES[1]].record == "1-0" and table[NAMES[0]].record == "0-1"
    assert table[NAMES[1]].pf == 1306
    # bad: unknown team
    bad = schedule_text().replace(NAMES[7], "Nobody FC")
    with pytest.raises(LeagueError):
        import_schedule(db, lg, parse_schedule(bad, NAMES + ["Nobody FC"], "2026-27"))


def test_import_rosters(db, imported):
    lg = imported
    import_schedule(db, lg, parse_schedule(schedule_text(), NAMES, "2026-27"))
    db.commit()
    drafted = {t: [] for t in NAMES}
    for p in parse_recap(recap_text()):
        drafted[p.team].append(p)
    rosters = {}
    for t in NAMES:
        ps = drafted[t]
        slots = list(config.STARTER_SLOTS)
        entries = []
        for k, slot in enumerate(slots):
            entries.append((slot, None, None, "") if k == 4 else
                           (slot, ps[k].name, f"{ps[k].nba_team}{ps[k].position}", ""))
        entries += [("Bench", ps[k].name, f"{ps[k].nba_team}{ps[k].position}", "") for k in (10, 11)]
        entries.append(("Bench", "Brand New Guy" if t == NAMES[0] else ps[12].name, "SAC", "DTD"))
        entries.append(("IR", ps[4].name, f"{ps[4].nba_team}{ps[4].position}", "O") if t == NAMES[3]
                       else ("IR", None, None, ""))
        rosters[t] = entries
    parsed = parse_rosters(roster_text(rosters), NAMES)
    assert parsed[NAMES[0]][-1].nba_team == "SA" or parsed[NAMES[0]][-1].nba_team == "SAS"
    res = import_rosters(db, lg, parsed)
    db.commit()
    assert "Brand New Guy" in res["placeholders"] and res["week"] == 2 and res["lineups"] == 8
    new_guy = db.scalar(select(Player).where(Player.name == "Brand New Guy"))
    assert new_guy.positions == "C" and new_guy.injury == "DTD"
    dlt = db.scalar(select(Team).where(Team.name == NAMES[3]))
    spot = db.scalar(select(RosterSpot).where(RosterSpot.team_id == dlt.id, RosterSpot.on_ir.is_(True)))
    assert spot and spot.player.injury == "OUT"
    lu = lineups.preview_lineup(db, dlt.id, 2)
    assert lu.get(4) is None and sum(1 for v in lu.values() if v) == 9  # ESPN's empty C slot kept
    assert db.scalar(select(func.count(RosterSpot.id))) == 8 * 12 + 1  # 9 starters + 3 bench each, plus one IR


def test_web_imports(db, imported):
    c = TestClient(app)
    c.post("/login", data={"username": "del", "password": "x"})
    # use the commissioner's generated login: reset it first
    from app.auth import hash_password
    from app.models import User
    u = db.scalar(select(User).where(User.username == "del"))
    u.password_hash = hash_password("pw")
    db.commit()
    c.post("/login", data={"username": "del", "password": "pw"})
    page = c.get("/commish").text
    assert "Import ESPN schedule" in page and "Import ESPN rosters" in page
    r = c.post("/commish/import-schedule", data={"schedule": schedule_text()}, follow_redirects=True)
    assert "Imported 3 matchups, 2 playoff rounds, 4 past results" in r.text
    assert "League settings" not in r.text  # dates come from ESPN now
    mid = db.scalar(select(Matchup).where(Matchup.week == 1)).id
    assert "Result imported from ESPN" in c.get(f"/matchup/{mid}").text
    assert "Matchup 2" in c.get("/").text
