# Hoops League

A self-hosted fantasy basketball site for one league. Built for phones.

The main difference from ESPN: **matchups are scored by average fantasy points per game**, so a team with more games that week has no built-in edge.

## How scoring works

- Lineups are weekly. Each week runs Monday to Sunday (US Eastern) and locks at 00:00 Monday.
- Each starter's score for the week = their fantasy points that week ÷ games they played.
- A starter with no games that week scores 0.
- Team score = sum of the 10 starters' weekly averages.
- Fantasy points use ESPN's default points scoring (PTS 1, 3PM 1, FGM 2, FGA -1, FTM 1, FTA -1, REB 1, AST 2, STL 4, BLK 4, TOV -2).

Change scoring weights, roster slots, bench size or waiver length in `app/config.py`.

## Features

- Live snake draft with pick timer, autopick, and commissioner undo
- Weekly lineups with position eligibility (PG, SG, SF, PF, C, G, F, 3 UTIL, 3 bench)
- Free agent adds, 48 hour waivers on dropped players, rolling waiver priority
- Trades with accept, reject, cancel, and commissioner veto
- Standings, playoff bracket (2, 4 or 8 teams), and scoreboard
- Win probability for every matchup, updated as games are played
- Power rankings: 40% all-play record, 30% actual record, 30% last 3 weeks of scoring
- Can be added to a phone home screen like an app

## Try it with fake data

```sh
pip install -r requirements.txt
export STATS_PROVIDER=fake DATABASE_URL=sqlite:///./demo.db
python -m app.demo
HOOPS_FAKE_NOW=2026-12-16T23:00:00-05:00 uvicorn app.main:app --reload
```

Open http://localhost:8000 and log in as `demo1` / `demo` (commissioner) through `demo8`.

## Set up your league

On first start, `app/bootstrap.py` creates the league and a commissioner login from environment variables (see Hosting). Then log in and open More > Commissioner:

- **Import from ESPN:** paste a teams CSV (`name,abbrev,manager`) and the full text of the ESPN draft recap page, and enter your team's abbreviation. Your login becomes that team's owner. Every other team gets a login (username = abbreviation, lowercase) and a random password, shown once on the next screen.
- **Or start fresh:** add teams, set the draft order, and run a live draft.

The commissioner can reset any team's password from the same page.

Command line versions of all of this are in `app/cli.py` and `app/importer.py`.

## Stats sources

Set `STATS_PROVIDER`:

| Value | Cost | Notes |
|---|---|---|
| `nba_api` (default) | Free | Uses stats.nba.com, which blocks many cloud servers. If the Commissioner page shows "Stats synced: never" after a day, switch to balldontlie. |
| `balldontlie` | Paid tier for box scores | Set `BALLDONTLIE_API_KEY`. |
| `fake` | Free | Made-up data for testing. |

## Background jobs

`python -m app.jobs tick` syncs players once a day, pulls box scores, locks lineups, processes waivers, and finalizes each week at 10:00 ET the Monday after it ends. It is safe to run as often as you like.

`start.sh` runs the first-run setup, the web server, and a loop that ticks every 20 minutes, so one service is enough.
The Commissioner page also has Sync now, Finalize week and Process waivers buttons.

## Hosting

You need a host that builds the `Dockerfile` from GitHub and gives you a persistent disk for the SQLite database (mounted at `/data`). Railway, Render (paid plan, for the disk) and Fly.io all work. Steps for Railway, all in the browser:

1. Sign in at railway.com with GitHub. New Project > Deploy from GitHub repo > pick this repo.
2. On the service, open Settings > Volumes (or right-click the canvas > Volume) and add a volume with mount path `/data`.
3. Open Variables and add:
   ```
   SECRET_KEY=<long random string>
   LEAGUE_NAME=Hoops League
   LEAGUE_SEASON=2026-27
   LEAGUE_START=2026-11-09
   LEAGUE_TEAMS=8
   LEAGUE_WEEKS=20
   PLAYOFF_TEAMS=4
   ADMIN_USERNAME=<your login>
   ADMIN_PASSWORD=<your password>
   ```
   `LEAGUE_START` must be a Monday. 20 regular weeks + 2 playoff weeks from Nov 9 ends Apr 11.
4. Settings > Networking > Generate Domain. Railway gives you an HTTPS URL.
5. Redeploy. Check the deploy logs for `bootstrap: created ...`.
6. Open the URL on your phone, log in, and go to More > Commissioner to import or add teams.
7. Send each friend the URL and their login. On iPhone: Share > Add to Home Screen.

Notes:
- Only run one instance. The database is a single file.
- Pushing to the repo redeploys automatically. The volume keeps your data.
- To start over, delete the volume's `hoops.db` (or the volume) and redeploy.

Environment variables: `SECRET_KEY` (required), `DATABASE_URL` (default in Docker: `sqlite:////data/hoops.db`), `STATS_PROVIDER`, `BALLDONTLIE_API_KEY`, `LEAGUE_TZ`, `PORT`, `JOB_MINUTES`, plus the `LEAGUE_*` and `ADMIN_*` values above.

## Tests

```sh
pip install pytest
pytest
```

The tests play a full fake season through the draft, lineups, waivers, trades, and playoffs, and load every page.

## Project layout

```
app/
  config.py        scoring, roster slots, env settings
  models.py        database tables
  scoring.py       average-per-game scoring
  weeks.py         week dates and lineup locks
  services/        draft, lineups, roster (adds/waivers), trades, schedule, analytics
  stats/           nba_api, balldontlie, fake providers
  jobs.py          background sync and scoring
  cli.py           league setup commands
  main.py          web routes
  templates/       pages
  static/          CSS and icons
tests/
```
