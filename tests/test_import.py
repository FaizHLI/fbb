"""ESPN import, using made-up teams and players in the ESPN draft recap format."""
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.auth import hash_password
from app.cli import create_league
from app.importer import import_league, norm_name, parse_recap, parse_teams
from app.jobs import sync_players
from app.main import app
from app.models import DraftPick, Matchup, Player, RosterSpot, Team, User
from app.services.lineups import preview_lineup
from app.stats.base import PlayerInfo
from tests.conftest import START

TEAMS = [("Alpha Squad", "ALP"), ("Bravo Ballers", "BRV"), ("Charlie Hustle", "CHL"), ("Delta Dunks", "DEL"),
         ("Echo Chamber", "ECH"), ("Foxtrot Five", "FOX"), ("Golf Course", "GLF"), ("Hotel Lobby", "HTL")]
TEAMS_CSV = "name,abbrev,manager\n" + "\n".join(f"{n},{a},someone{i}" for i, (n, a) in enumerate(TEAMS))
POS = ["PG", "SG", "SF", "PF", "C"]
NBA = ["DEN", "SA", "NY", "GS", "NO", "WSH", "UTAH", "BOS"]
SPECIAL = {0: "Zoran Jović", 1: "Marcus Lee Jr.", 2: "De'Andre Hill"}


def player_name(i):
    return SPECIAL.get(i, f"Player Number{i:03d}")


def recap_text(rounds=13):
    out = []
    order = [n for n, _ in TEAMS]
    for r in range(1, rounds + 1):
        out += [f"Round {r}", "NO.", "", "Player", "", "Team"]
        teams = order if r % 2 else list(reversed(order))
        for k, team in enumerate(teams, start=1):
            i = (r - 1) * 8 + k - 1
            out += [str(k), "", f"{player_name(i)} {NBA[i % 8]}, {POS[i % 5]}", "", team]
    return "\n".join(out)


class Provider:
    def players(self, season):
        names = {0: "Zoran Jovic", 1: "Marcus Lee"}  # spelled differently than ESPN
        out = [PlayerInfo(5000 + i, names.get(i, player_name(i)), "XXX", [POS[i % 5]]) for i in range(104)]
        return out + [PlayerInfo(9999, "Free Agent Guy", "BOS", ["PG"])]

    def season_averages(self, season):
        return {}

    def game_logs(self, *a):
        return []


def test_parse_recap():
    picks = parse_recap(recap_text())
    assert len(picks) == 104
    assert (picks[0].name, picks[0].nba_team, picks[0].position, picks[0].team) == \
        ("Zoran Jović", "DEN", "PG", "Alpha Squad")
    assert picks[-1].round == 13 and picks[-1].team == "Hotel Lobby"
    assert norm_name("Marcus Lee Jr.") == norm_name("Marcus Lee")
    assert norm_name("Zoran Jović") == "zoran jovic"
    assert norm_name("De'Andre Hill") == norm_name("DeAndre Hill")


def test_import_then_link(db):
    lg = create_league(db, "Hoops", "2026-27", START, weeks=20, playoff_teams=4)
    logins, placeholders = import_league(db, parse_teams(TEAMS_CSV), parse_recap(recap_text()), "DEL")
    db.commit()
    assert len(logins) == 8 and len(placeholders) == 104
    assert lg.status == "season"
    mine = db.scalar(select(Team).where(Team.abbrev == "DEL"))
    assert mine.draft_slot == 4 and mine.user.username == "del" and mine.user.is_commish
    assert db.scalar(select(func.count(User.id)).where(User.is_commish.is_(True))) == 1
    assert db.scalar(select(func.count(RosterSpot.id))) == 104
    assert db.scalar(select(func.count(Matchup.id))) == 80
    first = db.scalar(select(Team).where(Team.abbrev == "ALP"))
    assert first.waiver_priority == 8
    assert len([v for v in preview_lineup(db, mine.id, 1).values() if v]) >= 8

    sync_players(db, lg, Provider())
    db.commit()
    assert db.scalar(select(func.count(Player.id)).where(Player.id < 0)) == 0
    z = db.scalar(select(Player).where(Player.name == "Zoran Jovic"))
    assert db.scalar(select(RosterSpot).where(RosterSpot.player_id == z.id)).team_id == first.id
    assert db.scalar(select(DraftPick).where(DraftPick.overall == 1)).player_id == z.id
    assert db.scalar(select(func.count(RosterSpot.id))) == 104


def test_import_matches_synced_players(db):
    lg = create_league(db, "Hoops", "2026-27", START)
    sync_players(db, lg, Provider())
    _, placeholders = import_league(db, parse_teams(TEAMS_CSV), parse_recap(recap_text()), "ALP")
    assert placeholders == []


def test_web_import(db):
    create_league(db, "Hoops", "2026-27", START)
    db.add(User(username="boss", password_hash=hash_password("pw"), is_commish=True))
    db.commit()
    c = TestClient(app)
    c.post("/login", data={"username": "boss", "password": "pw"})
    assert "Import from ESPN" in c.get("/commish").text
    r = c.post("/commish/import", data={"teams": TEAMS_CSV, "draft": recap_text(), "commish": "BRV"})
    assert r.status_code == 200 and "only shown once" in r.text
    db.expire_all()
    boss = db.scalar(select(User).where(User.username == "boss"))
    assert boss.team.abbrev == "BRV"
    assert not db.scalar(select(User).where(User.username == "brv"))
    assert c.get(f"/team/{boss.team.id}").status_code == 200
