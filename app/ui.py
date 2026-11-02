"""Small display helpers for templates: avatars and team colors."""
import os

# Optional photo source. A URL template with {id} (the stats provider's player id),
# e.g. https://example.com/players/{id}.png. Leave unset to show initials only.
PLAYER_IMAGE_URL = os.environ.get("PLAYER_IMAGE_URL", "")

TEAM_COLORS = {
    "ATL": "#C8102E", "BOS": "#007A33", "BKN": "#444444", "CHA": "#1D1160", "CHI": "#CE1141", "CLE": "#860038",
    "DAL": "#00538C", "DEN": "#0E2240", "DET": "#C8102E", "GSW": "#1D428A", "HOU": "#CE1141", "IND": "#002D62",
    "LAC": "#1D428A", "LAL": "#552583", "MEM": "#5D76A9", "MIA": "#98002E", "MIL": "#00471B", "MIN": "#0C2340",
    "NOP": "#0C2340", "NYK": "#006BB6", "OKC": "#007AC1", "ORL": "#0077C0", "PHI": "#006BB6", "PHX": "#1D1160",
    "POR": "#E03A3E", "SAC": "#5A2D81", "SAS": "#8A8D8F", "TOR": "#CE1141", "UTA": "#002B5C", "WAS": "#002B5C",
}


def initials(name: str) -> str:
    parts = [p for p in name.replace(".", "").split() if p.lower() not in ("jr", "sr", "ii", "iii", "iv")]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


def team_color(abbr: str) -> str:
    return TEAM_COLORS.get(abbr or "", "#55606e")


def player_img(player_id) -> str:
    if not PLAYER_IMAGE_URL or not player_id or int(player_id) <= 0:
        return ""
    return PLAYER_IMAGE_URL.replace("{id}", str(player_id))


def register(env) -> None:
    env.globals.update(initials=initials, team_color=team_color, player_img=player_img)
