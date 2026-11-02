"""Import teams and a completed draft from ESPN.

  python -m app.importer --teams teams.csv --draft draft_recap.txt --commish ABC

Or paste both on the Commissioner page (no shell needed).

Creates one login per team (username = abbreviation, lowercase) with a random
password, fills every roster from the draft, sets the draft order from round 1,
and starts the season. Writes the logins to league_logins.csv.

Run `python -m app.jobs sync-players` first so draft names match real players.
Names that don't match yet are kept as placeholders and get linked to the real
player automatically on a later player sync.
"""
import argparse
import csv
import re
import secrets
import unicodedata
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import hash_password
from .db import SessionLocal, init_db, utcnow
from .models import DraftPick, League, LineupSlot, Player, RosterSpot, Team, User
from .services.common import LeagueError, get_league, log
from .stats.base import expand_positions

ESPN_TEAMS = {"SA": "SAS", "NY": "NYK", "GS": "GSW", "NO": "NOP", "WSH": "WAS", "UTAH": "UTA", "PHO": "PHX"}
POS_RE = re.compile(r"^(?P<name>.+?)\s+(?P<team>[A-Z]{2,4}),\s*(?P<pos>PG|SG|SF|PF|C|G|F|GF|FC)$")
SKIP = {"no.", "player", "team"}


