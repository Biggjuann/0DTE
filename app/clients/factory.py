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


@dataclass
class PivotProviders:
    pivots: object        # PivotsProvider
    market: MarketData    # quotes, chains, prior-day OHLC, VIX
    broker: Broker
    vix_symbol: str
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
        # invalidate_fn lets the client force a token refresh on a 401 (the
        # shared token rotates ~every 30 min under us).
        schwab = SchwabClient(token_fn=token.get_token, invalidate_fn=token.invalidate)
        gamma_md = None
        if "gammagamma" in (settings.market_data_provider, settings.options_provider):
            gamma_md = GammaMarketData()

        quote_src = gamma_md if settings.market_data_provider == "gammagamma" else schwab
        option_src = gamma_md if settings.options_provider == "gammagamma" else schwab
        market = quote_src if quote_src is option_src else CompositeMarketData(quote_src, option_src)

        if settings.market_data_provider != "gammagamma" and not settings.token_share_key:
            log.warning("MARKET_DATA_PROVIDER=schwab but SCHWAB_TOKEN_SHARE_KEY is unset — "
                        "Schwab spot quotes will fail and the price will look frozen.")

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


def build_pivot_providers() -> PivotProviders:
    """Pivot strategy is Schwab-backed (needs daily OHLC + VIX) in live mode."""
    from app.clients.pivots import PivotsProvider
    if settings.live:
        from app.clients.schwab import SchwabClient
        from app.clients.token import SharedTokenProvider
        token = SharedTokenProvider()
        schwab = SchwabClient(token_fn=token.get_token, invalidate_fn=token.invalidate)
        log.info("Pivot providers: LIVE (Schwab quotes/OHLC/orders, vix=%s, dry_run=%s)",
                 settings.vix_symbol, settings.dry_run)
        return PivotProviders(pivots=PivotsProvider(schwab), market=schwab, broker=schwab,
                              vix_symbol=settings.vix_symbol, mode="live")
    mock = MockProvider()
    log.info("Pivot providers: MOCK")
    return PivotProviders(pivots=PivotsProvider(mock), market=mock, broker=mock,
                          vix_symbol=settings.vix_symbol, mode="mock")
