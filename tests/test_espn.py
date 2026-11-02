"""ESPN parser against a sample shaped like ESPN's public API responses."""
from datetime import date

from app.stats.espn_provider import completed_events, parse_box, parse_roster

SUMMARY = {"boxscore": {"players": [{
    "team": {"abbreviation": "DEN"},
    "statistics": [{
        "names": ["MIN", "FG", "3PT", "FT", "OREB", "DREB", "REB", "AST", "STL", "BLK", "TO", "PF", "+/-", "PTS"],
        "athletes": [
            {"athlete": {"id": "3112335", "displayName": "Nikola Jokic"},
             "stats": ["36", "12-20", "2-5", "6-7", "3", "10", "13", "11", "2", "1", "3", "2", "+10", "32"]},
            {"athlete": {"id": "1"}, "didNotPlay": True, "stats": []},
            {"athlete": {"id": "2"}, "stats": ["0", "0-0", "0-0", "0-0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0"]},
        ]}]}]}}
SCOREBOARD = {"events": [
    {"id": "401", "date": "2026-11-03T00:30Z", "status": {"type": {"state": "post", "completed": True}}},
    {"id": "402", "date": "2026-11-03T03:00Z", "status": {"type": {"state": "in", "completed": False}}},
]}
ROSTER = {"athletes": [{"id": "3112335", "displayName": "Nikola Jokic", "position": {"abbreviation": "C"}},
                       {"id": "9", "fullName": "Some Guard", "position": {"abbreviation": "G"}}]}


def test_parse_box():
    logs = parse_box(SUMMARY, "401", date(2026, 11, 2))
    assert len(logs) == 1
    g = logs[0]
    assert (g.player_id, g.pts, g.reb, g.ast, g.fgm, g.fga, g.tpm, g.ftm, g.fta, g.tov) == \
        (3112335, 32, 13, 11, 12, 20, 2, 6, 7, 3)


def test_labels_variant_and_events():
    s = {"boxscore": {"players": [{"statistics": [dict(SUMMARY["boxscore"]["players"][0]["statistics"][0])]}]}}
    s["boxscore"]["players"][0]["statistics"][0]["labels"] = s["boxscore"]["players"][0]["statistics"][0].pop("names")
    assert len(parse_box(s, "401", date(2026, 11, 2))) == 1
    assert completed_events(SCOREBOARD) == [("401", date(2026, 11, 2))]  # 7:30pm ET on Nov 2


def test_roster():
    ps = parse_roster(ROSTER, "SA")
    assert [(p.name, p.positions, p.nba_team) for p in ps] == [
        ("Nikola Jokic", ["C"], "SAS"), ("Some Guard", ["PG", "SG"], "SAS")]