def norm_name(name: str) -> str:
    """'Nikola Jokić' -> 'nikola jokic'; drops Jr., III, periods and apostrophes."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'’]", "", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


@dataclass
class RecapPick:
    round: int
    pick: int
    name: str
    nba_team: str
    position: str
    team: str


def parse_recap(text: str) -> list[RecapPick]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    picks: list[RecapPick] = []
    rnd = 0
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"^Round\s+(\d+)$", ln, re.I)
        if m:
            rnd = int(m.group(1))
            i += 1
            continue
        if ln.lower() in SKIP:
            i += 1
            continue
        if ln.isdigit() and i + 2 < len(lines):
            pm = POS_RE.match(lines[i + 1])
            if not pm:
                raise LeagueError(f"Can't read player line: {lines[i + 1]!r}")
            picks.append(RecapPick(rnd, int(ln), pm["name"].strip(), pm["team"], pm["pos"], lines[i + 2]))
            i += 3
            continue
        raise LeagueError(f"Unexpected line in draft recap: {ln!r}")
    return picks


def read_teams(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return parse_teams(f.read())


def parse_teams(text: str) -> list[dict]:
    """CSV with a header row: name,abbrev[,manager]"""
    import io
    rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            for r in csv.DictReader(io.StringIO(text.strip()))]
    for r in rows:
        if not r.get("name") or not r.get("abbrev"):
            raise LeagueError("teams.csv needs name and abbrev columns.")
    return rows


def find_player(db: Session, pick: RecapPick, by_name: dict[str, list[Player]]) -> Player:
    nba_team = ESPN_TEAMS.get(pick.nba_team, pick.nba_team)
    matches = by_name.get(norm_name(pick.name), [])
    if len(matches) > 1:
        same_team = [p for p in matches if p.nba_team == nba_team]
        matches = same_team or matches
    if matches:
        return matches[0]
    # Placeholder with a negative id; linked to the real player on a later sync.
    low = db.scalar(select(func.min(Player.id))) or 0
    p = Player(id=min(low, 0) - 1, name=pick.name[:80], nba_team=nba_team[:5],
               positions=",".join(expand_positions(pick.position)) or pick.position, active=True)
    db.add(p)
    db.flush()
    by_name.setdefault(norm_name(pick.name), []).append(p)
    return p


def import_league(db: Session, teams_rows: list[dict], picks: list[RecapPick], commish_abbrev: str,
                  commish_user: User | None = None) -> tuple[list[tuple], list[str]]:
    """If commish_user is given (an existing login with no team), it becomes the
    owner of the commissioner's team instead of creating a new login for it."""
    league = get_league(db)
    if league.status != "setup":
        raise LeagueError("Import only works on a league that hasn't drafted yet.")
    if commish_abbrev.upper() not in {r["abbrev"].upper() for r in teams_rows}:
        raise LeagueError(f"No team with abbreviation {commish_abbrev} in the team list.")
    if db.scalar(select(func.count(Team.id))):
        raise LeagueError("This league already has teams. Start from a fresh database.")
    if len(teams_rows) != league.num_teams:
        raise LeagueError(f"League is set for {league.num_teams} teams but the file has {len(teams_rows)}.")

    name_to_row = {r["name"]: r for r in teams_rows}
    unknown = sorted({p.team for p in picks} - set(name_to_row))
    if unknown:
        raise LeagueError(f"Draft has teams not in teams.csv: {', '.join(unknown)}")
    rounds = max(p.round for p in picks)
    if len(picks) != rounds * league.num_teams:
        raise LeagueError(f"Expected {rounds * league.num_teams} picks, found {len(picks)}.")

    logins, teams = [], {}
    for r in teams_rows:
        is_commish = r["abbrev"].upper() == commish_abbrev.upper()
        if is_commish and commish_user is not None:
            user, username, password = commish_user, commish_user.username, "(your current password)"
        else:
            username = re.sub(r"[^a-z0-9]", "", r["abbrev"].lower())[:40]
            if db.scalar(select(User).where(User.username == username)):
                username += "1"
            password = secrets.token_urlsafe(6)
            user = User(username=username, password_hash=hash_password(password), is_commish=is_commish)
            db.add(user)
        db.flush()
        t = Team(name=r["name"][:80], abbrev=r["abbrev"].upper()[:6], manager=r.get("manager", "")[:80],
                 user_id=user.id)
        db.add(t)
        db.flush()
        teams[r["name"]] = t
        logins.append((r["name"], username, password, "yes" if user.is_commish else ""))

    first_round = sorted((p for p in picks if p.round == 1), key=lambda p: p.pick)
    for slot, p in enumerate(first_round, start=1):
        teams[p.team].draft_slot = slot

    by_name: dict[str, list[Player]] = {}
    for pl in db.scalars(select(Player)):
        by_name.setdefault(norm_name(pl.name), []).append(pl)

    placeholders = []
    used = set()
    now = utcnow()
    for p in sorted(picks, key=lambda p: (p.round, p.pick)):
        player = find_player(db, p, by_name)
        if player.id in used:
            raise LeagueError(f"{p.name} was drafted twice.")
        used.add(player.id)
        if player.id < 0:
            placeholders.append(p.name)
        overall = (p.round - 1) * league.num_teams + p.pick
        db.add(DraftPick(overall=overall, round=p.round, team_id=teams[p.team].id, player_id=player.id, made_at=now))
        db.add(RosterSpot(team_id=teams[p.team].id, player_id=player.id, acquired_at=now))
    db.flush()

    from .services.draft import _finish
    league.status = "drafting"
    _finish(db, league)
    log(db, "draft", "Draft imported from ESPN")
    db.flush()
    return logins, placeholders


def merge_placeholders(db: Session) -> list[str]:
    """Point placeholder players (negative ids) at the real player with the same name."""
    real: dict[str, list[Player]] = {}
    for p in db.scalars(select(Player).where(Player.id > 0)):
        real.setdefault(norm_name(p.name), []).append(p)
    merged = []
    for ph in list(db.scalars(select(Player).where(Player.id < 0))):
        cands = real.get(norm_name(ph.name), [])
        if len(cands) > 1:
            cands = [c for c in cands if c.nba_team == ph.nba_team] or cands
        if not cands:
            continue
        target = cands[0]
        if db.scalar(select(RosterSpot).where(RosterSpot.player_id == target.id)):
            continue  # real player already rostered; leave for the commissioner
        for model in (RosterSpot, DraftPick, LineupSlot):
            for row in db.scalars(select(model).where(model.player_id == ph.id)):
                row.player_id = target.id
        db.flush()
        db.delete(ph)
        merged.append(ph.name)
    db.flush()
    return merged


