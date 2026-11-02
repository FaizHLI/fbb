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
