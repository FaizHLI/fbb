from .. import config
from .base import GameLog, PlayerInfo, StatsProvider


def get_provider(name: str | None = None) -> StatsProvider:
    name = name or config.STATS_PROVIDER
    if name == "fake":
        from .fake_provider import FakeProvider
        return FakeProvider()
    if name == "balldontlie":
        from .balldontlie_provider import BallDontLieProvider
        return BallDontLieProvider()
    if name == "nba_api":
        from .nba_api_provider import NbaApiProvider
        return NbaApiProvider()
    raise ValueError(f"Unknown STATS_PROVIDER: {name}")


__all__ = ["get_provider", "GameLog", "PlayerInfo", "StatsProvider"]