def main():
    ap = argparse.ArgumentParser(prog="python -m app.importer")
    ap.add_argument("--teams", required=True, help="CSV with name,abbrev,manager")
    ap.add_argument("--draft", required=True, help="ESPN draft recap copied as text")
    ap.add_argument("--commish", required=True, help="abbreviation of the commissioner's team")
    ap.add_argument("--out", default="league_logins.csv")
    args = ap.parse_args()
    init_db()
    db = SessionLocal()
    try:
        with open(args.draft, encoding="utf-8") as f:
            picks = parse_recap(f.read())
        logins, placeholders = import_league(db, read_teams(args.teams), picks, args.commish)
        db.commit()
    except LeagueError as e:
        db.rollback()
        raise SystemExit(f"Error: {e}")
    finally:
        db.close()
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["team", "username", "password", "commissioner"])
        w.writerows(logins)
    print(f"Imported {len(logins)} teams and {len(picks)} picks. Logins saved to {args.out}")
    if placeholders:
        print(f"{len(placeholders)} players not found in the player list yet (linked on next player sync):")
        print("  " + ", ".join(placeholders))


if __name__ == "__main__":
    main()


# ---- ESPN schedule -----------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                      "nov", "dec"], start=1)}
PERIOD_RE = re.compile(r"^(?:Matchup\s+(?P<num>\d+)|Playoff Round\s+(?P<rnd>\d+))\s*"
                       r"\((?P<m1>[A-Za-z]{3})\w*\s+(?P<d1>\d{1,2})\s*-\s*(?:(?P<m2>[A-Za-z]{3})\w*\s+)?(?P<d2>\d{1,2})\)")
NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


@dataclass
class SchedGame:
    away: str
    home: str
    away_score: float | None
    home_score: float | None


@dataclass
class SchedPeriod:
    start: "date"
    end: "date"
    playoff_round: int | None
    games: list


def _season_date(season: str, month: str, day: int):
    from datetime import date
    m = MONTHS[month.lower()]
    first_year = int(season[:4])
    return date(first_year if m >= 7 else first_year + 1, m, day)


def parse_schedule(text: str, team_names: list[str], season: str) -> list[SchedPeriod]:
    """Read ESPN's league Schedule page (copied as text). Team names must match exactly;
    in each matchup the first team listed is away, the second is home, and any two
    numbers between them are the away and home scores."""
    names = set(team_names)
    out: list[SchedPeriod] = []
    cur = None
    pending, nums = None, []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        m = PERIOD_RE.match(ln)
        if m:
            if pending:
                raise LeagueError(f"{pending} has no opponent in the matchup before {ln!r}.")
            start = _season_date(season, m["m1"], int(m["d1"]))
            end = _season_date(season, m["m2"] or m["m1"], int(m["d2"]))
            cur = SchedPeriod(start, end, int(m["rnd"]) if m["rnd"] else None, [])
            out.append(cur)
            continue
        if cur is None:
            continue
        if ln in names:
            if pending is None:
                pending, nums = ln, []
            else:
                scores = nums[-2:] if len(nums) >= 2 else [None, None]
                cur.games.append(SchedGame(pending, ln, scores[0], scores[1]))
                pending, nums = None, []
        elif pending is not None and NUM_RE.match(ln):
            nums.append(float(ln))
    if pending:
        raise LeagueError(f"{pending} has no opponent in the last matchup.")
    if not out:
        raise LeagueError("No matchups found. Copy the whole Schedule page from ESPN.")
    return out


