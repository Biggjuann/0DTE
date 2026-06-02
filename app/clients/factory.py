"""Wire up providers based on DATA_MODE.

mock -> one MockProvider satisfies all four roles.
live -> Gammagamma (levels) + MM (trend/flow/token) + Schwab (quotes/orders).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.clients.base import Broker, LevelsProvider, MarketData, MMProvider
from app.clients.mock import MockProvider
from app.config import settings

log = logging.getLogger("factory")


@dataclass
class Providers:
    levels: LevelsProvider
    mm: MMProvider
    market: MarketData
    broker: Broker
    mode: str


def build_providers() -> Providers:
    if settings.live:
        from app.clients.gammagamma import GammaGammaProvider
        from app.clients.mm import MMProviderLive
        from app.clients.schwab import SchwabClient

        mm = MMProviderLive()
        schwab = SchwabClient(token_fn=mm.get_schwab_token)
        log.info("Providers: LIVE (Gammagamma + MM + Schwab, dry_run=%s)", settings.dry_run)
        return Providers(
            levels=GammaGammaProvider(),
            mm=mm,
            market=schwab,
            broker=schwab,
            mode="live",
        )

    mock = MockProvider()
    log.info("Providers: MOCK (synthetic feed)")
    return Providers(levels=mock, mm=mock, market=mock, broker=mock, mode="mock")
