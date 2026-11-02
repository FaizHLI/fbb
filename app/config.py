"""League-wide settings.

Things that rarely change (roster slots, scoring weights) live here as constants.
Things that differ per install (database, secret key, stats source) come from
environment variables. Per-league choices (start date, weeks, playoff size,
draft timer) are stored in the database and set with `python -m app.cli init-league`.
"""
import os
from zoneinfo import ZoneInfo

# ---- Environment ----------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./hoops.db")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-before-going-live")
# "nba_api" (free, stats.nba.com), "balldontlie" (needs API key), or "fake" (demo data)
STATS_PROVIDER = os.environ.get("STATS_PROVIDER", "nba_api")
BALLDONTLIE_API_KEY = os.environ.get("BALLDONTLIE_API_KEY", "")
# NBA schedule runs on US Eastern time; weeks start Monday 00:00 in this zone.
LEAGUE_TZ = ZoneInfo(os.environ.get("LEAGUE_TZ", "America/New_York"))

# ---- Roster ---------------------------------------------------------------
# Starting slots, in display order. Duplicates are fine (three UTIL spots).
STARTER_SLOTS = ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL", "UTIL", "UTIL"]
BENCH_SIZE = 3
ROSTER_MAX = len(STARTER_SLOTS) + BENCH_SIZE  # also the number of draft rounds

SLOT_ELIGIBILITY = {
    "PG": {"PG"},
    "SG": {"SG"},
    "SF": {"SF"},
    "PF": {"PF"},
    "C": {"C"},
    "G": {"PG", "SG"},
    "F": {"SF", "PF"},
    "UTIL": {"PG", "SG", "SF", "PF", "C"},
}

# ---- Scoring (ESPN default points scoring) --------------------------------
SCORING = {
    "pts": 1,
    "tpm": 1,   # threes made
    "fgm": 2,
    "fga": -1,
    "ftm": 1,
    "fta": -1,
    "reb": 1,
    "ast": 2,
    "stl": 4,
    "blk": 4,
    "tov": -2,
}

# A starter needs at least this many games in a week for their average to count.
# With 1, any game counts. A starter with 0 games scores 0 for the week.
MIN_GAMES_FOR_AVG = 1

# ---- Waivers --------------------------------------------------------------
WAIVER_HOURS = 48  # how long a dropped player sits on waivers
