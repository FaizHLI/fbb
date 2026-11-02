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
| `espn` (default) | Free | ESPN's public site API. No key. Player names match ESPN fantasy, so imported drafts link up exactly. Unofficial, so it could change. |
| `nba_api` | Free | Uses stats.nba.com, which blocks most cloud servers (including GitHub Actions). |
| `balldontlie` | Paid tier for box scores | Set `BALLDONTLIE_API_KEY`. |
| `fake` | Free | Made-up data for testing. |

## Background jobs

`python -m app.jobs tick` syncs players once a day, pulls box scores, locks lineups, processes waivers, and finalizes each week at 10:00 ET the Monday after it ends. It is safe to run as often as you like.

`start.sh` runs the first-run setup, the web server, and a loop that ticks every 20 minutes, so one service is enough.
The Commissioner page also has Sync now, Finalize week and Process waivers buttons.

## Hosting (free)

Three free services, all set up in the browser:

- **Neon** holds the database (free Postgres).
- **Render** runs the website (free instance; it sleeps when idle, so the first visit after a while takes up to a minute).
- **GitHub Actions** runs the background jobs every hour, even while the website sleeps (`.github/workflows/jobs.yml`).

### 1. Database (Neon)

1. Sign up at neon.tech and create a project (any name, closest region to US East).
2. Copy the connection string. It looks like `postgresql://user:pass@ep-xxx.aws.neon.tech/neondb?sslmode=require`.

### 2. Website (Render)

1. Sign up at render.com with GitHub. New > Web Service > pick this repo.
2. Language: Docker. Instance type: Free.
3. Under Advanced, set Health Check Path to `/healthz` and add these environment variables:
   ```
   DATABASE_URL=<Neon connection string>
   RUN_JOBS=0
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
4. Create Web Service. The deploy logs should show `bootstrap: created Hoops League`.

### 3. Background jobs (GitHub Actions)

1. In this repo on GitHub: Settings > Secrets and variables > Actions > New repository secret.
   Name `DATABASE_URL`, value = the same Neon connection string.
2. Actions tab > League jobs > Run workflow, to test it. After that it runs every hour on its own.

### 4. Use it

Open your `https://<name>.onrender.com` link, log in, and go to More > Commissioner to import from ESPN or add teams. On iPhone: Share > Add to Home Screen.

Notes:
- Pushing to the repo redeploys the website. Data lives in Neon, so nothing is lost.
- GitHub pauses scheduled workflows in public repos after 60 days with no commits. If that happens, the Actions tab shows a button to turn it back on.
- Private repos get a monthly allowance of free Actions minutes. An hourly run uses roughly 1 to 2 minutes, which should fit, but check Settings > Billing if the repo is private.
- If the League jobs run fails with a stats error, check its log. As a backup you can add repo secrets `STATS_PROVIDER=balldontlie` and `BALLDONTLIE_API_KEY=<key>` (box scores need a paid balldontlie plan).

### Paid alternative

Any host with a persistent disk can run everything in one service with SQLite: deploy the Dockerfile, mount a disk at `/data`, leave `DATABASE_URL` unset and `RUN_JOBS` at its default. Leave the GitHub `DATABASE_URL` secret unset in that case; the workflow then does nothing.

Environment variables: `SECRET_KEY` (required), `DATABASE_URL`, `RUN_JOBS`, `PLAYER_IMAGE_URL` (optional photo source, a URL with `{id}` for the player id; initials show when unset), `STATS_PROVIDER`, `BALLDONTLIE_API_KEY`, `LEAGUE_TZ`, `PORT`, `JOB_MINUTES`, plus the `LEAGUE_*` and `ADMIN_*` values above.

## Tests

```sh
pip install pytest
pytest
# against Postgres too:
TEST_DATABASE_URL=postgresql://... pytest
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
  stats/           espn, nba_api, balldontlie, fake providers
  jobs.py          background sync and scoring
  cli.py           league setup commands
  main.py          web routes
  templates/       pages
  static/          CSS and icons
tests/
```
