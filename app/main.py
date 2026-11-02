"""Web app. Run: uvicorn app.main:app --host 0.0.0.0 --port 8000"""
from contextlib import asynccontextmanager
from datetime import timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .auth import check_password
from .db import get_db, init_db, utcnow
from .models import (DraftPick, League, Matchup, Player, PlayerGame, RosterSpot, Team, Trade,
                     Transaction, User, WaiverClaim)
from .scoring import game_fpts, player_avgs
from .services import analytics, draft, lineups, roster, schedule, trades
from .services.common import LeagueError, get_league
from .weeks import (current_week, first_open_week, is_locked, today_local, total_weeks, week_end,
                    week_label, week_start)

BASE = Path(__file__).parent
@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="Hoops League", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, max_age=60 * 60 * 24 * 90,
                   same_site="lax", https_only=False)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals["SLOTS"] = config.STARTER_SLOTS
templates.env.globals["ROSTER_MAX"] = config.ROSTER_MAX
from . import ui  # noqa: E402
ui.register(templates.env)
templates.env.filters["local"] = lambda dt: dt.replace(tzinfo=timezone.utc).astimezone(config.LEAGUE_TZ) if dt else dt
templates.env.filters["log2"] = lambda n: max(1, int(n).bit_length() - 1)


# ---- helpers ----------------------------------------------------------------

class NotLoggedIn(Exception):
    pass


@app.exception_handler(NotLoggedIn)
def _to_login(request: Request, exc):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(LeagueError)
def _league_error(request: Request, exc: LeagueError):
    flash(request, str(exc), "error")
    return RedirectResponse(_back(request), status_code=303)


def _back(request: Request, default: str = "/") -> str:
    ref = request.headers.get("referer")
    if ref:
        u = urlparse(ref)
        return u.path + (f"?{u.query}" if u.query else "")
    return default


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session["flash"] = request.session.get("flash", []) + [[kind, msg]]


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user:
        raise NotLoggedIn()
    return user


def my_team(user: User) -> Team | None:
    return user.team


def render(request: Request, name: str, db: Session, user: User | None, **ctx):
    league = db.scalar(select(League))
    msgs = request.session.pop("flash", [])
    pending_trades = 0
    if user and user.team:
        pending_trades = db.scalar(select(func.count(Trade.id)).where(
            Trade.receiver_team_id == user.team.id, Trade.status == "pending")) or 0
    return templates.TemplateResponse(request, name, {
        "user": user, "me": user.team if user else None, "league": league, "flashes": msgs,
        "pending_trades": pending_trades, "nav": ctx.pop("nav", ""), **ctx})


def redirect(url: str):
    return RedirectResponse(url, status_code=303)


def team_map(db: Session) -> dict[int, Team]:
    return {t.id: t for t in db.scalars(select(Team))}


def owners(db: Session) -> dict[int, int]:
    return {r.player_id: r.team_id for r in db.scalars(select(RosterSpot))}


def season_avgs(db: Session, league: League, ids) -> dict[int, tuple[int, float]]:
    return player_avgs(db, ids, start=league.start_date - timedelta(days=30))


def recent_avgs(db: Session, ids, days: int = 14) -> dict[int, tuple[int, float]]:
    return player_avgs(db, ids, start=today_local() - timedelta(days=days))


# ---- auth -------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "login.html", db, None)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if not user or not check_password(password, user.password_hash):
        flash(request, "Wrong username or password.", "error")
        return redirect("/login")
    request.session["uid"] = user.id
    return redirect("/")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


# ---- home / matchups --------------------------------------------------------

def _matchup_card(db, league, m, tmap):
    view = analytics.matchup_view(db, league, m)
    wp = analytics.win_probability(db, league, m, view)
    return {"m": m, "home": tmap[m.home_team_id], "away": tmap[m.away_team_id],
            "hs": view[0], "as": view[2], "wp": wp, "live": not m.final and is_locked(league, m.week)}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    league = get_league(db)
    ctx = {"nav": "home"}
    if league.status in ("season", "playoffs", "done"):
        week = current_week(league)
        tmap = team_map(db)
        team = user.team
        mine = None
        if team:
            m = db.scalar(select(Matchup).where(Matchup.week == week, or_(
                Matchup.home_team_id == team.id, Matchup.away_team_id == team.id)))
            if m:
                mine = _matchup_card(db, league, m, tmap)
        others = [_matchup_card(db, league, m, tmap) for m in db.scalars(
            select(Matchup).where(Matchup.week == week).order_by(Matchup.id))
            if not mine or m.id != mine["m"].id]
        nxt = week + 1 if week < total_weeks(league) else None
        ctx.update(week=week, label=week_label(league, week), mine=mine, others=others,
                   next_week=nxt, locked=is_locked(league, week))
        db.commit()
    return render(request, "home.html", db, user, **ctx)


