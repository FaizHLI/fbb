"""Auto lineup (projections + optimizer), IR rules, and the column auto-migration."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

from app import config
from app.db import _add_missing_columns
from app.jobs import sync_schedule, sync_stats
from app.main import app
from app.models import Player, PlayerGame, RosterSpot, Team, TeamGame
from app.services import draft, lineups, roster
from app.services.common import LeagueError
from app.services.projections import best_lineup, project
from app.stats.fake_provider import FakeProvider
from tests.conftest import START, set_now


def drafted(db, lg):
    draft.set_draft_order(db, lg)
    draft.start_draft(db, lg)
    while lg.status == "drafting":
        pk = draft.current_pick(db)
        draft.make_pick(db, lg, pk.team, draft.best_available(db).id)
    db.commit()
    return db.scalar(select(Team).where(Team.name == "Team 1"))


def test_optimizer_is_exact():
    """Greedy would put the big C/PF into C and lose the PF slot; the best answer uses everyone."""
    def P(i, pos):
        return Player(id=i, name=f"P{i}", positions=pos)
    players = [P(1, "PG"), P(2, "SG"), P(3, "SF"), P(4, "PF,C"), P(5, "C"), P(6, "PG,SG"), P(7, "SF,PF"),
               P(8, "PG"), P(9, "SG"), P(10, "C"), P(11, "PF")]
    value = {1: 30, 2: 30, 3: 30, 4: 50, 5: 20, 6: 25, 7: 25, 8: 22, 9: 21, 10: 18, 11: 5}
    pick = best_lineup(players, value)
    assert len(pick) == len(config.STARTER_SLOTS)
    assert set(pick.values()) == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}  # all but the 5-point PF
    for slot, pid in pick.items():
        assert lineups.eligible(next(p for p in players if p.id == pid), config.STARTER_SLOTS[slot])


def test_projection_uses_schedule_and_availability(db, league8):
    lg = league8
    t1 = drafted(db, lg)
    fake = FakeProvider()
    set_now(f"{START + timedelta(days=13)}T20:00:00")   # Sunday of week 2
    sync_stats(db, lg, days_back=20, provider=fake)
    sync_schedule(db, lg, fake, back=20, ahead=10)
    db.commit()
    players = lineups.roster_players(db, t1.id)
    proj = project(db, lg, players, 3)
    star = players[0]
    sched = list(db.scalars(select(TeamGame).where(TeamGame.team == star.nba_team,
                                                   TeamGame.game_date >= START + timedelta(days=14),
                                                   TeamGame.game_date <= START + timedelta(days=20))))
    assert proj[star.id].known_schedule and proj[star.id].games_left == len(sched)
    # A player who sat out his team's last 3 games projects near zero.
    sitter = players[1]
    db.query(PlayerGame).filter(PlayerGame.player_id == sitter.id,
                                PlayerGame.game_date >= START).delete()
    db.commit()
    proj2 = project(db, lg, players, 3)
    assert proj2[sitter.id].play_rate <= 0.05
    assert proj2[sitter.id].expected < proj2[sitter.id].ppg * 0.5


def test_auto_endpoint(db, league8):
    lg = league8
    t1 = drafted(db, lg)
    fake = FakeProvider()
    set_now(f"{START + timedelta(days=13)}T20:00:00")
    sync_stats(db, lg, days_back=20, provider=fake)
    sync_schedule(db, lg, fake, back=20, ahead=10)
    db.commit()
    c = TestClient(app)
    c.post("/login", data={"username": "user1", "password": "pw"})
    page = c.get(f"/team/{t1.id}?week=3").text
    assert "Auto lineup" in page and "IR" in page
    r = c.post(f"/team/{t1.id}/lineup/3/auto", headers={"Accept": "application/json"})
    j = r.json()
    assert j["ok"] and j["projected"] > 0
    assert sum(1 for x in j["lineup"] if x) >= 8
    saved = lineups.preview_lineup(db, t1.id, 3)
    assert [saved.get(i) for i in range(len(config.STARTER_SLOTS))] == j["lineup"]
    # can't auto a locked week or someone else's team
    assert not c.post(f"/team/{t1.id}/lineup/1/auto", headers={"Accept": "application/json"}).json()["ok"]
    t2 = db.scalar(select(Team).where(Team.name == "Team 2"))
    assert c.post(f"/team/{t2.id}/lineup/3/auto", headers={"Accept": "application/json"}).status_code == 403


def test_ir_rules(db, league8):
    lg = league8
    t1 = drafted(db, lg)
    mine = lineups.roster_players(db, t1.id)
    hurt, healthy = mine[3], mine[4]
    hurt.injury, healthy.injury = "OUT", ""
    db.commit()
    with pytest.raises(LeagueError):
        roster.move_to_ir(db, lg, t1, healthy.id)          # not OUT
    roster.move_to_ir(db, lg, t1, hurt.id)
    assert roster.roster_count(db, t1.id) == config.ROSTER_MAX - 1
    assert hurt.id not in lineups.preview_lineup(db, t1.id, 1).values()
    # open spot lets you add a free agent without dropping
    fa = db.scalar(select(Player).where(Player.id.not_in(select(RosterSpot.player_id))).order_by(Player.rank))
    roster.add_free_agent(db, lg, t1, fa.id)
    assert roster.roster_count(db, t1.id) == config.ROSTER_MAX
    # second IR guy: IR full
    other = mine[5]
    other.injury = "OUT"
    with pytest.raises(LeagueError):
        roster.move_to_ir(db, lg, t1, other.id)
    # he gets healthy: can't add until fixed, can't activate while roster full
    hurt.injury = ""
    fa2 = db.scalar(select(Player).where(Player.id.not_in(select(RosterSpot.player_id))).order_by(Player.rank))
    with pytest.raises(LeagueError, match="still on IR"):
        roster.add_free_agent(db, lg, t1, fa2.id, drop_id=mine[-1].id)
    with pytest.raises(LeagueError, match="roster is full"):
        roster.activate_from_ir(db, lg, t1, hurt.id)
    roster.drop_player(db, lg, t1, mine[-1].id)
    roster.activate_from_ir(db, lg, t1, hurt.id)
    assert roster.roster_count(db, t1.id) == config.ROSTER_MAX
    db.commit()


def test_ir_web(db, league8):
    lg = league8
    t1 = drafted(db, lg)
    p = lineups.roster_players(db, t1.id)[2]
    p.injury = "OUT"
    db.commit()
    c = TestClient(app)
    c.post("/login", data={"username": "user1", "password": "pw"})
    assert "Move to IR" in c.get(f"/players/{p.id}").text
    r = c.post("/team/ir", data={"player_id": p.id, "action": "ir"}, follow_redirects=True)
    assert "moved to IR" in r.text
    assert "Activate from IR" in c.get(f"/players/{p.id}").text


def test_add_missing_columns(tmp_path):
    """An old database without the new columns gets them added on startup."""
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE players (id INTEGER PRIMARY KEY, name VARCHAR(80))"))
        conn.execute(text("INSERT INTO players (id, name) VALUES (1, 'Old Guy')"))
        conn.execute(text("CREATE TABLE roster_spots (id INTEGER PRIMARY KEY, team_id INTEGER, player_id INTEGER)"))
    _add_missing_columns(eng)
    cols = {c["name"] for c in inspect(eng).get_columns("players")}
    assert {"injury", "rank", "positions"} <= cols
    with eng.connect() as conn:
        assert conn.execute(text("SELECT injury FROM players")).scalar() == ""
        assert "on_ir" in {c["name"] for c in inspect(eng).get_columns("roster_spots")}


def test_make_room_flow(db, league8):
    lg = league8
    t1 = drafted(db, lg)
    c = TestClient(app)
    c.post("/login", data={"username": "user1", "password": "pw"})
    fa = db.scalar(select(Player).where(Player.id.not_in(select(RosterSpot.player_id))).order_by(Player.rank))
    # full roster: Add sends you to the make-room page
    r = c.post(f"/players/{fa.id}/add", follow_redirects=False)
    assert r.headers["location"] == f"/make-room?pid={fa.id}&action=add"
    page = c.get(r.headers["location"]).text
    assert "Make room" in page and "DROP" in page and fa.name in page
    victim = lineups.roster_players(db, t1.id)[-1]
    r = c.post(f"/players/{fa.id}/add", data={"drop_id": victim.id}, follow_redirects=True)
    assert f"Added {fa.name}" in r.text
    db.expire_all()
    assert roster.owner_id(db, fa.id) == t1.id and roster.owner_id(db, victim.id) is None

    # IR activation with a full roster
    hurt = lineups.roster_players(db, t1.id)[2]
    hurt.injury = "OUT"
    db.commit()
    roster.move_to_ir(db, lg, t1, hurt.id)
    from app.models import WaiverHold
    fa2 = db.scalar(select(Player).where(Player.id.not_in(select(RosterSpot.player_id)),
                                         Player.id.not_in(select(WaiverHold.player_id))).order_by(Player.rank))
    roster.add_free_agent(db, lg, t1, fa2.id)
    hurt.injury = ""
    db.commit()
    j = c.post("/team/ir", data={"player_id": hurt.id, "action": "activate"}, headers={"Accept": "application/json"}).json()
    assert j["need_drop"] and "action=activate" in j["url"]
    assert "Activating" in c.get(j["url"]).text
    out = lineups.roster_players(db, t1.id)[-1]
    r = c.post("/team/ir", data={"player_id": hurt.id, "action": "activate", "drop_id": out.id}, follow_redirects=True)
    assert "activated" in r.text
    db.expire_all()
    assert roster.roster_count(db, t1.id) == config.ROSTER_MAX and not lineups.ir_players(db, t1.id)
    assert roster.owner_id(db, out.id) is None
