"""Click through every page as a user and as the commissioner."""
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.jobs import lock_lineups, sync_stats
from app.main import app
from app.models import DraftPick, Matchup, Player, RosterSpot, Team
from app.services import draft, schedule
from app.stats.fake_provider import FakeProvider
from tests.conftest import START, set_now


def login(name):
    c = TestClient(app)
    r = c.post("/login", data={"username": name, "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return c


def ok(c, url):
    r = c.get(url)
    assert r.status_code == 200, (url, r.status_code, r.text[:500])
    assert "Traceback" not in r.text
    return r.text


def test_pages_through_season(db, league8):
    lg = league8
    anon = TestClient(app)
    assert anon.get("/", follow_redirects=False).status_code == 303
    assert anon.post("/login", data={"username": "user1", "password": "nope"}, follow_redirects=False).headers["location"] == "/login"

    boss = login("user1")
    u2 = login("user2")
    for url in ["/", "/draft", "/players", "/standings", "/power", "/scoreboard", "/more", "/activity",
                "/claims", "/trades", "/commish", "/team/1"]:
        ok(boss, url)
    assert u2.get("/commish", follow_redirects=False).status_code == 303  # not commish

    # Start the draft from the web
    boss.post("/commish/randomize-order")
    r = boss.post("/commish/start-draft", follow_redirects=False)
    assert r.headers["location"] == "/draft"
    db.expire_all()
    html = ok(boss, "/draft")
    assert "Round 1" in html
    ok(boss, "/draft/state")
    ok(boss, "/draft/board")
    assert "Draft" in ok(boss, "/draft/players?pos=C")

    # Everyone picks through the web, using the right logged-in client
    clients = {f"user{i}": (boss if i == 1 else u2 if i == 2 else login(f"user{i}")) for i in range(1, 9)}
    while True:
        db.expire_all()
        pick = draft.current_pick(db)
        if not pick:
            break
        uname = pick.team.user.username
        best = draft.best_available(db)
        r = clients[uname].post("/draft/pick", data={"player_id": best.id}, headers={"HX-Request": "true"})
        assert r.status_code == 200
    db.expire_all()
    assert db.scalar(select(Team).limit(1)) and draft.current_pick(db) is None

    t1 = db.scalar(select(Team).where(Team.name == "Team 1"))
    t2 = db.scalar(select(Team).where(Team.name == "Team 2"))
    html = ok(boss, f"/team/{t1.id}")
    assert "Save lineup" in html
    # save lineup form roundtrip (keep same)
    import re
    sel = {}
    for name, body in re.findall(r'<select name="(slot\d+)"(.*?)</select>', html, flags=re.S):
        m = re.search(r'<option value="(\d+)" selected', body)
        sel[name] = m.group(1) if m else ""
    assert sum(1 for v in sel.values() if v) >= 8
    r = boss.post(f"/team/{t1.id}/lineup/1", data=sel, follow_redirects=True)
    assert "Lineup saved" in r.text

    # add a free agent with a drop
    db.expire_all()
    fa = db.scalar(select(Player).where(Player.id.not_in(select(RosterSpot.player_id))).order_by(Player.rank))
    mine = [s.player_id for s in db.scalars(select(RosterSpot).where(RosterSpot.team_id == t1.id))]
    ok(boss, f"/players/{fa.id}")
    r = boss.post(f"/players/{fa.id}/add", data={"drop_id": str(mine[-1])}, follow_redirects=True)
    assert f"Added {fa.name}" in r.text
    # claim the dropped player as user2
    their = [s.player_id for s in db.scalars(select(RosterSpot).where(RosterSpot.team_id == t2.id))]
    r = u2.post(f"/players/{mine[-1]}/add", data={"drop_id": str(their[-1])}, follow_redirects=False)
    r = u2.get(r.headers["location"])
    assert "Claim submitted" in r.text
    # trade offer and accept
    db.expire_all()
    give = db.scalar(select(RosterSpot).where(RosterSpot.team_id == t1.id)).player_id
    get = db.scalar(select(RosterSpot).where(RosterSpot.team_id == t2.id, RosterSpot.player_id != their[-1])).player_id
    ok(boss, f"/trades/new?with_team={t2.id}")
    r = boss.post("/trades", data={"with_team": t2.id, "give": [give], "get": [get]}, follow_redirects=True)
    assert "Trade sent" in r.text
    assert '<span class="badge">1</span>' in ok(u2, "/more")
    tid = int(re.search(r'/trades/(\d+)/accept', ok(u2, "/trades")).group(1))
    u2.post(f"/trades/{tid}/accept")
    db.expire_all()
    assert db.scalar(select(RosterSpot).where(RosterSpot.player_id == give)).team_id == t2.id

    # Simulate weeks with stats and check pages render live and final states
    fake = FakeProvider()
    for week in range(1, 7):
        ws = START + timedelta(days=7 * (week - 1))
        set_now(f"{ws + timedelta(days=2)}T22:00:00")
        lock_lineups(db, lg)
        sync_stats(db, lg, days_back=4, provider=fake)
        db.commit()
        html = ok(boss, "/")
        assert "%" in html and "proj" in html
        mid = db.scalar(select(Matchup).where(Matchup.week == week)).id
        ok(boss, f"/matchup/{mid}")
        ok(boss, f"/team/{t1.id}?week={week}")
        ok(boss, f"/scoreboard?week={week}")
        set_now(f"{ws + timedelta(days=7)}T12:00:00")
        sync_stats(db, lg, days_back=7, provider=fake)
        schedule.finalize_due_weeks(db, lg)
        db.commit()
        for url in ["/standings", "/power", "/players?sort=avg", "/players?status=all&sort=recent&pos=PG",
                    "/activity", "/claims", "/commish", f"/players/{give}"]:
            ok(boss, url)
    html = ok(boss, "/standings")
    assert "Final" in html
    assert "▲" in ok(boss, "/power") or "▼" in ok(boss, "/power")


def test_commish_edits_login(db, league8):
    boss = login("user1")
    t2 = db.scalar(select(Team).where(Team.name == "Team 2"))
    r = boss.post("/commish/edit-login", data={"team_id": t2.id, "username": "Bob", "password": "newpass"},
                  follow_redirects=True)
    assert "Login updated for Team 2: bob" in r.text
    assert "taken" in boss.post("/commish/edit-login", data={"team_id": t2.id, "username": "user3"},
                                follow_redirects=True).text
    c = TestClient(app)
    assert c.post("/login", data={"username": "bob", "password": "newpass"},
                  follow_redirects=False).headers["location"] == "/"
    assert c.post("/login", data={"username": "user2", "password": "pw"},
                  follow_redirects=False).headers["location"] == "/login"


def test_commish_changes_start_date(db, league8):
    from app.services import draft as d
    lg = league8
    d.set_draft_order(db, lg)
    d.start_draft(db, lg)
    while lg.status == "drafting":
        pk = d.current_pick(db)
        d.make_pick(db, lg, pk.team, d.best_available(db).id)
    db.commit()
    boss = login("user1")
    assert "League settings" in boss.get("/commish").text
    bad = boss.post("/commish/settings", data={"start": "2026-10-20", "weeks": 22, "playoff_teams": 4},
                    follow_redirects=True)
    assert "must be a Monday" in bad.text
    r = boss.post("/commish/settings", data={"name": "New Name", "start": "2026-10-19", "weeks": 22,
                                             "playoff_teams": 4}, follow_redirects=True)
    assert "Settings saved" in r.text
    db.expire_all()
    from app.models import League
    lg2 = db.scalar(select(League))
    assert str(lg2.start_date) == "2026-10-19" and lg2.regular_season_weeks == 22 and lg2.name == "New Name"
    assert db.scalar(select(func.count(Matchup.id))) == 22 * 4


def test_player_search_views(db, league8):
    from app.services import draft as d
    lg = league8
    d.set_draft_order(db, lg)
    d.start_draft(db, lg)
    while lg.status == "drafting":
        pk = d.current_pick(db)
        d.make_pick(db, lg, pk.team, d.best_available(db).id)
    db.commit()
    fake = FakeProvider()
    set_now(f"{START + timedelta(days=10)}T20:00:00")
    sync_stats(db, lg, days_back=30, provider=fake)
    from app.jobs import sync_schedule
    sync_schedule(db, lg, fake, back=30, ahead=10)
    db.commit()
    c = login("user1")
    for view in ("season", "l7", "l15", "l30", "proj"):
        for sort in ("avg", "fpts", "pts", "tov", "rank"):
            html = ok(c, f"/players?view={view}&sort={sort}")
            assert "stat-tbl" in html
    html = ok(c, "/players?status=owned&team=BOS&healthy=1&pos=PG")
    assert "players" in html
    html = ok(c, "/players?view=proj")
    assert "PROJ" in html and "LEFT" in html
    r = c.get("/players?q=a&view=l7", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "<html" not in r.text and "stat-tbl" in r.text
