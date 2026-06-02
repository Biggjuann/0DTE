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
        from app.clients.gammamarket import CompositeMarketData, GammaMarketData
        from app.clients.mm import MMProviderLive
        from app.clients.schwab import SchwabClient
        from app.clients.token import SharedTokenProvider

        mm = MMProviderLive()
        token = SharedTokenProvider()                  # shared Schwab token
        schwab = SchwabClient(token_fn=token.get_token)  # broker (orders) + data
        gamma_md = None
        if "gammagamma" in (settings.market_data_provider, settings.options_provider):
            gamma_md = GammaMarketData()

        quote_src = gamma_md if settings.market_data_provider == "gammagamma" else schwab
        option_src = gamma_md if settings.options_provider == "gammagamma" else schwab
        market = quote_src if quote_src is option_src else CompositeMarketData(quote_src, option_src)

        log.info("Providers: LIVE (levels=Gammagamma, mm=MM, quotes=%s, options=%s, "
                 "orders=Schwab[%s], dry_run=%s)",
                 settings.market_data_provider, settings.options_provider,
                 settings.schwab_auth_mode, settings.dry_run)
        return Providers(
            levels=GammaGammaProvider(),
            mm=mm,
            market=market,
            broker=schwab,
            mode="live",
        )

    mock = MockProvider()
    log.info("Providers: MOCK (synthetic feed)")
    return Providers(levels=mock, mm=mock, market=mock, broker=mock, mode="mock")