@app.get("/scoreboard", response_class=HTMLResponse)
def scoreboard(request: Request, week: int | None = None, db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    league = get_league(db)
    week = max(1, min(week or current_week(league), total_weeks(league)))
    tmap = team_map(db)
    cards = [_matchup_card(db, league, m, tmap) for m in
             db.scalars(select(Matchup).where(Matchup.week == week).order_by(Matchup.id))]
    db.commit()
    return render(request, "scoreboard.html", db, user, nav="league", week=week, cards=cards,
                  label=week_label(league, week), total=total_weeks(league))


@app.get("/matchup/{mid}", response_class=HTMLResponse)
def matchup_page(request: Request, mid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    league = get_league(db)
    m = db.get(Matchup, mid)
    if not m:
        raise LeagueError("Matchup not found.")
    view = analytics.matchup_view(db, league, m)
    wp = analytics.win_probability(db, league, m, view)
    tmap = team_map(db)
    rows = list(zip(view[1], view[3]))
    db.commit()
    return render(request, "matchup.html", db, user, nav="home", m=m, home=tmap[m.home_team_id],
                  away=tmap[m.away_team_id], hs=view[0], as_=view[2], rows=rows, wp=wp,
                  label=week_label(league, m.week))


# ---- team / lineup ------------------------------------------------------------

@app.get("/team/{tid}", response_class=HTMLResponse)
def team_page(request: Request, tid: int, week: int | None = None, db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    league = get_league(db)
    team = db.get(Team, tid)
    if not team:
        raise LeagueError("Team not found.")
    players = lineups.roster_players(db, tid)
    in_season = league.status in ("season", "playoffs", "done")
    ctx = {"nav": "team" if user.team and user.team.id == tid else "league", "team": team, "players": players}
    ids = [p.id for p in players]
    ctx["savg"] = season_avgs(db, league, ids)
    if in_season:
        week = week or (first_open_week(league) if league.status != "done" else current_week(league))
        week = max(1, min(week, total_weeks(league)))
        locked = is_locked(league, week)
        lu = analytics.lineup_for(db, league, tid, week)
        from .scoring import team_week
        score, rows = team_week(db, league, tid, week, lu)
        starters = set(v for v in lu.values() if v)
        editable = not locked and user.team and user.team.id == tid
        options = {i: [p for p in players if lineups.eligible(p, s)] for i, s in enumerate(config.STARTER_SLOTS)}
        ctx["roster_json"] = [{"id": p.id, "name": p.name, "team": p.nba_team, "pos": p.position_list,
                               "avg": ctx["savg"].get(p.id, (0, None))[1], "gp": ctx["savg"].get(p.id, (0, 0))[0],
                               "img": ui.player_img(p.id), "ini": ui.initials(p.name),
                               "color": ui.team_color(p.nba_team)} for p in players]
        ctx["lineup_json"] = [lu.get(i) for i in range(len(config.STARTER_SLOTS))]
        ctx["elig_json"] = {k: sorted(v) for k, v in config.SLOT_ELIGIBILITY.items()}
        ctx.update(week=week, label=week_label(league, week), locked=locked, lineup=lu, rows=rows, score=score,
                   bench=[p for p in players if p.id not in starters], editable=editable, options=options,
                   total=total_weeks(league), open_week=first_open_week(league))
        db.commit()
    return render(request, "team.html", db, user, **ctx)


@app.post("/team/{tid}/lineup/{week}")
async def save_lineup(request: Request, tid: int, week: int, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    league = get_league(db)
    if not user.team or user.team.id != tid:
        raise LeagueError("You can only edit your own lineup.")
    form = await request.form()
    wants_json = "application/json" in request.headers.get("accept", "")
    assignment = {}
    for i in range(len(config.STARTER_SLOTS)):
        v = form.get(f"slot{i}")
        assignment[i] = int(v) if v else None
    try:
        lineups.set_lineup(db, league, user.team, week, assignment)
        if form.get("apply_rest"):
            for w in range(week + 1, total_weeks(league) + 1):
                if not is_locked(league, w):
                    lineups.set_lineup(db, league, user.team, w, assignment)
    except LeagueError as e:
        db.rollback()
        if wants_json:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        raise
    db.commit()
    if wants_json:
        return JSONResponse({"ok": True})
    flash(request, "Lineup saved.")
    return redirect(f"/team/{tid}?week={week}")


@app.post("/team/drop")
def drop(request: Request, player_id: int = Form(...), db: Session = Depends(get_db),
         user: User = Depends(current_user)):
    league = get_league(db)
    if not user.team:
        raise LeagueError("You don't have a team.")
    p = db.get(Player, player_id)
    roster.drop_player(db, league, user.team, player_id)
    db.commit()
    flash(request, f"Dropped {p.name}.")
    return redirect(f"/team/{user.team.id}")


# ---- players ----------------------------------------------------------------

POSITIONS = ["PG", "SG", "SF", "PF", "C"]


def _player_query(q: str, pos: str, status: str, own: dict[int, int]):
    stmt = select(Player).where(Player.active.is_(True))
    if q:
        stmt = stmt.where(Player.name.ilike(f"%{q.strip()}%"))
    if pos in POSITIONS:
        stmt = stmt.where(Player.positions.like(f"%{pos}%"))
    if status == "fa" and own:
        stmt = stmt.where(Player.id.not_in(list(own)))
    return stmt.order_by(Player.rank.is_(None), Player.rank, Player.name)


@app.get("/players", response_class=HTMLResponse)
def players_page(request: Request, q: str = "", pos: str = "", status: str = "fa", sort: str = "rank",
                 page: int = 1, db: Session = Depends(get_db), user: User = Depends(current_user)):
    league = get_league(db)
    own = owners(db)
    found = list(db.scalars(_player_query(q, pos, status, own)))
    ids = [p.id for p in found]
    savg, ravg = season_avgs(db, league, ids), recent_avgs(db, ids)
    if sort == "avg":
        found.sort(key=lambda p: -savg.get(p.id, (0, -99))[1])
    elif sort == "recent":
        found.sort(key=lambda p: -ravg.get(p.id, (0, -99))[1])
    per = 40
    shown = found[(page - 1) * per: page * per]
    waivers = {p.id: roster.waiver_until(db, p.id) for p in shown}
    ctx = dict(nav="players", players=shown, q=q, pos=pos, status=status, sort=sort, page=page,
               more=len(found) > page * per, savg=savg, ravg=ravg, own=own, tmap=team_map(db),
               waivers=waivers, positions=POSITIONS)
    if request.headers.get("HX-Request"):
        return render(request, "_player_rows.html", db, user, **ctx)
    return render(request, "players.html", db, user, **ctx)


@app.get("/players/{pid}", response_class=HTMLResponse)
def player_page(request: Request, pid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    league = get_league(db)
    p = db.get(Player, pid)
    if not p:
        raise LeagueError("Player not found.")
    games = list(db.scalars(select(PlayerGame).where(PlayerGame.player_id == pid)
                            .order_by(PlayerGame.game_date.desc()).limit(15)))
    owner_id = roster.owner_id(db, pid)
    my_roster = lineups.roster_players(db, user.team.id) if user.team else []
    full = len(my_roster) >= config.ROSTER_MAX
    return render(request, "player.html", db, user, nav="players", p=p, games=games, fpts=game_fpts,
                  owner=db.get(Team, owner_id) if owner_id else None, waiver=roster.waiver_until(db, pid),
                  savg=season_avgs(db, league, [pid]).get(pid), ravg=recent_avgs(db, [pid]).get(pid),
                  my_roster=my_roster, full=full)


@app.post("/players/{pid}/add")
def add_player(request: Request, pid: int, drop_id: str = Form(""), db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    league = get_league(db)
    if not user.team:
        raise LeagueError("You don't have a team.")
    drop_pid = int(drop_id) if drop_id else None
    p = db.get(Player, pid)
    if roster.waiver_until(db, pid):
        roster.submit_claim(db, league, user.team, pid, drop_pid)
        flash(request, f"Claim submitted for {p.name}.")
        db.commit()
        return redirect("/claims")
    roster.add_free_agent(db, league, user.team, pid, drop_pid)
    db.commit()
    flash(request, f"Added {p.name}.")
    return redirect(f"/team/{user.team.id}")


@app.get("/claims", response_class=HTMLResponse)
def claims_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    claims = []
    if user.team:
        claims = list(db.scalars(select(WaiverClaim).where(WaiverClaim.team_id == user.team.id)
                                 .order_by(WaiverClaim.created_at.desc()).limit(30)))
    order = list(db.scalars(select(Team).order_by(Team.waiver_priority, Team.id)))
    holds = {c.add_player_id: roster.waiver_until(db, c.add_player_id) for c in claims}
    return render(request, "claims.html", db, user, nav="more", claims=claims, order=order, holds=holds)


@app.post("/claims/{cid}/cancel")
def cancel_claim(request: Request, cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    roster.cancel_claim(db, user.team, cid)
    db.commit()
    return redirect("/claims")


# ---- draft ------------------------------------------------------------------

def _draft_ctx(db: Session, league: League, user: User):
    draft.check_timer(db, league)
    db.commit()
    pick = draft.current_pick(db)
    made = list(db.scalars(select(DraftPick).where(DraftPick.player_id.is_not(None))
                           .order_by(DraftPick.overall.desc())))
    my_turn = bool(pick and user.team and pick.team_id == user.team.id)
    upcoming = list(db.scalars(select(DraftPick).where(DraftPick.player_id.is_(None))
                               .order_by(DraftPick.overall).limit(8)))
    return dict(pick=pick, made=made, my_turn=my_turn, upcoming=upcoming, left=draft.seconds_left(league),
                n_made=len(made))


def _board(db: Session) -> list[DraftPick]:
    return list(db.scalars(select(DraftPick).order_by(DraftPick.overall)))


@app.get("/draft/board", response_class=HTMLResponse)
def draft_board(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "_draft_board.html", db, user, board=_board(db))


@app.get("/draft", response_class=HTMLResponse)
def draft_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    league = get_league(db)
    ctx = _draft_ctx(db, league, user) if league.status != "setup" else {}
    order = draft.teams_in_order(db)
    mine = lineups.roster_players(db, user.team.id) if user.team else []
    return render(request, "draft.html", db, user, nav="draft", order=order, mine=mine,
                  positions=POSITIONS, board=_board(db), **ctx)


@app.get("/draft/state", response_class=HTMLResponse)
def draft_state(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    league = get_league(db)
    return render(request, "_draft_state.html", db, user, **_draft_ctx(db, league, user))


@app.get("/draft/players", response_class=HTMLResponse)
def draft_players(request: Request, q: str = "", pos: str = "", db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    league = get_league(db)
    taken = draft.drafted_ids(db)
    stmt = select(Player).where(Player.active.is_(True))
    if taken:
        stmt = stmt.where(Player.id.not_in(taken))
    if q:
        stmt = stmt.where(Player.name.ilike(f"%{q.strip()}%"))
    if pos in POSITIONS:
        stmt = stmt.where(Player.positions.like(f"%{pos}%"))
    found = list(db.scalars(stmt.order_by(Player.rank.is_(None), Player.rank, Player.name).limit(60)))
    pick = draft.current_pick(db)
    my_turn = bool(pick and user.team and pick.team_id == user.team.id)
    return render(request, "_draft_players.html", db, user, players=found, my_turn=my_turn,
                  live=league.status == "drafting")


@app.post("/draft/pick")
def draft_pick(request: Request, player_id: int = Form(...), for_team: str = Form(""),
               db: Session = Depends(get_db), user: User = Depends(current_user)):
    league = get_league(db)
    if for_team and user.is_commish:
        draft.make_pick(db, league, None, player_id, by_commish=True)
    else:
        draft.make_pick(db, league, user.team, player_id)
    db.commit()
    if request.headers.get("HX-Request"):
        return HTMLResponse("", headers={"HX-Trigger": "picked"})
    return redirect("/draft")


# ---- league -----------------------------------------------------------------

@app.get("/standings", response_class=HTMLResponse)
def standings_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    league = get_league(db)
    table = schedule.standings(db, league)
    tmap = team_map(db)
    rounds = {}
    for m in db.scalars(select(Matchup).where(Matchup.is_playoff.is_(True))
                        .order_by(Matchup.playoff_round, Matchup.bracket_slot)):
        rounds.setdefault(m.playoff_round, []).append(m)
    seeds = {s.team.id: s.seed for s in table}
    return render(request, "standings.html", db, user, nav="league", table=table, rounds=rounds,
                  tmap=tmap, seeds=seeds)


@app.get("/power", response_class=HTMLResponse)
def power_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    league = get_league(db)
    through, rows = analytics.power_rankings(db, league)
    return render(request, "power.html", db, user, nav="league", through=through, rows=rows)


@app.get("/activity", response_class=HTMLResponse)
def activity(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    items = list(db.scalars(select(Transaction).order_by(Transaction.created_at.desc()).limit(100)))
    return render(request, "activity.html", db, user, nav="more", items=items)


@app.get("/more", response_class=HTMLResponse)
def more(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    teams = list(db.scalars(select(Team).order_by(Team.name)))
    return render(request, "more.html", db, user, nav="more", teams=teams)


# ---- trades -----------------------------------------------------------------

@app.get("/trades", response_class=HTMLResponse)
def trades_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = select(Trade).order_by(Trade.created_at.desc()).limit(50)
    if not user.is_commish and user.team:
        q = q.where(or_(Trade.proposer_team_id == user.team.id, Trade.receiver_team_id == user.team.id,
                        Trade.status == "accepted"))
    items = list(db.scalars(q))
    return render(request, "trades.html", db, user, nav="more", items=items,
                  teams=[t for t in db.scalars(select(Team).order_by(Team.name))
                         if not user.team or t.id != user.team.id])


@app.get("/trades/new", response_class=HTMLResponse)
def new_trade(request: Request, with_team: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    league = get_league(db)
    other = db.get(Team, with_team)
    if not other or not user.team:
        raise LeagueError("Team not found.")
    mine = lineups.roster_players(db, user.team.id)
    theirs = lineups.roster_players(db, other.id)
    savg = season_avgs(db, league, [p.id for p in mine + theirs])
    return render(request, "trade_new.html", db, user, nav="more", other=other, mine=mine, theirs=theirs, savg=savg)


@app.post("/trades")
async def create_trade(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    league = get_league(db)
    form = await request.form()
    other = db.get(Team, int(form["with_team"]))
    give = [int(x) for x in form.getlist("give")]
    get = [int(x) for x in form.getlist("get")]
    trades.propose(db, league, user.team, other, give, get, str(form.get("note", "")))
    db.commit()
    flash(request, "Trade sent.")
    return redirect("/trades")


@app.post("/trades/{trade_id}/{action}")
def trade_action(request: Request, trade_id: int, action: str, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    league = get_league(db)
    if action == "accept":
        trades.accept(db, league, user.team, trade_id)
    elif action == "reject":
        trades.reject(db, user.team, trade_id)
    elif action == "cancel":
        trades.cancel(db, user.team, trade_id)
    elif action == "veto" and user.is_commish:
        trades.veto(db, trade_id)
    else:
        raise LeagueError("Unknown action.")
    db.commit()
    return redirect("/trades")


# ---- commissioner -----------------------------------------------------------

def require_commish(user: User = Depends(current_user)) -> User:
    if not user.is_commish:
        raise LeagueError("Commissioner only.")
    return user


@app.get("/commish", response_class=HTMLResponse)
def commish_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_commish)):
    league = get_league(db)
    teams = draft.teams_in_order(db)
    n_players = db.scalar(select(func.count(Player.id)).where(Player.active.is_(True)))
    open_weeks = sorted(set(db.scalars(select(Matchup.week).where(Matchup.final.is_(False)))))
    pending = list(db.scalars(select(Trade).where(Trade.status == "pending")))
    finalized = bool(db.scalar(select(Matchup).where(Matchup.final.is_(True)).limit(1)))
    return render(request, "commish.html", db, user, nav="more", teams=teams, n_players=n_players, finalized=finalized,
                  open_weeks=open_weeks, pending=pending, provider=config.STATS_PROVIDER)


@app.post("/commish/{action}")
async def commish_action(request: Request, action: str, db: Session = Depends(get_db),
                         user: User = Depends(require_commish)):
    league = get_league(db)
    form = await request.form()
    if action == "add-team":
        from .cli import create_team
        if form.get("mine") and not user.team:
            if len(list(db.scalars(select(Team)))) >= league.num_teams:
                raise LeagueError("The league is full.")
            db.add(Team(name=str(form["team"]).strip(), user_id=user.id))
        else:
            create_team(db, str(form["team"]), str(form["username"]), str(form["password"]))
        flash(request, "Team added.")
    elif action == "randomize-order":
        draft.set_draft_order(db, league)
        flash(request, "Draft order randomized.")
    elif action == "move-order":
        ids = [t.id for t in draft.teams_in_order(db)]
        tid, d = int(form["team_id"]), int(form["dir"])
        i = ids.index(tid)
        j = max(0, min(len(ids) - 1, i + d))
        ids[i], ids[j] = ids[j], ids[i]
        draft.set_draft_order(db, league, ids)
    elif action == "start-draft":
        draft.start_draft(db, league)
        db.commit()
        return redirect("/draft")
    elif action == "undo-pick":
        draft.undo_last_pick(db, league)
        flash(request, "Last pick undone.")
    elif action == "settings":
        from datetime import date as _date
        from sqlalchemy import delete as _delete
        from .models import LineupSlot
        if db.scalar(select(Matchup).where(Matchup.final.is_(True)).limit(1)):
            raise LeagueError("Settings are locked once a week has been finalized.")
        try:
            start = _date.fromisoformat(str(form["start"]))
        except ValueError:
            raise LeagueError("Start date must look like 2026-10-19.")
        if start.weekday() != 0:
            raise LeagueError("Start date must be a Monday.")
        weeks, playoff = int(form["weeks"]), int(form["playoff_teams"])
        if not 1 <= weeks <= 30 or playoff not in (2, 4, 8) or playoff > league.num_teams:
            raise LeagueError("Check the weeks and playoff teams.")
        league.name = str(form.get("name") or league.name).strip()[:100]
        league.start_date, league.regular_season_weeks, league.playoff_teams = start, weeks, playoff
        if league.status in ("season", "playoffs"):
            league.status = "season"
            db.execute(_delete(LineupSlot))   # week numbers moved; lineups rebuild from rosters
            schedule.create_regular_season(db, league)
        flash(request, "Settings saved.")
    elif action == "timer":
        league.draft_timer_seconds = max(0, int(form["seconds"]))
        flash(request, "Timer updated.")
    elif action == "sync":
        db.commit()
        from .jobs import tick
        log = []
        tick(log=log.append)
        flash(request, "Synced. " + "; ".join(log[:4]))
        return redirect("/commish")
    elif action == "finalize":
        schedule.finalize_week(db, league, int(form["week"]))
        flash(request, "Week finalized.")
    elif action == "import":
        from .importer import import_league, parse_recap, parse_teams
        rows = parse_teams(str(form.get("teams", "")))
        picks = parse_recap(str(form.get("draft", "")))
        if not picks:
            raise LeagueError("Paste the draft recap.")
        commish_user = user if not user.team else None
        logins, placeholders = import_league(db, rows, picks, str(form.get("commish", "")), commish_user)
        db.commit()
        return render(request, "imported.html", db, user, nav="more", logins=logins, placeholders=placeholders)
    elif action == "edit-login":
        from .auth import hash_password
        team = db.get(Team, int(form["team_id"]))
        if not team or not team.user:
            raise LeagueError("Pick a team.")
        username = str(form.get("username", "")).strip().lower()
        pw = str(form.get("password", "")).strip()
        if not username and not pw:
            raise LeagueError("Enter a new username, a new password, or both.")
        if username and username != team.user.username:
            if not username.replace("_", "").replace(".", "").isalnum() or len(username) > 50:
                raise LeagueError("Usernames can use letters, numbers, _ and . only.")
            if db.scalar(select(User).where(User.username == username)):
                raise LeagueError(f"Username {username} is taken.")
            team.user.username = username
        if pw:
            if len(pw) < 4:
                raise LeagueError("Password must be at least 4 characters.")
            team.user.password_hash = hash_password(pw)
        flash(request, f"Login updated for {team.name}: {team.user.username}.")
    elif action == "process-waivers":
        res = roster.process_waivers(db, league, now=utcnow() + timedelta(days=365))
        flash(request, f"Processed {len(res)} claims.")
    else:
        raise LeagueError("Unknown action.")
    db.commit()
    return redirect("/commish")


@app.get("/healthz")
def healthz():
    return {"ok": True}
