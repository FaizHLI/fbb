"""End-to-end season with fake stats: draft -> lineups -> moves -> trades -> playoffs."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app import config
from app.models import DraftPick, LineupSlot, Matchup, Player, PlayerGame, RosterSpot, Team, WaiverClaim
from app.scoring import team_week
from app.services import analytics, draft, lineups, roster, schedule, trades
from app.services.common import LeagueError
from app.stats.fake_provider import FakeProvider
from app.jobs import sync_stats, lock_lineups
from tests.conftest import START, set_now


def run_draft(db, lg):
    draft.set_draft_order(db, lg)
    draft.start_draft(db, lg)
    while lg.status == "drafting":
        pick = draft.current_pick(db)
        draft.make_pick(db, lg, pick.team, draft.best_available(db).id)
    db.commit()


def test_snake_order_and_rules(db, league8):
    lg = league8
    draft.set_draft_order(db, lg)
    draft.start_draft(db, lg)
    picks = list(db.scalars(select(DraftPick).order_by(DraftPick.overall)))
    assert len(picks) == 8 * config.ROSTER_MAX
    assert picks[7].team_id == picks[8].team_id  # snake turn
    assert picks[0].team_id == picks[15].team_id
    wrong = db.get(Team, picks[1].team_id)
    with pytest.raises(LeagueError):
        draft.make_pick(db, lg, wrong, draft.best_available(db).id)
    p = draft.best_available(db)
    draft.make_pick(db, lg, picks[0].team, p.id)
    with pytest.raises(LeagueError):
        draft.make_pick(db, lg, picks[1].team, p.id)  # already taken


def test_draft_timer_autopicks(db, league8):
    lg = league8
    draft.start_draft(db, lg)
    assert not draft.check_timer(db, lg)
    set_now("2026-11-01T12:01:05")
    assert draft.check_timer(db, lg)
    first = db.scalar(select(DraftPick).where(DraftPick.overall == 1))
    assert first.auto and first.player_id
    assert not draft.check_timer(db, lg)  # fresh clock for next pick


def test_full_season(db, league8):
    lg = league8
    fake = FakeProvider()
    run_draft(db, lg)
    assert lg.status == "season"
    assert db.scalar(select(func.count(RosterSpot.id))) == 8 * config.ROSTER_MAX
    assert db.scalar(select(func.count(Matchup.id))) == 4 * 4

    t1 = db.scalar(select(Team).where(Team.name == "Team 1"))
    # Week 1 lineup auto-fills from the roster and is editable before Monday
    lu = lineups.preview_lineup(db, t1.id, 1)
    assert sum(1 for i in range(len(config.STARTER_SLOTS)) if lu.get(i)) >= 8
    bench = [p for p in lineups.roster_players(db, t1.id) if p.id not in lu.values()]
    util_idx = config.STARTER_SLOTS.index("UTIL")
    new = dict(lu)
    new[util_idx] = bench[0].id
    lineups.set_lineup(db, lg, t1, 1, new)
    bad = dict(new)
    bad[util_idx + 1] = bench[0].id
    with pytest.raises(LeagueError):
        lineups.set_lineup(db, lg, t1, 1, bad)  # duplicate
    db.commit()

    # Free agent add with drop, then waivers on the dropped player
    fa = db.scalar(select(Player).where(Player.id.not_in(select(RosterSpot.player_id))).order_by(Player.rank))
    dropped = bench[1]
    roster.add_free_agent(db, lg, t1, fa.id, drop_id=dropped.id)
    assert roster.waiver_until(db, dropped.id)
    t2 = db.scalar(select(Team).where(Team.name == "Team 2"))
    t3 = db.scalar(select(Team).where(Team.name == "Team 3"))
    with pytest.raises(LeagueError):
        roster.add_free_agent(db, lg, t2, dropped.id, drop_id=lineups.roster_players(db, t2.id)[-1].id)
    t2_drop = lineups.roster_players(db, t2.id)[-1]
    t3_drop = lineups.roster_players(db, t3.id)[-1]
    roster.submit_claim(db, lg, t2, dropped.id, t2_drop.id)
    roster.submit_claim(db, lg, t3, dropped.id, t3_drop.id)
    db.commit()
    hi, lo = sorted([t2, t3], key=lambda t: t.waiver_priority)
    set_now("2026-11-03T20:00:00")
    roster.process_waivers(db, lg)
    assert roster.owner_id(db, dropped.id) == hi.id
    assert hi.waiver_priority == 8
    lost = db.scalar(select(WaiverClaim).where(WaiverClaim.team_id == lo.id))
    assert lost.status == "lost"
    db.commit()

    # Trade
    give = lineups.roster_players(db, t1.id)[-1]
    get = lineups.roster_players(db, t2.id)[-1]
    tr = trades.propose(db, lg, t1, t2, [give.id], [get.id])
    trades.accept(db, lg, t2, tr.id)
    assert roster.owner_id(db, give.id) == t2.id and roster.owner_id(db, get.id) == t1.id
    db.commit()

    # Play the season week by week
    for week in range(1, 7):
        ws = START + timedelta(days=7 * (week - 1))
        set_now(f"{ws}T08:00:00")
        lock_lineups(db, lg)
        with pytest.raises(LeagueError):
            lineups.set_lineup(db, lg, t1, week, lineups.preview_lineup(db, t1.id, week))
        # mid-week win probability
        set_now(f"{ws + timedelta(days=3)}T23:00:00")
        sync_stats(db, lg, days_back=4, provider=fake)
        m = db.scalar(select(Matchup).where(Matchup.week == week).limit(1))
        wp = analytics.win_probability(db, lg, m)
        assert 1 <= wp.home <= 99 and wp.home + wp.away == 100
        # Monday after, 11am: finalize
        set_now(f"{ws + timedelta(days=7)}T11:00:00")
        sync_stats(db, lg, days_back=7, provider=fake)
        done = schedule.finalize_due_weeks(db, lg)
        assert week in done
        db.commit()
        for mm in db.scalars(select(Matchup).where(Matchup.week == week)):
            assert mm.final and mm.home_score > 0
            hs, rows = team_week(db, lg, mm.home_team_id, week)
            assert hs == mm.home_score
            assert any(r.games for r in rows)
        if week == 4:
            assert lg.status == "playoffs"
            semis = list(db.scalars(select(Matchup).where(Matchup.week == 5)))
            assert len(semis) == 2 and all(s.is_playoff for s in semis)
            table = schedule.standings(db, lg)
            seeds = {s.team.id: s.seed for s in table}
            assert sorted(seeds[s.home_team_id] for s in semis) == [1, 2]
            pw_week, rows = analytics.power_rankings(db, lg)
            assert pw_week == 4 and [r.rank for r in rows] == list(range(1, 9))
            assert all(r.prev_rank for r in rows)
            ap = rows[0]
            assert ap.all_play_w + ap.all_play_l + ap.all_play_t == 4 * 7
    assert lg.status == "done"
    final = db.scalar(select(Matchup).where(Matchup.week == 6))
    assert final.winner_team_id in (final.home_team_id, final.away_team_id)


def test_avg_scoring_ignores_game_count(db, league8):
    """A starter with 4 games of 30 and one with 2 games of 30 count the same."""
    lg = league8
    run_draft(db, lg)
    t1 = db.scalar(select(Team).where(Team.name == "Team 1"))
    lu = lineups.ensure_lineup(db, t1.id, 1)
    a, b = lu[0], lu[1]
    def add(pid, d, pts):
        db.add(PlayerGame(player_id=pid, game_id=f"x{pid}{d}", game_date=START + timedelta(days=d), pts=pts))
    for d in range(4):
        add(a, d, 30)
    for d in range(2):
        add(b, d, 30)
    db.flush()
    _, rows = team_week(db, lg, t1.id, 1)
    assert rows[0].avg == rows[1].avg == 30.0
    assert rows[2].avg == 0.0  # no games