def import_schedule(db: Session, league: League, periods_in: list[SchedPeriod]) -> dict:
    """Replace the schedule with ESPN's: matchup dates, pairings, and final scores
    for matchups that already have them."""
    import json
    from math import log2
    from sqlalchemy import delete
    from .models import LineupSlot, Matchup
    if league.status not in ("setup", "season"):
        raise LeagueError("The schedule can only be imported before the playoffs.")
    teams = {t.name: t for t in db.scalars(select(Team))}
    if len(teams) != league.num_teams:
        raise LeagueError("Import teams (or add them) before the schedule.")
    regular = [p for p in periods_in if p.playoff_round is None]
    playoffs = [p for p in periods_in if p.playoff_round is not None]
    if not regular:
        raise LeagueError("No regular season matchups found.")
    if any(p.playoff_round is None for p in periods_in[len(regular):]):
        raise LeagueError("Playoff rounds must come after the regular season.")
    for i, p in enumerate(periods_in):
        if p.end < p.start or (i and p.start <= periods_in[i - 1].end):
            raise LeagueError(f"Matchup dates overlap or go backwards near {p.start}.")
    for n, p in enumerate(regular, start=1):
        seen = [g.away for g in p.games] + [g.home for g in p.games]
        unknown = sorted(set(seen) - set(teams))
        if unknown:
            raise LeagueError(f"Unknown team in matchup {n}: {', '.join(unknown)}")
        if len(p.games) != league.num_teams // 2 or len(set(seen)) != league.num_teams:
            raise LeagueError(f"Matchup {n} should have every team exactly once.")
    playoff_teams = 2 ** len(playoffs) if playoffs else league.playoff_teams
    if playoffs and playoff_teams > league.num_teams:
        raise LeagueError("More playoff rounds than the league size allows.")

    league.periods = json.dumps([[p.start.isoformat(), p.end.isoformat()] for p in regular + playoffs])
    league.start_date = regular[0].start
    league.regular_season_weeks = len(regular)
    if playoffs:
        league.playoff_teams = playoff_teams
    elif league.playoff_teams > 1:
        # keep 7-day playoff weeks after the last regular matchup
        extra = []
        last = regular[-1].end
        from datetime import timedelta
        for _ in range(int(log2(league.playoff_teams))):
            extra.append([(last + timedelta(days=1)).isoformat(), (last + timedelta(days=7)).isoformat()])
            last = last + timedelta(days=7)
        league.periods = json.dumps(json.loads(league.periods) + extra)

    db.execute(delete(LineupSlot))
    db.execute(delete(Matchup))
    finals = 0
    for week, p in enumerate(regular, start=1):
        for g in p.games:
            m = Matchup(week=week, home_team_id=teams[g.home].id, away_team_id=teams[g.away].id)
            if g.home_score is not None and g.away_score is not None:
                m.home_score, m.away_score, m.final = g.home_score, g.away_score, True
                m.winner_team_id = (m.home_team_id if g.home_score > g.away_score else
                                    m.away_team_id if g.away_score > g.home_score else None)
                finals += 1
            db.add(m)
    db.flush()
    log(db, "week", f"Schedule imported from ESPN ({len(regular)} matchups, {finals} results)")
    return {"weeks": len(regular), "playoff_rounds": len(playoffs), "results": finals}


# ---- ESPN rosters ------------------------------------------------------------

ROSTER_SLOTS = {"PG", "SG", "SF", "PF", "C", "G", "F", "UTIL", "BENCH", "BE", "BN", "IR"}
RECORD_RE = re.compile(r"^\(\d+-\d+(?:-\d+)?\)$")
INJURY_MAP = {"O": "OUT", "OUT": "OUT", "DTD": "DTD", "SSPD": "SUSP", "SUSP": "SUSP", "IR": "OUT"}
ACQ_WORDS = ("draft", "trade", "free agency", "waivers", "waiver", "add")


@dataclass
class RosterEntry:
    slot: str          # PG..UTIL, BN, IR
    name: str
    nba_team: str
    positions: list
    injury: str


def _split_team_pos(s: str) -> tuple[str, list[str]] | None:
    """'TORSF, SG' -> ('TOR', ['SF', 'SG']); 'SAC' -> ('SA', ['C'])."""
    parts = [p.strip() for p in s.split(",")]
    first = parts[0]
    if len(first) < 3 or not first.isupper() or not first.isalpha():
        return None
    if first[-2:] in ("PG", "SG", "SF", "PF") and len(first) - 2 >= 2:
        team, pos1 = first[:-2], first[-2:]
    elif first[-1] == "C" and len(first) - 1 >= 2:
        team, pos1 = first[:-1], "C"
    else:
        return None
    rest = parts[1:]
    if any(p not in ("PG", "SG", "SF", "PF", "C") for p in rest):
        return None
    return team, [pos1] + rest


def _undouble(s: str) -> str:
    """ESPN prints names twice when copied: 'Cade CunninghamCade Cunningham'."""
    h = len(s) // 2
    return s[:h] if len(s) % 2 == 0 and s[:h] == s[h:] else s


