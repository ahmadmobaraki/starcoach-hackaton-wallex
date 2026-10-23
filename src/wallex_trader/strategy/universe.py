from __future__ import annotations

import logging
from dataclasses import dataclass, field

from wallex_trader.config import Settings
from wallex_trader.models import FeeRate, MarketMeta, OrderBook
from wallex_trader.tradability import blocked_markets, normalize_market

logger = logging.getLogger("trader.universe")


@dataclass(slots=True)
class UniverseResult:
    selected: list[MarketMeta] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)


class UniverseSelector:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def select(
        self,
        metadata: dict[str, MarketMeta],
        books: dict[str, OrderBook] | None = None,
        fees: dict[str, FeeRate] | None = None,
    ) -> UniverseResult:
        excluded: dict[str, str] = {}
        eligible: list[MarketMeta] = []
        blocked = blocked_markets(self.settings.blocked_markets_file)
        for symbol in self.settings.universe_or_default():
            if normalize_market(symbol) in blocked:
                excluded[symbol] = "not tradable in this account"
                continue
            meta = metadata.get(symbol)
            if meta is None:
                excluded[symbol] = "not listed on Wallex"
                continue
            if not meta.is_spot:
                excluded[symbol] = "not a spot market"
                continue
            if not meta.is_tmn_based or meta.quote_asset != "TMN":
                excluded[symbol] = "not a TMN market"
                continue
            if meta.is_risky:
                excluded[symbol] = "market flagged risky"
                continue
            if meta.is_new or meta.listing_age_days < self.settings.universe_min_listing_age_days:
                excluded[symbol] = "new listing"
                continue
            if meta.quote_volume_24h < self.settings.universe_min_quote_volume_24h:
                excluded[symbol] = f"24h quote volume {meta.quote_volume_24h:,.0f} below floor"
                continue
            if meta.price <= 0:
                excluded[symbol] = "no valid price"
                continue
            if meta.price_precision < 0 or meta.amount_precision < 0:
                excluded[symbol] = "unknown precision"
                continue
            eligible.append(meta)
        eligible.sort(key=lambda item: item.quote_volume_24h, reverse=True)
        selected = eligible[: self.settings.universe_max_symbols]
        for meta in eligible[self.settings.universe_max_symbols :]:
            excluded[meta.symbol] = "ranked below universe cap"
        if not selected:
            logger.error("universe selection returned no symbols")
        return UniverseResult(selected=selected, excluded=excluded)