def parse_rosters(text: str, team_names: list[str]) -> dict[str, list[RosterEntry]]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    names = set(team_names)
    out: dict[str, list[RosterEntry]] = {}
    team = None
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln in names and i + 1 < len(lines) and RECORD_RE.match(lines[i + 1]):
            team = ln
            out.setdefault(team, [])
            i += 2
            continue
        if team and ln.upper() in ROSTER_SLOTS and i + 1 < len(lines):
            nxt = lines[i + 1]
            if nxt.lower() == "empty":
                i += 2
                continue
            # name line (doubled), display line (name + injury tag), team/positions line
            if i + 3 < len(lines):
                name = _undouble(nxt)
                disp = lines[i + 2]
                tp = _split_team_pos(lines[i + 3])
                if tp and disp.startswith(name):
                    tag = disp[len(name):].strip().upper()
                    slot = {"BENCH": "BN", "BE": "BN"}.get(ln.upper(), ln.upper())
                    out[team].append(RosterEntry(slot, name, ESPN_TEAMS.get(tp[0], tp[0]), tp[1],
                                                 INJURY_MAP.get(tag, "")))
                    i += 4
                    continue
        i += 1
    if not any(out.values()):
        raise LeagueError("No players found. Copy the whole Rosters page from ESPN.")
    return out


def import_rosters(db: Session, league: League, rosters: dict[str, list[RosterEntry]]) -> dict:
    """Make every roster match ESPN, including IR, and set the current week's
    lineup from ESPN's slots."""
    from sqlalchemy import delete
    from . import config
    from .models import WaiverClaim, WaiverHold
    from .weeks import current_week, total_weeks
    if league.status not in ("season", "playoffs"):
        raise LeagueError("Import the draft (or run it) before importing rosters.")
    teams = {t.name: t for t in db.scalars(select(Team))}
    missing = sorted(set(teams) - set(rosters))
    if missing:
        raise LeagueError(f"Rosters page is missing: {', '.join(missing)}")
    by_name: dict[str, list[Player]] = {}
    for pl in db.scalars(select(Player)):
        by_name.setdefault(norm_name(pl.name), []).append(pl)
    seen, placeholders = set(), []
    for tname, entries in rosters.items():
        active = [e for e in entries if e.slot != "IR"]
        if len(active) > config.ROSTER_MAX or len(entries) - len(active) > config.IR_SLOTS:
            raise LeagueError(f"{tname} has more players than the roster allows.")
    db.execute(delete(RosterSpot))
    db.execute(delete(LineupSlot))
    db.execute(delete(WaiverClaim).where(WaiverClaim.status == "pending"))
    db.execute(delete(WaiverHold))
    week = current_week(league)
    now = utcnow()
    lineups_set = 0
    for tname, entries in rosters.items():
        team = teams[tname]
        slot_idx: dict[int, int] = {}
        free = {s: [i for i, x in enumerate(config.STARTER_SLOTS) if x == s] for s in set(config.STARTER_SLOTS)}
        for e in entries:
            pick = RecapPick(0, 0, e.name, e.nba_team, e.positions[0], tname)
            player = find_player(db, pick, by_name)
            if player.id in seen:
                raise LeagueError(f"{e.name} is on two rosters.")
            seen.add(player.id)
            if player.id < 0:
                placeholders.append(e.name)
                player.positions = ",".join(e.positions)
            if e.injury or player.id < 0:
                player.injury = e.injury
            db.add(RosterSpot(team_id=team.id, player_id=player.id, acquired_at=now, on_ir=e.slot == "IR"))
            if free.get(e.slot):
                slot_idx[free[e.slot].pop(0)] = player.id
        db.flush()
        if slot_idx and week <= total_weeks(league):
            for i in range(len(config.STARTER_SLOTS)):
                db.add(LineupSlot(team_id=team.id, week=week, slot_index=i, player_id=slot_idx.get(i)))
            lineups_set += 1
    db.flush()
    log(db, "add", "Rosters imported from ESPN")
    return {"players": len(seen), "placeholders": placeholders, "lineups": lineups_set, "week": week}
